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
    assert auth.check('', 'iff/work')[0] is pool_ok
    assert auth.check('', 'local')[0] is direct_ok
    assert auth.check('Bearer bogus', 'local')[0] is bogus_ok


def test_off_grace_allows_unregistered_key_with_warning(auth, caplog):
    auth.store.set_policy('off')
    with caplog.at_level('WARNING', logger='inferfabric.security'):
        assert auth.check('Bearer not-a-real-key', 'iff/work')[0]
    assert any('not-a-real-key' not in r.message and 'off' in r.message for r in caplog.records)


def test_off_grace_rejects_revoked_and_out_of_scope(auth):
    auth.store.set_policy('off')
    meta, token = auth.store.create_key('client', ['iff/work'], None)
    auth.store.revoke_key(meta['id'])
    assert not auth.check(token, 'iff/work')[0]
    meta2, scoped = auth.store.create_key('scoped', ['iff/work'], None)
    assert not auth.check(scoped, 'iff/other')[0]
    assert auth.check(scoped, 'iff/work')[0]


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
    meta, token = auth.store.create_key('client', ['iff/work'], None)
    assert auth.check('Bearer ' + token, 'iff/work')[0]
    assert auth.check(token, 'iff/work')[0]
    assert auth.key_name(token) == 'client'
    assert not auth.check(token, 'local')[0]
    assert not auth.check(token, 'iff/other')[0]
    auth.store.revoke_key(meta['id'])
    assert not auth.check(token, 'iff/work')[0]


@pytest.mark.parametrize('policy', ['off', 'pools', 'all'])
def test_legacy_auth_never_downgraded(tmp_path, policy):
    path = tmp_path / 'api_keys.yaml'
    path.write_text('primary: legacy-root\nguests:\n- key: legacy-pool\n  name: legacy\n  models: [iff/work]\n')
    auth = SecurityAuth(AuthManager(path), KeyStore(tmp_path / 'security.db', provision=True))
    auth.store.set_policy(policy)
    assert not auth.check('', 'local')[0]
    assert auth.check('legacy-root', 'local')[0]
    assert auth.check('legacy-pool', 'iff/work')[0]
    assert not auth.check('legacy-pool', 'local')[0]
    _, new = auth.store.create_key('new', ['iff/work'], None)
    assert auth.check(new, 'iff/work')[0]
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


def test_activation_is_explicit_and_missing_store_is_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv('IFF_SECURITY_ENABLED', raising=False)
    assert isinstance(build_auth(tmp_path), AuthManager)
    monkeypatch.setenv('IFF_SECURITY_ENABLED', '1')
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        build_auth(tmp_path)
    KeyStore(tmp_path / 'security.db', provision=True)
    assert isinstance(build_auth(tmp_path), SecurityAuth)
    monkeypatch.delenv('IFF_SECURITY_ENABLED')
    # An initialized store is also an enduring activation marker.
    assert isinstance(build_auth(tmp_path), SecurityAuth)
    (tmp_path / 'security.enabled').touch()
    (tmp_path / 'security.db').unlink()
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        build_auth(tmp_path)
