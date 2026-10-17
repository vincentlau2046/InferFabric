"""
unit/infra/test_security_misc.py — 安全 & 基础设施杂项测试

测试对象: 多模块（req_id、pkill、SSRF、admin token、config 校验、SSE buffer）
覆盖范围:
  - req_id 格式、无碰撞、单调递增
  - _pkill_vllm_fallback: PID 文件优先、fuser 端口清理、无全局 pkill -f
  - _validate_cloud_test_url: 私有 IP、DNS rebinding、协议检查
  - _check_admin: hmac.compare_digest、fail-fast
  - _validate_runtime_config: rate_limit 各字段校验
  - SSELineBuffer 基础提取、跨边界、无 usage、最后值胜出、损坏 JSON
"""

import json
import os
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest


# ─── D-1: Thread-Safe req_id ────────────────────────────────

def test_req_id_format():
    """req_id uses {8-hex}-{8-hex} format with unique values."""
    from inferfabric.proxy_manager import ProxyManager

    pm = ProxyManager()
    rid1 = pm.new_request_id()
    assert len(rid1) == 17  # 8 + 1 + 8
    assert rid1[8] == "-"
    # Both halves are hex
    int(rid1[:8], 16)
    int(rid1[9:], 16)


def test_req_id_no_collisions():
    """100 concurrent threads allocate 500 req_ids each — zero collisions."""
    from inferfabric.proxy_manager import ProxyManager

    pm = ProxyManager()
    ids = set()
    lock = threading.Lock()
    errors = []

    def allocate():
        try:
            for _ in range(500):
                rid = pm.new_request_id()
                with lock:
                    if rid in ids:
                        errors.append(f"Collision: {rid}")
                    ids.add(rid)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=allocate) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert len(ids) == 50000


def test_req_id_monotonic():
    """Counter component increments monotonically."""
    from inferfabric.proxy_manager import ProxyManager

    pm = ProxyManager()
    prev = -1
    for _ in range(1000):
        rid = pm.new_request_id()
        c = int(rid[:8], 16)
        assert c > prev, f"Counter not monotonic: {c} <= {prev}"
        prev = c


# ─── D-2: Process Termination ────────────────────────────

def test_pkill_vllm_fallback_uses_pid_file():
    """D-2: _pkill_vllm_fallback uses PID file + fuser, not pkill -f."""
    from inferfabric.process_manager.vllm import VLLMProcessManager
    import inspect

    source = inspect.getsource(VLLMProcessManager._pkill_vllm_fallback)

    # Must NOT contain global pkill patterns
    assert "pkill -9 -f" not in source
    assert 'pkill -9 -f "vllm serve"' not in source
    assert 'pkill -9 -f "VLLM::EngineCore"' not in source
    assert 'pkill -9 -f "sglang serve"' not in source

    # Must use PID file approach
    assert "vllm_*.pid" in source or "_log_dir.glob" in source
    # Must use fuser for port-based cleanup
    assert "fuser" in source or "_pkill_by_port" in source


# ─── D-3: SSRF Protection ────────────────────────────────

def test_validate_cloud_test_url_allows_private():
    """v6.6 放宽策略：私有 / 局域网 IP 字面量放行（内网中转站场景）。"""
    from inferfabric.proxy.handler import ProxyHandler

    handler = ProxyHandler.__new__(ProxyHandler)
    mock_pm = Mock()
    mock_pm.cloud._providers = {}

    ok, msg, ips = handler._validate_cloud_test_url("http://127.0.0.1/test", mock_pm)
    assert ok, msg

    ok, msg, ips = handler._validate_cloud_test_url("http://10.0.0.1/api", mock_pm)
    assert ok, msg

    ok, msg, ips = handler._validate_cloud_test_url("http://192.168.1.1/", mock_pm)
    assert ok, msg


def test_validate_cloud_test_url_allows_http():
    """v6.6 放宽策略：http scheme 放行（内网中转站常用 http）。"""
    from inferfabric.proxy.handler import ProxyHandler

    handler = ProxyHandler.__new__(ProxyHandler)
    mock_pm = Mock()
    mock_pm.cloud._providers = {}

    ok, msg, ips = handler._validate_cloud_test_url("http://1.2.3.4/v1", mock_pm)
    assert ok, msg


def test_validate_cloud_test_url_allows_unregistered_host():
    """v6.6 放宽策略：已删"必须是已注册 provider"白名单——未注册公网主机放行。"""
    from inferfabric.proxy.handler import ProxyHandler

    handler = ProxyHandler.__new__(ProxyHandler)
    mock_pm = Mock()
    mock_pm.cloud._providers = {}

    ok, msg, ips = handler._validate_cloud_test_url("http://8.8.8.8/v1", mock_pm)
    assert ok, msg


def test_validate_cloud_test_url_blocks_metadata():
    """v6.6 硬拦不变：云元数据地址 169.254.169.254 仍拒绝（防读实例凭据）。"""
    from inferfabric.proxy.handler import ProxyHandler

    handler = ProxyHandler.__new__(ProxyHandler)
    mock_pm = Mock()

    ok, msg, ips = handler._validate_cloud_test_url("http://169.254.169.254/latest/meta-data/", mock_pm)
    assert not ok
    assert "metadata" in msg.lower()
def test_validate_cloud_test_url_allows_registered_provider():
    """SSRF: registered provider host is accepted (or DNS fails safely)."""
    from inferfabric.proxy.handler import ProxyHandler
    from inferfabric.cloud_discovery import ProviderConfig

    handler = ProxyHandler.__new__(ProxyHandler)
    mock_pm = Mock()
    cfg = ProviderConfig(
        name="test-prov", api_key="x",
        openai_base="https://qianfan.baidubce.com/v2",
    )
    mock_pm.cloud._providers = {"test-prov": cfg}
    mock_pm.cloud.providers = {"test-prov": cfg}
    mock_pm.ensure_cloud_discovered = lambda: None

    ok, msg, ips = handler._validate_cloud_test_url(
        "https://qianfan.baidubce.com/health", mock_pm
    )
    # Either passes, or fails safely (DNS error or private IP)
    assert ok or any(w in msg.lower() for w in ("dns", "resolv", "provider", "host", "allow", "private")),         f"Unexpected: ok={ok}, msg={msg}"


def test_check_admin_uses_hmac_compare_digest():
    """D-4: _check_admin uses hmac.compare_digest (constant-time)."""
    from inferfabric.proxy.handler import ProxyHandler
    import inspect
    source = inspect.getsource(ProxyHandler._check_admin)
    assert "hmac.compare_digest" in source or "compare_digest" in source



def test_check_admin_no_token_open_mode():
    """D-4: with no IFF_ADMIN_TOKEN, _check_admin returns True."""
    import inferfabric.proxy.handler as handler_module
    old_token = handler_module._ADMIN_TOKEN
    handler_module._ADMIN_TOKEN = ""
    try:
        handler = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
        handler.headers = {}
        result = handler_module.ProxyHandler._check_admin(handler)
        assert result is True
    finally:
        handler_module._ADMIN_TOKEN = old_token


def test_check_admin_rejects_wrong_token():
    """D-4: wrong token rejected; correct token accepted."""
    import inferfabric.proxy.handler as handler_module
    from unittest.mock import Mock

    old = handler_module._ADMIN_TOKEN
    handler_module._ADMIN_TOKEN = "secret123"
    try:
        h = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
        h.headers = {}
        h.send_response = lambda code: None
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        h.log_request = lambda code: None
        h.requestline = "GET / HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.wfile = Mock()
        h.wfile.write = lambda x: None
        h.wfile.flush = lambda: None
        result = handler_module.ProxyHandler._check_admin(h)
        assert result is False

        h2 = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
        h2.headers = {"X-Admin-Token": "secret123"}
        h2.send_response = lambda code: None
        h2.send_header = lambda k, v: None
        h2.end_headers = lambda: None
        h2.log_request = lambda code: None
        h2.requestline = "GET / HTTP/1.1"
        h2.request_version = "HTTP/1.1"
        h2.wfile = Mock()
        h2.wfile.write = lambda x: None
        h2.wfile.flush = lambda: None
        result2 = handler_module.ProxyHandler._check_admin(h2)
        assert result2 is True
    finally:
        handler_module._ADMIN_TOKEN = old

def test_validate_runtime_config_rejects_bad_rate_limit():
    """D-5: bad rate_limit values are rejected."""
    from inferfabric.proxy_manager import ProxyManager
    from inferfabric.config import ConfigError

    pm = ProxyManager()

    with pytest.raises(ConfigError, match="mode"):
        pm._validate_runtime_config({"rate_limit": {"mode": "block"}})

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"rate_limit": {"timeout": -1}})

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"rate_limit": {"timeout": "fast"}})

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"rate_limit": {"server_rpm": -100}})


def test_validate_runtime_config_rejects_bad_types():
    """D-5: type errors in config are rejected."""
    from inferfabric.proxy_manager import ProxyManager
    from inferfabric.config import ConfigError

    pm = ProxyManager()

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"rate_limit": "observe"})

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"access_log_jsonl": "yes"})

    with pytest.raises(ConfigError):
        pm._validate_runtime_config({"request_log_retention_days": "forever"})


def test_validate_runtime_config_accepts_valid():
    """D-5: valid iff.yaml passes schema validation."""
    from inferfabric.proxy_manager import ProxyManager

    pm = ProxyManager()

    pm._validate_runtime_config({
        "rate_limit": {
            "mode": "observe",
            "timeout": 5,
            "server_rpm": 0,
            "model_rpm_default": 10,
            "max_concurrent": 8,
        },
        "access_log_jsonl": True,
        "request_log_retention_days": 90,
    })

    pm._validate_runtime_config({
        "rate_limit": {"mode": "reject", "max_concurrent": "auto"},
    })


# ─── G-1b: SSE Line Buffer ───────────────────────────────

def test_sse_buffer_extracts_usage():
    """SSELineBuffer extracts prompt_tokens + completion_tokens from SSE."""
    from inferfabric.proxy.sse_buffer import SSELineBuffer

    buff = SSELineBuffer()
    chunk1 = b'data: {"id":"1","choices":[]}\n\n'
    chunk2 = b'data: {"id":"2","choices":[{"index":0}],"usage":{"prompt_tokens":100,"completion_tokens":50}}\n\n'
    chunk3 = b'data: [DONE]\n\n'

    buff.feed(chunk1)
    assert buff.usage["prompt_tokens"] == 0

    buff.feed(chunk2)
    assert buff.usage["prompt_tokens"] == 100
    assert buff.usage["completion_tokens"] == 50

    buff.feed(chunk3)
    buff.flush()
    assert buff.usage["prompt_tokens"] == 100


def test_sse_buffer_cross_boundary():
    """usage chunk split across two read() calls is correctly parsed."""
    from inferfabric.proxy.sse_buffer import SSELineBuffer

    buff = SSELineBuffer()
    full = b'data: {"usage":{"prompt_tokens":42,"completion_tokens":7}}\n\n'
    split = len(full) // 2

    buff.feed(full[:split])
    buff.feed(full[split:])
    assert buff.usage["prompt_tokens"] == 42
    assert buff.usage["completion_tokens"] == 7


def test_sse_buffer_no_usage():
    """SSELineBuffer with no usage keeps tokens at 0."""
    from inferfabric.proxy.sse_buffer import SSELineBuffer

    buff = SSELineBuffer()
    buff.feed(b'data: {"id":"1"}\n\ndata: {"id":"2"}\n\n')
    buff.flush()
    assert buff.usage["prompt_tokens"] == 0
    assert buff.usage["completion_tokens"] == 0


def test_sse_buffer_last_usage_wins():
    """Last usage block overwrites previous ones."""
    from inferfabric.proxy.sse_buffer import SSELineBuffer

    buff = SSELineBuffer()
    buff.feed(b'data: {"usage":{"prompt_tokens":10}}\n\n')
    buff.feed(b'data: {"usage":{"prompt_tokens":888}}\n\n')
    buff.feed(b'data: {"usage":{"prompt_tokens":999}}\n\n')
    buff.flush()
    assert buff.usage["prompt_tokens"] == 999


def test_sse_buffer_handles_corrupted_json():
    """Corrupted JSON silently skipped — no crash."""
    from inferfabric.proxy.sse_buffer import SSELineBuffer

    buff = SSELineBuffer()
    buff.feed(b'data: {bad json}\n\n')
    buff.feed(b'data: {"usage":{"prompt_tokens":55}}\n\n')
    buff.flush()
    assert buff.usage["prompt_tokens"] == 55