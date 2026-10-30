import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from inferfabric.security.key_store import KeyStore


@pytest.fixture
def store(tmp_path):
    return KeyStore(tmp_path / 'security.db', provision=True)


def test_random_key_digest_only_and_reopen(store):
    meta, token = store.create_key('tool', ['iff/work'], None)
    _, other = store.create_key('tool2', None, None)
    assert token != other
    assert len(base64.urlsafe_b64decode(token.removeprefix('sk-iff-') + '=')) == 32
    assert 'digest' not in meta and token not in repr(meta)
    assert store.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(store.path) as db:
        dump = '\n'.join(db.iterdump())
    assert token not in dump and other not in dump
    again = KeyStore(store.path)
    assert again.check(token, 'iff/work') == (True, 'ok')
    assert again.key_name(token) == 'tool'
    assert token not in repr(again.list_keys())


def test_exact_scope_expiry_and_revocation(store):
    meta, token = store.create_key('pool', ['iff/work'], None)
    assert store.check(token, 'iff/work')[0]
    for model in ('iff/other', 'work', 'Qwen38-27B-TXT'):
        assert not store.check(token, model)[0]
    store.revoke_key(meta['id'])
    assert not store.check(token, 'iff/work')[0]
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
    ('x', 'iff/a', None), ('x', [1], None),
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
    assert store.key_status(token) == 'revoked'
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
