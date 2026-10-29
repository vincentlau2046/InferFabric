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


def set_password(store, password):
    data = _password_bytes(password)
    salt = secrets.token_bytes(16)
    record = json.dumps({'v': 1, 'salt': salt.hex(), 'hash': _derive(data, salt).hex()})
    with store._db() as db:
        db.execute('UPDATE settings SET password=? WHERE id=1', (record,))
    log.info('Console password updated; existing sessions invalidated')


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
            try:
                raw = self._record()
                record = json.loads(raw)
                if record['v'] != 1 or len(record['salt']) != 32 or len(record['hash']) != 64:
                    raise ValueError('invalid password record')
                valid = hmac.compare_digest(
                    _derive(_password_bytes(password), bytes.fromhex(record['salt'])),
                    bytes.fromhex(record['hash']))
            except (ValueError, KeyError, TypeError):
                valid = False
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
