"""Password verification and short-lived in-process console sessions."""
from collections import deque
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

log = logging.getLogger('inferfabric.security')


class LoginError(Exception):
    def __init__(self, status=401):
        super().__init__('login rate limited' if status == 429 else 'invalid login')
        self.status = status


def _password_bytes(password):
    if not isinstance(password, str) or len(password) > 1024:
        raise ValueError('invalid password length')
    data = password.encode('utf-8')
    if not 12 <= len(data) <= 1024:
        raise ValueError('password must be 12–1024 UTF-8 bytes')
    return data


def _derive(data, salt):
    return hashlib.scrypt(data, salt=salt, n=16384, r=8, p=1, dklen=32)


def set_password(store, password, expect=None):
    """Update the console password record (PR3 WS4; review I6 conditional guard).

    expect=None     无条件更新（iff keys set-password CLI 既有语义）
    expect='unset'  仅当前无口令时写入（网页首设；rowcount=0 → False → 409）
    expect=<raw>    仅当前记录与已验证原文一致时写入（改密竞态守卫）

    Returns True when applied; conditional expects return False (never raise)
    when the guard fails, so a concurrent write can never be silently overwritten.
    """
    data = _password_bytes(password)
    salt = secrets.token_bytes(16)
    record = json.dumps({'v': 1, 'salt': salt.hex(), 'hash': _derive(data, salt).hex()})
    with store._db() as db:
        if expect == 'unset':
            cur = db.execute(
                'UPDATE settings SET password=? WHERE id=1 AND (password IS NULL OR password=?)',
                (record, ''))
        elif expect is not None:
            cur = db.execute(
                'UPDATE settings SET password=? WHERE id=1 AND password=?',
                (record, expect))
        else:
            cur = db.execute('UPDATE settings SET password=? WHERE id=1', (record,))
    if expect is None:
        log.info('Console password updated; existing sessions invalidated')
    return expect is None or cur.rowcount == 1


def password_set(store):
    """True when a console password record exists (settings.id=1 password non-empty).

    Db-layer errors report False rather than raising — status endpoints must
    not 500 on a missing/damaged store (PR3 WS4).
    """
    import sqlite3
    try:
        with store._db() as db:
            row = db.execute('SELECT password FROM settings WHERE id=1').fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False


class ConsoleAuth:
    def __init__(self, store, *, clock=time.monotonic):
        self.store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts = deque()
        self._sessions = {}

    def _record(self):
        with self.store._db() as db:
            row = db.execute('SELECT password FROM settings WHERE id=1').fetchone()
        if row is None or not row[0]:
            raise ValueError('console password not initialized')
        return row[0]

    def _match_record(self, password):
        """(valid, raw) for password against the stored record; (False, None) on unset/damaged."""
        try:
            raw = self._record()
            record = json.loads(raw)
            if record['v'] != 1 or len(record['salt']) != 32 or len(record['hash']) != 64:
                raise ValueError('invalid password record')
            valid = hmac.compare_digest(
                _derive(_password_bytes(password), bytes.fromhex(record['salt'])),
                bytes.fromhex(record['hash']))
        except (ValueError, KeyError, TypeError):
            return False, None
        return valid, raw

    def login(self, password):
        with self._lock:
            now = self._clock()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 5:
                log.warning('Console login rate limited')
                raise LoginError(429)
            # Bound total KDF work, including successful login churn.
            self._attempts.append(now)
            valid, raw = self._match_record(password)
            if not valid:
                log.warning('Console login rejected')
                raise LoginError()
            self._sessions = {s: value for s, value in self._sessions.items() if value[1] > now}
            if len(self._sessions) >= 128:
                raise LoginError(429)
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[session] = (csrf, now + 900, raw)
            log.info('Console login accepted')
            return session, csrf

    def verify_current(self, password):
        """Verify password against the current record (PR3 WS4 password change).

        Returns the raw verified record on success (the change action feeds it
        back as the conditional-write guard — review I6). Raises LoginError on
        wrong/unset password. Touches neither the login rate-limit window nor
        the session store.
        """
        valid, raw = self._match_record(password)
        if not valid:
            log.warning('Console password verification rejected')
            raise LoginError()
        return raw

    def validate(self, session, csrf, *, write):
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None:
                return False
            if entry[1] <= self._clock() or not hmac.compare_digest(entry[2], self._record()):
                self._sessions.pop(session, None)
                return False
            return not write or (isinstance(csrf, str) and hmac.compare_digest(entry[0], csrf))

    def logout(self, session):
        with self._lock:
            self._sessions.pop(session, None)
