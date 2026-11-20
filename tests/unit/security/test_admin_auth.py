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


def test_born_token_reads_existing_iff_value(tmp_path, monkeypatch):
    # CloudDiscovery._inject_secrets_env（ProxyManager 构造副作用）会把真实
    # secrets.env 的 IFF_ADMIN_TOKEN setdefault 进 os.environ——born-token 语义
    # 是「显式 env 覆盖」，本测试须先清掉环境才能验证文件读取路径。
    monkeypatch.delenv("IFF_ADMIN_TOKEN", raising=False)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("IFF_ADMIN_TOKEN=existing-value-123\n# comment\nOTHER=x\n")
    env_file.chmod(0o600)
    assert proxy_handler._ensure_admin_token(env_file) == "existing-value-123"


def test_born_token_reads_legacy_macfabric_value(tmp_path, monkeypatch):
    """Fork-parity alias: legacy MACFABRIC_ADMIN_TOKEN entries honored read-only."""
    monkeypatch.delenv("IFF_ADMIN_TOKEN", raising=False)
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

# ── review PR#15 fixes (B1 + N1–N4) ──

def test_admin_gate_non_loopback_bind_enforces_token_by_default(monkeypatch):
    """B1: non-loopback PROXY_HOST + default mode → token enforced (401 without)."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "0.0.0.0")
    h = _FakeHandler({"Host": "0.0.0.0:8999"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "0.0.0.0:8999") is False
    assert h.replied[1] == 401
    h2 = _FakeHandler({"X-Admin-Token": "tok-abc", "Host": "0.0.0.0:8999"},
                      admin_token="tok-abc")
    assert proxy_handler._admin_gate(h2, "0.0.0.0:8999") is True


def test_admin_gate_non_loopback_bind_explicit_opt_out_still_open(monkeypatch):
    """B1: explicit IFF_ADMIN_REQUIRED=0 opts out even on non-loopback bind."""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "0")
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "0.0.0.0")
    h = _FakeHandler({"Host": "0.0.0.0:8999"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "0.0.0.0:8999") is True


def test_admin_auth_enabled_explicit_opt_out_on_loopback(monkeypatch):
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "0")
    assert proxy_handler._admin_auth_enabled() is False


def test_validate_safety_non_loopback_explicit_opt_out_empty_token_raises(monkeypatch):
    """B1: opt-out + network bind + empty token → startup still refused."""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "0")
    monkeypatch.setattr(proxy_handler, "_ADMIN_TOKEN", "")
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError):
        proxy_handler._validate_admin_token_safety()


def test_admin_gate_ipv6_loopback_host_accepted(monkeypatch):
    """N1: "[::1]:8999" Host must resolve to ::1, not "[" (which 403'd)."""
    monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
    monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
    h = _FakeHandler({"X-Admin-Token": "t", "Host": "[::1]:8999"}, admin_token="t")
    assert proxy_handler._admin_gate(h, "[::1]:8999") is True
    assert h.replied is None


def test_admin_gate_ipv6_origin_accepted(monkeypatch):
    """N1: bracketed IPv6 Origin netloc resolves to ::1 (write request)."""
    h = _FakeHandler({"X-Admin-Token": "t", "Origin": "http://[::1]:8999"},
                     admin_token="t")
    assert proxy_handler._admin_gate(h, "[::1]:8999", write=True) is True


def test_admin_gate_non_ascii_token_401_not_crash(monkeypatch):
    """N3: latin-1 decoded header with non-ASCII → clean 401, no TypeError."""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    h = _FakeHandler({"X-Admin-Token": "tök-énv"}, admin_token="tok-abc")
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 401


def test_born_token_write_failure_degrades_to_in_memory(tmp_path, monkeypatch, caplog):
    """N4: read-only state dir → warn + continue with in-memory token, no raise."""
    import logging
    from inferfabric.cloud_discovery import SecretsManager
    env_file = tmp_path / "secrets.env"
    monkeypatch.delenv("IFF_ADMIN_TOKEN", raising=False)

    def boom(self, key, value):
        raise OSError("read-only file system")

    monkeypatch.setattr(SecretsManager, "write", boom)
    with caplog.at_level(logging.WARNING, logger="inferfabric"):
        token = proxy_handler._ensure_admin_token(env_file)
    assert token, "in-memory born token must still be returned"
    assert not env_file.exists()
    assert any("persist" in r.getMessage().lower() for r in caplog.records)


def test_purge_pool_candidates_reads_born_token_from_secrets_env(tmp_path, monkeypatch):
    """N2: no env token → CLI purge reads the born token from secrets.env."""
    import inferfabric.uninstaller as un
    env_file = tmp_path / "secrets.env"
    env_file.write_text("IFF_ADMIN_TOKEN=born-secret-9\n")
    env_file.chmod(0o600)
    monkeypatch.delenv("IFF_ADMIN_TOKEN", raising=False)

    captured = {}

    class _FakeSM:
        def __init__(self, path=None):
            pass

        def load(self):
            return {"IFF_ADMIN_TOKEN": "born-secret-9"}

    import inferfabric.cloud_discovery as cd
    monkeypatch.setattr(cd, "SecretsManager", _FakeSM)

    def _fake_admin_request(method, path, *, host, port, token, timeout):
        captured["token"] = token
        raise un.PoolsConfigError("stop here")

    monkeypatch.setattr(un, "_admin_request", _fake_admin_request)
    pools = tmp_path / "model_pools.yaml"
    pools.write_text(
        "pools:\n"
        "  fabric-a:\n"
        "    candidates:\n"
        "      - id: c1\n"
        "        kind: local\n"
        "        model_name: m1\n"
    )
    try:
        un.purge_pool_candidates(pools, {"m1"})
    except un.PoolsConfigError:
        pass
    assert captured.get("token") == "born-secret-9"
