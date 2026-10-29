"""Compatibility layer for digest keys and staged anonymous access."""
import logging
import os
import sqlite3

from inferfabric.proxy.auth import AuthManager
from inferfabric.security.key_store import KeyStore

log = logging.getLogger('inferfabric.security')


class SecurityAuth:
    def __init__(self, legacy, store):
        self.legacy = legacy
        self.store = store

    @property
    def enabled(self):
        # Existing call sites must execute check even while anonymous access is allowed.
        return True

    def check(self, header, model):
        token = AuthManager._strip_bearer(header)
        try:
            policy = self.store.get_policy()
            if not token:
                required = self.legacy.enabled or policy == 'all' or (
                    policy == 'pools' and model.startswith('iff/'))
                return (False, 'API key required') if required else (True, 'ok')
            result = self.store.check(token, model)
            if result[0]:
                return result
            if self.legacy.enabled:
                return self.legacy.check(header, model)
            return result
        except (OSError, sqlite3.DatabaseError, ValueError):
            log.error('Security store unavailable; request denied')
            return False, 'security store unavailable'

    def key_name(self, header):
        token = AuthManager._strip_bearer(header)
        if not token:
            return 'anonymous'
        try:
            name = self.store.key_name(token)
            if name != 'anonymous':
                return name
            return self.legacy.key_name(header) if self.legacy.enabled else 'anonymous'
        except (OSError, sqlite3.DatabaseError, ValueError):
            return 'anonymous'

    def reload(self, config_path=None):
        self.store.validate()  # Raises so ConfigReloader reports failure, never disables auth.
        self.legacy.reload(config_path)


def build_auth(data_dir):
    path = data_dir / 'security.db'
    marker = data_dir / 'security.enabled'
    legacy = AuthManager(data_dir / 'api_keys.yaml')
    if (os.environ.get('IFF_SECURITY_ENABLED') == '1' or marker.exists()
            or marker.is_symlink() or path.exists() or path.is_symlink()):
        store = KeyStore(path)
        # Enduring marker prevents deletion of the DB from reverting to anonymous access.
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        return SecurityAuth(legacy, store)
    return legacy
