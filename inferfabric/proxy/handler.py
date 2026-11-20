"""
inferfabric/proxy/handler.py — ProxyHandler, ThreadedHTTPServer, main.

Core HTTP handler with routing, dashboard, and delegation to:
  chat_handlers.py — chat completions
  metrics.py — vLLM Prometheus metrics

Extracted from proxy.py (v4.1 P3 split).
"""

import errno
import re
import sys
import os
import signal
import socket
import logging
import json
import hmac
import hashlib
import ipaddress
import http.server
import socketserver
import tempfile
import threading
import time
import yaml
from pathlib import Path
from urllib.parse import urlparse
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses
import sqlite3

from inferfabric.state import GPUMode
from inferfabric.agent_registry import request_protocol
from inferfabric.agent_stats import AGENT_GRAN, aggregate_agent_stats
from inferfabric.metrics_aggregator import cost_of_row
from inferfabric.proxy.request_logger import RequestLog
from inferfabric.anomaly_collector import AnomalyEvent
from inferfabric.proxy import pack_handlers

# Admin token for control-plane routes (/switch, /stop, /deploy, /pull, etc.)
# If set, requests must include X-Admin-Token header matching this value.
# If not set (default), all control routes are open (localhost-only binding is the security boundary).
_ADMIN_TOKEN = os.environ.get("IFF_ADMIN_TOKEN", "")


def _admin_auth_enabled():
    """Control-plane auth flag (F7 pick): default OFF on loopback, opt-in strict mode.

    First-install experience on a loopback gateway wins — API keys authenticate
    the cloud request path and must not gate local UI usage. Explicit
    IFF_ADMIN_REQUIRED=1 (aliases: true/yes/on, case-insensitive; legacy
    MACFABRIC_ADMIN_REQUIRED honored for fork parity) enables strict mode;
    explicit =0/false/no/off opts out.

    Review PR#15 B1: a non-loopback bind defaults auth ON (unless explicitly
    opted out). Born-token bootstrap means _ADMIN_TOKEN is never empty after
    main(), which made the startup "empty token + non-loopback → refuse"
    guard unreachable — without this default, a network-bound gateway in
    default mode would expose the control plane unauthenticated.
    """
    for name in ("IFF_ADMIN_REQUIRED", "MACFABRIC_ADMIN_REQUIRED"):
        val = os.environ.get(name, "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
    if PROXY_HOST not in ("127.0.0.1", "localhost", "::1"):
        return True
    return False


def _ensure_admin_token(secrets_path=None):
    """F7 born-token — deterministic provisioning of the control-plane token.

    Priority: explicit IFF_ADMIN_TOKEN env (ops override) > secrets.env key
    IFF_ADMIN_TOKEN (0600; generated and written back when missing; legacy
    MACFABRIC_ADMIN_TOKEN entries are honored read-only for fork parity).
    Idempotent: an existing value is returned as-is, never rotated.
    """
    env = os.environ.get("IFF_ADMIN_TOKEN", "")
    if env:
        return env
    from pathlib import Path
    from inferfabric.cloud_discovery import SecretsManager
    sm = SecretsManager(Path(secrets_path) if secrets_path else None)
    data = sm.load()
    token = data.get("IFF_ADMIN_TOKEN") or data.get("MACFABRIC_ADMIN_TOKEN", "")
    if not token:
        import secrets as _secrets
        token = _secrets.token_urlsafe(32)
        try:
            sm.write("IFF_ADMIN_TOKEN", token)
        except OSError:
            # Review PR#15 N4: read-only IFF_DATA_DIR must not crash-loop the
            # gateway at startup. Degrade to the in-memory token (this process
            # still enforces it); persistence resumes when the state dir is
            # writable. Strict mode stays fail-closed via
            # _validate_admin_token_safety (empty token → refuse to start).
            log.warning(
                "Could not persist born admin token to secrets.env — "
                "continuing with an in-memory token for this process"
            )
    return token


def _host_hostname(netloc):
    """Hostname from a Host/Origin netloc, bracket-aware (review PR#15 N1).

    "[::1]:8999" → "::1"; the previous ``split(":")[0]`` turned it into "[",
    403-ing every admin route for IPv6 loopback deployments (fail-closed but
    unusable).
    """
    if netloc.startswith("["):
        return netloc[1:].split("]")[0]
    return netloc.split(":")[0]


def _admin_gate(handler, expected_host, write=False):
    """F7 unified control-plane gate (pure function; _check_admin delegates).

    Semantics: auth OFF by default on loopback (zero-friction first install);
    ON for non-loopback binds unless IFF_ADMIN_REQUIRED is explicitly opted
    out (review PR#15 B1). Strict mode (IFF_ADMIN_REQUIRED=1) is fail-closed —
    no/invalid token → 401.
    Host/Origin checks apply in BOTH modes unconditionally: they block browser
    cross-site requests and DNS rebinding, not local usage.
    """
    loopback = ("127.0.0.1", "localhost", "::1")
    host = handler.headers.get("Host", "")
    origin = handler.headers.get("Origin")
    host_ok = (not host) or host == expected_host or _host_hostname(host) in loopback
    origin_ok = origin is None or (
        origin in (f"http://{expected_host}", f"https://{expected_host}")
        or _host_hostname(origin.split("//")[-1].split("/")[0]) in loopback)
    if _admin_auth_enabled():
        token = handler.headers.get("X-Admin-Token", "")
        configured = getattr(handler, "_admin_token", "")
        # compare bytes (review PR#15 N3): http.server decodes headers as
        # latin-1; a non-ASCII token string would raise TypeError inside
        # compare_digest and hang the connection instead of 401-ing.
        if (not configured
                or not hmac.compare_digest(token.encode("utf-8"),
                                           configured.encode("utf-8"))):
            handler._send_json({"error": "Unauthorized", "status": "unauthorized"}, 401)
            return False
    if not host_ok:
        handler._send_json({"error": "untrusted host"}, 403)
        return False
    if write or origin is not None:
        if not origin_ok:
            handler._send_json({"error": "untrusted origin"}, 403)
            return False
    return True
from inferfabric.proxy_manager import (
    ProxyManager, PROXY_HOST, PROXY_PORT, IFF_DATA_DIR,
    HEALTH_CHECK_INTERVAL, WATCHDOG_INTERVAL,
)
from inferfabric import forwarder, __version__
from inferfabric.proxy.chat_handlers import handle_chat, handle_ollama_native
from inferfabric.proxy.connect_handlers import (
    handle_connect_apply, handle_connect_rollback, handle_connect_status,
)
from inferfabric.proxy.metrics import handle_vllm_metrics
from inferfabric.engine_adapter import get_adapter
from inferfabric.watchdog import ModelWatchdog

log = logging.getLogger("inferfabric.proxy")

# C2: 昂贵采集（mgr.status 健康探测 + metrics 24h 扫描）的 TTL 缓存窗口（秒）。
# 窗口内 304 命中 / 200 都复用缓存 → 昂贵工作最多每 SNAPSHOT_EXP_TTL 秒跑一次，
# 避免 3s 轮询把 100k 样本扫描 + 3×3s 健康探测打到与 chat 转发共享的 32 线程池上。
SNAPSHOT_EXP_TTL = 15.0


def _snapshot_etag(payload: dict) -> str:
    """C1: snapshot etag = 全 payload 字段组内容哈希（排除 ts/rev/etag 易变元数据）。

    覆盖 status/system/models/history/token_stats/request_log/metrics_24h/
    local_models，任意一组变化即失效 etag → dashboard 重取，杜绝稳态冻结
    （recent requests / 24h 指标 / GPU 温度被吞掉）。
    """
    content = {k: v for k, v in payload.items() if k not in ("ts", "rev", "etag")}
    # DeviceTelemetry T5: the device sampling timestamp is volatile-only in
    # the hash — actual values, sources and capabilities still invalidate.
    for holder_key in ("device", "system"):
        holder = content.get(holder_key)
        if not isinstance(holder, dict):
            continue
        if holder_key == "device":
            env = holder
        else:
            env = holder.get("device")
        if isinstance(env, dict) and isinstance(env.get("snapshot"), dict):
            env = dict(env)
            env["snapshot"] = {k: v for k, v in env["snapshot"].items()
                               if k != "timestamp"}
            if holder_key == "device":
                content["device"] = env
            else:
                content["system"] = {**holder, "device": env}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class _ExpensiveCache:
    """C2: 昂贵采集（mgr.status 健康探测 + metrics 24h 扫描）的 TTL 单飞缓存。

    - TTL 内复用缓存 → 昂贵工作最多每 ttl 秒跑一次。
    - 单飞 (single-flight)：同一时刻至多一个线程重算；慢重算（GPU 驱动挂起时
      健康探测 3×3s）期间，并发调用方拿旧值而非排队 → 不会堆叠占满 32 线程池。
    """

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.Lock()      # 保护 _entry
        self._relock = threading.Lock()    # 保护"重算进行中"（单飞）
        self._entry = None                 # (value_dict, at)

    def get_or_refresh(self, compute):
        """返回昂贵采集结果；缓存新鲜则复用，过期则单飞重算。

        compute: 无参可调用，返回 dict（昂贵采集结果）。
        """
        now = time.time()
        with self._lock:
            e = self._entry
            if e is not None and now - e[1] <= self._ttl:
                return e[0]
        # 缓存过期或为空 → 尝试成为唯一的重算线程
        if self._relock.acquire(blocking=False):
            try:
                value = compute()
                with self._lock:
                    self._entry = (value, time.time())
                return value
            finally:
                self._relock.release()
        # 已有线程在重算：拿旧值（有界陈旧 = 一次重算时长），不触发第二次慢采集
        with self._lock:
            e = self._entry
            if e is not None:
                return e[0]
        # 竞态首调（尚无 entry 且重算进行中）：自行重算（罕见，仅启动瞬间）
        return compute()


def _metrics_axis(pm) -> list[tuple[str, str]]:
    """v6.0: 指标图 x 轴（配置驱动）— (model_name, source) 列表。

    local = models.d 友好名（ModelConfig.name）；cloud = 启用云预设的模型短名
    （CloudDiscovery.cloud_models 键）。同名 local 优先、去重保序。
    构建失败返回 []（降级为数据驱动轴，不拖垮指标采集）。"""
    axis: list[tuple[str, str]] = []
    try:
        for m in pm.mgr._models.values():
            axis.append((m.name, "local"))
    except Exception:
        pass
    try:
        for model_id in pm.cloud.cloud_models:
            axis.append((model_id, "cloud"))
    except Exception:
        pass
    seen = set()
    out: list[tuple[str, str]] = []
    for name, src in axis:
        if name and name not in seen:
            seen.add(name)
            out.append((name, src))
    return out


# 延迟趋势档位（单位语义统一：分钟/小时/天，月/周不做——数据仅 30d 回放）：
#   minute = 近 60min · 12×5min  桶宽 5min（请求级 1min 太碎，5min 对齐一次请求量级）
#   hour   = 近 24h  · 24×1h
#   day    = 近 30 天 · 30×1d（deque 回放 720h=30d，天档恰在数据边界）
_LAT_BUCKET_MS = {"minute": 5 * 60 * 1000, "hour": 3600 * 1000, "day": 86400 * 1000}
_LAT_CACHE_TTL = {"minute": 300.0, "hour": 300.0, "day": 300.0}   # 5min——延迟趋势历史值无需秒级刷新
# 单飞 TTL 缓存：重扫描（deque + 分位）不随 3s 轮询重算；按 window 各一个实例。
_lat_series_cache = {w: _ExpensiveCache(ttl=_LAT_CACHE_TTL[w]) for w in _LAT_CACHE_TTL}

# v6.2 功耗/电费：档位 小时/天/周 各一实例 TTL 缓存（5min——功耗历史无需秒级刷新）。
# month 保留键（端点只读无害），UI 不再提供「月」；week = 近~90 天 · 13×7d 周桶。
_POWER_CACHE_TTL_GRANS = ("hour", "day", "week", "month")
_power_series_cache = {g: _ExpensiveCache(ttl=300.0) for g in _POWER_CACHE_TTL_GRANS}
# DeviceTelemetry T4: /api/device_power 独立缓存（不能与 /api/power 共用，
# 两个端点读不同流，共享 key 会互相返回对方的流）。
_device_power_series_cache = {g: _ExpensiveCache(ttl=300.0) for g in _POWER_CACHE_TTL_GRANS}


def _compute_expensive(pm) -> dict:
    """C2: 采集昂贵字段组（mgr.status 健康探测 + metrics 24h 扫描）。
    各组独立兜底（单组失败不拖垮另一组）。"""
    exp = {}
    try:
        # DeviceTelemetry T5: skeleton only — gpu_* fields are refilled from
        # the shared cached reading so one probe feeds device/system/status.
        exp["status"] = pm.mgr.status(include_device=False)
    except Exception:
        exp["status"] = {}
    try:
        exp["metrics_24h"] = pm.metrics.get_metrics(
            "24h", axis_models=_metrics_axis(pm))
    except Exception:
        exp["metrics_24h"] = {}
    return exp


# ═══ v5.2 Route Tables ═══════════════════════════════════════

def _admin(fn):
    """Admin guard: checks _check_admin before executing handler."""
    def wrapper(handler, pm):
        if not handler._check_admin():
            return
        fn(handler, pm)
    return wrapper


def _serve_api_spec(handler, pm):
    """GET /api/openapi.json — OpenAPI 规范。"""
    from inferfabric.api_spec import get_openapi_spec
    handler._send_json(get_openapi_spec())


# 逻辑模型池管理接口（admin 路由表引用；零协议转换、零出网校验）
from inferfabric.model_pool import admin as mp_admin  # noqa: E402
from inferfabric.security import admin as key_admin  # noqa: E402
from inferfabric.security.auth_facade import SecurityAuth, build_auth  # noqa: E402

_GET_ROUTES = {
    "/":                        lambda h, pm: h._serve_dashboard(pm),
    "/health":                  lambda h, pm: h._send_json({"status": "ok", "gpu_mode": pm.mgr.gpu_mode}),
    "/status":                  lambda h, pm: h._send_json(pm.mgr.status()),
    "/models":                  lambda h, pm: h._send_json(pm.mgr.list_models()),
    "/profiles":                lambda h, pm: (log.warning("/profiles is deprecated"), h._send_json(pm.mgr.list_models()))[1],
    "/local-models":            lambda h, pm: h._send_json({"discovered": [], "configured": list(pm.mgr._models.keys())}),
    "/v1/models":               lambda h, pm: h._handle_v1_models(pm),
    "/system":                  lambda h, pm: h._send_json(
        h._system_info(pm.telemetry.get_device_snapshot())),
    "/api/metrics":             lambda h, pm: h._handle_api_metrics(pm),
    "/api/latency":           lambda h, pm: h._handle_api_latency(pm),
    "/api/power":             lambda h, pm: h._handle_api_power(pm),
    "/api/device_metrics":    lambda h, pm: h._handle_api_device_metrics(pm),
    "/api/device_power":      lambda h, pm: h._handle_api_device_power(pm),
    "/api/request_log":         lambda h, pm: h._handle_request_log(pm),
    "/api/token-stats":         lambda h, pm: h._handle_token_stats(pm),
    "/api/token-curve":         lambda h, pm: h._handle_token_curve(pm),
    "/api/snapshot":            lambda h, pm: h._handle_snapshot(pm),
    "/api/agent-stats":         lambda h, pm: h._handle_agent_stats(pm),
    "/api/agents":              lambda h, pm: h._handle_agents(pm),
    "/api/anomalies":           lambda h, pm: h._handle_anomalies(pm),
    "/metrics":                 lambda h, pm: h._handle_metrics(pm),
    "/history":                 lambda h, pm: h._send_json(pm.mgr.state.get_history(limit=30)),
    "/vllm_metrics":            lambda h, pm: h._handle_vllm_metrics(pm),
    "/engine_metrics":         lambda h, pm: h._handle_engine_metrics(pm),
    # monitor.js 实际 fetch /api/engine_metrics（与其它 /api/* 路由一致）；
    # 旧 /engine_metrics 保留做向后兼容。
    "/api/engine_metrics":     lambda h, pm: h._handle_engine_metrics(pm),
    "/watchdog_status":         lambda h, pm: _handle_watchdog_status(h),
    "/admin/packs/download-status": _admin(lambda h, pm: h._send_json(_pack_downloads_snapshot())),
    # Capability packs Catalog/transactions — install accepts Catalog pack_id only
    "/admin/packs/catalog":    _admin(lambda h, pm: h._handle_pack_catalog(pm)),
    "/admin/packs/status":     _admin(lambda h, pm: h._handle_pack_status(pm)),
    "/admin/packs/policy":     _admin(lambda h, pm: h._handle_pack_policy_get(pm)),
    "/admin/packs/uninstall-preview": _admin(lambda h, pm: h._handle_pack_uninstall_preview(pm)),
    "/admin/security/status":  _admin(lambda h, pm: h._handle_security_status(pm)),
    "/admin/connect/status":    lambda h, pm: handle_connect_status(h, pm),
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/admin/cloud/presets":     _admin(lambda h, pm: h._handle_cloud_presets(pm)),
    "/admin/tune/preview":      _admin(lambda h, pm: h._handle_tune_preview(pm)),
    "/admin/tune/scenarios":    _admin(lambda h, pm: h._handle_tune_scenarios(pm)),
    # ─── Admin: Logical Model Pools ─────────────────────────────────
    "/admin/model-pools":       _admin(lambda h, pm: mp_admin.handle_get_model_pools(h, pm)),
    "/admin/model-pools/status": _admin(lambda h, pm: mp_admin.handle_get_model_pools_status(h, pm)),
    "/admin/model-pools/runtime": _admin(lambda h, pm: mp_admin.handle_get_model_pools_runtime(h, pm)),
    "/admin/model-pools/catalog":   _admin(lambda h, pm: mp_admin.handle_get_model_pools_catalog(h, pm)),
    "/admin/model-pools/latency":   _admin(lambda h, pm: mp_admin.handle_get_model_pools_latency(h, pm)),
    "/admin/model-pools/events":    _admin(lambda h, pm: mp_admin.handle_get_model_pools_events(h, pm)),
    "/api/openapi.json":        _serve_api_spec,
    "/admin/keys":              _admin(lambda h, pm: key_admin.handle(h, pm, "list")),
}

_POST_ROUTES = {
    "/admin/keys/login":        _admin(lambda h, pm: key_admin.handle(h, pm, "login")),
    "/admin/keys/logout":       _admin(lambda h, pm: key_admin.handle(h, pm, "logout")),
    "/admin/keys":              _admin(lambda h, pm: key_admin.handle(h, pm, "create")),
    "/admin/keys/revoke":       _admin(lambda h, pm: key_admin.handle(h, pm, "revoke")),
    "/admin/keys/policy":       _admin(lambda h, pm: key_admin.handle(h, pm, "policy")),
    "/v1/chat/completions":     lambda h, pm: h._handle_chat(pm),
    "/v1/completions":          lambda h, pm: h._handle_chat(pm),
    "/v1/messages":             lambda h, pm: h._handle_messages(pm),
    "/v1/responses":            lambda h, pm: h._handle_responses(pm),
    "/switch":                  _admin(lambda h, pm: h._handle_switch(pm)),
    "/stop":                    _admin(lambda h, pm: h._handle_stop(pm)),
    "/sleep":                   _admin(lambda h, pm: h._handle_sleep(pm)),
    "/wake":                    _admin(lambda h, pm: h._handle_wake(pm)),
    "/api/chat":                lambda h, pm: h._handle_chat(pm),
    "/api/generate":            lambda h, pm: h._handle_chat(pm),
    "/reset":                   _admin(lambda h, pm: h._handle_reset(pm)),
    "/reconcile":               _admin(lambda h, pm: h._handle_reconcile(pm)),
    "/deploy":                  _admin(lambda h, pm: h._handle_deploy(pm)),
    "/pull":                    _admin(lambda h, pm: h._handle_pull(pm)),
    "/reload-config":          _admin(lambda h, pm: h._handle_reload_config(pm)),
    "/admin/cache/toggle":     _admin(lambda h, pm: h._handle_cache_toggle(pm)),
    "/admin/auto-switch/toggle": _admin(lambda h, pm: h._handle_auto_switch_toggle(pm)),
    "/admin/models/set-autostart": _admin(lambda h, pm: h._handle_set_autostart(pm)),
    "/admin/gpu-clear":        _admin(lambda h, pm: h._handle_gpu_clear(pm)),
    "/api/agents":             _admin(lambda h, pm: h._handle_post_agents(pm)),
    "/api/agents/reclassify":  _admin(lambda h, pm: h._handle_reclassify(pm)),

    # ─── Admin: Cloud Provider Management (PR-D) ─────────────────
    "/admin/cloud/reload":      _admin(lambda h, pm: h._handle_cloud_reload(pm)),
    "/admin/cloud/discover":    _admin(lambda h, pm: h._handle_cloud_discover(pm)),
    "/admin/cloud/test":        _admin(lambda h, pm: h._handle_cloud_test(pm)),
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/admin/cloud/provider-models": _admin(lambda h, pm: h._handle_cloud_provider_models(pm)),
    "/admin/cloud/provider-test": _admin(lambda h, pm: h._handle_cloud_provider_test(pm)),
    "/admin/tune":              _admin(lambda h, pm: h._handle_tune(pm)),
    # ─── Admin: Logical Model Pools ─────────────────────────────────
    # 更新用 POST（同步 server 仅路由 GET/POST；body 携带 expected_revision）
    "/admin/model-pools":       _admin(lambda h, pm: mp_admin.handle_put_model_pools(h, pm)),
    "/admin/model-pools/validate": _admin(lambda h, pm: mp_admin.handle_validate_model_pools(h, pm)),
    "/admin/model-pools/dry-run": _admin(lambda h, pm: mp_admin.handle_post_model_pools_dry_run(h, pm)),
    # 单池 enabled 原子开关（服务端字段级修改，CAS；非客户端读-改-写）
    "/admin/model-pools/toggle": _admin(lambda h, pm: mp_admin.handle_post_model_pools_toggle(h, pm)),
    "/v1/embeddings":           lambda h, pm: h._handle_embeddings(pm),
    "/v1/rerank":               lambda h, pm: h._handle_rerank(pm),
    # ─── Admin: Capability Packs (F4) ─────────────────────────────
    "/admin/packs/install":    _admin(lambda h, pm: h._handle_pack_install(pm)),
    "/admin/packs/uninstall":  _admin(lambda h, pm: h._handle_pack_uninstall(pm)),
    "/admin/packs/custom":     _admin(lambda h, pm: h._handle_pack_custom(pm)),
    # Legacy /admin/packs/download kept as-is (user-imported model path).
    "/admin/packs/download":        _admin(lambda h, pm: h._handle_pack_download(pm)),
    "/admin/packs/policy":          _admin(lambda h, pm: h._handle_pack_policy_post(pm)),
    "/admin/security/enable":       _admin(lambda h, pm: h._handle_security_enable(pm)),
    "/admin/security/keys":         _admin(lambda h, pm: h._handle_security_create_key(pm)),
    "/admin/security/keys/revoke":  _admin(lambda h, pm: h._handle_security_revoke_key(pm)),
    # Connect（F10）：自带更严门禁（回环对端 + 精确 authority + 强制 token），
    # 不走 _admin 的无 token 兼容放行 — 见 connect_handlers._gate。
    "/admin/connect/apply":     lambda h, pm: handle_connect_apply(h, pm),
    "/admin/connect/rollback":  lambda h, pm: handle_connect_rollback(h, pm),
}

_DELETE_ROUTES = {
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/api/agents":             _admin(lambda h, pm: h._handle_delete_agent(pm)),
}

# ─── Legacy pack download state (user-imported model path) ────────

_PACK_DOWNLOADS: dict = {}
_PACK_DL_LOCK = threading.Lock()

# Zero system installs; uninstalls go through _handle_pack_uninstall's engine-unit
# refcount teardown.

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WHISPER_SRC = _REPO_ROOT / "engines/whisper/whisper.cpp"
_WHISPER_BIN = _WHISPER_SRC / "build/bin/whisper-server"
_WHISPER_CMAKE = (_REPO_ROOT / "engines/_tools/cmake-3.31.6-macos10.10-universal/"
                  "CMake.app/Contents/bin/cmake")


def _ensure_whisper_engine(st: dict) -> tuple[bool, str]:
    """Skip if the whisper-server binary exists; otherwise build in background
    (progress written to st.detail/log_tail)."""
    if _WHISPER_BIN.exists():
        return True, "cached"
    if not _WHISPER_SRC.exists():
        return False, f"whisper.cpp source missing: {_WHISPER_SRC}"
    import subprocess as _sp
    env = dict(os.environ)
    env["PATH"] = str(_WHISPER_CMAKE.parent) + ":" + env.get("PATH", "")
    for label, argv in (
            ("cmake 配置…", [str(_WHISPER_CMAKE), "-B", "build",
                             "-DWHISPER_BUILD_EXAMPLES=ON", "-DWHISPER_BUILD_TESTS=OFF",
                             "-DGGML_METAL=ON"]),
            ("编译中…", [str(_WHISPER_CMAKE), "--build", "build",
                         "--config", "Release", "-j", "8",
                         "--target", "whisper-server"])):
        st["detail"] = label
        proc = _sp.run(argv, cwd=str(_WHISPER_SRC), env=env,
                       capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            st["log_tail"] = tail
            return False, f"{label} exit={proc.returncode}: {tail[-300:]}"
    if not _WHISPER_BIN.exists():
        return False, "build finished but whisper-server not found"
    return True, "built"


def _install_whisper_pack(pm, name: str, model_file: Path) -> dict:
    """Write asr_server yaml (Metal whisper-server, OpenAI-compatible
    /v1/audio/transcriptions) + hot reload. Explicit absolute paths
    (isolation rule: never probe conda)."""
    yaml_path = Path(pm.mgr.models_dir) / f"{name}.yaml"
    if name in pm.mgr._models:
        return {"status": "already_configured", "message": name}
    content = f"""name: {name}
description: "whisper.cpp ASR (Metal, OpenAI-compatible /v1/audio/transcriptions)"
type: asr_server
gpu_role: none
model_type: asr
ledger_mode: on_demand
asr_server:
  conda_env: whisper            # python_bin declared explicitly → no conda probe (isolation)
  python_bin: {_WHISPER_BIN}
  port: 8881
  working_dir: {_WHISPER_SRC / "build/bin"}
  health_url: "http://127.0.0.1:8881/health"
  health_check_timeout: 180
  start_cmd: "{_WHISPER_BIN} --model {model_file} --host 127.0.0.1 --port 8881 --inference-path /v1/audio/transcriptions"
"""
    yaml_path.write_text(content, encoding="utf-8")
    pm.mgr.reload_models()
    return {"status": "installed", "message": f"{name} installed (not started)"}


def _pack_download_state(name: str) -> dict:
    with _PACK_DL_LOCK:
        return _PACK_DOWNLOADS.setdefault(name, {})

def _pack_downloads_snapshot() -> dict:
    with _PACK_DL_LOCK:
        return {k: dict(v) for k, v in _PACK_DOWNLOADS.items()}


def _collect_engine_refs(model) -> tuple[str | None, list]:
    """Collect engine units and file paths referenced by a model config
    (containerized uninstall decision).

    Returns: (engine_unit_name | None, [absolute paths]).
    engine_unit = first-level dir name under <repo>/engines/; absolute and
    relative paths both collected (relative resolved against the repo root
    first, cwd as fallback).
    """
    from dataclasses import fields, is_dataclass
    repo_root = Path(__file__).resolve().parents[3]
    engines_root = (repo_root / "engines").resolve()
    candidates: list[Path] = []

    def _walk(obj):
        if not is_dataclass(obj):
            return
        for f in fields(obj):
            try:
                v = getattr(obj, f.name)
            except Exception:
                continue
            if isinstance(v, (str, Path)) and str(v):
                sv = str(v)
                if sv.startswith("/"):
                    candidates.append(Path(sv))
                elif not sv.startswith("~") and "/" in sv or sv.endswith((".gguf", ".onnx", ".bin")):
                    # Relative path: resolve against repo root (cwd=repo root convention)
                    candidates.append(repo_root / sv)
            elif is_dataclass(v):
                _walk(v)

    _walk(model)
    engine_unit = None
    resolved: list[Path] = []
    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        resolved.append(rp)
        if engines_root in rp.parents:
            rel = rp.relative_to(engines_root)
            unit = rel.parts[0] if rel.parts else None
            if unit and engine_unit is None:
                engine_unit = unit
    return engine_unit, resolved

# Helper for watchdog route
def _handle_watchdog_status(handler):
    wd = getattr(handler.server, "watchdog", None)
    if wd:
        handler._send_json({"fail_counts": wd.fail_counts, "running": wd.running})
    else:
        handler._send_json({"error": "watchdog not initialized"}, 503)

# ═══════════════════════════════════════════════════════════════


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    # 修复:HTTP/1.0 下发送 Transfer-Encoding: chunked 会导致 HTTP 客户端
    # (reqwest/atomcode v5)解析失败 "unexpected transfer-encoding parsed"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("[proxy] " + fmt, *args)

    @property
    def proxy(self):
        return self.server.proxy_mgr

    def _safe_write(self, data: bytes):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ─── HTTP methods ─────────────────────────────────────────────

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path not in _GET_ROUTES:
                self._send_json({"error": "not found"}, 404)
                return
            _GET_ROUTES[path](self, pm)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("GET %s error: %s", self.path, e)

    def do_POST(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path not in _POST_ROUTES:
                self._send_json({"error": "not found"}, 404)
                return
            _POST_ROUTES[path](self, pm)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("POST %s error: %s", self.path, e)

    def do_DELETE(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path not in _DELETE_ROUTES:
                self._send_json({"error": "not found"}, 404)
                return
            _DELETE_ROUTES[path](self, pm)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("DELETE %s error: %s", self.path, e)

    # ─── Dashboard ────────────────────────────────────────────────

    def _serve_dashboard(self, pm):
        body = None
        try:
            from inferfabric.dashboard import get_html
            html = get_html()
            # Inject token stats. 用 pm.telemetry 上的 collector（带 db，DB 驱动、
            # 引擎无关、双 scope local/cloud），而非裸 TokenStatsCollector()（无 db，
            # 只能读本地 vllm/sglang 文件 → ninfer 数据缺失，按天数据卡住的根因）。
            # JS 端 filter by window。
            try:
                stats_json = json.dumps(pm.telemetry.token_collector._load_full_state())
                # 防御 </script> 注入：json.dumps 已转义 </script>，额外移除
                stats_json = stats_json.replace('</', '<\\/')
                html = html.replace(
                    '</head>',
                    '<script>window.__TOKEN_STATS__ = ' + stats_json + ';</script></head>'
                )
            except Exception as e:
                log.warning("Failed to inject token stats: %s", e)
            body = html.encode("utf-8")
        except ImportError:
            pass
        if body is None:
            body = (
                "<!DOCTYPE html><html><head><title>InferFabric</title>"
                "<style>body{font-family:sans-serif;background:#0f1117;color:#e2e8f0;padding:24px}"
                "h1{color:#3b82f6}</style></head><body>"
                "<h1>InferFabric</h1><p>Dashboard unavailable. Use <code>iff status</code></p>"
                "</body></html>"
            ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self._safe_write(body)

    # ─── Chat ─────────────────────────────────────────────────────

    def _handle_chat(self, pm):
        data = self._read_body()
        if data is None:
            return
        handle_chat(self, pm, data)

    # ─── OpenAI Responses API handler（Issue #1，codex CLI 兼容） ──

    def _handle_responses(self, pm):
        from inferfabric.proxy.responses_bridge import (
            handle_pool_responses, handle_responses_direct,
        )
        data = self._read_body()
        if data is None:
            return
        model = data.get("model", "")
        if handle_pool_responses(self, pm, data, model):
            return
        handle_responses_direct(self, pm, data, model)

    # ─── Anthropic Messages handler ───────────────────────────────

    def _handle_messages(self, pm):
        """Handle Anthropic Messages API requests with model-name routing.

        Routing priority (PR-2a):
          1. If profile_state == SWITCHING → 503 + Retry-After
          2. Parse `model` field → find_model_by_served_name()
             a. Model is active → route to its port
             b. Model is not active → auto-switch or fallback to first active LLM
          3. Cloud Discovery resolve_route() match → cloud provider
          4. No match → fallback to first active LLM, then 503
        """
        data = self._read_body()
        if data is None:
            return

        original_model = data.get("model", "")
        auth_header = self.headers.get("Authorization", "") or self.headers.get("x-api-key", "")

        # PR-B: Request context for logging
        req_id = pm.new_request_id()
        req_start = time.monotonic()
        key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
        # v6.5: 客户端 Agent 分类（每请求一次，缓存到 handler 供所有 RequestLog 站点取值）。
        # 生产必挂 pm.agent_registry；getattr 兜底仅为单测裸 fake（无 path/headers/registry）冷启动。
        _reg = getattr(pm, "agent_registry", None)
        if _reg is not None:
            self._agent_hit = _reg.classify(
                request_protocol(getattr(self, "path", "")),
                getattr(self, "headers", {}))
        # G-1b: 挂到 handler 上，供 _forward_local / forwarder 写 RequestLog
        self._req_id = req_id
        self._req_start = req_start
        self._key_name = key_name
        self._usage = {"prompt_tokens": 0, "prompt_tokens_cached": 0,
                       "completion_tokens": 0}
        req_model = data.get("model", "")
        req_status = 200
        req_error = None
        req_route = "local"
        req_cloud_provider = None

        # 逻辑模型池（iff/*）：完整名鉴权 + 池选择，先于直连鉴权/缓存/
        # SWITCHING guard（池调度不切换本地模型，guard 不适用；spec §13）
        # 函数内 import 避免 handler ↔ model_pool.dispatch 循环依赖
        if req_model.startswith("fabric-") and getattr(pm, "pool_registry", None) is not None:
            from inferfabric.model_pool.dispatch import handle_pool_messages
            if handle_pool_messages(self, pm, data, req_model):
                return

        # PR-A: Auth check
        if pm.auth.enabled:
            requested_model_for_auth = req_model
            if "/" in requested_model_for_auth:
                requested_model_for_auth = requested_model_for_auth.split("/")[-1]
            auth_ok, auth_reason = pm.auth.check(auth_header, requested_model_for_auth)
            if not auth_ok:
                req_status = 401
                req_error = auth_reason
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=req_model,
                    status=req_status, error=req_error,
                    agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                    ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                    duration_ms=(time.monotonic()-req_start)*1000,
                ))
                self._send_json({"error": auth_reason, "status": "unauthorized"}, 401)
                return

        # PK4 T1: pack uninstall in flight → 503 pack_uninstalling
        # （评审 PR#17 N1：移到 auth 之后——未鉴权客户端不得探测卸载状态）
        _pack_pm = getattr(pm, "pack_manager", None)
        if _pack_pm is not None and original_model and \
                _pack_pm.is_model_disabled(original_model):
            self._send_json(
                {"error": "pack_uninstalling",
                 "status": "pack_uninstalling"}, 503)
            return

        # R5: 响应缓存查找（stream=false + temperature=0 的请求）
        cache_enabled = getattr(pm, '_runtime_config', {}).get("cache", {}).get("enabled", True)
        if cache_enabled and not data.get("stream", False) and data.get("temperature", 0) in (0, None):
            response_cache = getattr(pm, 'response_cache', None)
            cached = response_cache.get(original_model, data) if response_cache is not None else None
            if cached is not None:
                # 缓存命中不记 RequestLog（方案 A）：回放的是历史响应，
                # 未发生推理 —— 落库会让 token 统计/费用重复计数，日志表
                # 出现「1ms 却带数万 tokens」的假完成行。LRU 生效由
                # ResponseCache.stats()（snapshot local_models.cache_stats）
                # 与 journal 的 cache HIT 行反映。
                log.info("/v1/messages → cache HIT for %s", original_model)
                self._send_json(cached["body"], 200)
                return

        log.info("/v1/messages body: max_tokens=%s, model=%s, messages_count=%d, tools_count=%d, body_size=%d",
                 data.get("max_tokens"), data.get("model"),
                 len(data.get("messages", [])),
                 len(data.get("tools", [])),
                 len(json.dumps(data)))

        # PR-6e/PR-2b: SWITCHING guard — only 503 if request is NOT for the switching target
        from inferfabric.state import ServiceState
        profile_state = pm.mgr.state.get("profile_state", "")
        requested_model = data.get("model", "")
        if profile_state == ServiceState.SWITCHING:
            switching_target = pm.mgr.state.get("switching_target") or ""
            target_model = pm.mgr.find_model_by_served_name(requested_model) if requested_model else None
            if target_model and target_model.name == switching_target:
                # Request is for the switching target → let it proceed (will route once active)
                log.info("/v1/messages → target %s is switching, proceeding", switching_target)
            else:
                # Not the switching target → 503
                log.info("/v1/messages → 503 (switching to %s, not %s)", switching_target, requested_model)
                elapsed = (time.monotonic() - req_start) * 1000
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=original_model,
                    status=503, error="model_switching",
                    agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                    ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                    duration_ms=elapsed,
                ))
                pm.anomalies.record(AnomalyEvent(
                    category="routing", severity="warning", model=original_model,
                    status_code=503,
                    message=f"Model switching to {switching_target}, request for {original_model} rejected",
                    possible_cause="本地模型正在切换中，请求的是另一个本地模型。等待切换完成或换一个模型。",
                ))
                self._send_json(
                    {"error": "Model is switching, please retry", "status": "switching", "retry_after": 30},
                    503,
                    extra_headers={"Retry-After": "30"},
                )
                return

        # PR-2a: Model-name routing
        # (target_model already resolved above if SWITCHING; re-resolve only if not)
        if profile_state != ServiceState.SWITCHING:
            target_model = pm.mgr.find_model_by_served_name(requested_model) if requested_model else None

        if target_model and target_model.port and target_model.name in pm.mgr.active_services:
            # Requested model is active → route directly
            log.info("/v1/messages → LOCAL %s (port %d) [matched by model=%s]",
                     target_model.name, target_model.port, requested_model)
            self._forward_local(pm, data, auth_header, target_model, original_model)
            return

        # PR-2b: Auto-switch on demand — if model is known but not active
        if target_model and target_model.port and target_model.name not in pm.mgr.active_services:
            if pm.auto_switch:
                log.info("/v1/messages → auto-switch to %s [model=%s not active]",
                         target_model.name, requested_model)
                switched = pm.ensure_service(target_model.name)
                if switched is None:
                    elapsed = (time.monotonic() - req_start) * 1000
                    pm.logger.log(RequestLog(
                        req_id=req_id, key_name=key_name, model=original_model,
                        status=409, error="switch_in_progress",
                        agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                        ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                        duration_ms=elapsed,
                    ))
                    pm.anomalies.record(AnomalyEvent(
                        category="routing", severity="info", model=target_model.name,
                        status_code=409,
                        message=f"Auto-switch to {target_model.name} conflict: switch already in progress",
                        possible_cause="另一请求正在发起同模型切换，线程锁被占用。立即重试通常可恢复。",
                    ))
                    self._send_json({"error": "switch already in progress", "status": "conflict"}, 409)
                    return
                if switched:
                    # Switch succeeded (ensure_service already verified healthy)
                    log.info("/v1/messages → LOCAL %s (port %d) [after auto-switch]",
                             target_model.name, target_model.port)
                    self._forward_local(pm, data, auth_header, target_model, original_model)
                    return
                else:
                    # Switch failed or model not healthy → 503
                    log.warning("/v1/messages → 503 auto-switch to %s failed", target_model.name)
                    elapsed = (time.monotonic() - req_start) * 1000
                    pm.logger.log(RequestLog(
                        req_id=req_id, key_name=key_name, model=original_model,
                        status=503, error="auto_switch_failed",
                        agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                        ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                        duration_ms=elapsed,
                    ))
                    pm.anomalies.record(AnomalyEvent(
                        category="model", severity="error", model=target_model.name,
                        status_code=503,
                        message=f"Auto-switch to {target_model.name} failed",
                        possible_cause="vLLM 启动失败 / OOM / 配置错误 / 模型文件损坏。检查 vLLM 日志。",
                    ))
                    self._send_json(
                        {"error": f"Auto-switch to {target_model.name} failed, retry later",
                         "status": "switch_failed", "retry_after": 10},
                        503,
                        extra_headers={"Retry-After": "10"},  # R0: match ensure_service cooldown
                    )
                    return
            else:
                log.info("/v1/messages → model %s known but not active, AUTO_SWITCH=off",
                         target_model.name)
                elapsed = (time.monotonic() - req_start) * 1000
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=original_model,
                    status=503, error="model_not_active",
                    agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                    ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                    duration_ms=elapsed,
                ))
                pm.anomalies.record(AnomalyEvent(
                    category="config", severity="warning", model=target_model.name,
                    status_code=503,
                    message=f"Model {target_model.name} not active and AUTO_SWITCH=off",
                    possible_cause="模型在配置中但未启动，且 auto_switch 被禁用。手动 /switch 或启用 AUTO_SWITCH。",
                ))
                self._send_json(
                    {"error": f"Model {target_model.name} not active, auto-switch disabled",
                     "status": "not_active", "retry_after": 10},
                    503,
                    extra_headers={"Retry-After": "10"},
                )
                return

        # Step 2 (PR-D): Unified cloud routing via CloudDiscovery
        if requested_model:
            pm.ensure_cloud_discovered()
            local_model_names = {m.served_name for m in pm.mgr._models.values() if m.served_name}
            route = pm.cloud.resolve_route(requested_model, local_model_names)
            if route and route.startswith("cloud:"):
                provider_name = route.split(":", 1)[1]
                provider_cfg = pm.cloud.get_provider_config(provider_name)
                cloud_model = pm.cloud.resolve_cloud_model(provider_name, requested_model)
                if provider_cfg and cloud_model:
                    log.info("/v1/messages → CLOUD %s [%s] [model=%s]",
                             provider_name, "anthropic", requested_model)
                    result = forwarder.forward_to_cloud(
                        self, data, provider_cfg, cloud_model,
                        protocol="anthropic", original_model=original_model,
                    )
                    pm.logger.log(RequestLog(
                        model=original_model or requested_model, status=result.status, route=f"cloud:{provider_name}",
                        key_name=key_name, req_id=req_id,
                        cloud_provider=provider_name,
                        agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                        ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                        tokens_in=result.usage.get("prompt_tokens", 0),
                        tokens_in_cached=result.usage.get("prompt_tokens_cached", 0),
                        tokens_out=result.usage.get("completion_tokens", 0),
                        ttft_ms=result.ttft_ms,
                        duration_ms=result.duration_ms,
                        error=result.error,
                    ))
                    return
                else:
                    log.warning("/v1/messages → cloud route matched but config missing: %s", route)

        # Step 3 (R8): Unknown model name — 404 + anomaly + RequestLog
        # (Former Step 6 fallback to first active LLM + Step 7 cloud fallback
        #  were removed — they silently routed unrecognized model names, in
        #  violation of the transparent-gateway architecture boundary.)
        elapsed = (time.monotonic() - req_start) * 1000
        log.warning("/v1/messages → rejecting unknown model: %s", requested_model)
        pm.logger.log(RequestLog(
            req_id=req_id, key_name=key_name, model=original_model,
            status=404, error="unknown_model",
            agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
            ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
            duration_ms=elapsed,
        ))
        pm.anomalies.record(AnomalyEvent(
            category="routing", severity="warning", model=requested_model,
            status_code=404,
            message=f"Unknown model '{requested_model}' — rejected: no match in local served_names or cloud_models",
            possible_cause="1. 客户端传了不存在的模型名；2. cloud_provider.yaml 缺少对应 model_id；"
                           "3. models.d/*.yaml 的 served_name 配置未覆盖此模型名",
        ))
        self._send_json({"error": f"Unknown model: {requested_model}"}, 404)

    def _forward_local(self, pm, data, auth_header, model_obj, original_model):
        """Forward request to a local model with rate limiting."""
        model_name = data.get("model", "")
        gate = pm.dual_gate.acquire(model_name, timeout=30)
        if not gate.ok:
            self._send_json(
                {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"},
                429,
            )
            return

        # R7: 多副本端口选择 — 用 dataclasses.replace 生成请求局部副本，
        # 不再改写共享 model_obj.port（并发请求交错覆盖会串端口，且 finally
        # 恢复救不了已在途的请求）。model_obj 是 ModelConfig dataclass。
        _selected_port = None
        _replicas = getattr(model_obj, 'replicas', None)
        if isinstance(_replicas, (list, tuple)) and _replicas:
            _selected_port = pm.get_target_port(model_name)
            if _selected_port:
                model_obj = dataclasses.replace(model_obj, port=_selected_port)

        # MACFABRIC P1: 无原生 /v1/messages 的引擎走翻译桥（Anthropic→OpenAI）。
        if not getattr(model_obj, "anthropic_native", False):
            from inferfabric.proxy.anthropic_bridge import forward_anthropic_via_openai
            status = forward_anthropic_via_openai(
                self, pm, data, auth_header, model_obj, original_model
            )
        else:
            status = forwarder.forward_anthropic_local(
                self, pm, data, auth_header, model_obj, original_model
            )
        try:
            if status is not None:
                usage = getattr(self, '_usage', {}) or {}
                pm.logger.log(RequestLog(
                    req_id=getattr(self, '_req_id', ''),
                    key_name=getattr(self, '_key_name', 'anonymous'),
                    model=original_model or model_name,
                    status=200 if status == 200 else 502,
                    ttft_ms=getattr(self, '_ttft_ms', None),
                    route="local",
                    agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                    ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_in_cached=int(usage.get("prompt_tokens_cached") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    duration_ms=(time.monotonic() - getattr(self, '_req_start', time.monotonic())) * 1000,
                ))
        finally:
            gate.release()
            if _selected_port:
                # 副本并发计数仍需释放；model_obj 现在是请求局部副本，
                # 无需恢复共享对象的 port（H2：不再改写共享状态）
                pm.release_port(model_obj.name, _selected_port)

    # ─── v1 Models ────────────────────────────────────────────────

    def _handle_v1_models(self, pm):
        """Forward /v1/models — merge local + cloud models."""
        active = list(pm.mgr.active_services)

        def _fetch_models(svc, port):
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/v1/models")
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 200:
                    data = json.loads(body)
                    return data.get("data", [])
                return []
            except Exception as e:
                log.warning("/v1/models fetch failed for %s (port %d): %s", svc, port, e)
                return []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # Local models from active vLLM services (skip ollama/embedding ports)
        all_models = []
        if active:
            model_ports = {}
            for svc in active:
                m = pm.mgr.get_model(svc)
                # Only query vLLM services (not llama_cpp embedding models)
                if m and m.port and m.type == "vllm":
                    model_ports[svc] = m.port
            if model_ports:
                with ThreadPoolExecutor(max_workers=len(model_ports)) as executor:
                    futures = {executor.submit(_fetch_models, svc, port): svc
                               for svc, port in model_ports.items()}
                    for fut in as_completed(futures):
                        all_models.extend(fut.result())
            # Add ollama/embedding models from config (they don't have /v1/models)
            for svc in active:
                m = pm.mgr.get_model(svc)
                if m and m.type != "vllm":
                    all_models.append({"id": m.served_name or m.name, "object": "model",
                                       "owned_by": "local", "type": m.type})
        else:
            # No active services — return configured model list
            for m in pm.mgr._models.values():
                if m.type != "ollama_daemon":
                    all_models.append({"id": m.served_name or m.name, "object": "model",
                                       "owned_by": "local", "type": m.type})

        # PR-D: Merge cloud models (with capabilities from v4.6.0)
        pm.ensure_cloud_discovered()
        seen_cloud_ids = set()
        for model_id, cm in pm.cloud.cloud_models.items():
            # Skip provider-prefixed keys (e.g., "baidu-codingplan/deepseek-v4-flash")
            # to avoid duplicates — the short name key is already added.
            if "/" in model_id:
                continue
            # Also skip if already in local model list
            existing_ids = {m.get("id") for m in all_models}
            if model_id not in existing_ids and model_id not in seen_cloud_ids:
                seen_cloud_ids.add(model_id)
                all_models.append(cm.to_api_dict())

        # 逻辑模型池：启用且有可解析候选的池以逻辑名列出（Task 10）。
        # 不合并候选能力（无 capability union）— 池是路由目标，非能力聚合。
        pool_reg = getattr(pm, "pool_registry", None)
        if pool_reg is not None:
            snap = pool_reg.snapshot()
            existing_ids = {m.get("id") for m in all_models}
            for name, ps in snap.pools.items():
                logical = f"fabric-{name}"
                if (ps.pool.enabled and ps.candidates
                        and logical not in existing_ids):
                    all_models.append({"id": logical, "object": "model",
                                       "owned_by": "iff-pool"})

        if all_models:
            self._send_json({"object": "list", "data": all_models})
        else:
            self._send_json({"error": "no upstream available"}, 503)

    # ─── vLLM Metrics ────────────────────────────────────────────

    def _handle_api_metrics(self, pm):
        """返回聚合指标 (G-2 MetricsAggregator)"""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            window = qs.get("window", ["24h"])[0]
            if window not in ("1h", "24h", "7d", "all"):
                window = "24h"
            data = pm.metrics.get_metrics(window, axis_models=_metrics_axis(pm))
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/metrics failed: %s", e)
            self._send_json({"error": "metrics unavailable"}, 500)

    def _handle_api_latency(self, pm):
        """GET /api/latency?window=minute|hour|day — 模型延迟趋势（时间分桶 × 逐模型分位）。"""
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query)
            window = qs.get("window", ["hour"])[0]
            if window not in ("minute", "hour", "day"):
                window = "hour"
            source_of = {name: src for name, src in _metrics_axis(pm)}
            data = _lat_series_cache[window].get_or_refresh(
                lambda: pm.metrics.get_latency_series(
                    window, bucket_ms=_LAT_BUCKET_MS[window], top_n=5,
                    percentiles=(0.50, 0.95), source_of=source_of))
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/latency failed: %s", e)
            self._send_json({"error": "latency series unavailable"}, 500)

    def _handle_api_power(self, pm):
        """GET /api/power?gran=hour|day|week|month — 功耗/电费分桶序列（v6.2）。

        每桶 {t, avg_w, kwh, cum_kwh, cum_yuan}；口径：GPU 板卡功耗、¥1/度。
        5min TTL 单飞缓存（采样 60s + 前端 5min 刷新，窗外无需秒级重算）。
        """
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query)
            gran = qs.get("gran", ["hour"])[0]
            if gran not in _POWER_CACHE_TTL_GRANS:
                gran = "hour"
            data = _power_series_cache[gran].get_or_refresh(
                lambda: pm.telemetry.get_power_series(gran))
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/power failed: %s", e)
            self._send_json({"error": "power series unavailable"}, 500)

    def _handle_api_device_power(self, pm):
        """GET /api/device_power?gran=hour|day|week|month — 当前聚合流序列。

        DeviceTelemetry T4：与 /api/power 同 shape，但只读 v011
        device_power_samples 的 aggregate 流（legacy 流留在 /api/power）。
        5min TTL 单飞缓存与 /api/power 相同。
        """
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query)
            gran = qs.get("gran", ["hour"])[0]
            if gran not in _POWER_CACHE_TTL_GRANS:
                gran = "hour"
            data = _device_power_series_cache[gran].get_or_refresh(
                lambda: pm.telemetry.get_device_power_series(gran))
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/device_power failed: %s", e)
            self._send_json({"error": "device power series unavailable"}, 500)

    def _handle_vllm_metrics(self, pm):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            port = int(qs.get("port", ["8000"])[0])
        except (ValueError, IndexError):
            self._send_json({"error": "invalid port"}, 400)
            return
        try:
            result, status = handle_vllm_metrics(f"port={port}")
        except RuntimeError as e:
            self._send_json({"error": str(e)}, 502)
            return
        self._send_json(result, status)

    def _handle_engine_metrics(self, pm):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("model") or [None])[0]
        if not name or name not in pm.mgr._models:
            self._send_json({"error": "unknown model"}, 404)
            return
        model = pm.mgr._models[name]
        try:
            adapter = get_adapter(model.type)
            result = adapter.fetch_engine_metrics(model)
            if result is None:
                self._send_json({"sleep_state": 0}, 200)
                return
            self._send_json(result, 200)
        except Exception as e:
            log.error("engine_metrics failed for %s: %s", name, e)
            self._send_json({"error": str(e)}, 502)

    def _tune_model(self, pm, name: str):
        """tune 处理器共用的模型查找（D1：磁盘为启动参数真相源）。

        长驻代理的内存注册表可能落后于别处（CLI/另一标签页）的 tune apply，
        所以每次 tune 请求前重读「YAML + 应用层」并同步内存注册表；
        重读失败（或测试桩无 models_dir）时回退内存注册表。
        """
        models_dir = getattr(pm.mgr, "models_dir", None)
        if models_dir:
            from inferfabric.config import load_models
            try:
                pm.mgr._models = load_models(models_dir)
            except Exception as e:
                log.warning("tune: 重读模型注册表失败（沿用内存）: %s", e)
        return pm.mgr._models.get(name)

    def _handle_tune_preview(self, pm):
        """GET /admin/tune/preview?model=X&preset=Y — 场景 diff 预览（不写盘）。"""
        from urllib.parse import urlparse, parse_qs
        from inferfabric import tune
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("model") or [None])[0]
        preset = (qs.get("preset") or [None])[0]
        if not name or not preset:
            self._send_json({"error": "Missing model/preset"}, 400)
            return
        model = self._tune_model(pm, name)
        if model is None:
            self._send_json({"error": f"unknown model: {name}"}, 404)
            return
        try:
            self._send_json(tune.preview(model, preset), 200)
        except tune.TuneError as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_tune_scenarios(self, pm):
        """GET /admin/tune/scenarios?model=X — 场景选择集 + 当前生效场景。

        D5：active 直读应用层文件（≡ 容器实际值），不读代理内存；
        choices = ["default"] + 已定义场景（default = 模型 YAML 当前值，永远存在）。
        """
        from urllib.parse import urlparse, parse_qs
        from inferfabric import tune
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get("model") or [None])[0]
        if not name:
            self._send_json({"error": "Missing model"}, 400)
            return
        model = self._tune_model(pm, name)
        if model is None:
            self._send_json({"error": f"unknown model: {name}"}, 404)
            return
        self._send_json({
            "model": name,
            "choices": tune.scenario_choices(model),
            "active": tune.current_active(name),
        }, 200)

    def _handle_tune(self, pm):
        """POST /admin/tune {model, preset, restart?} — 应用场景并重启（默认）。"""
        from inferfabric import tune
        data = self._read_body()
        if data is None:
            return
        name = data.get("model")
        preset = data.get("preset")
        if not name or not preset:
            self._send_json({"error": "Missing model/preset"}, 400)
            return
        model = self._tune_model(pm, name)
        if model is None:
            self._send_json({"error": f"unknown model: {name}"}, 404)
            return
        restart = data.get("restart", True)
        try:
            r = tune.apply(model, preset, dry=False, restart=restart, mgr=pm.mgr)
            ok = r["status"].startswith(("applied", "rolled")) or \
                r["status"] in ("already_default", "restarted")
            self._send_json(r, 200 if ok else 500)
        except tune.TuneError as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_request_log(self, pm):
        """返回最近请求日志 (D-1)"""
        try:
            from urllib.parse import urlparse, parse_qs
            import time
            qs = parse_qs(urlparse(self.path).query)
            limit = min(int(qs.get("limit", ["50"])[0]), 500)
            since = float(qs.get("since", ["0"])[0])
            rows = pm.telemetry.query_request_log(since=since, limit=limit)
            logs = []
            for r in rows:
                logs.append({
                    "timestamp": r["timestamp"],
                    "model": r["model"],
                    "status": r["status"],
                    "tokens_in": r["tokens_in"],
                    "tokens_in_cached": int(r.get("tokens_in_cached") or 0),
                    "tokens_out": r["tokens_out"],
                    "ttft_ms": round(r["ttft_ms"], 1) if r["ttft_ms"] else None,
                    "duration_ms": round(r["duration_ms"], 1) if r["duration_ms"] else None,
                    "route": r["route"],
                    "key_name": r.get("key_name", ""),
                    "error": r.get("error", ""),
                    "agent": r.get("agent", ""),
                    "ua": r.get("ua", ""),
                })
            self._send_json({"logs": logs, "count": len(logs)}, 200)
        except Exception as e:
            log.error("/api/request_log failed: %s", e)
            self._send_json({"error": "request log unavailable"}, 500)

    def _handle_anomalies(self, pm):
        """GET /api/anomalies — 返回异常事件 (R9)。"""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            since = float(qs.get("since", ["0"])[0])
            limit = min(int(qs.get("limit", ["100"])[0]), 500)
            category = qs.get("category", [None])[0]
            severity = qs.get("severity", [None])[0]
            events = pm.anomalies.query(since=since, limit=limit,
                                         category=category, severity=severity)
            self._send_json({
                "events": [{
                    "id": e.id,
                    "ts": e.ts,
                    "category": e.category,
                    "severity": e.severity,
                    "model": e.model,
                    "message": e.message,
                    "status_code": e.status_code,
                    "possible_cause": e.possible_cause,
                    "detail": e.detail,
                } for e in events],
                "count": len(events),
            })
        except Exception as e:
            log.error("/api/anomalies failed: %s", e)
            self._send_json({"error": "anomalies unavailable"}, 500)

    def _handle_metrics(self, pm):
        """GET /metrics — Prometheus 文本格式 (R6)。"""
        try:
            from inferfabric.proxy.metrics_exporter import generate_metrics
            text = generate_metrics(pm.telemetry, pm.anomalies)
            self._send_text(text, 200, content_type="text/plain; version=0.0.4; charset=utf-8")
        except Exception as e:
            log.error("/metrics failed: %s", e)
            self._send_text("metrics error", 500)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"):
        """发送纯文本响应。"""
        body = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_token_stats(self, pm):
        """返回 token 用量统计 (v5.x: API endpoint for live polling)"""
        try:
            stats = pm.telemetry.token_collector._load_full_state()
            self._send_json(stats, 200)
        except Exception as e:
            log.error("/api/token-stats failed: %s", e)
            self._send_json({"error": "token stats unavailable"}, 500)

    def _handle_token_curve(self, pm):
        """GET /api/token-curve — 模型 Token 使用曲线 (v5.7)。

        统一单位语义（分钟/小时/天/周，月整体弃用；标签 = 分桶单位）：
          minute : 近 60min  → 12×5min  桶      （原 hour；桶宽 1min→5min）
          hour   : 近 24h    → 24×1h    桶      （原 day）
          day    : 近 30 天  → 30×1d    桶      （原 month；31→30）
          week   : 近 90 天  → 13×7d    周桶    （新增）
        墙钟对齐固定窗分桶（bucketing.wallclock_slots）：桶边界钉死整 5 分钟 /
        整点 / 本地零点 / 本地周一，不随 now 滚动——历史桶闭合后值固定，仅含 now
        的末桶随新请求增长（与功耗卡同口径；取代旧「相对年龄」划窗式）。
        Y 轴 = tokens_in + tokens_out 总和；dual-scope {local, cloud}。
        每桶 cost 字段（¥，4 位小数）：云端请求按价格表（¥/1M tokens，
        MetricsAggregator.price_config）逐请求累加，未配价模型计 0；本地桶恒 0。
        数据源 = 监控 TAB Token 双卡的「云端窗口累计费用」折线。
        """
        from urllib.parse import urlparse, parse_qs
        from inferfabric.bucketing import wallclock_slots, bucket_index, WALL

        try:
            qs = parse_qs(urlparse(self.path).query or "")
            g = (qs.get("granularity", ["hour"])[0]).lower()
            if g not in WALL:
                self._send_json({"error": f"invalid granularity: {g}"}, 400)
                return
            spec = WALL[g]
            now_ts = time.time()
            slots = wallclock_slots(g, now_ts)
            n = spec["n"]
            # SQL 窗口剪枝：只取窗口内的行（since = 末桶起点）。桶边界由 slots 决定，
            # 非此 since——since 仅收窄行集，不决定归属。
            since = int(slots[0][0])
            rows = pm.telemetry.query_request_log(since=since, limit=100000)

            # 价格表（¥/1M tokens）：云端桶 cost 字段数据源（启动时由
            # ProxyManager._load_price_config 注入 MetricsAggregator，只读视图
            # 见其 price_config property）。本地桶无价格，cost 恒 0。
            prices = pm.metrics.price_config

            # 每桶含 prompt/completion 拆分（供 Prompt/Completion 堆叠条图表用）；
            # tokens 保留（= prompt+completion，向后兼容已有消费者）；
            # cached = 缓存命中 token 数（供缓存命中率 = cached/prompt）；
            # t = 桶起点 epoch 秒（前端格式化 x 轴标签，取代裸下标 x）；
            # cost = 桶费用（¥）——云端请求按 (in/1M×价入 + out/1M×价出) 逐请求累加，
            # 云端卡「窗口累计费用」折线数据源；本地无价格恒 0（本地只花电费，
            # 电费在 功耗/电费 卡）
            def _empty():
                return [{"x": i, "t": int(slots[i][0]), "tokens": 0, "prompt": 0,
                         "completion": 0, "cached": 0, "requests": 0, "cost": 0.0}
                        for i in range(n)]
            local_b = _empty()
            cloud_b = _empty()

            for r in rows:
                ts = r.get("timestamp")
                if ts is None:
                    continue
                # 墙钟分桶：左闭右开 start<=ts<end。窗外（早于首桶起点）→ -1 丢弃。
                idx = bucket_index(slots, ts)
                if idx < 0:
                    continue
                try:
                    tokens_in = int(r.get("tokens_in") or 0)
                    tokens_out = int(r.get("tokens_out") or 0)
                    cached = int(r.get("tokens_in_cached") or 0)
                except (ValueError, TypeError):
                    continue
                tokens = tokens_in + tokens_out
                target = cloud_b if r.get("cloud_provider") else local_b
                target[idx]["tokens"] += tokens
                target[idx]["prompt"] += tokens_in
                target[idx]["completion"] += tokens_out
                target[idx]["cached"] += cached
                target[idx]["requests"] += 1
                # 云端计费：与 agent-stats 共用 cost_of_row 唯一计价入口（口径防漂移）
                if r.get("cloud_provider"):
                    target[idx]["cost"] += cost_of_row(prices, r)

            # cost 输出保留 4 位小数（与 metrics cost_yuan 同口径），避免浮点长尾
            for b in local_b + cloud_b:
                b["cost"] = round(b["cost"], 4)

            self._send_json({
                "granularity": g,
                "local": local_b,
                "cloud": cloud_b,
            }, 200)
        except Exception as e:
            log.error("/api/token-curve failed: %s", e)
            self._send_json({"error": "token curve unavailable"}, 500)

    def _handle_agent_stats(self, pm):
        """GET /api/agent-stats?granularity=minute|hour|day|week&scope=all|local|cloud

        客户端 Agent 用量分桶（v6.5）。数据源 = request_log（SQLite），
        费用经 cost_of_row 与 /api/token-curve 同口径。"""
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query or "")
            g = (qs.get("granularity", ["hour"])[0]).lower()
            if g not in AGENT_GRAN:
                self._send_json({"error": f"invalid granularity: {g}"}, 400)
                return
            scope = (qs.get("scope", ["all"])[0]).lower()
            if scope not in ("all", "local", "cloud"):
                self._send_json({"error": f"invalid scope: {scope}"}, 400)
                return
            spec = AGENT_GRAN[g]
            since = int(time.time() - spec["since"])
            rows = pm.telemetry.query_request_log(since=since, limit=100000)
            meta = {d.id: {"name": d.name, "color": d.color, "source": d.source}
                    for d in pm.agent_registry.all()}
            res = aggregate_agent_stats(rows, g, scope, pm.metrics.price_config, meta)
            res["granularity"] = g
            self._send_json(res, 200)
        except Exception as e:
            log.error("/api/agent-stats failed: %s", e)
            self._send_json({"error": "agent stats unavailable"}, 500)

    def _handle_agents(self, pm):
        """GET /api/agents — 已知 Agent 清单（dashboard 图例/管理）。"""
        self._send_json({"agents": [{
            "id": d.id, "name": d.name, "color": d.color, "source": d.source,
            "rules": [{"header": r.header, "value": r.value, "regex": r.regex,
                       "protocol": r.protocol} for r in d.rules],
        } for d in pm.agent_registry.all()]}, 200)

    def _handle_post_agents(self, pm):
        """POST /api/agents — 一键认领（admin-token 保护）。

        两种语义（body 有 parent_id 分流）：
          - 新建独立 Agent: {id, name, header, pattern, color}
          - 归入已有 Agent: {parent_id, header, pattern}  （子进程工具归属）
        """
        try:
            data = self._read_body()
            if not isinstance(data, dict):
                self._send_json({"error": "body required"}, 400)
                return
            if data.get("parent_id"):
                d = pm.agent_registry.add_alias(
                    str(data.get("parent_id")),
                    str(data.get("header") or "user-agent"),
                    str(data.get("pattern") or ""),
                )
            else:
                d = pm.agent_registry.add_from_ui(
                    str(data.get("id") or ""), str(data.get("name") or ""),
                    str(data.get("header") or "user-agent"),
                    str(data.get("pattern") or ""),
                    str(data.get("color") or "#94a3b8"),
                )
            # 认领后自动 reclassify：映射层变了，用新规则重新分类无 def 的历史行
            # （unknown + observed 残留；事实层 ua/x_app 不变）。
            # 必须在响应前完成——前端拿到 200 即刷新 agent-stats，若 reclassify
            # 在响应后异步进行，首次刷新会读到重分类前的旧状态，且被 5min TTL
            # 锁住，「识别」看起来不生效（2026-10-04 线上排查根因）。
            reclassified = 0
            try:
                known = {x.id for x in pm.agent_registry.all()}
                reclassified = pm.telemetry.reclassify_request_log(
                    pm.agent_registry.classify, known)
            except Exception as e:
                log.warning("post-claim reclassify failed (non-fatal): %s", e)
            self._send_json({"agent": {
                "id": d.id, "name": d.name, "color": d.color, "source": d.source,
            }, "reclassified": reclassified}, 200)
        except KeyError:
            self._send_json({"error": "agent id already exists"}, 409)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            log.error("/api/agents POST failed: %s", e)
            self._send_json({"error": "claim failed"}, 500)

    def _handle_delete_agent(self, pm):
        """DELETE /api/agents?id=<id> — 管理模式删除（仅用户目录文件）。"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        aid = (qs.get("id") or [""])[0]
        try:
            pm.agent_registry.remove(aid)
            self._send_json({"ok": True}, 200)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            log.error("/api/agents DELETE failed: %s", e)
            self._send_json({"error": "delete failed"}, 500)

    def _handle_reclassify(self, pm):
        """POST /api/agents/reclassify — 认领后重新分类无 def 的历史行。

        事实层（ua/x_app）不变，用当前 registry 映射层重新 classify。
        处理 unknown 聚合桶 + observed 残留（agent id 无对应 def）。
        返回更新行数。认领后自动调用——unassigned 立即清空。
        """
        try:
            reg = pm.agent_registry
            known = {d.id for d in reg.all()}
            n = pm.telemetry.reclassify_request_log(reg.classify, known)
            self._send_json({"reclassified": n, "ok": True}, 200)
        except Exception as e:
            log.error("/api/agents/reclassify failed: %s", e)
            self._send_json({"error": "reclassify failed"}, 500)

    def _handle_snapshot(self, pm):
        """GET /api/snapshot — single consistent control-plane snapshot.

        Consolidates /status + /system + /models + /history + token stats +
        recent request logs + 24h metrics into ONE response so the dashboard
        polls a single endpoint (eliminates cross-panel state gaps).

        Change detection: response carries `etag` (content hash over ALL payload
        field groups, excluding volatile meta) and `rev` (first 8 chars). The
        dashboard sends the previous etag as `If-None-Match` and gets a cheap
        `304 Not Modified` when unchanged.

        C1: etag covers every field group (not just status+models), so any change
        (request_log / metrics / GPU temp) invalidates it and the dashboard
        refetches instead of freezing on a stale value.
        C2: the expensive collectors (mgr.status health probes + metrics 24h scan)
        are served from a per-process TTL single-flight cache (_ExpensiveCache);
        a matching If-None-Match with a fresh cache serves 304 without re-running
        them, and a slow health-probe burst cannot pile up across the 32-thread
        pool that chat forwarding shares.
        """
        now = time.time()

        # C2: 昂贵采集（status 健康探测 + metrics 24h 扫描）走 TTL 单飞缓存，
        # 窗口内复用 → 昂贵工作最多每 SNAPSHOT_EXP_TTL 秒跑一次。
        exp_cache = getattr(pm, "_snap_exp_cache", None)
        if exp_cache is None:
            exp_cache = _ExpensiveCache(ttl=float(getattr(pm, "_snap_exp_ttl", SNAPSHOT_EXP_TTL)))
            pm._snap_exp_cache = exp_cache
        exp = exp_cache.get_or_refresh(lambda: _compute_expensive(pm))
        # copy: the cached dict must not be mutated by the refill below
        status = dict(exp.get("status") or {})
        metrics_24h = exp.get("metrics_24h") or {}

        # DeviceTelemetry T5: ONE cached reading (15s TTL, one probe per
        # expiry) feeds device + system + status.gpu_* — one timestamp
        # everywhere. Observation cache only; admission/control paths use
        # the raw provider.
        from inferfabric.platform.telemetry import (
            capabilities_to_dict, legacy_gpu_fields, snapshot_to_dict,
        )
        dev_tel = pm.telemetry.device_telemetry
        dev_snap = pm.telemetry.get_device_snapshot()
        device_env = {"snapshot": snapshot_to_dict(dev_snap),
                      "capabilities": capabilities_to_dict(dev_tel.capabilities())}
        legacy = legacy_gpu_fields(dev_snap)
        status.update({"gpu_used_mb": legacy["gpu_used_mb"],
                       "gpu_total_mb": legacy["gpu_total_mb"],
                       "gpu_util_pct": legacy["gpu_util_pct"]})

        # 便宜字段每次轮询都重取（保证 request_log / GPU 温度等实时性，C1）
        # 传原始 caps 对象 —— _system_info 内部做 dict 转换，传已转换的
        # dict 会二次转换崩溃（'dict' has no .memory）。
        system = self._system_info(dev_snap, dev_tel.capabilities())
        models = pm.mgr.list_models()
        try:
            history = pm.mgr.state.get_history(30)
        except Exception:
            history = []
        try:
            token_stats = pm.telemetry.token_collector._load_full_state()
        except Exception:
            token_stats = {}
        try:
            rows = pm.telemetry.query_request_log(since=int(now - 3600), limit=50)
            request_log = [
                {"timestamp": r["timestamp"], "model": r["model"], "status": r["status"],
                 "tokens_in": r["tokens_in"],
                 "tokens_in_cached": int(r.get("tokens_in_cached") or 0),
                 "tokens_out": r["tokens_out"],
                 "ttft_ms": round(r["ttft_ms"], 1) if r["ttft_ms"] else None,
                 "duration_ms": round(r["duration_ms"], 1) if r["duration_ms"] else None,
                 "route": r["route"], "key_name": r.get("key_name", ""), "error": r.get("error", ""),
                 "agent": r.get("agent", ""), "ua": r.get("ua", "")}
                for r in rows
            ]
        except Exception:
            request_log = []

        # R11b: 速率限制只读指示器 — 数据源 = DualGateLimiter.describe()
        # （pm 无 dual_gate 时 → None，前端回退静态标签「配置见 iff.yaml」）
        _dg = getattr(pm, 'dual_gate', None)
        # C1: etag 覆盖全部 payload 字段组（内容哈希，排除 ts/rev/etag）
        content = {
            "status": status, "system": system, "models": models,
            "device": device_env,
            "history": history, "token_stats": token_stats or {},
            "request_log": request_log, "metrics_24h": metrics_24h or {},
            "local_models": {"discovered": [], "configured": list(pm.mgr._models.keys()),
             "cache_enabled": getattr(pm, 'response_cache', None) is not None,
             "cache_stats": (getattr(pm, 'response_cache', None).stats()
                             if getattr(pm, 'response_cache', None) is not None else None),
             "auto_switch": {"enabled": bool(getattr(pm, 'auto_switch', True)),
                             "source": getattr(pm, '_auto_switch_source', 'default')},
             "rate_limit": (_dg.describe() if _dg is not None else None)},
        }
        etag_raw = _snapshot_etag(content)
        etag = f'"{etag_raw}"'

        payload = {"ts": now, "rev": etag_raw[:8], "etag": etag, **content}

        inm = self.headers.get("If-None-Match", "")
        if inm:
            parts = [p.strip() for p in inm.split(",")]
            if etag in parts or etag_raw in parts:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._safe_write(body)

    # ─── System Info ─────────────────────────────────────────────

    def _system_info(self, snapshot=None, capabilities=None):
        """System info; device values come from the shared telemetry snapshot.

        DeviceTelemetry T5: no GPU subprocess here — the snapshot is taken
        once by the caller (TelemetryHub 15s cache) and projected. Device
        fields are None when unavailable (never 0).
        """
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0,
                "gpu_util_pct": None, "gpu_clock_mhz": None,
                "gpu_power_w": None, "gpu_temp_c": None}
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            total_kb = int([l for l in mem.splitlines() if l.startswith("MemTotal")][0].split()[1])
            avail_kb = int([l for l in mem.splitlines() if l.startswith("MemAvailable")][0].split()[1])
            info["ram_total_gb"] = round(total_kb / 1024**2, 1)
            info["ram_used_gb"] = round((total_kb - avail_kb) / 1024**2, 1)
        except Exception:
            pass
        try:
            with open("/proc/loadavg") as f:
                loadavg = f.read().split()[0]
            info["cpu_percent"] = round(float(loadavg) / info["cpu_cores"] * 100, 1)
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                info["uptime_seconds"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        if snapshot is not None:
            from inferfabric.platform.telemetry import (
                capabilities_to_dict, snapshot_to_dict,
            )
            # system.gpu_* = device utilization semantics (spec §14.2);
            # legacy VRAM occupancy lives in status.gpu_util_pct.
            info["gpu_util_pct"] = snapshot.utilization_pct
            info["gpu_clock_mhz"] = snapshot.clock_mhz
            info["gpu_power_w"] = snapshot.power_w
            info["gpu_temp_c"] = snapshot.temperature_c
            info["device"] = {"snapshot": snapshot_to_dict(snapshot)}
            if capabilities is not None:
                info["device"]["capabilities"] = capabilities_to_dict(capabilities)
        info["version"] = __version__
        return info

    def _handle_api_device_metrics(self, pm):
        """GET /api/device_metrics — platform-neutral device reading.

        {snapshot, capabilities}; snapshot carries timestamp + sources.
        JSON null = unavailable (never 0). Served from the hub's 15s
        observation cache — the same envelope /api/snapshot.device uses.
        """
        try:
            dev_tel = pm.telemetry.device_telemetry
            snap = pm.telemetry.get_device_snapshot()
            from inferfabric.platform.telemetry import (
                capabilities_to_dict, snapshot_to_dict,
            )
            self._send_json({
                "snapshot": snapshot_to_dict(snap),
                "capabilities": capabilities_to_dict(dev_tel.capabilities()),
            }, 200)
        except Exception as e:
            log.error("/api/device_metrics failed: %s", e)
            self._send_json({"error": "device metrics unavailable"}, 500)

    # ─── Control helpers ─────────────────────────────────────────

    def _read_body(self):
        return forwarder.read_body(self)

    def _send_json(self, data, status=200, extra_headers=None):
        forwarder.send_json(self, data, status, extra_headers=extra_headers)

    # ─── Admin Auth ─────────────────────────────────────────────

    def _check_admin(self) -> bool:
        """Check admin token for control-plane routes (F7 unified gate).
        Returns True if allowed, False if denied (401/403 sent)."""
        self._admin_token = _ADMIN_TOKEN
        # Bracket IPv6 binds (review PR#15 N1): Host for "::1" is "[::1]:8999".
        expected = (f"[{PROXY_HOST}]:{PROXY_PORT}" if ":" in PROXY_HOST
                    else f"{PROXY_HOST}:{PROXY_PORT}")
        return _admin_gate(self, expected,
                           write=self.command not in ("GET", "HEAD", "OPTIONS"))

    def _handle_pack_catalog(self, pm):
        self._send_json(pack_handlers.get_pack_manager().catalog_payload())

    def _handle_pack_status(self, pm):
        self._send_json(pack_handlers.get_pack_manager().status_payload())

    def _handle_pack_policy_get(self, pm):
        code, payload = pack_handlers.get_pack_manager().policy_get()
        self._send_json(payload, code)

    def _handle_pack_policy_post(self, pm):
        data = self._read_body()
        code, payload = pack_handlers.get_pack_manager().policy_put(data or {})
        self._send_json(payload, code)

    # ─── Capability pack uninstall preview (F2) ──────────────────────
    def _handle_pack_uninstall_preview(self, pm):
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        pack_id = (qs.get("pack_id") or [""])[0]
        code, payload = pack_handlers.get_pack_manager().preview_uninstall(pack_id)
        self._send_json(payload, code)

    def _handle_pack_custom(self, pm):
        """POST /admin/packs/custom — user-extended capability pack
        (unified-architecture ruling 2026-11-16).

        body: {name, url, capability, sha256?}; validation chain runs in a
        background thread; progress/result surface through the existing
        /admin/packs/status state machine.
        """
        data = self._read_body()
        code, payload = pack_handlers.get_pack_manager().start_custom_install(
            data)
        self._send_json(payload, code)

    def _handle_pack_uninstall(self, pm):
        """Uninstall entry split: body with ``pack_id`` → capability pack
        state machine; otherwise legacy flow (user model / discovery artifact
        cleanup, kept as-is)."""
        data = self._read_body()
        if data and data.get("pack_id"):
            code, payload = pack_handlers.get_pack_manager().uninstall(
                str(data["pack_id"]), bool(data.get("confirmed")))
            self._send_json(payload, code)
            return
        self._handle_pack_uninstall_legacy(pm)

    def _handle_pack_uninstall_legacy(self, pm):
        """Legacy uninstall — end-to-end "containerized" cleanup:

        stop service → delete models.d/<name>.yaml → hot reload
        → model file cleanup (project models/ only; ~/models user data untouched)
        → engine unit refcount hitting zero removes the whole directory
        (isolated env torn down entirely, zero system residue)
        → pool linkage: purge local candidates referencing the model.
        """
        data = self._read_body()
        if data is None:
            return
        name = data.get("name", "")
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        model = pm.mgr._models.get(name)
        if model is None:
            self._send_json({"error": f"Unknown model: {name}"}, 404)
            return
        # Collect paths referenced by the model (engine unit + model files)
        engine_unit, owned_files = _collect_engine_refs(model)
        # Stop if running (shared/independent both go through stop_service)
        stopped = None
        if name in pm.mgr.active_services:
            stopped = pm.mgr.stop_service(name)
        yaml_path = getattr(model, "yaml_path", None)
        if not yaml_path or not Path(yaml_path).exists():
            self._send_json({"error": f"YAML not found: {yaml_path}"}, 404)
            return
        try:
            Path(yaml_path).unlink()
        except Exception as e:
            self._send_json({"error": f"Delete failed: {e}"}, 500)
            return
        # D1: next switch re-reads models.d; reload here so the list takes
        # effect immediately (must go through mgr.reload_models: syncs
        # _gpu_state/_lifecycle references)
        pm.mgr.reload_models()

        # Model file cleanup: only files under the project models/ dir
        removed_files = []
        repo_root = Path(__file__).resolve().parents[3]
        project_models = repo_root / "models"
        for f in owned_files:
            try:
                rp = f.resolve()
                if project_models == rp or project_models in rp.parents:
                    if rp.is_file():
                        rp.unlink()
                        removed_files.append(str(rp))
            except OSError:
                continue

        # Engine unit teardown: after reload, no remaining model references
        # engines/<unit>/ → remove the whole directory
        removed_engine = None
        if engine_unit is not None:
            still_used = False
            for m in pm.mgr._models.values():
                try:
                    u, _ = _collect_engine_refs(m)
                    if u == engine_unit:
                        still_used = True
                        break
                except Exception:
                    continue
            target = repo_root / "engines" / engine_unit
            if not still_used and target.is_dir() and target.parent.name == "engines" \
                    and target.parent.resolve() == (repo_root / "engines").resolve():
                import shutil
                shutil.rmtree(target, ignore_errors=True)
                removed_engine = str(target)

        # Pool linkage: purge local candidates referencing the model
        # (source module deleted → pools auto-sync)
        purged, pool_rev = 0, None
        try:
            purged, pool_rev = pm.pool_registry.purge_candidates(
                lambda c: c.get("kind") == "local" and c.get("model_name") == name)
        except Exception as e:
            log.warning("pool purge after uninstall %s failed: %s", name, e)

        log.info("Pack uninstalled: %s (stopped=%s, files=%d, engine=%s, pool_purged=%d)",
                 name, bool(stopped), len(removed_files), removed_engine, purged)
        self._send_json({"status": "uninstalled", "model": name,
                         "stopped": bool(stopped),
                         "removed_model_files": removed_files,
                         "removed_engine_dir": removed_engine,
                         "pool_candidates_purged": purged})

    def _handle_pack_download(self, pm):
        """POST /admin/packs/download {name, url, filename, model_type_label?}

        One-click pack install (legacy path):
        1. models.d/<name>.yaml exists → already_configured
        2. model file already at <repo>/models/<filename> → skip download
        3. otherwise background streaming download (.part → atomic rename),
           GET /admin/packs/download-status for progress; auto install_model
           on completion (writes YAML only, does not start).
        """
        import threading as _threading
        import urllib.request as _ureq
        data = self._read_body()
        name = ((data or {}).get("name") or "").strip()
        url = ((data or {}).get("url") or "").strip()
        filename = ((data or {}).get("filename") or "").strip()
        label = ((data or {}).get("model_type_label") or "").strip()
        engine = ((data or {}).get("engine") or "").strip()   # "whisper" → build engine first
        if not name or not url or not filename:
            self._send_json({"error": "name/url/filename required"}, 400)
            return
        if "/" in filename or ".." in filename:
            self._send_json({"error": "invalid filename"}, 400)
            return
        if not self._validate_cloud_test_url(url, pm)[0] and not url.startswith("https://"):
            self._send_json({"error": "invalid url"}, 400)
            return
        if name in pm.mgr._models:
            self._send_json({"status": "already_configured", "model": name})
            return
        dest_dir = Path(self._pack_models_dir(pm))
        dest = dest_dir / filename
        if engine != "whisper" and dest.exists() and dest.stat().st_size > 0:
            result = pm.mgr.install_model(name, "llama_cpp", model_type_label=label)
            self._send_json(result)
            return
        if _pack_download_state(name).get("status") == "downloading":
            self._send_json({"status": "already_downloading", "model": name})
            return
        dest_dir.mkdir(parents=True, exist_ok=True)

        def _worker():
            _pack_download_state(name).update(status="downloading", downloaded=0, total=0,
                                              error="", started=time.time())
            try:
                # ── Engine stage (ASR end-to-end): in-project whisper-server build (Metal) ──
                if engine == "whisper":
                    st = _pack_download_state(name)
                    st.update(status="building", detail="检查引擎…", error="")
                    ok, msg = _ensure_whisper_engine(st)
                    if not ok:
                        st.update(status="error", error=f"引擎构建失败: {msg[:300]}")
                        return
                    if not dest.exists() or dest.stat().st_size == 0:
                        st.update(status="downloading", detail="下载模型…")
                        req = _ureq.Request(url, headers={"User-Agent": "InferFabric/1.0"})
                        with _ureq.urlopen(req, timeout=60) as resp:
                            total = int(resp.headers.get("Content-Length") or 0)
                            st["total"] = total
                            tmp = dest.with_suffix(".part")
                            done = 0
                            with open(tmp, "wb") as f:
                                while True:
                                    chunk = resp.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    done += len(chunk)
                                    st["downloaded"] = done
                            tmp.rename(dest)
                    st.update(status="installing", detail="写配置…")
                    result = _install_whisper_pack(pm, name, dest)
                    st["status"] = ("installed" if result.get("status") in
                                    ("installed", "already_configured") else "error")
                    st["error"] = result.get("message", "")
                    log.info("ASR pack install %s: %s", name, result)
                    return
                req = _ureq.Request(url, headers={"User-Agent": "InferFabric/1.0"})
                with _ureq.urlopen(req, timeout=60) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    _pack_download_state(name)["total"] = total
                    tmp = dest.with_suffix(".part")
                    done = 0
                    with open(tmp, "wb") as f:
                        while True:
                            chunk = resp.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            _pack_download_state(name)["downloaded"] = done
                    tmp.rename(dest)
                _pack_download_state(name)["status"] = "installing"
                result = pm.mgr.install_model(name, "llama_cpp", model_type_label=label)
                _pack_download_state(name)["status"] = (
                    "installed" if result.get("status") in ("installed", "already_configured")
                    else "error")
                _pack_download_state(name)["error"] = result.get("message", "")
                log.info("Pack download+install %s: %s", name, result)
            except Exception as e:
                log.error("Pack download %s failed: %s", name, e)
                _pack_download_state(name).update(status="error", error=str(e)[:300])
                dest.with_suffix(".part").unlink(missing_ok=True)

        _threading.Thread(target=_worker, daemon=True, name=f"pack-dl-{name}").start()
        self._send_json({"status": "download_started", "model": name})

    def _pack_models_dir(self, pm) -> str:
        models_dir = Path(pm.mgr.models_dir)
        try:
            return str(models_dir.parents[1] / "models")
        except IndexError:
            return str(Path.home() / "models")

    def _handle_pack_install(self, pm):
        """Install entry split (spec §6.1 ruling):

        - body with ``pack_id`` → capability pack transaction (Catalog-only,
          URL/hash provided by the backend)
        - otherwise legacy flow: local discovery artifact writes models.d YAML
          (user-imported model path, kept as-is)
        """
        data = self._read_body()
        if data and data.get("pack_id"):
            code, payload = pack_handlers.get_pack_manager().start_install(
                str(data["pack_id"]))
            self._send_json(payload, code)
            return
        name = ((data or {}).get("name") or "").strip()
        model_type = ((data or {}).get("type") or "llama_cpp").strip()
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        result = pm.mgr.install_model(name, model_type)
        code = 200 if result.get("status") in ("installed", "already_configured") else 400
        self._send_json(result, code)

    # ── App 设置页鉴权开关（/admin/security/*，F9-T2；thin handlers 包装
    #    上游 KeyStore，语义与 fork security.js 消费契约一致） ──────────

    def _handle_security_status(self, pm):
        """GET /admin/security/status — App 设置页鉴权开关状态 + 密钥清单（无明文）。"""
        auth = pm.auth
        if isinstance(auth, SecurityAuth):
            policy = auth.store.get_policy()
            enforced = policy == "all" or auth.legacy.enabled
            keys = [{"id": k.get("id"), "name": k.get("name"),
                     "created_at": k.get("created_at"),
                     "revoked": k.get("revoked_at") is not None}
                    for k in auth.store.list_keys()]
            self._send_json({"active": True, "policy": policy, "enforced": enforced,
                             "key_count": sum(1 for k in keys if not k["revoked"]),
                             "keys": keys})
        else:
            self._send_json({"active": False, "policy": None, "enforced": auth.enabled,
                             "key_count": 0, "keys": []})

    def _handle_security_create_key(self, pm):
        """POST /admin/security/keys {name?} — 新建 API key（明文仅本次回显一次）。"""
        data = self._read_body() or {}
        name = (data.get("name") or "inferfabric-app").strip()[:64] or "inferfabric-app"
        auth = pm.auth
        if not isinstance(auth, SecurityAuth):
            self._send_json({"error": "鉴权未启用 — 先开启 API Key 鉴权"}, 400)
            return
        try:
            entry, token = auth.store.create_key(name, None, None)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        except sqlite3.IntegrityError:
            # 同名冲突（UNIQUE(name) 含已吊销行）：400 让用户改名，而非
            # 落入 do_POST 的吞异常路径（连接无响应关闭，浏览器侧表现为挂起）
            self._send_json({"error": f"同名 key 已存在（含已吊销）— 请换一个名称：{name}"}, 400)
            return
        log.info("API key created via app settings: name=%s id=%s", name, entry.get("id"))
        # no-store：明文 key 仅本次回显，绝不进共享缓存（M2-T2）
        self._send_json({"status": "ok", "key": token, "name": name,
                         "note": "明文仅本次返回 — 请立即复制保存"},
                        extra_headers={"Cache-Control": "no-store"})

    def _handle_security_revoke_key(self, pm):
        """POST /admin/security/keys/revoke {id} — 撤销指定 key。"""
        data = self._read_body() or {}
        kid = data.get("id")
        auth = pm.auth
        if not isinstance(auth, SecurityAuth):
            self._send_json({"error": "鉴权未启用"}, 400)
            return
        if not isinstance(kid, str) or not kid:
            self._send_json({"error": "id (string) required"}, 400)
            return
        try:
            auth.store.revoke_key(kid)
        except KeyError:
            self._send_json({"error": f"key {kid} 不存在"}, 404)
            return
        log.info("API key revoked via app settings: id=%s", kid)
        self._send_json({"status": "ok"})

    def _handle_security_enable(self, pm):
        """POST /admin/security/enable {enforce: bool, key_name?: str}

        App 设置页一键鉴权：
        - enforce=true：security.db/marker 缺失时按启动同源路径初始化并热替换
          pm.auth / pm.security_admin（与 proxy_manager 构造一致）；policy → 'all'
          （全部客户端请求需带 key）；无任何 key 时自动生成首个（明文 token
          仅本次响应返回一次，库内只存摘要）。
        - enforce=false：policy → 'off'（迁移宽限语义，不删除已建 key）。
        """
        data = self._read_body()
        if data is None:
            return
        enforce = bool(data.get("enforce"))
        key_name = (data.get("key_name") or "inferfabric-app").strip()[:64]
        try:
            if enforce:
                if not isinstance(pm.auth, SecurityAuth):
                    # build_auth 内部 KeyStore(path) 不带 provision — 首次启用时
                    # DB 尚不存在会直接 [Errno 2]；与启动同源：DB 缺失先按
                    # provision=True 建库（iff keys init 语义）。
                    # 标记（security.enabled）不在此预写：build_auth 成功路径
                    # 自行落标记 — 预写会在 build_auth 抛异常时残留，重启后
                    # 状态页误报 active（#18 评审 N3）。
                    db_path = IFF_DATA_DIR / "security.db"
                    if not (db_path.exists() or db_path.is_symlink()):
                        from inferfabric.security.key_store import KeyStore
                        KeyStore(db_path, provision=True)
                    pm.auth = build_auth(IFF_DATA_DIR)
                    if not isinstance(pm.auth, SecurityAuth):
                        self._send_json({"error": "security init failed"}, 500)
                        return
                    from inferfabric.security.admin import SecurityAdmin
                    configured = os.environ.get("IFF_CONSOLE_ORIGINS", "")
                    origins = ([o.strip() for o in configured.split(",") if o.strip()]
                               if configured else
                               [f"http://127.0.0.1:{PROXY_PORT}",
                                f"http://localhost:{PROXY_PORT}"])
                    pm.security_admin = SecurityAdmin(pm.auth.store, origins=origins)
                    log.info("Security store initialized from app settings toggle")
                pm.auth.store.set_policy("all")
                token = None
                if not pm.auth.store.list_keys():
                    entry, token = pm.auth.store.create_key(key_name, None, None)
                    log.info("API key created from app settings: name=%s", key_name)
                # no-store：本响应可能携带一次性明文 key（M2-T2）
                self._send_json({"status": "ok", "policy": "all", "enforced": True,
                                 "key": token,
                                 "note": None if token else "已有密钥 — 在 /admin/keys 列表管理（明文不再回显）"},
                                extra_headers={"Cache-Control": "no-store"})
            else:
                if isinstance(pm.auth, SecurityAuth):
                    pm.auth.store.set_policy("off")
                    self._send_json({"status": "ok", "policy": "off", "enforced": False},
                                    extra_headers={"Cache-Control": "no-store"})
                else:
                    self._send_json({"status": "ok", "policy": None, "enforced": False,
                                     "note": "鉴权从未启用"},
                                    extra_headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, sqlite3.Error) as e:
            log.error("security enable failed: %s", e)
            self._send_json({"error": f"security toggle failed: {e}"}, 500)

    def _handle_switch(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model") or data.get("profile")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return

        # Gate: respect switching_target (idle is always allowed as escape hatch)
        switching = pm.mgr.state.get("switching_target") or ""
        if switching and switching != target and target != "idle":
            self._send_json(
                {"status": "error", "message": "GPU switch in progress (%s)" % switching},
                409,
            )
            return

        if target == "idle":
            for svc in list(pm.mgr.active_services):
                pm.mgr.state.record_manual_stop(svc)
        elif target != "idle":
            pm.mgr.state.clear_manual_stop(target)
        result = pm.mgr.switch(target)
        pm.mgr.state.set("switching_target", "")
        self._send_json(result)

    def _handle_stop(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.stop_service(target)
        if result.get("status") in ("stopped", "already_stopped"):
            pm.mgr.state.record_manual_stop(target)
            self._send_json(result, 200)
        else:
            # genuine error（未运行/未知模型/锁占用/GPU 未释放等）——4xx，
            # 前端 doModelAction 显示 message。注：exclusive 模型经 stop_service
            # 转走 _switch_to_idle，成功返回 stopped → 200（不再走此分支）。
            self._send_json(result, 400)

    def _handle_reset(self, pm):
        for svc in list(pm.mgr.active_services):
            pm.mgr.state.record_manual_stop(svc)
        pm.mgr.force_reset()
        self._send_json({"status": "reset", "gpu_mode": GPUMode.IDLE})

    def _handle_sleep(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.sleep_model(target)
        self._send_json(result)

    def _handle_wake(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.wake_model(target)
        self._send_json(result)

    def _handle_reconcile(self, pm):
        result = pm.mgr.reconcile()
        self._send_json(result)

    def _handle_reload_config(self, pm):
        """POST /reload-config — 热加载 models.d/*.yaml 并刷新 dashboard 缓存。"""
        if hasattr(pm, 'config_reloader') and pm.config_reloader:
            # 池重载已并入 reload_all（依赖域 models/auth/cloud 之后执行，
            # 评审 #20）；失败域如实回报（B2）
            failed = pm.config_reloader.reload_all() or []
            body = {"status": "reloaded"}
            if failed:
                body["failed"] = failed
            self._send_json(body)
        else:
            # Fallback: direct reload if ConfigReloader not available
            pm.mgr.reload_models()
            # 池重载在依赖（models）之后（评审 #20）
            pool_reload_note = None
            if hasattr(pm, 'pool_registry') and pm.pool_registry is not None:
                try:
                    pm.pool_registry.reload_from_disk()
                except Exception as e:
                    pool_reload_note = f"model_pools reload failed: {e}"
            try:
                from inferfabric.dashboard import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
            body = {"status": "reloaded"}
            if pool_reload_note:
                body.setdefault("failed", []).append(pool_reload_note)
            self._send_json(body)

    def _handle_deploy(self, pm):
        data = self._read_body()
        if data is None:
            return
        name = data.get("name")
        model_type = data.get("type", "vllm")
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        result = pm.mgr.auto_deploy(name, model_type)
        # already_configured means YAML exists; still attempt switch
        if result.get("status") == "already_configured":
            result = pm.mgr.switch(name)
        self._send_json(result)

    def _handle_pull(self, pm):
        data = self._read_body()
        if data is None:
            return
        name = data.get("name")
        framework = data.get("framework", "")
        if not name:
            self._send_json({"error": "Missing name"}, 400)
            return
        result = pm.mgr.pull_model(name, framework)
        self._send_json(result)

    def _handle_set_autostart(self, pm):
        """POST /admin/models/set-autostart {name, enabled} — 改 models.d YAML
        autostart 字段 + 热重载（F9/T3，fork 移植）。

        行级编辑保持 YAML 可读性（注释/键序不动）；autostart 缺失时插到
        首个顶级 name: 之后。评审 PR#19 修复：B1 原子写（读-改-写持全局锁
        + 同目录 tmp 0600 + safe_load 写前验证 + os.replace——上游
        load_models 无 per-file 容错，原地截断重写遇重启/断电损坏单文件
        即全网关崩溃循环）；N1 enabled 严格 bool（缺 key/"false" 字符串
        一律 400）；N2 写前验证 flag 落位（无可插 name: 行时不再静默
        no-op 报 ok）；N3 响应带 reload 状态。"""
        data = self._read_body() or {}
        name = (data.get("name") or "").strip()
        if not name or not isinstance(data.get("enabled"), bool):
            self._send_json({"error": "name (str) and enabled (bool) are required"}, 400)
            return
        enabled = data["enabled"]
        model = pm.mgr._models.get(name)
        if model is None:
            self._send_json({"error": "Unknown model: " + name}, 404)
            return
        yaml_path = getattr(model, "yaml_path", None)
        if not yaml_path or not Path(yaml_path).exists():
            self._send_json({"error": "YAML not found: " + str(yaml_path)}, 404)
            return
        flag_line = "autostart: " + ("true" if enabled else "false")
        try:
            # B1：读-改-写全程持锁，防并发 set-autostart 交错丢更新
            with _AUTOSTART_YAML_LOCK:
                text = Path(yaml_path).read_text()
                lines = text.split("\n")
                replaced = False
                out = []
                for ln in lines:
                    if ln.startswith("autostart:"):
                        out.append(flag_line)
                        replaced = True
                    else:
                        out.append(ln)
                if not replaced:
                    for i, ln in enumerate(out):
                        if ln.startswith("name:"):
                            out.insert(i + 1, flag_line)
                            break
                new_text = "\n".join(out)
                # 写前验证：新全文可解析 + N2 flag 确实落位。验证失败时
                # 原文件分毫未动——不存在"验证前已损坏"窗口。
                parsed = yaml.safe_load(new_text)
                if not isinstance(parsed, dict) or parsed.get("autostart") is not enabled:
                    self._send_json(
                        {"error": "YAML update verification failed (autostart flag not landed)"}, 500)
                    return
                # 同目录 tmp（0600）+ os.replace 原子落盘；异常时清 tmp
                fd, tmp = tempfile.mkstemp(
                    dir=str(Path(yaml_path).parent),
                    prefix=".autostart-", suffix=".yaml.tmp")
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(new_text)
                    os.replace(tmp, yaml_path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        except Exception as e:
            self._send_json({"error": "YAML update failed: " + str(e)}, 500)
            return
        # N3：热重载结果进响应——config_reloader（连带 dashboard 缓存
        # 失效）优先，裸 fake pm（单测）回落 mgr 直调。
        reload_status = "skipped"
        reloader = getattr(pm, "config_reloader", None)
        if reloader is not None:
            try:
                reloader.reload_models()
                reload_status = "ok"
            except Exception as e:
                reload_status = "failed: " + str(e)
        elif hasattr(pm.mgr, "reload_models"):
            try:
                pm.mgr.reload_models()
                reload_status = "ok"
            except Exception as e:
                reload_status = "failed: " + str(e)
        log.info("Autostart %s -> %s (reload: %s)", name, enabled, reload_status)
        self._send_json({"status": "ok", "model": name, "autostart": enabled,
                         "reload": reload_status})

    def _handle_cache_toggle(self, pm):
        """POST /admin/cache/toggle — 切换响应缓存开关。"""
        from inferfabric.proxy.response_cache import ResponseCache
        enabled = getattr(pm, 'response_cache', None) is not None
        if enabled:
            pm.response_cache = None
        else:
            pm.response_cache = ResponseCache(maxsize=getattr(pm, '_runtime_config', {}).get("cache", {}).get("max_entries", 500))
        new_state = pm.response_cache is not None
        log.info("Cache toggled: %s → %s", enabled, new_state)
        self._send_json({"cache_enabled": new_state})

    def _handle_auto_switch_toggle(self, pm):
        """POST /admin/auto-switch/toggle — 切换自动切换（立即生效，无需重启 proxy）。

        返回 { auto_switch, source, env_locked, hint }：
          source: env | file | default — 启动时 auto_switch 的取值来源
          env_locked: 显式 env EDGE_AUTO_SWITCH 存在时为 True，此时 UI 写入
            只作用于当前进程，重启后回到 env 值（hint 非 null）。
        """
        result = pm.set_auto_switch(not pm.auto_switch)
        log.info("Auto switch toggled → %s (source=%s)", result["auto_switch"], result["source"])
        self._send_json(result)

    def _handle_gpu_clear(self, pm):
        """POST /admin/gpu-clear — 清理 GPU CUDA 状态（修复显存碎片）。"""
        try:
            result = pm.mgr._proc.clear_gpu_cuda_state(gpu_index=0, force=True)
            self._send_json({
                "status": result.get("status", "unknown"),
                "before_mb": result.get("before_mb"),
                "after_mb": result.get("after_mb"),
                "method": result.get("method"),
            })
        except Exception as e:
            log.error("gpu-clear failed: %s", e)
            self._send_json({"status": "error", "message": str(e)}, 500)

    # ─── Admin: Cloud Provider Management (PR-D) ─────────────────

    def _handle_cloud_reload(self, pm):
        """POST /admin/cloud/reload — 热加载 cloud_provider.yaml。"""
        from inferfabric.cloud_discovery import CloudDiscovery
        from inferfabric.proxy_manager import IFF_DATA_DIR
        pm.cloud.reload(IFF_DATA_DIR / "cloud_provider.yaml")
        models = pm.cloud.discover_all()
        pm._cloud_discovered = True
        # Restart polling after reload (reload stops the old polling thread)
        pm.cloud.start_polling()
        # 双键注册表只数短名键（与 discover 一致，避免计数膨胀）
        unique = sum(1 for k in models if "/" not in k)
        self._send_json({
            "status": "reloaded",
            "providers": len(pm.cloud.providers),
            "cloud_models": unique,
        })

    def _handle_cloud_discover(self, pm):
        """POST /admin/cloud/discover — 手动触发模型发现。"""
        models = pm.cloud.discover_all()
        pm._cloud_discovered = True
        # 注册表是双键（短名 + provider/ 前缀）——只数短名键，避免把 4 个模型报成 8
        unique = {k: m for k, m in models.items() if "/" not in k}
        self._send_json({
            "status": "discovered",
            "cloud_models": len(unique),
            "models": [
                {
                    "id": m.model_id,
                    "provider": m.provider,
                    "openai": m.openai_available,
                    "anthropic": m.anthropic_available,
                    "discovered_at": m.discovered_at,
                }
                for m in unique.values()
            ],
        })

    def _validate_cloud_test_url(self, url: str, pm) -> tuple:
        """Validate a cloud provider test URL (v6.6 relaxed policy).

        Returns (is_valid: bool, reason: str, resolved_ips: list[str]).
        The resolved_ips list is returned on success so the caller can
        connect directly to the verified IP (TOCTOU mitigation — prevents
        DNS rebinding between validation and connection).

        Policy (admin-only endpoint; the whole point is to validate a
        provider BEFORE it is registered, so no registry whitelist):
          - scheme must be http or https (http for intranet relay stations)
          - host must be an IP literal or resolve via DNS
          - cloud metadata address 169.254.169.254 is still rejected
          - private / LAN / loopback targets are ALLOWED (intranet relay
            scenario) and logged — the endpoint is localhost/admin-gated,
            so an attacker with dashboard access can already operate this box.
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "invalid URL", []

        if parsed.scheme not in ("http", "https"):
            return False, "only http/https URLs are allowed", []

        hostname = parsed.hostname
        if not hostname:
            return False, "missing hostname in URL", []

        _METADATA_IP = "169.254.169.254"

        def _ip_kind(ip_str: str) -> str:
            """'metadata' | 'ok'."""
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return "ok"
            check_ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
            if check_ip == ipaddress.ip_address(_METADATA_IP):
                return "metadata"
            return "ok"

        # IP literal host (intranet relay: http://192.168.x.x:3000/v1) — no DNS
        try:
            ipaddress.ip_address(hostname)
            if _ip_kind(hostname) == "metadata":
                return False, "cloud metadata address not allowed", []
            log.warning("Cloud test → IP literal %s (admin-initiated)", hostname)
            return True, "ok (ip literal)", [hostname]
        except ValueError:
            pass

        safe_ips = []
        try:
            resolved = socket.getaddrinfo(hostname, None)
            for family, _type, _proto, _canonname, sockaddr in resolved:
                ip_str = sockaddr[0]
                if _ip_kind(ip_str) == "metadata":
                    continue
                try:
                    ip = ipaddress.ip_address(ip_str)
                    check_ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
                    if check_ip.is_private or check_ip.is_loopback or check_ip.is_link_local:
                        log.warning(
                            "Cloud test → private/LAN address %s (%s) — "
                            "admin-initiated intranet relay test", hostname, ip_str)
                except ValueError:
                    pass
                safe_ips.append(ip_str)
        except socket.gaierror:
            return False, f"DNS resolution failed for {hostname}", []

        if not safe_ips:
            return False, "no valid IPs resolved", []

        if parsed.scheme == "http":
            log.warning("Cloud test → plain http endpoint %s (intranet relay)", url)

        return True, "ok", safe_ips

    def _make_pinned_conn(self, host: str, port: int, resolved_ip: str,
                          use_tls: bool, timeout: int = 15):
        """Build an HTTP connection to a verified IP (TOCTOU mitigation).

        TCP-connects to resolved_ip, optionally wraps TLS with
        server_hostname=host (SNI + cert validation). Tests patch this
        factory to inject canned connections.
        """
        import http.client
        conn = http.client.HTTPConnection(resolved_ip, port, timeout=timeout)
        conn.connect()
        if use_tls:
            import ssl
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(conn.sock, server_hostname=host)
            conn.sock = sock
        conn._http_vsn_str = 'HTTP/1.1'
        return conn

    def _probe_cloud_endpoint(self, base_url: str, api_key: str, protocol: str,
                              probe_model: str, resolved_ip: str) -> dict:
        """Probe one provider base URL; classify the result.

        Returns {"status", "protocol", "url", "http_status", "model_count",
        "message"} where status is one of:
        ok | ok_no_listing | rate_limited | auth_failed | model_unavailable | error

        - probe_model set  -> real 1-token completion (strongest signal)
        - otherwise        -> GET {base}/models listing
        Anthropic bases accept both "https://host/.../v1" and bare-base forms.
        """
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        use_tls = scheme == "https"
        base = base_url.rstrip("/")

        if protocol == "anthropic":
            if base.endswith("/v1"):
                list_paths = [base + "/models"]
                msg_paths = [base + "/messages"]
            else:
                list_paths = [base + "/v1/models", base + "/models"]
                msg_paths = [base + "/v1/messages", base + "/messages"]
        else:
            list_paths = [base + "/models"]
            msg_paths = [base + "/chat/completions"]

        def _auth_headers() -> dict:
            h = {"Content-Type": "application/json"}
            h["Host"] = host if not parsed.port else f"{host}:{parsed.port}"
            if protocol == "anthropic":
                # 空 key = 公开端点裸测：不发空 x-api-key（§3.3-A）；协议头恒发
                if api_key:
                    h["x-api-key"] = api_key
                h["anthropic-version"] = "2023-06-01"
            elif api_key:
                h["Authorization"] = f"Bearer {api_key}"
            return h

        def _read_body(resp, limit=2000) -> str:
            try:
                return resp.read(limit).decode("utf-8", "replace")
            except Exception:
                return ""

        def _detail_from(raw: str) -> str:
            """Short human-readable error detail from an upstream error body."""
            if not raw:
                return ""
            try:
                obj = json.loads(raw)
                err = obj.get("error") if isinstance(obj, dict) else None
                if isinstance(err, dict) and err.get("message"):
                    return str(err["message"])[:160]
                if isinstance(err, str):
                    return err[:160]
                if isinstance(obj, dict) and obj.get("detail"):
                    return str(obj["detail"])[:160]
            except Exception:
                pass
            return raw[:160].strip()

        paths = msg_paths if probe_model else list_paths
        last = {"status": "error", "http_status": 0, "model_count": 0, "message": "no response"}
        for path_url in paths:
            path = path_url[len(base):] or "/"
            try:
                conn = self._make_pinned_conn(host, port, resolved_ip, use_tls)
            except Exception as e:
                return {"status": "error", "protocol": protocol, "url": base_url,
                        "http_status": 0, "model_count": 0,
                        "message": f"connect failed: {e}"}
            try:
                if probe_model:
                    body = json.dumps({"model": probe_model, "max_tokens": 1,
                                       "messages": [{"role": "user", "content": "ping"}]})
                    conn.request("POST", path, body=body, headers=_auth_headers())
                    resp = conn.getresponse()
                    raw = _read_body(resp)
                    code = resp.status
                    if 200 <= code < 300:
                        last = {"status": "ok", "http_status": code, "model_count": 1,
                                "message": f"探测模型 '{probe_model}' 可用（真实生成 1 token 验证）"}
                    elif code in (401, 403):
                        detail = _detail_from(raw)
                        # §3.4：百炼 key 正常、模型不在套餐也回 403（detail 含 not_supported）
                        # → 归为 model_unavailable（连接与鉴权正常，仅该模型不可用）
                        unsupported = bool(re.search(
                            r'not.?supported|unsupported.?model|model.?(not|un)?found',
                            detail, re.IGNORECASE))
                        if code == 403 and unsupported:
                            last = {"status": "model_unavailable", "http_status": code, "model_count": 0,
                                    "message": f"探测模型 '{probe_model}' 不在套餐 (HTTP {code}): {detail or '上游未返回详情'}"}
                        else:
                            last = {"status": "auth_failed", "http_status": code, "model_count": 0,
                                    "message": f"鉴权失败 (HTTP {code}): {detail or 'API Key 无效或无权限'}"}
                    else:
                        last = {"status": "model_unavailable", "http_status": code, "model_count": 0,
                                "message": f"模型 '{probe_model}' 不可用 (HTTP {code}): {_detail_from(raw) or '上游未返回详情'}"}
                else:
                    conn.request("GET", path, headers=_auth_headers())
                    resp = conn.getresponse()
                    raw = _read_body(resp)
                    code = resp.status
                    if 200 <= code < 300:
                        try:
                            mc = len((json.loads(raw) or {}).get("data", []))
                        except Exception:
                            mc = -1
                        last = {"status": "ok", "http_status": code, "model_count": mc,
                                "message": "连接正常" + (f"，上游列出 {mc} 个模型" if mc >= 0 else "")}
                    elif code in (401, 403):
                        last = {"status": "auth_failed", "http_status": code, "model_count": 0,
                                "message": f"鉴权失败 (HTTP {code}): {_detail_from(raw) or 'API Key 无效或无权限'}"}
                    elif code == 429:
                        last = {"status": "rate_limited", "http_status": code, "model_count": 0,
                                "message": f"可达但限流 (HTTP 429): {_detail_from(raw) or '上游未返回详情'}"}
                    else:
                        # 404 on /models: reachable but no listing (e.g. Baidu
                        # qianfan) — connection OK, model list unknown.
                        last = {"status": "ok_no_listing", "http_status": code, "model_count": 0,
                                "message": f"可达但无模型列表 (HTTP {code}) — 可填探测模型进一步验证"}
                if last["status"] != "error":
                    return {"status": last["status"], "protocol": protocol, "url": base_url,
                            "http_status": last["http_status"], "model_count": last["model_count"],
                            "message": last["message"]}
            except Exception as e:
                last = {"status": "error", "http_status": 0, "model_count": 0,
                        "message": f"request failed: {e}"}
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        return {"status": last["status"], "protocol": protocol, "url": base_url,
                "http_status": last.get("http_status", 0), "model_count": 0,
                "message": last.get("message", "no response")}

    #: probe 结果中视为『通过』的状态（UI 添加门禁用）
    CLOUD_TEST_PASS_STATUSES = ("ok", "ok_no_listing", "rate_limited")

    def _handle_cloud_test(self, pm):
        """POST /admin/cloud/test — 探测单个 provider base URL（允许未注册）。

        body: {url: base_url, api_key, protocol: openai|anthropic, probe_model?}
        返回 {status, protocol, url, http_status, model_count, message, pass}
        """
        data = self._read_body()
        if not data:
            self._send_json({"error": "No body"}, 400)
            return
        base_url = data.get("url", "")
        api_key = data.get("api_key", "")
        protocol = data.get("protocol", "openai")
        probe_model = (data.get("probe_model") or "").strip()
        if not base_url:
            self._send_json({"error": "Missing url"}, 400)
            return
        if protocol not in ("openai", "anthropic"):
            self._send_json({"error": "Invalid protocol"}, 400)
            return

        valid, reason, safe_ips = self._validate_cloud_test_url(base_url, pm)
        if not valid:
            log.warning("Cloud test URL rejected: %s — reason: %s", base_url, reason)
            self._send_json({"error": f"URL check failed: {reason}"}, 400)
            return

        resolved_ip = safe_ips[0]
        result = self._probe_cloud_endpoint(base_url, api_key, protocol, probe_model, resolved_ip)
        result["pass"] = result["status"] in self.CLOUD_TEST_PASS_STATUSES
        self._send_json(result)

    def _handle_cloud_provider_test(self, pm):
        """POST /admin/cloud/provider-test — 用已存 key 测试已注册 provider 的全部 base。

        body: {provider, probe_model?}
        返回 {provider, key_set, results: [...], ok: bool}
        """
        data = self._read_body()
        name = ((data or {}).get("provider") or "").strip()
        probe_model = ((data or {}).get("probe_model") or "").strip()
        if not name:
            self._send_json({"error": "Missing provider"}, 400)
            return
        cfg = pm.cloud.providers.get(name)
        if cfg is None:
            self._send_json({"error": f"Provider '{name}' not found"}, 404)
            return

        key_set = bool(cfg.api_key)
        targets = []
        if cfg.openai_base:
            targets.append(("openai", cfg.openai_base))
        if cfg.anthropic_base:
            targets.append(("anthropic", cfg.anthropic_base))
        if not targets:
            self._send_json({"provider": name, "key_set": key_set, "results": [],
                             "ok": False, "message": "该 Provider 未配置任何 Base URL"}, 400)
            return

        results = []
        for protocol, base_url in targets:
            try:
                valid, reason, safe_ips = self._validate_cloud_test_url(base_url, pm)
                if not valid:
                    results.append({"status": "error", "protocol": protocol, "url": base_url,
                                    "http_status": 0, "model_count": 0,
                                    "message": f"URL check failed: {reason}"})
                    continue
                results.append(self._probe_cloud_endpoint(
                    base_url, cfg.api_key, protocol, probe_model, safe_ips[0]))
            except Exception as e:
                results.append({"status": "error", "protocol": protocol, "url": base_url,
                                "http_status": 0, "model_count": 0, "message": str(e)})
        for r in results:
            r["pass"] = r["status"] in self.CLOUD_TEST_PASS_STATUSES
        ok = all(r["pass"] for r in results)
        self._send_json({"provider": name, "key_set": key_set, "results": results, "ok": ok})

    def _handle_cloud_providers(self, pm):
        """GET/POST/DELETE /admin/cloud/providers — 列出、添加或删除 provider。"""
        if self.command == "GET":
            providers = []
            all_models = pm.cloud.cloud_models  # 快照一次（含短名 + provider/ 前缀双键）
            for name, cfg in pm.cloud.providers.items():
                env_set = bool(os.environ.get(cfg.key_env_var, "")) if cfg.key_env_var else False
                # v6.1 Phase 1: 可路由数 = 短名键计数（避免双键重复统计）
                routable = sum(1 for k, m in all_models.items()
                               if "/" not in k and m.provider == name)
                providers.append({
                    "name": name,
                    "enabled": cfg.enabled,
                    "openai_base": cfg.openai_base,
                    "anthropic_base": cfg.anthropic_base,
                    "discovery_enabled": cfg.discovery_enabled,
                    "discovery_interval": cfg.discovery_interval,
                    "include_pattern": cfg.include_pattern,
                    "enabled_models": list(cfg.enabled_models),
                    # model_specs: [{id, manual, supports_vision, supports_tools,
                    #   context_window, max_output_tokens, price_input, price_output}]
                    # — manual=true 表示空 spec 手填模型；能力标签三态透传
                    # （未声明 = None/null，显式 false = 排除约束 — 7.8.0
                    # cross-review I-1：bool() 塌缩会让前端无法区分两者）
                    "model_specs": [
                        {"id": mid, "manual": not bool(spec),
                         "supports_vision": (spec or {}).get("supports_vision"),
                         "supports_tools": (spec or {}).get("supports_tools"),
                         # 属性四键三态透传（None=未设置）：属性浮窗 openAttrsModal
                         # 的 findSpec()+spec[k] 以本序列化为回填源 — 缺键即
                         # 「存得进、读不回」（#18 评审 B1）
                         "context_window": (spec or {}).get("context_window"),
                         "max_output_tokens": (spec or {}).get("max_output_tokens"),
                         "price_input": (spec or {}).get("price_input"),
                         "price_output": (spec or {}).get("price_output")}
                        for mid, spec in cfg.model_specs.items()
                    ],
                    "candidates": [c.model_id for c in pm.cloud.get_candidates(name)],
                    # 可路由模型的能力快照（短名 → caps）— 候选行 chips 状态源；
                    # 三态透传（None=未声明/false=显式排除，7.8.0 I-1）；
                    # 未入注册表的候选不在 map（不给未路由候选渲染打标入口）
                    "routable_caps": {
                        k: {"supports_vision": m.supports_vision,
                            "supports_tools": m.supports_tools}
                        for k, m in all_models.items()
                        if "/" not in k and m.provider == name
                    },
                    "routable_count": routable,
                    "model_count": routable,  # 兼容旧字段：语义对齐可路由数
                    "key_env_var": cfg.key_env_var,
                    "key_env_set": env_set,
                    "preset_id": cfg.preset_id,
                })
            # Include cloud models with capabilities (deduplicate: skip provider/ prefixed keys)
            models = []
            seen_ids = set()
            for mid, cm in pm.cloud.cloud_models.items():
                # Dual-key registry: "model_id" + "provider/model_id"
                # Only emit the short-name entry to avoid duplicates
                if "/" in mid:
                    continue
                if cm.model_id in seen_ids:
                    continue
                seen_ids.add(cm.model_id)
                d = cm.to_api_dict()
                d["provider"] = cm.provider
                d["openai_available"] = cm.openai_available
                d["anthropic_available"] = cm.anthropic_available
                d["discovered_at"] = cm.discovered_at
                models.append(d)
            self._send_json({
                "providers": providers,
                "models": models,
                "total_cloud_models": len(pm.cloud.cloud_models),
                "last_discovery": pm.cloud._last_discovery,
            })
        elif self.command == "DELETE":
            data = self._read_body()
            name = (data or {}).get("name", "")
            try:
                pm.cloud.drop_provider(name)
            except KeyError:
                self._send_json({"error": f"Provider '{name}' not found"}, 404)
                return
            except Exception as e:
                log.error("Failed to drop provider '%s': %s", name, e)
                self._send_json({"error": f"Failed to delete provider: {e}"}, 500)
                return
            try:
                pm.cloud.save_config()
            except Exception as e:
                log.error("Failed to persist provider deletion: %s", e)
                self._send_json({"error": "Failed to save config", "detail": str(e)}, 500)
                return
            self._send_json({"status": "deleted", "provider": name})
        elif self.command == "POST":
            data = self._read_body()
            if data is None:
                return

            # v4.7.0: Support preset-based addition
            preset_id = data.get("preset")
            if preset_id:
                presets = pm.cloud.load_presets()
                preset = presets.get(preset_id)
                if not preset:
                    self._send_json({"error": f"Unknown preset: {preset_id}"}, 400)
                    return
                name = data.get("name", preset_id)
                env_var = preset.env_var or pm.cloud._env_key_for_provider(name)
                # Build model_specs from preset models
                model_specs = {}
                for mid, mspec in preset.models.items():
                    if isinstance(mspec, dict):
                        model_specs[mid] = mspec
            else:
                # Manual mode (backward compatible)
                name = data.get("name")
                if not name:
                    self._send_json({"error": "Missing provider name"}, 400)
                    return
                env_var = pm.cloud._env_key_for_provider(name)

            # #7: Duplicate provider name check
            if name in pm.cloud._providers:
                self._send_json({"error": f"Provider '{name}' already exists"}, 400)
                return

            # #5: Write API key to secrets.env FIRST, before in-memory config
            api_key = data.get("api_key", "")
            if api_key and not api_key.startswith("${"):
                try:
                    pm.cloud.secrets.write(env_var, api_key)
                except Exception as e:
                    log.error("Failed to write secrets.env: %s", e)
                    self._send_json({"error": "Failed to save API key", "detail": str(e)}, 500)
                    return
                # B1: 明文 key → 内存持有真实值（与 _load_config 解析前展开 ${VAR} 的行为一致）；
                # 持久化时 _serialize_providers 依 key_env_var 重新导出 ${REF}，YAML 只存引用。
                api_key_mem = api_key
            else:
                # 传入的即 ${REF}（或空）：注入 env 后按引用解析为真实值
                api_key_mem = api_key

            # Inject secrets.env so newly written / referenced keys are available immediately
            pm.cloud._inject_secrets_env()

            # B1: 将 ${REF} 解析为真实值，保证 POST 后立即转发不再发字面量 ${VAR}（否则云端 401）
            if api_key_mem.startswith("${") and api_key_mem.endswith("}"):
                api_key_mem = os.environ.get(api_key_mem[2:-1], "")

            from inferfabric.cloud_discovery import ProviderConfig
            if preset_id:
                cfg = ProviderConfig(
                    name=name,
                    api_key=api_key_mem,
                    openai_base=preset.openai_base,
                    anthropic_base=preset.anthropic_base,
                    timeout=preset.timeout,
                    enabled=True,
                    discovery_enabled=preset.discovery,
                    key_env_var=env_var,
                    preset_id=preset_id,
                    model_specs=model_specs,
                )
            else:
                cfg = ProviderConfig(
                    name=name,
                    api_key=api_key_mem,
                    openai_base=data.get("openai_base", ""),
                    anthropic_base=data.get("anthropic_base", ""),
                    timeout=data.get("timeout", 60),
                    enabled=data.get("enabled", True),
                    discovery_enabled=data.get("discovery_enabled", True),
                    discovery_endpoint=data.get("discovery_endpoint", "/models"),
                    discovery_interval=data.get("discovery_interval", 3600),
                    include_pattern=data.get("include_pattern", ""),
                    key_env_var=env_var,
                )

            with pm.cloud._models_lock:
                pm.cloud._providers[name] = cfg
                # Register spec-only models from new provider
                pm.cloud._register_spec_only_models(pm.cloud._cloud_models)
            try:
                pm.cloud.save_config()
            except Exception as e:
                log.error("Failed to persist provider addition: %s", e)
                self._send_json({"error": "Failed to save config", "detail": str(e)}, 500)
                return
            self._send_json({"status": "added", "provider": name})

    def _handle_cloud_provider_models(self, pm):
        """POST /admin/cloud/provider-models — 模型策展（v6.1 Phase 1；L2b 打标）。

        body: {provider, action: enable|disable|add|remove|tag, model,
               supports_vision?, supports_tools?}
        - enable:  勾选发现候选 → 白名单 + 可路由
        - disable: 取消勾选 → 隐藏（spec/manual 同名仍在 spec 侧）
        - add:     手填模型名 → 空 spec 复用 model_specs 管道（恒可路由）；
                   可带能力标签（面板「视觉/工具调用」选择框）
        - remove:  移除手填模型（不存在 → 404）
        - tag:     能力打标 — 写 model_specs → 重建注册表（catalog 出 features，
                   池候选据此做模态路由）；spec 不存在则创建（打标即声明）
        每次操作后 save_config() 持久化 + 刷新内存注册表。
        """
        data = self._read_body()
        if data is None:
            return
        provider = (data.get("provider") or "").strip()
        action = (data.get("action") or "").strip()
        model = (data.get("model") or "").strip()
        valid_actions = {"enable", "disable", "add", "remove", "tag"}
        if not provider or not action or not model:
            self._send_json({"error": "Missing provider/action/model"}, 400)
            return
        if action not in valid_actions:
            self._send_json({"error": f"Invalid action '{action}' — expected "
                                      f"{'/'.join(sorted(valid_actions))}"}, 400)
            return
        # 能力标签：bool；属性编辑：context_window/max_output_tokens 取 int、
        # price_input/price_output 取数值（None/空 = 清除属性，update_model_spec 语义）。
        # tag 必须至少带一个可接受键（否则为空操作）。
        caps: dict = {}
        for key in ("supports_vision", "supports_tools"):
            if key in data:
                val = data[key]
                if not isinstance(val, bool):
                    self._send_json({"error": f"'{key}' must be a boolean"}, 400)
                    return
                caps[key] = val
        for key in ("context_window", "max_output_tokens"):
            if key in data:
                val = data[key]
                if val is None or val == "":
                    caps[key] = None
                elif isinstance(val, bool) or not isinstance(val, int):
                    self._send_json({"error": f"'{key}' must be an integer"}, 400)
                    return
                else:
                    caps[key] = val
        for key in ("price_input", "price_output"):
            if key in data:
                val = data[key]
                if val is None or val == "":
                    caps[key] = None
                elif isinstance(val, bool) or not isinstance(val, (int, float)):
                    self._send_json({"error": f"'{key}' must be a number"}, 400)
                    return
                else:
                    caps[key] = float(val)
        if action == "tag" and not caps:
            self._send_json({"error": "tag requires supports_vision and/or "
                                      "supports_tools"}, 400)
            return
        try:
            if action == "enable":
                count = pm.cloud.enable_model(provider, model)
            elif action == "disable":
                count = pm.cloud.disable_model(provider, model)
            elif action == "add":
                count = pm.cloud.add_manual_model(provider, model, caps=caps or None)
            elif action == "tag":
                count = pm.cloud.update_model_spec(provider, model, caps)
            else:  # remove
                count = pm.cloud.remove_manual_model(provider, model)
        except KeyError as e:
            # 策展方法抛 KeyError：参数是 provider 名 → 404 provider；否则 → 404 model
            what = e.args[0] if e.args else provider
            if what == provider:
                self._send_json({"error": f"Provider '{provider}' not found"}, 404)
            else:
                self._send_json({"error": f"Model '{what}' not found for provider "
                                          f"'{provider}'"}, 404)
            return
        except Exception as e:
            log.error("provider-models %s %s/%s failed: %s", action, provider, model, e)
            self._send_json({"error": f"Failed to {action}: {e}"}, 500)
            return
        try:
            pm.cloud.save_config()
        except Exception as e:
            log.error("Failed to persist provider-models %s %s/%s: %s",
                      action, provider, model, e)
            self._send_json({"error": "Failed to save config", "detail": str(e)}, 500)
            return
        self._send_json({"status": "ok", "provider": provider, "model": model,
                         "action": action, "routable_count": count})

    def _handle_cloud_presets(self, pm):
        """GET /admin/cloud/presets — 返回预设厂商列表。"""
        from inferfabric.cloud_discovery import CloudDiscovery
        presets = CloudDiscovery.load_presets()
        result = []
        for pid, p in presets.items():
            result.append({
                "id": p.id,
                "display_name": p.display_name,
                "icon": p.icon,
                "openai_base": p.openai_base,
                "anthropic_base": p.anthropic_base,
                "env_var": p.env_var,
                "discovery": p.discovery,
                "model_count": len(p.models),
            })
        self._send_json({"presets": result})

    def _pipeline_guard(self, pm, model_name: str, data: dict, model_type: str) -> dict:
        """Shared security pipeline for /v1/embeddings and /v1/rerank (A1/A7).

        Applies, in the same order as the chat path:
          1. model resolution (404)
          2. auth (401)
          3. switch guard (503 when SWITCHING and not the switching target)
          4. model-type / port validation (400 / 500)
          5. AUTO_SWITCH respect (503 when model inactive and AUTO_SWITCH=off, A7)
          6. dual_gate rate limit (429)

        Every blocking outcome writes a RequestLog entry (R1) and returns:
          {"blocked": True, "status": int, "body": dict, "headers": dict|None}
        On pass returns:
          {"blocked": False, "gate": <gate>, "ctx": {
               "req_id","req_start","key_name","svc_name","model_obj","port"}}
        The caller forwards and records the terminal outcome, releasing gate.
        """
        from inferfabric.state import ServiceState

        req_id = pm.new_request_id()
        req_start = time.monotonic()
        auth_header = self.headers.get("Authorization", "") or self.headers.get("x-api-key", "")
        key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
        # v6.5: 客户端 Agent 分类（缓存到 handler，embeddings/rerank 站点取值）
        _reg = getattr(pm, "agent_registry", None)
        if _reg is not None:
            self._agent_hit = _reg.classify(
                request_protocol(getattr(self, "path", "")),
                getattr(self, "headers", {}))

        def _block(status, body, error, headers=None):
            pm.logger.log(RequestLog(
                req_id=req_id, key_name=key_name, model=model_name,
                status=status, error=error,
                agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                duration_ms=(time.monotonic() - req_start) * 1000,
            ))
            return {"blocked": True, "status": status, "body": body, "headers": headers}

        # 1. Model resolution
        svc_name = pm.model_to_service(model_name)
        if not svc_name:
            pm.anomalies.record(AnomalyEvent(
                category="routing", severity="warning", model=model_name,
                status_code=404,
                message=f"Unknown model '{model_name}' (embeddings/rerank)",
                possible_cause="Model not configured in models.d or not a matching endpoint type",
            ))
            return _block(404, {"error": f"Unknown model: {model_name}"}, "unknown_model")

        # 2. Auth
        if pm.auth.enabled:
            model_for_auth = model_name.split("/")[-1] if "/" in model_name else model_name
            auth_ok, auth_reason = pm.auth.check(auth_header, model_for_auth)
            if not auth_ok:
                return _block(401, {"error": auth_reason, "status": "unauthorized"}, auth_reason)

        # 3. Switch guard
        profile_state = pm.mgr.state.get("profile_state", "")
        if profile_state == ServiceState.SWITCHING:
            switching_target = pm.mgr.state.get("switching_target") or ""
            if svc_name != switching_target:
                pm.anomalies.record(AnomalyEvent(
                    category="routing", severity="warning", model=model_name,
                    status_code=503,
                    message=f"Model switching to {switching_target}, {model_name} request rejected",
                    possible_cause="本地模型正在切换中，请求的是另一个模型。",
                ))
                return _block(
                    503,
                    {"error": "Model is switching, please retry", "status": "switching", "retry_after": 30},
                    "model_switching",
                    headers={"Retry-After": "30"},
                )

        # 4. Model-type / port validation
        model_obj = pm.mgr.get_model(svc_name)
        if not model_obj or model_obj.model_type != model_type:
            return _block(400, {"error": f"Model '{model_name}' is not a {model_type} model"}, "model_type_mismatch")
        port = model_obj.port
        if not port:
            return _block(500, {"error": f"No port configured for model '{model_name}'"}, "no_port")

        # 5. AUTO_SWITCH respect (A7): inactive + auto_switch=off → 503, do NOT switch
        if svc_name not in pm.mgr.active_services:
            if not pm.auto_switch:
                pm.anomalies.record(AnomalyEvent(
                    category="routing", severity="warning", model=model_name,
                    status_code=503,
                    message=f"Model {svc_name} not active and AUTO_SWITCH=off",
                    possible_cause="模型在配置中但未启动，且 auto_switch 被禁用。手动 /switch 或启用 AUTO_SWITCH。",
                ))
                return _block(
                    503,
                    {"error": f"Model {svc_name} not active, auto-switch disabled",
                     "status": "not_active", "retry_after": 10},
                    "auto_switch_disabled",
                    headers={"Retry-After": "10"},
                )
            # AUTO_SWITCH on: caller will auto-start

        # 6. Rate limit
        gate = pm.dual_gate.acquire(model_name, timeout=30)
        if not gate.ok:
            return _block(429, {"error": f"Rate limited: {gate.reason}", "status": "rate_limit"}, gate.reason)

        return {
            "blocked": False,
            "gate": gate,
            "ctx": {
                "req_id": req_id, "req_start": req_start, "key_name": key_name,
                "svc_name": svc_name, "model_obj": model_obj, "port": port,
            },
        }

    def _handle_embeddings(self, pm):
        """Handle OpenAI-compatible /v1/embeddings requests."""
        data = self._read_body()
        if data is None:
            return

        model_name = data.get("model", "")
        if not model_name:
            self._send_json({"error": "model field is required"}, 400)
            return

        # A1/A7: security pipeline (auth → switch guard → AUTO_SWITCH → rate limit)
        guard = self._pipeline_guard(pm, model_name, data, "embedding")
        if guard["blocked"]:
            self._send_json(guard["body"], guard["status"], extra_headers=guard.get("headers"))
            return

        gate = guard["gate"]
        ctx = guard["ctx"]
        svc_name = ctx["svc_name"]
        port = ctx["port"]
        status = 503
        try:
            # Auto-start if not running (guard only passes when AUTO_SWITCH=on)
            if svc_name not in pm.mgr.active_services:
                log.info("Embedding model %s not running — auto-starting", svc_name)
                result = pm.mgr.switch(svc_name)
                if result.get("status") != "switched":
                    msg = result.get("message", "unknown error")
                    log.error("Failed to start embedding model %s: %s", svc_name, msg)
                    self._send_json({"error": f"Failed to start embedding model: {msg}"}, 503)
                    return
                if not pm._wait_healthy(svc_name, timeout=30):
                    self._send_json({"error": f"Embedding model '{svc_name}' failed health check within 30s"}, 503)
                    return
            elif not pm._wait_healthy(svc_name, timeout=10):
                log.warning("Embedding model %s not healthy, attempting restart", svc_name)
                pm.mgr.stop_independent(svc_name)
                result = pm.mgr.switch(svc_name)
                if result.get("status") != "switched" or not pm._wait_healthy(svc_name, timeout=30):
                    self._send_json({"error": f"Embedding model '{svc_name}' failed to restart"}, 503)
                    return

            body = json.dumps(data).encode("utf-8")
            conn = pm.make_conn(port, timeout=30)
            try:
                conn.request("POST", "/v1/embeddings", body=body,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp_body = resp.read()
                status = resp.status
                self.send_response(status)
                for k, v in resp.getheaders():
                    self.send_header(k, v)
                self.end_headers()
                self._safe_write(resp_body)
            except Exception as e:
                log.error("Embedding request failed: %s", e)
                self._send_json({"error": "Upstream unavailable", "detail": str(e)}, 503)
            finally:
                conn.close()
        finally:
            pm.logger.log(RequestLog(
                req_id=ctx["req_id"], key_name=ctx["key_name"], model=model_name,
                status=status, route="local",
                agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                duration_ms=(time.monotonic() - ctx["req_start"]) * 1000,
            ))
            gate.release()

    def _handle_rerank(self, pm):
        """Handle /v1/rerank requests — direct port, same pattern as embeddings."""
        data = self._read_body()
        if data is None:
            return

        model_name = data.get("model", "")
        if not model_name:
            self._send_json({"error": "model field is required"}, 400)
            return

        # A1/A7: security pipeline (auth → switch guard → AUTO_SWITCH → rate limit)
        guard = self._pipeline_guard(pm, model_name, data, "rerank")
        if guard["blocked"]:
            self._send_json(guard["body"], guard["status"], extra_headers=guard.get("headers"))
            return

        gate = guard["gate"]
        ctx = guard["ctx"]
        svc_name = ctx["svc_name"]
        port = ctx["port"]
        status = 503
        try:
            # Auto-start if not running (guard only passes when AUTO_SWITCH=on)
            if svc_name not in pm.mgr.active_services:
                log.info("Rerank model %s not running — auto-starting", svc_name)
                result = pm.mgr.switch(svc_name)
                if result.get("status") != "switched":
                    msg = result.get("message", "unknown error")
                    log.error("Failed to start rerank model %s: %s", svc_name, msg)
                    self._send_json({"error": f"Failed to start rerank model: {msg}"}, 503)
                    return
                if not pm._wait_healthy(svc_name, timeout=30):
                    self._send_json({"error": f"Rerank model '{svc_name}' failed health check within 30s"}, 503)
                    return
            elif not pm._wait_healthy(svc_name, timeout=10):
                log.warning("Rerank model %s not healthy, attempting restart", svc_name)
                pm.mgr.stop_independent(svc_name)
                result = pm.mgr.switch(svc_name)
                if result.get("status") != "switched" or not pm._wait_healthy(svc_name, timeout=30):
                    self._send_json({"error": f"Rerank model '{svc_name}' failed to restart"}, 503)
                    return

            body = json.dumps(data).encode("utf-8")
            conn = pm.make_conn(port, timeout=30)
            try:
                conn.request("POST", "/v1/rerank", body=body,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp_body = resp.read()
                status = resp.status
                self.send_response(status)
                for k, v in resp.getheaders():
                    self.send_header(k, v)
                self.end_headers()
                self._safe_write(resp_body)
            except Exception as e:
                log.error("Rerank request failed: %s", e)
                self._send_json({"error": "Upstream unavailable", "detail": str(e)}, 503)
            finally:
                conn.close()
        finally:
            pm.logger.log(RequestLog(
                req_id=ctx["req_id"], key_name=ctx["key_name"], model=model_name,
                status=status, route="local",
                agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                    x_app=getattr(self, "_agent_hit", None) and self._agent_hit.x_app or "",
                duration_ms=(time.monotonic() - ctx["req_start"]) * 1000,
            ))
            gate.release()


# ─── Threaded HTTP Server ────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ─── Main ─────────────────────────────────────────────────────────

def _validate_admin_token_safety():
    """Validate that admin token configuration is safe before starting.

    F7 semantics: born-token bootstrap provisions the token before this check,
    so an empty _ADMIN_TOKEN in strict mode (IFF_ADMIN_REQUIRED=1) is a hard
    failure (fail-closed double guard). Default mode on loopback stays open
    with a warning. Review PR#15 B1: on a non-loopback bind, default mode now
    enables auth, so the retained non-loopback guard below only fires when
    auth was *explicitly* opted out (IFF_ADMIN_REQUIRED=0) with no token —
    exactly the operator action that must refuse to start.
    """
    if not _ADMIN_TOKEN:
        if _admin_auth_enabled():
            raise RuntimeError(
                "Admin token is empty — control-plane must fail closed. "
                "Born-token bootstrap should have provisioned IFF_ADMIN_TOKEN "
                "in the state-dir secrets.env; set IFF_ADMIN_TOKEN to override."
            )
        if PROXY_HOST not in ("127.0.0.1", "localhost", "::1"):
            # Upstream guard retained: network-bound deployment without a token
            # would expose control-plane routes unauthenticated.
            raise RuntimeError(
                f"Admin token is empty but PROXY_HOST={PROXY_HOST!r} is not localhost. "
                "This would expose control-plane routes without authentication. "
                "Set IFF_ADMIN_TOKEN to a secure value or set PROXY_HOST to 127.0.0.1."
            )
        log.warning(
            "Admin token is empty and admin auth is disabled — "
            "control plane open on loopback. Set IFF_ADMIN_REQUIRED=1 "
            "to enforce token auth."
        )


def _create_server(retries: int = 5, retry_delay: float = 2.0):
    """Create the HTTP server, retrying on EADDRINUSE (stale proxy holding the port).

    A previously-started proxy may still own PROXY_PORT (e.g. after a crash
    without clean shutdown). Bounded retry lets the old process exit and frees
    the port, instead of dying immediately and letting systemd restart-loop
    (Restart=always + RestartSec=5 → crash-restart every 5s, replaying 42k rows
    each time). If the port is still held after `retries`, re-raise OSError
    so the caller can exit non-zero.
    """
    for attempt in range(retries):
        try:
            return ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE and attempt < retries - 1:
                log.warning(
                    "Port %d in use (stale proxy still bound?) — retry %d/%d in %.0fs",
                    PROXY_PORT, attempt + 1, retries, retry_delay,
                )
                time.sleep(retry_delay)
            else:
                log.error("Cannot bind %s:%d: %s", PROXY_HOST, PROXY_PORT, e)
                raise
    raise RuntimeError("unreachable")



# F9/T3：set-autostart 的读-改-写全局锁（评审 PR#19 B1：防并发交错丢更新）。
_AUTOSTART_YAML_LOCK = threading.Lock()


def _run_autostart(mgr):
    """F9/T3（unification，fork 移植）：autostart 模型开机拉起线程体。

    默认 False——不自动启动是安全缺省。评审 PR#19 修复：
    - B2：不做 active_services 预检——SQLite 持久态脏关机后可能陈旧，
      盲信会静默空转；switch() 自带 already_active 健康检查 + GPU 占用/
      port 属主现实校验，交由它裁决，这里只记日志。
    - N4：exclusive 互斥预检——两个 exclusive autostart 反复互踢会白付
      两次 vLLM 全量加载，只启动先到者，其余告警跳过。
    - N5：失败（异常 / switch 拒绝）补 AnomalyEvent，不再只有 INFO 日志。
    - N6：模块级函数 + 按 name 排序确定性顺序，可测。
    """
    time.sleep(2)  # 等服务端口就绪
    models = sorted(getattr(mgr.mgr, "_models", {}).values(), key=lambda m: m.name)
    anomalies = getattr(mgr, "anomalies", None)
    exclusive_taken = None
    for m in models:
        if not getattr(m, "autostart", False):
            continue
        name = m.name
        if getattr(m, "is_exclusive", False) and exclusive_taken is not None:
            log.warning("Autostart %s skipped: exclusive GPU slot taken by %s",
                        name, exclusive_taken)
            if anomalies is not None:
                anomalies.record(AnomalyEvent(
                    category="model", severity="warning", model=name,
                    message=f"Autostart skipped: exclusive GPU slot taken by {exclusive_taken}",
                    possible_cause="多个 exclusive 模型同时设 autostart——只保留先到者，"
                                   "其余改 shared 或去掉 autostart",
                ))
            continue
        log.info("Autostart: launching %s (autostart: true)", name)
        try:
            result = mgr.mgr.switch(name)
        except Exception as e:
            log.error("Autostart %s failed: %s", name, e)
            if anomalies is not None:
                anomalies.record(AnomalyEvent(
                    category="model", severity="error", model=name,
                    message=f"Autostart {name} failed: {e}",
                    possible_cause="vLLM 启动失败 / OOM / 配置错误 / 模型文件损坏。检查网关日志。",
                ))
            continue
        status = (result or {}).get("status")
        if status == "ok":
            log.info("Autostart %s -> ok", name)
        elif status == "already_active":
            # B2：真已驻留（或 DB 陈旧但现实校验通过）——记录即可，不视为失败
            log.info("Autostart %s skipped: already active (verified by switch)", name)
        else:
            log.warning("Autostart %s -> %s: %s", name, status,
                        (result or {}).get("message", ""))
            if anomalies is not None:
                anomalies.record(AnomalyEvent(
                    category="model", severity="error", model=name,
                    message=f"Autostart {name} -> {status}: {(result or {}).get('message', '')}",
                    possible_cause="switch 被拒（GPU 占用 / 内存不足 / 切换中）。"
                                   "检查网关日志或 iff status。",
                ))
        if status in ("ok", "already_active") and getattr(m, "is_exclusive", False):
            exclusive_taken = name


def main():
    # R7: --async flag → aiohttp async server
    if "--async" in sys.argv:
        from inferfabric.proxy.async_server import start_async
        start_async()
        return

    import traceback

    def _global_excepthook(exc_type, exc_value, exc_tb):
        logging.critical('Unhandled exception (%s): %s', exc_type.__name__, exc_value)
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = _global_excepthook

    def _thread_excepthook(args):
        logging.critical('Unhandled thread exception: %s', args.exc_value)
    threading.excepthook = _thread_excepthook

    # Purge stale .pyc to prevent version-desync (INF-001649)
    import shutil
    pkg_dir = Path(__file__).resolve().parent.parent
    pycache = pkg_dir / "__pycache__"
    if pycache.is_dir():
        shutil.rmtree(pycache, ignore_errors=True)
        logging.getLogger("inferfabric").debug("Purged stale __pycache__: %s", pycache)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    log_dir = Path.home() / ".inferfabric" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_dir / "proxy.log", maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logging.getLogger("inferfabric").addHandler(fh)

    # F7 born-token: provision the control-plane token before the safety check
    # (0600 secrets.env under the state dir; idempotent, never rotated).
    # Review PR#15 N2: do NOT seed os.environ with the token — engine
    # subprocesses (vllm/sglang/docker) inherit the environment and would each
    # receive the control-plane secret. In-process consumers read the module
    # global; out-of-process consumers (e.g. iff remove) read secrets.env.
    from inferfabric.proxy_manager import IFF_DATA_DIR
    global _ADMIN_TOKEN
    _ADMIN_TOKEN = _ensure_admin_token(IFF_DATA_DIR / "secrets.env")
    _validate_admin_token_safety()

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    # Start runtime health watchdog
    watchdog = ModelWatchdog(mgr.mgr, check_interval=30, auto_restart=True)
    watchdog.start()

    server = _create_server()
    server.proxy_mgr = mgr
    server.watchdog = watchdog
    # PK4 T1: pack uninstall production wiring (real callback injection +
    # pm.pack_manager mount)
    from inferfabric.proxy.pack_handlers import attach_pack_uninstaller
    try:
        attach_pack_uninstaller(mgr)
    except Exception as exc:   # wiring failure must not block gateway start; uninstall API will honestly 404
        logging.getLogger("inferfabric.packs").warning(
            "pack uninstaller attach failed: %s", exc)

    _notify_socket = os.environ.get('NOTIFY_SOCKET')
    _notify_enabled = bool(_notify_socket)

    def sd_notify(message: str):
        if not _notify_socket:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(_notify_socket)
            sock.sendall(message.encode())
            sock.close()
        except Exception:
            pass

    def handle_signal(signum, frame):
        log.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # v5.2: Unified hot-reload via ConfigReloader
    from inferfabric.config_reloader import build_config_reloader
    config_reloader = build_config_reloader(mgr, auth=mgr.auth, cloud=mgr.cloud)
    mgr.config_reloader = config_reloader
    config_reloader.setup()

    # F9/T3：autostart 模型开机拉起（daemon 线程，失败不阻断启动）。
    # 线程体为模块级 _run_autostart（评审 PR#19 N6：可测）。
    threading.Thread(target=_run_autostart, args=(mgr,), daemon=True,
                     name="inferfabric-autostart").start()

    def watchdog_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(WATCHDOG_INTERVAL)
            if not shutdown_event.is_set():
                sd_notify("WATCHDOG=1")

    if _notify_enabled:
        threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()

    def health_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if not shutdown_event.is_set():
                mgr.health_check()

    try:
        rec = mgr.mgr.reconcile()
        if rec.get("actions"):
            log.info("Startup reconcile: %s", rec["actions"])
    except Exception as e:
        log.warning("Startup reconcile failed: %s", e)

    threading.Thread(target=health_loop, daemon=True, name="health").start()

    # Start token stats collector (5 min interval). 用 TelemetryHub 里带 db 的
    # collector（DB 驱动、引擎无关、双 scope），而非裸 db-less collector。
    mgr.telemetry.start_token_collector(lambda: mgr.mgr)

    sd_notify("READY=1")
    log.info("InferFabric Proxy: %s:%d (auto_switch=%s, threaded, v%s)",
             PROXY_HOST, PROXY_PORT, mgr.auto_switch, __version__)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("GPU mode: %s | Services: %s", mgr.mgr.gpu_mode, mgr.mgr.active_services)

    try:
        while not shutdown_event.is_set():
            # handle_request() blocks indefinitely when no connections arrive,
            # preventing shutdown_event from being checked.
            # Use select with timeout to make the loop responsive to SIGTERM.
            import select as _select
            readable, _, _ = _select.select([server.socket], [], [], 1.0)
            if readable:
                server.handle_request()
            # If no readable sockets, loop back and check shutdown_event
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Closing server...")
        watchdog.stop()
        mgr.telemetry.token_collector.stop()
        try:
            server.server_close()
        except Exception:
            pass
        log.info("Shutdown complete")
        sd_notify("STOPPING=1")
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
