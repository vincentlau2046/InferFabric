from unittest.mock import patch

import pytest

from inferfabric.security.console_auth import (
    ConsoleAuth, LoginError, password_set, set_password)
from inferfabric.security.key_store import KeyStore


@pytest.fixture
def console(tmp_path):
    store = KeyStore(tmp_path / 'security.db', provision=True)
    set_password(store, 'test-password-long')
    clock = [100.0]
    return ConsoleAuth(store, clock=lambda: clock[0]), clock


def test_login_csrf_expiry_and_restart(console):
    auth, clock = console
    session, csrf = auth.login('test-password-long')
    assert auth.validate(session, None, write=False)
    assert not auth.validate(session, None, write=True)
    assert not auth.validate(session, 'bad', write=True)
    assert auth.validate(session, csrf, write=True)
    assert not ConsoleAuth(auth.store).validate(session, None, write=False)
    clock[0] += 900
    assert not auth.validate(session, csrf, write=True)


def test_reset_invalidates_existing_sessions(console):
    auth, _ = console
    session, csrf = auth.login('test-password-long')
    set_password(auth.store, 'a-new-password-long')
    assert not auth.validate(session, csrf, write=True)
    with pytest.raises(LoginError):
        auth.login('test-password-long')
    assert auth.login('a-new-password-long')


def test_rate_limit_precedes_scrypt(console):
    auth, clock = console
    for _ in range(5):
        with pytest.raises(LoginError) as err:
            auth.login('wrong')
        assert err.value.status == 401
    with patch('inferfabric.security.console_auth.hashlib.scrypt', side_effect=AssertionError('must not hash')):
        with pytest.raises(LoginError) as err:
            auth.login('test-password-long')
        assert err.value.status == 429
    clock[0] += 61
    assert auth.login('test-password-long')


def test_password_private_hash_only_and_logout(console):
    auth, _ = console
    assert 'test-password-long' not in auth.store.path.read_bytes().decode(errors='replace')
    with pytest.raises(ValueError):
        set_password(auth.store, 'short')
    session, csrf = auth.login('test-password-long')
    auth.logout(session)
    assert not auth.validate(session, csrf, write=True)


def test_audit_has_no_secrets(console, caplog):
    auth, _ = console
    with caplog.at_level('INFO'):
        session, csrf = auth.login('test-password-long')
        with pytest.raises(LoginError):
            auth.login('wrong-secret')
    assert all(value not in caplog.text for value in ('test-password-long', 'wrong-secret', session, csrf))


def test_password_set_reports_initialization(tmp_path):
    # PR3（WS4）：设置页/reveal 门状态展示的判定源 — settings.id=1 是否有口令记录
    from inferfabric.security.console_auth import password_set
    store = KeyStore(tmp_path / 'security.db', provision=True)
    assert password_set(store) is False
    set_password(store, 'test-password-long')
    assert password_set(store) is True


def test_set_password_conditional_guards(tmp_path):
    # review I6：条件更新原语 — 竞态下后写不得静默覆盖（rowcount 守卫）
    store = KeyStore(tmp_path / 'security.db', provision=True)
    # expect='unset'：空库 → 成功；已有口令 → False（409 语义）
    assert set_password(store, 'first-password-x', expect='unset') is True
    assert set_password(store, 'second-password', expect='unset') is False
    # expect=<raw>：精确匹配已验证记录才可写（改密竞态守卫）
    with store._db() as db:
        raw = db.execute('SELECT password FROM settings WHERE id=1').fetchone()[0]
    assert isinstance(raw, str) and raw  # 当前记录原文
    assert set_password(store, 'changed-password', expect=raw) is True
    assert set_password(store, 'raced-password', expect=raw) is False  # 记录已变
    assert password_set(store) is True
    assert set_password(store, 'never-applied', expect=None) is True   # CLI 无条件路径保留
