"""Local, interactive provisioning. Never accepts passwords in argv."""
from datetime import datetime
import getpass
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from inferfabric.security.console_auth import _password_bytes, set_password
from inferfabric.security.key_store import KeyStore

# Everything a damaged store raises when opened: missing file (OperationalError),
# corruption (quick_check), bad permissions or schema (ValueError), I/O (OSError).
DAMAGED = (OSError, sqlite3.DatabaseError, ValueError)


def _artifacts(path):
    """The store file and its SQLite sidecars, in archive order."""
    return [Path(f'{path}{suffix}') for suffix in ('', '-wal', '-shm', '-journal')]


def _initialized(path, marker):
    return (marker.exists() or marker.is_symlink()
            or any(p.exists() or p.is_symlink() for p in _artifacts(path)))


def _prompt_password():
    password = getpass.getpass('New console password (12+ bytes): ')
    if password != getpass.getpass('Repeat password: '):
        raise ValueError('passwords do not match')
    _password_bytes(password)
    return password


def _ensure_marker(marker):
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def _refuse_init(path, marker):
    if not _initialized(path, marker):
        return
    try:
        KeyStore(path)
    except DAMAGED:
        raise FileExistsError('security store is damaged or incomplete; run '
                              '"iff keys recover" to rebuild it (all API keys '
                              'will be discarded)')
    raise FileExistsError('security already initialized; use "iff keys '
                          'set-password" to change the console password, or '
                          '"iff keys recover" for a full reset')


def _stage_store(data_dir, password, policy):
    """Build a complete store in a private staging dir; publish nothing on failure."""
    stage = Path(tempfile.mkdtemp(prefix='.security-stage-', dir=data_dir))
    try:
        store = KeyStore(stage / 'security.db', provision=True)
        if policy != 'off':
            store.set_policy(policy)
        set_password(store, password)
        return store, stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _publish_init(data_dir, path, marker, password):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store, stage = _stage_store(data_dir, password, 'off')
    try:
        # Store first, marker second: build_auth activates on either, so any
        # crash window leaves an enforced (or clearly absent) system, never a
        # marker pointing at a store that does not exist.
        os.replace(store.path, path)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    _ensure_marker(marker)


def _recover(data_dir, path, marker):
    if not _initialized(path, marker):
        raise ValueError('security not initialized; run "iff keys init" first')
    print('WARNING: recovery discards every existing API key and the console')
    print('password, then enforces policy "all" until you change it deliberately.')
    print('Old clients will receive 401 until new keys are issued.')
    if input('Type RECOVER SECURITY to continue: ') != 'RECOVER SECURITY':
        raise ValueError('recovery cancelled')
    password = _prompt_password()
    # Stage the replacement fully before touching the old store, so a failure
    # here leaves the (damaged) system exactly as it was.
    store, stage = _stage_store(data_dir, password, 'all')
    try:
        old = [p for p in _artifacts(path) if p.exists() or p.is_symlink()]
        backup = None
        if old:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = data_dir / f'security-recovery-{stamp}'
            n = 0
            while backup.exists():
                n += 1
                backup = data_dir / f'security-recovery-{stamp}-{n}'
            backup.mkdir(mode=0o700)
            for p in old:
                os.replace(p, backup / p.name)
        os.replace(store.path, path)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if not marker.exists() and not marker.is_symlink():
        _ensure_marker(marker)
    print('Security store rebuilt with policy "all". All previous API keys and')
    print('the console password were discarded.')
    if backup is not None:
        print(f'The previous store was archived at {backup}')
    print('Issue new keys via the console, or lower enforcement deliberately with:')
    print('  iff keys policy pools|off')


def run_keys(args, *, data_dir=None):
    if data_dir is None:
        from inferfabric.config import IFF_DATA_DIR
        data_dir = IFF_DATA_DIR
    data_dir = Path(data_dir)
    if args not in (['init'], ['set-password'], ['recover']) and not (
            len(args) == 2 and args[0] == 'policy' and args[1] in ('off', 'pools', 'all')):
        raise ValueError('Usage: iff keys init | set-password | recover | policy <off|pools|all>')
    path = data_dir / 'security.db'
    marker = data_dir / 'security.enabled'
    if args == ['init']:
        _refuse_init(path, marker)
        _publish_init(data_dir, path, marker, _prompt_password())
        print('Console password set. API key enforcement is unchanged (new stores default to off).')
        print('Start/restart the gateway explicitly to load security management; no restart was performed.')
        return
    if args == ['recover']:
        _recover(data_dir, path, marker)
        return
    try:
        store = KeyStore(path)
    except DAMAGED:
        if _initialized(path, marker):
            raise ValueError('security store is damaged; run "iff keys recover" to '
                             'rebuild it (all API keys will be discarded)')
        raise ValueError('security not initialized; run "iff keys init" first')
    if args[0] == 'policy':
        policy = args[1]
        if input(f'Type {policy} to change enforcement: ') != policy:
            raise ValueError('policy change cancelled')
        if policy == 'all' and input('Type ENABLE ALL after migrating every client: ') != 'ENABLE ALL':
            raise ValueError('policy change cancelled')
        store.set_policy(policy)
        print(f'Enforcement: {policy}')
        return
    set_password(store, _prompt_password())
    print('Console password set. Existing API keys and enforcement are unchanged.')
