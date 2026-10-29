"""Local, interactive provisioning. Never accepts passwords in argv."""
import getpass
import os
from pathlib import Path

from inferfabric.security.console_auth import _password_bytes, set_password
from inferfabric.security.key_store import KeyStore


def run_keys(args, *, data_dir=None):
    if data_dir is None:
        from inferfabric.config import IFF_DATA_DIR
        data_dir = IFF_DATA_DIR
    data_dir = Path(data_dir)
    if args not in (['init'], ['set-password']) and not (
            len(args) == 2 and args[0] == 'policy' and args[1] in ('off', 'pools', 'all')):
        raise ValueError('Usage: iff keys init | set-password | policy <off|pools|all>')
    path = data_dir / 'security.db'
    marker = data_dir / 'security.enabled'
    if args == ['init']:
        if path.exists() or marker.exists() or path.is_symlink() or marker.is_symlink():
            raise FileExistsError('security already initialized; use set-password for recovery')
    else:
        store = KeyStore(path)
    if args[0] == 'policy':
        policy = args[1]
        if input(f'Type {policy} to change enforcement: ') != policy:
            raise ValueError('policy change cancelled')
        if policy == 'all' and input('Type ENABLE ALL after migrating every client: ') != 'ENABLE ALL':
            raise ValueError('policy change cancelled')
        store.set_policy(policy)
        print(f'Enforcement: {policy}')
        return
    password = getpass.getpass('New console password (12+ bytes): ')
    if password != getpass.getpass('Repeat password: '):
        raise ValueError('passwords do not match')
    _password_bytes(password)
    if args == ['init']:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        store = KeyStore(path, provision=True)
    set_password(store, password)
    print('Console password set. API key enforcement is unchanged (new stores default to off).')
    if args == ['init']:
        print('Start/restart the gateway explicitly to load security management; no restart was performed.')
