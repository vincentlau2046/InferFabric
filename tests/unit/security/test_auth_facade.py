from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from inferfabric.proxy.auth import AuthManager
from inferfabric.security.auth_facade import SecurityAuth, build_auth
from inferfabric.security.key_store import KeyStore


@pytest.fixture
def auth(tmp_path):
    return SecurityAuth(AuthManager(), KeyStore(tmp_path / 'security.db', provision=True))


@pytest.mark.parametrize('policy,pool_ok,direct_ok,bogus_ok', [
    ('off', True, True, True), ('pools', False, True, False), ('all', False, False, False)])
def test_anonymous_matrix(auth, policy, pool_ok, direct_ok, bogus_ok):
    # off = 迁移宽限：未注册凭证不拦（记 warning）；pools/all 一律拒绝。
    auth.store.set_policy(policy)
    assert auth.enabled
    assert auth.check('', 'fabric-work')[0] is pool_ok
    assert auth.check('', 'local')[0] is direct_ok
    assert auth.check('Bearer bogus', 'local')[0] is bogus_ok


def test_off_grace_allows_unregistered_key_with_warning(auth, caplog):
    auth.store.set_policy('off')
    with caplog.at_level('WARNING', logger='inferfabric.security'):
        assert auth.check('Bearer not-a-real-key', 'fabric-work')[0]
    # 钉死宽限 warning 本身（且原值 key 不进日志）
    assert any('off policy' in r.message and 'unknown' in r.message
               and 'not-a-real-key' not in r.message for r in caplog.records)


def test_off_grace_denies_unknown_key_when_legacy_enabled(tmp_path):
    # legacy YAML 开启时 legacy 语义接管 — 未注册 key 无宽限
    path = tmp_path / 'api_keys.yaml'
    path.write_text('primary: legacy-root\n')
    auth = SecurityAuth(AuthManager(path), KeyStore(tmp_path / 'security.db', provision=True))
    auth.store.set_policy('off')
    assert not auth.check('Bearer unknown-key', 'local')[0]


def test_expired_key_denied_under_pools(auth, monkeypatch):
    # 过期 key 只在 off 宽限放行；pools/all 一律拒绝
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    _, token = auth.store.create_key('exp', None, deadline)
    monkeypatch.setattr(auth.store, '_now', lambda: deadline.timestamp() + 10)
    auth.store.set_policy('pools')
    assert not auth.check(token, 'local')[0]


def test_off_grace_treats_revoked_as_unknown_out_of_scope_still_rejected(auth):
    """issue #48：吊销即硬删 — off 宽限下吊销 token 成为 unknown（宽限放行，
    与「从未存在」一致）；pools/all 下吊销即时拒绝（见 live_revocation）。"""
    auth.store.set_policy('off')
    meta, token = auth.store.create_key('client', ['fabric-work'], None)
    auth.store.revoke_key(meta['id'])
    assert auth.check(token, 'fabric-work')[0]   # 宽限：unknown 放行
    meta2, scoped = auth.store.create_key('scoped', ['fabric-work'], None)
    assert not auth.check(scoped, 'fabric-other')[0]   # 已知 key 的 scope 仍拒绝
    assert auth.check(scoped, 'fabric-work')[0]


def test_off_grace_allows_expired_key(auth, monkeypatch):
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    _, token = auth.store.create_key('exp', None, deadline)
    monkeypatch.setattr(auth.store, '_now', lambda: deadline.timestamp())
    auth.store.set_policy('off')
    assert auth.check(token, 'local')[0]
    auth.store.set_policy('all')
    assert not auth.check(token, 'local')[0]


@pytest.mark.parametrize('policy', ['off', 'pools', 'all'])
def test_credential_scope_and_live_revocation(auth, policy):
    auth.store.set_policy(policy)
    meta, token = auth.store.create_key('client', ['fabric-work'], None)
    assert auth.check('Bearer ' + token, 'fabric-work')[0]
    assert auth.check(token, 'fabric-work')[0]
    assert auth.key_name(token) == 'client'
    assert not auth.check(token, 'local')[0]
    assert not auth.check(token, 'fabric-other')[0]
    auth.store.revoke_key(meta['id'])
    # issue #48：吊销即硬删 — pools/all 即时拒绝；off 宽限下 token 成为
    # unknown，按宽限语义放行（与从未存在一致，见 off_grace 专项测试）
    if policy == 'off':
        assert auth.check(token, 'fabric-work')[0]
    else:
        assert not auth.check(token, 'fabric-work')[0]


@pytest.mark.parametrize('policy', ['off', 'pools', 'all'])
def test_legacy_auth_never_downgraded(tmp_path, policy):
    path = tmp_path / 'api_keys.yaml'
    path.write_text('primary: legacy-root\nguests:\n- key: legacy-pool\n  name: legacy\n  models: [fabric-work]\n')
    auth = SecurityAuth(AuthManager(path), KeyStore(tmp_path / 'security.db', provision=True))
    auth.store.set_policy(policy)
    assert not auth.check('', 'local')[0]
    assert auth.check('legacy-root', 'local')[0]
    assert auth.check('legacy-pool', 'fabric-work')[0]
    assert not auth.check('legacy-pool', 'local')[0]
    _, new = auth.store.create_key('new', ['fabric-work'], None)
    assert auth.check(new, 'fabric-work')[0]
    assert not auth.check(new, 'local')[0]
    assert auth.key_name('legacy-pool') == 'legacy'


def test_corruption_fail_closed_and_reload_reports_failure(auth):
    auth.store.set_policy('all')
    auth.store.path.unlink()
    assert not auth.check('', 'local')[0]
    assert not auth.check('anything', 'local')[0]
    assert auth.key_name('anything') == 'anonymous'
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        auth.reload()


def test_activation_is_explicit_and_missing_store_provisions(tmp_path, monkeypatch):
    # F7 fork fix (2026-11-15 incident): activation is still explicit, but a
    # marker left behind by a failed enable with no DB used to crashloop the
    # startup loop with FileNotFoundError — provision per `iff keys init`
    # semantics instead.
    monkeypatch.delenv('IFF_SECURITY_ENABLED', raising=False)
    assert isinstance(build_auth(tmp_path), AuthManager)
    monkeypatch.setenv('IFF_SECURITY_ENABLED', '1')
    assert isinstance(build_auth(tmp_path), SecurityAuth)
    assert (tmp_path / 'security.db').exists()
    # An initialized store is also an enduring activation marker.
    monkeypatch.delenv('IFF_SECURITY_ENABLED')
    assert isinstance(build_auth(tmp_path), SecurityAuth)
    (tmp_path / 'security.enabled').touch()
    (tmp_path / 'security.db').unlink()
    assert isinstance(build_auth(tmp_path), SecurityAuth)
    assert (tmp_path / 'security.db').exists()
