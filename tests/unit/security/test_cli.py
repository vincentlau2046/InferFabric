import pytest

from inferfabric.security.cli import run_keys
from inferfabric.security.console_auth import ConsoleAuth
from inferfabric.security.key_store import KeyStore


def test_init_reinit_reset_and_confirmations(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'test-password-long')
    run_keys(['init'], data_dir=tmp_path)
    store = KeyStore(tmp_path / 'security.db')
    assert store.get_policy() == 'off'
    assert (tmp_path / 'security.enabled').exists()
    session, csrf = ConsoleAuth(store).login('test-password-long')
    with pytest.raises((ValueError, FileExistsError)):
        run_keys(['init'], data_dir=tmp_path)
    assert store.get_policy() == 'off'
    monkeypatch.setattr('builtins.input', lambda prompt: 'wrong')
    with pytest.raises(ValueError):
        run_keys(['policy', 'all'], data_dir=tmp_path)
    assert store.get_policy() == 'off'
    monkeypatch.setattr('builtins.input', lambda prompt: 'all' if 'all' in prompt else 'ENABLE ALL')
    # Explicit per-step typed confirmations, not a blanket --yes.
    answers = iter(['all', 'ENABLE ALL'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    run_keys(['policy', 'all'], data_dir=tmp_path)
    assert store.get_policy() == 'all'
    auth = ConsoleAuth(store)
    session, csrf = auth.login('test-password-long')
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'changed-password-long')
    run_keys(['set-password'], data_dir=tmp_path)
    assert not auth.validate(session, csrf, write=True)
    output = capsys.readouterr().out
    assert 'test-password-long' not in output and 'changed-password-long' not in output


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_recover_damaged_store_stays_enforced(tmp_path, monkeypatch, capsys, damage):
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'test-password-long')
    run_keys(['init'], data_dir=tmp_path)
    store = KeyStore(tmp_path / 'security.db')
    store.set_policy('pools')
    _, old_token = store.create_key('old-client', None, None)
    old_auth = ConsoleAuth(store)
    old_session, old_csrf = old_auth.login('test-password-long')
    if damage == 'missing':
        store.path.unlink()
    else:
        store.path.write_bytes(b'corrupt sqlite')
    monkeypatch.setattr('builtins.input', lambda prompt: 'RECOVER SECURITY')
    run_keys(['recover'], data_dir=tmp_path)
    recovered = KeyStore(store.path)
    assert recovered.get_policy() == 'all'
    assert not recovered.check(old_token, 'iff/work')[0]
    assert recovered.list_keys() == []
    assert ConsoleAuth(recovered).login('test-password-long')
    assert (tmp_path / 'security.enabled').exists()
    assert recovered.path.stat().st_mode & 0o777 == 0o600
    if damage == 'corrupt':
        backups = list(tmp_path.glob('security-recovery-*/security.db'))
        assert len(backups) == 1 and backups[0].read_bytes() == b'corrupt sqlite'
    # Live objects bound to the same path see the replacement: sessions do not
    # survive a recovery, and the gateway needs no restart to pick up the store.
    assert not old_auth.validate(old_session, old_csrf, write=True)
    assert old_token not in capsys.readouterr().out


def test_recovery_requires_explicit_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'test-password-long')
    run_keys(['init'], data_dir=tmp_path)
    path = tmp_path / 'security.db'
    before = path.read_bytes()
    monkeypatch.setattr('builtins.input', lambda prompt: 'no')
    with pytest.raises(ValueError, match='cancelled'):
        run_keys(['recover'], data_dir=tmp_path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['security.db', 'security.enabled']


def test_init_preparation_failure_does_not_leave_activation(tmp_path, monkeypatch):
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'test-password-long')
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr('inferfabric.security.cli.set_password', fail)
    with pytest.raises(OSError):
        run_keys(['init'], data_dir=tmp_path)
    assert not (tmp_path / 'security.enabled').exists()
    assert not (tmp_path / 'security.db').exists()


def test_init_password_mismatch_creates_nothing(tmp_path, monkeypatch):
    passwords = iter(['test-password-long', 'different-password'])
    monkeypatch.setattr('getpass.getpass', lambda prompt: next(passwords))
    with pytest.raises(ValueError):
        run_keys(['init'], data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
