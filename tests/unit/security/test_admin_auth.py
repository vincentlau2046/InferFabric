"""F7 unified control-plane auth — born-token / fail-closed / Host·Origin gate.

Contract (ported from fork batch-1 TOP1, spec
docs/superpowers/specs/2026-11-17-batch1-auth-network-state.md):
- _ensure_admin_token: secrets.env missing IFF_ADMIN_TOKEN → generate and write
  back (0600); existing value returned as-is (rotation = user edits file →
  restart); legacy MACFABRIC_ADMIN_TOKEN entries honored read-only;
  explicit IFF_ADMIN_TOKEN env wins (ops override channel).
- _admin_gate: token check OFF by default (zero-friction loopback first
  install); strict mode via IFF_ADMIN_REQUIRED=1 (legacy MACFABRIC_
  ADMIN_REQUIRED alias) is fail-closed — no/invalid token → 401.
- Host/Origin checks apply in BOTH modes: non-loopback Host → 403; cross-site
  Origin on write → 403; native apps without Origin → allowed.
- _validate_admin_token_safety: strict mode + empty token → RuntimeError;
  non-loopback PROXY_HOST + empty token → RuntimeError (upstream guard
  retained); default mode on loopback → warn only.
"""

import stat

import pytest

from inferfabric.proxy import handler as proxy_handler


def _read_env_file(path):
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


# ── born-token ──

def test_born_token_generates_when_missing(tmp_path, monkeypatch):
    env_file = tmp_path / "secrets.env"
    monkeypatch.delenv("IFF_ADMIN_TOKEN", raising=False)
    token = proxy_handler._ensure_admin_token(env_file)
    assert token, "born token must be non-empty"
    data = _read_env_file(env_file)
    assert data.get("IFF_ADMIN_TOKEN") == token
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # Idempotent: same value again (never rotated)
    assert proxy_handler._ensure_admin_token(env_file) == token


def test_born_token_reads_existing_iff_value(tmp_path):
    env_file = tmp_path / "secrets.env"
    env_file.write_text("IFF_ADMIN_TOKEN=existing-value-123\n# comment\nOTHER=x\n")
    env_file.chmod(0o600)
    assert proxy_handler._ensure_admin_token(env_file) == "existing-value-123"


def test_born_token_reads_legacy_macfabric_value(tmp_path):
    """Fork-parity alias: legacy MACFABRIC_ADMIN_TOKEN entries honored read-only."""
    env_file = tmp_path / "secrets.env"
    env_file.write_text("MACFABRIC_ADMIN_TOKEN=legacy-value-456\n")
    env_file.chmod(0o600)
    assert proxy_handler._ensure_admin_token(env_file) == "legacy-value-456"


def test_born_token_env_override_wins(tmp_path, monkeypatch):
    """Explicit IFF_ADMIN_TOKEN env beats secrets.env (ops override channel)."""
    monkeypatch.setenv("IFF_ADMIN_TOKEN", "explicit-ops-token")
    assert proxy_handler._ensure_admin_token(tmp_path / "secrets.env") == "explicit-ops-token"


# ── fail-closed + Host/Origin ──

class _FakeHandler:
    """Minimal stand-in (pure-function gate tests, no socket)."""

    def __init__(self, headers, admin_token):
        self.headers = headers
        self._admin_token = admin_token
        self.replied = None

    def _send_json(self, data, status=200):
        self.replied = (data, status)


def test_admin_gate_default_open_without_token(monkeypatch):
    """Default mode: no token config → allowed (Host/Origin checks still apply)."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({}, admin_token="")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True
    assert h.replied is None


def test_admin_gate_strict_mode_fail_closed(monkeypatch):
    """Strict mode: no token config → 401 fail-closed."""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    h = _FakeHandler({}, admin_token="")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 401


def test_admin_gate_legacy_required_alias_enables_strict(monkeypatch):
    """Legacy MACFABRIC_ADMIN_REQUIRED=1 enables strict mode (fork parity)."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.setenv("MACFABRIC_ADMIN_REQUIRED", "1")
    h = _FakeHandler({}, admin_token="")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 401


def test_admin_gate_strict_mode_accepts_matching_token(monkeypatch):
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    h = _FakeHandler({"X-Admin-Token": "tok-abc"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True
    assert h.replied is None


def test_admin_gate_default_mode_wrong_token_still_open(monkeypatch):
    """Default mode: token not checked (strict-mode-only concern)."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({"X-Admin-Token": "wrong"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True


def test_admin_gate_strict_mode_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    h = _FakeHandler({"X-Admin-Token": "wrong"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 401


def test_admin_gate_rejects_non_loopback_host(monkeypatch):
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({"X-Admin-Token": "tok-abc", "Host": "0.0.0.0:8999"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 403


def test_admin_gate_rejects_cross_site_origin_on_write(monkeypatch):
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({"X-Admin-Token": "tok-abc", "Origin": "http://evil.example"},
                     admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is False
    assert h.replied[1] == 403


def test_admin_gate_allows_native_app_no_origin(monkeypatch):
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({"X-Admin-Token": "tok-abc"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is True


def test_admin_gate_allows_loopback_origin():
    h = _FakeHandler({"X-Admin-Token": "tok-abc", "Origin": "http://127.0.0.1:8999"},
                     admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is True


# ── startup safety ──

def test_validate_admin_token_safety_strict_mode_raises_on_empty(monkeypatch):
    """Strict mode + empty token → fail-closed (double guard)."""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    monkeypatch.setattr(proxy_handler, "_ADMIN_TOKEN", "")
    with pytest.raises(RuntimeError):
        proxy_handler._validate_admin_token_safety()


def test_validate_admin_token_safety_default_mode_warns_only(monkeypatch, caplog):
    """Default mode + empty token on loopback → explicit warning, no raise."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    monkeypatch.setattr(proxy_handler, "_ADMIN_TOKEN", "")
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "127.0.0.1")
    proxy_handler._validate_admin_token_safety()


def test_validate_admin_token_safety_non_loopback_host_raises(monkeypatch):
    """Upstream guard retained: network-bound host + empty token → refuse start."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    monkeypatch.setattr(proxy_handler, "_ADMIN_TOKEN", "")
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError):
        proxy_handler._validate_admin_token_safety()
