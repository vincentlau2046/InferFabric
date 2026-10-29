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


def test_init_password_mismatch_creates_nothing(tmp_path, monkeypatch):
    passwords = iter(['test-password-long', 'different-password'])
    monkeypatch.setattr('getpass.getpass', lambda prompt: next(passwords))
    with pytest.raises(ValueError):
        run_keys(['init'], data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
