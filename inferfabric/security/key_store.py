"""Transactional security store. Raw API keys are never persisted."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class KeyStore:
    def __init__(self, path: Path, *, provision: bool = False):
        self.path = Path(path).absolute()
        if provision:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                with self._db() as db:
                    db.executescript('''
                        CREATE TABLE settings (
                            id INTEGER PRIMARY KEY CHECK(id=1),
                            policy TEXT NOT NULL CHECK(policy IN ('off','pools','all')),
                            password TEXT
                        );
                        INSERT INTO settings VALUES (1, 'off', NULL);
                        CREATE TABLE api_keys (
                            id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL,
                            name TEXT UNIQUE NOT NULL, models TEXT,
                            created_at REAL NOT NULL, expires_at REAL,
                            revoked_at REAL
                        );
                    ''')
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
        self.validate()

    @contextmanager
    def _db(self):
        # mode=rw never silently recreates a deleted live store.
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def validate(self):
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ValueError('security store must be a private regular file (0600)')
        with self._db() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('invalid security store')
            row = db.execute('SELECT policy, password FROM settings WHERE id=1').fetchone()
            if not row or row['policy'] not in ('off', 'pools', 'all'):
                raise ValueError('invalid security policy')
            db.execute('SELECT id,digest,name,models,created_at,expires_at,revoked_at FROM api_keys LIMIT 0')

    @staticmethod
    def _now():
        return time.time()

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @staticmethod
    def _metadata(row):
        return {k: json.loads(row[k]) if k == 'models' and row[k] is not None else row[k]
                for k in ('id', 'name', 'models', 'created_at', 'expires_at', 'revoked_at')}

    def create_key(self, name, models, expires_at):
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('invalid key name')
        if models is not None and (not isinstance(models, list) or not models
                or len(models) > 200 or any(not isinstance(m, str) or not m
                or len(m) > 200 or any(ord(c) < 32 for c in m) for m in models)):
            raise ValueError('invalid model scope')
        expiry = None
        if expires_at is not None:
            if not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
                raise ValueError('expiry must be a timezone-aware datetime')
            expiry = expires_at.timestamp()
            if expiry <= self._now():
                raise ValueError('expiry must be in the future')
        token = 'sk-iff-' + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(16)
        with self._db() as db:
            db.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,?,NULL)',
                       (key_id, self._digest(token), name.strip(),
                        json.dumps(models) if models is not None else None,
                        self._now(), expiry))
            row = db.execute('SELECT * FROM api_keys WHERE id=?', (key_id,)).fetchone()
        return self._metadata(row), token

    def list_keys(self):
        with self._db() as db:
            return [self._metadata(row) for row in db.execute('SELECT * FROM api_keys ORDER BY created_at,id')]

    def revoke_key(self, key_id):
        with self._db() as db:
            r = db.execute('UPDATE api_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=?',
                           (self._now(), key_id))
            if not r.rowcount:
                raise KeyError('unknown key id')

    def _entry(self, token):
        if not isinstance(token, str) or len(token) > 4096:
            return None
        with self._db() as db:
            row = db.execute('SELECT * FROM api_keys WHERE digest=?', (self._digest(token),)).fetchone()
        if row is None or row['revoked_at'] is not None:
            return None
        if row['expires_at'] is not None and row['expires_at'] <= self._now():
            return None
        return row

    def key_status(self, token):
        """Validity only (no scope): 'valid' | 'unknown' | 'revoked' | 'expired'."""
        if not isinstance(token, str) or len(token) > 4096:
            return 'unknown'
        with self._db() as db:
            row = db.execute('SELECT * FROM api_keys WHERE digest=?', (self._digest(token),)).fetchone()
        if row is None:
            return 'unknown'
        if row['revoked_at'] is not None:
            return 'revoked'
        if row['expires_at'] is not None and row['expires_at'] <= self._now():
            return 'expired'
        return 'valid'

    def check(self, token, model):
        row = self._entry(token)
        if row is None:
            return False, 'invalid or expired key'
        if row['models'] is not None and model not in json.loads(row['models']):
            return False, 'model not allowed'
        return True, 'ok'

    def key_name(self, token):
        row = self._entry(token)
        return row['name'] if row else 'anonymous'

    def get_policy(self):
        with self._db() as db:
            row = db.execute('SELECT policy FROM settings WHERE id=1').fetchone()
            if not row or row[0] not in ('off', 'pools', 'all'):
                raise ValueError('invalid security policy')
            return row[0]

    def set_policy(self, policy):
        if policy not in ('off', 'pools', 'all'):
            raise ValueError('invalid security policy')
        with self._db() as db:
            db.execute('UPDATE settings SET policy=? WHERE id=1', (policy,))
