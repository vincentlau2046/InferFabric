import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import time

import pytest

from inferfabric.security.key_store import KeyStore


@pytest.fixture
def store(tmp_path):
    return KeyStore(tmp_path / 'security.db', provision=True)


def test_random_key_digest_only_and_reopen(store):
    meta, token = store.create_key('tool', ['fabric-work'], None)
    _, other = store.create_key('tool2', None, None)
    assert token != other
    assert len(base64.urlsafe_b64decode(token.removeprefix('sk-iff-') + '=')) == 32
    assert 'digest' not in meta and token not in repr(meta)
    assert store.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(store.path) as db:
        dump = '\n'.join(db.iterdump())
    assert token not in dump and other not in dump
    again = KeyStore(store.path)
    assert again.check(token, 'fabric-work') == (True, 'ok')
    assert again.key_name(token) == 'tool'
    assert token not in repr(again.list_keys())


def test_exact_scope_expiry_and_revocation(store):
    meta, token = store.create_key('pool', ['fabric-work'], None)
    assert store.check(token, 'fabric-work')[0]
    for model in ('fabric-other', 'work', 'Qwen38-27B-TXT'):
        assert not store.check(token, model)[0]
    store.revoke_key(meta['id'])
    assert not store.check(token, 'fabric-work')[0]
    assert store.key_name(token) == 'anonymous'
    with pytest.raises(KeyError):
        store.revoke_key('missing')


def test_expiry_boundary(store, monkeypatch):
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    _, token = store.create_key('exp', None, deadline)
    monkeypatch.setattr(store, '_now', lambda: deadline.timestamp())
    assert not store.check(token, 'anything')[0]


@pytest.mark.parametrize('name,models,expiry', [
    ('', None, None), ('x\nforged', None, None), ('x', [], None),
    ('x', 'fabric-a', None), ('x', [1], None),
    ('x', None, datetime(2030, 1, 1)), ('x', None, 'tomorrow'),
    ('x', None, datetime.now(timezone.utc) - timedelta(days=1)),
])
def test_invalid_inputs(store, name, models, expiry):
    with pytest.raises(ValueError):
        store.create_key(name, models, expiry)
    assert store.list_keys() == []


def test_concurrent_connections_preserve_changes(store):
    second = KeyStore(store.path)
    def create(i):
        return (store if i % 2 else second).create_key(str(i), None, None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(create, range(12)))
    assert len(store.list_keys()) == 12
    assert all(second.check(token, 'x')[0] for _, token in result)
    second.revoke_key(result[0][0]['id'])
    assert not store.check(result[0][1], 'x')[0]


def test_key_status_valid_unknown_revoked_expired(store, monkeypatch):
    meta, token = store.create_key('a', None, None)
    assert store.key_status(token) == 'valid'
    assert store.key_status('sk-iff-never-issued') == 'unknown'
    store.revoke_key(meta['id'])
    # issue #48：吊销即硬删 — token 不再被识别（unknown，而非 revoked 态）
    assert store.key_status(token) == 'unknown'
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    _, exp = store.create_key('b', None, deadline)
    assert store.key_status(exp) == 'valid'
    monkeypatch.setattr(store, '_now', lambda: deadline.timestamp())
    assert store.key_status(exp) == 'expired'
    assert store.key_status('x' * 5000) == 'unknown'


def test_policy_transaction_rollback(store):
    assert store.get_policy() == 'off'
    store.set_policy('pools')
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_policy BEFORE UPDATE ON settings BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.DatabaseError):
        store.set_policy('all')
    assert store.get_policy() == 'pools'
    with pytest.raises(ValueError):
        store.set_policy('typo')


def test_runtime_missing_corrupt_and_reinit_rejected(tmp_path, store):
    with pytest.raises((OSError, sqlite3.DatabaseError, ValueError)):
        KeyStore(tmp_path / 'missing.db')
    bad = tmp_path / 'bad.db'
    bad.write_bytes(b'not sqlite')
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        KeyStore(bad)
    store.set_policy('all')
    with pytest.raises(FileExistsError):
        KeyStore(store.path, provision=True)
    assert KeyStore(store.path).get_policy() == 'all'


def test_active_key_id_valid_unknown_revoked_expired(store, monkeypatch):
    meta, token = store.create_key('a', None, None)
    assert store.active_key_id(token) == meta['id']
    assert store.active_key_id('sk-iff-never-issued') is None
    store.revoke_key(meta['id'])
    assert store.active_key_id(token) is None
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    _, exp = store.create_key('b', None, deadline)
    assert store.active_key_id(exp) is not None
    monkeypatch.setattr(store, '_now', lambda: deadline.timestamp())
    assert store.active_key_id(exp) is None


# ── issue #48：吊销即硬删 — 列表只保留有效 key ───────────────────

def test_revoke_hard_deletes_row_and_frees_name(store):
    meta, token = store.create_key('client-a', None, None)
    store.revoke_key(meta['id'])
    # 行已物理删除：列表/校验/状态三面都不再见
    assert all(k['id'] != meta['id'] for k in store.list_keys())
    assert store.check(token, 'x')[0] is False
    assert store.key_status(token) == 'unknown'   # 不再被识别（非 revoked 态）
    # UNIQUE 名字随行删除而释放 — 同名可重建
    meta2, _ = store.create_key('client-a', None, None)
    assert meta2['id'] != meta['id']


def test_revoke_unknown_id_still_raises(store):
    with pytest.raises(KeyError):
        store.revoke_key('nonexistent-id')


def test_legacy_revoked_rows_purged_on_open(tmp_path):
    """升级迁移：存量 revoked 行在任意进程首次打开时被清除（幂等）。"""
    import sqlite3 as _sq
    p = tmp_path / 'legacy.db'
    store = KeyStore(p, provision=True)
    meta, token = store.create_key('old-client', None, None)
    with _sq.connect(p) as db:
        db.execute('UPDATE api_keys SET revoked_at=? WHERE id=?',
                   (time.time(), meta['id']))
    # 模拟 7.12.3 及以前的生产库：列表仍带出 revoked 行
    with _sq.connect(p) as db:
        db.row_factory = _sq.Row
        assert db.execute('SELECT revoked_at FROM api_keys WHERE id=?',
                          (meta['id'],)).fetchone()['revoked_at'] is not None
    reopened = KeyStore(p)          # 任意打开者触发迁移
    assert all(k['id'] != meta['id'] for k in reopened.list_keys())
    with _sq.connect(p) as db:      # 库内也无残留（幂等：再开一次不报错）
        assert db.execute('SELECT COUNT(*) FROM api_keys WHERE '
                          'revoked_at IS NOT NULL').fetchone()[0] == 0
    KeyStore(p)                      # 第二次打开 no-op
    assert reopened.check(token, 'x')[0] is False
