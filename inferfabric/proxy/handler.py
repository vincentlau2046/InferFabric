"""
inferfabric/proxy/handler.py — ProxyHandler, ThreadedHTTPServer, main.

Core HTTP handler with routing, dashboard, and delegation to:
  chat_handlers.py — chat completions
  metrics.py — vLLM Prometheus metrics

Extracted from proxy.py (v4.1 P3 split).
"""

import errno
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
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from http.client import HTTPConnection
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses

from inferfabric.state import GPUMode
from inferfabric.agent_registry import request_protocol
from inferfabric.agent_stats import AGENT_GRAN, aggregate_agent_stats
from inferfabric.metrics_aggregator import cost_of_row
from inferfabric.proxy.request_logger import RequestLog
from inferfabric.anomaly_collector import AnomalyEvent

# Admin token for control-plane routes (/switch, /stop, /deploy, /pull, etc.)
# If set, requests must include X-Admin-Token header matching this value.
# If not set (default), all control routes are open (localhost-only binding is the security boundary).
_ADMIN_TOKEN = os.environ.get("IFF_ADMIN_TOKEN", "")
from inferfabric.proxy_manager import (
    ProxyManager, PROXY_HOST, PROXY_PORT,
    HEALTH_CHECK_INTERVAL, WATCHDOG_INTERVAL,
)
from inferfabric import forwarder, __version__
from inferfabric.proxy.chat_handlers import handle_chat, handle_ollama_native
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


def _compute_expensive(pm) -> dict:
    """C2: 采集昂贵字段组（mgr.status 健康探测 + metrics 24h 扫描）。
    各组独立兜底（单组失败不拖垮另一组）。"""
    exp = {}
    try:
        exp["status"] = pm.mgr.status()
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

_GET_ROUTES = {
    "/":                        lambda h, pm: h._serve_dashboard(pm),
    "/health":                  lambda h, pm: h._send_json({"status": "ok", "gpu_mode": pm.mgr.gpu_mode}),
    "/status":                  lambda h, pm: h._send_json(pm.mgr.status()),
    "/models":                  lambda h, pm: h._send_json(pm.mgr.list_models()),
    "/profiles":                lambda h, pm: (log.warning("/profiles is deprecated"), h._send_json(pm.mgr.list_models()))[1],
    "/local-models":            lambda h, pm: h._send_json({"discovered": [], "configured": list(pm.mgr._models.keys())}),
    "/v1/models":               lambda h, pm: h._handle_v1_models(pm),
    "/system":                  lambda h, pm: h._send_json(h._system_info()),
    "/api/metrics":             lambda h, pm: h._handle_api_metrics(pm),
    "/api/latency":           lambda h, pm: h._handle_api_latency(pm),
    "/api/power":             lambda h, pm: h._handle_api_power(pm),
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
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/admin/cloud/presets":     _admin(lambda h, pm: h._handle_cloud_presets(pm)),
    "/admin/tune/preview":      _admin(lambda h, pm: h._handle_tune_preview(pm)),
    "/admin/tune/scenarios":    _admin(lambda h, pm: h._handle_tune_scenarios(pm)),
    "/api/openapi.json":        _serve_api_spec,
}

_POST_ROUTES = {
    "/v1/chat/completions":     lambda h, pm: h._handle_chat(pm),
    "/v1/completions":          lambda h, pm: h._handle_chat(pm),
    "/v1/messages":             lambda h, pm: h._handle_messages(pm),
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
    "/admin/gpu-clear":        _admin(lambda h, pm: h._handle_gpu_clear(pm)),
    "/api/agents":             _admin(lambda h, pm: h._handle_post_agents(pm)),
    "/api/agents/reclassify":  _admin(lambda h, pm: h._handle_reclassify(pm)),

    # ─── Admin: Cloud Provider Management (PR-D) ─────────────────
    "/admin/cloud/reload":      _admin(lambda h, pm: h._handle_cloud_reload(pm)),
    "/admin/cloud/discover":    _admin(lambda h, pm: h._handle_cloud_discover(pm)),
    "/admin/cloud/test":        _admin(lambda h, pm: h._handle_cloud_test(pm)),
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/admin/cloud/provider-models": _admin(lambda h, pm: h._handle_cloud_provider_models(pm)),
    "/admin/tune":              _admin(lambda h, pm: h._handle_tune(pm)),
    "/v1/embeddings":           lambda h, pm: h._handle_embeddings(pm),
    "/v1/rerank":               lambda h, pm: h._handle_rerank(pm),
}

_DELETE_ROUTES = {
    "/admin/cloud/providers":   _admin(lambda h, pm: h._handle_cloud_providers(pm)),
    "/api/agents":             _admin(lambda h, pm: h._handle_delete_agent(pm)),
}

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
                cloud_model = pm.cloud.cloud_models.get(
                    requested_model.split("/")[-1] if "/" in requested_model else requested_model
                )
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
                # Only query vLLM services (not ollama_cpp embedding models)
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
        status = exp.get("status") or {}
        metrics_24h = exp.get("metrics_24h") or {}

        # 便宜字段每次轮询都重取（保证 request_log / GPU 温度等实时性，C1）
        system = self._system_info()
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

    def _system_info(self):
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0,
                "gpu_util_pct": 0, "gpu_clock_mhz": 0, "gpu_power_w": 0}
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
        try:
            import subprocess as _sub
            r = _sub.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,clocks.current.graphics,power.draw,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                vals = r.stdout.strip().splitlines()[0].split(",")
                info["gpu_util_pct"] = round(float(vals[0].strip().replace(" ", "")), 1)
                info["gpu_clock_mhz"] = int(vals[1].strip().replace(" ", ""))
                info["gpu_power_w"] = round(float(vals[2].strip().replace(" ", "")), 1)
                if len(vals) >= 4:
                    info["gpu_temp_c"] = round(float(vals[3].strip().replace(" ", "")), 1)
        except Exception:
            pass
        info["version"] = __version__
        return info

    # ─── Control helpers ─────────────────────────────────────────

    def _read_body(self):
        return forwarder.read_body(self)

    def _send_json(self, data, status=200, extra_headers=None):
        forwarder.send_json(self, data, status, extra_headers=extra_headers)

    # ─── Admin Auth ─────────────────────────────────────────────

    def _check_admin(self) -> bool:
        """Check admin token for control-plane routes.
        Returns True if allowed, False if denied (401 sent)."""
        if not _ADMIN_TOKEN:
            return True  # No token configured → open (localhost-only binding is security)
        token = self.headers.get("X-Admin-Token", "")
        if hmac.compare_digest(token, _ADMIN_TOKEN):
            return True
        self._send_json({"error": "Unauthorized", "status": "unauthorized"}, 401)
        return False

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
            failed = pm.config_reloader.reload_all() or []
            body = {"status": "reloaded"}
            if failed:
                # B2: 如实回报失败域，不再无条件声称成功
                body["failed"] = failed
            self._send_json(body)
        else:
            # Fallback: direct reload if ConfigReloader not available
            pm.mgr.reload_models()
            try:
                from inferfabric.dashboard import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
            self._send_json({"status": "reloaded"})

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
        self._send_json({
            "status": "reloaded",
            "providers": len(pm.cloud.providers),
            "cloud_models": len(models),
        })

    def _handle_cloud_discover(self, pm):
        """POST /admin/cloud/discover — 手动触发模型发现。"""
        models = pm.cloud.discover_all()
        pm._cloud_discovered = True
        self._send_json({
            "status": "discovered",
            "cloud_models": len(models),
            "models": [
                {
                    "id": m.model_id,
                    "provider": m.provider,
                    "openai": m.openai_available,
                    "anthropic": m.anthropic_available,
                    "discovered_at": m.discovered_at,
                }
                for m in models.values()
            ],
        })

    def _validate_cloud_test_url(self, url: str, pm) -> tuple:
        """Validate a cloud provider test URL for SSRF protection.

        Returns (is_valid: bool, reason: str, resolved_ips: list[str]).
        The resolved_ips list is returned on success so the caller can
        connect directly to the verified IP (TOCTOU mitigation — prevents
        DNS rebinding between validation and connection).
        Checks:
          - scheme must be https
          - host must not resolve to a private/internal IP
          - host must be a registered cloud provider base URL
        """
        # Parse URL
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "invalid URL", []

        # Only allow HTTPS
        if parsed.scheme != "https":
            return False, "only https URLs are allowed", []

        hostname = parsed.hostname
        if not hostname:
            return False, "missing hostname in URL", []

        # DNS resolve and check for private IPs
        _PRIVATE_NETS = [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / cloud metadata
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),
        ]
        safe_ips = []
        try:
            resolved = socket.getaddrinfo(hostname, None)
            for family, _type, _proto, _canonname, sockaddr in resolved:
                ip_str = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    # IPv4-mapped IPv6: unwrap and check against v4 nets
                    check_ip = ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip
                    for net in _PRIVATE_NETS:
                        if check_ip in net:
                            return False, f"private IP address not allowed: {ip_str}", []
                    safe_ips.append(ip_str)
                except ValueError:
                    pass
        except socket.gaierror:
            return False, f"DNS resolution failed for {hostname}", []

        if not safe_ips:
            return False, "no valid IPs resolved", []

        # Check against registered cloud provider whitelist
        try:
            pm.ensure_cloud_discovered()
            provider_hosts = set()
            for _pname, pcfg in pm.cloud.providers.items():
                for base_field in ("openai_base", "anthropic_base"):
                    base_url = getattr(pcfg, base_field, "") or ""
                    if base_url:
                        try:
                            parsed_base = urlparse(base_url)
                            if parsed_base.hostname:
                                provider_hosts.add(parsed_base.hostname)
                        except Exception:
                            pass
            if hostname not in provider_hosts:
                return False, f"host '{hostname}' is not a registered cloud provider", []
        except Exception as e:
            log.warning("Cloud provider whitelist check failed: %s", e)
            return False, "cloud provider registry unavailable", []

        return True, "ok", safe_ips

    def _handle_cloud_test(self, pm):
        """POST /admin/cloud/test — 测试 Provider 连接。"""
        data = self._read_body()
        if not data:
            self._send_json({"error": "No body"}, 400)
            return
        url = data.get("url", "")
        api_key = data.get("api_key", "")
        if not url:
            self._send_json({"error": "Missing url"}, 400)
            return

        # SSRF validation (returns resolved IPs to prevent DNS rebinding TOCTOU)
        valid, reason, safe_ips = self._validate_cloud_test_url(url, pm)
        if not valid:
            log.warning("Cloud test URL rejected: %s — reason: %s", url, reason)
            self._send_json({"error": f"SSRF check failed: {reason}"}, 400)
            return

        # Use the first resolved IP directly to prevent DNS rebinding
        # between validation and connection (TOCTOU mitigation).
        # We use http.client.HTTPConnection for the TCP socket to the
        # verified IP, then manually wrap with ssl.SSLSocket using
        # server_hostname=<original hostname> for SNI + cert validation.
        resolved_ip = safe_ips[0]
        try:
            import http.client
            import ssl
            parsed = urlparse(url)
            port = parsed.port or 443
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            # Build TCP connection to verified IP, then TLS with original hostname
            ctx = ssl.create_default_context()
            # Step 1: TCP connect to the verified IP (no DNS rebind possible)
            tcp_conn = http.client.HTTPConnection(resolved_ip, port, timeout=15)
            tcp_conn.connect()
            # Step 2: TLS wrap with server_hostname=original hostname (SNI + cert check)
            sock = ctx.wrap_socket(tcp_conn.sock, server_hostname=parsed.hostname)
            tcp_conn.sock = sock
            tcp_conn._http_vsn_str = 'HTTP/1.1'

            headers = {
                "Host": parsed.hostname if not parsed.port else f"{parsed.hostname}:{parsed.port}",
                "Content-Type": "application/json",
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            tcp_conn.request("GET", path, headers=headers)
            resp = tcp_conn.getresponse()
            body = json.loads(resp.read().decode("utf-8")) or {}
            model_count = len(body.get("data", []))
            tcp_conn.close()
            self._send_json({"status": "ok", "model_count": model_count})
        except Exception as e:
            self._send_json({"error": str(e)}, 502)

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
                    # model_specs: [{id, manual}] — manual=true 表示空 spec 手填模型
                    "model_specs": [
                        {"id": mid, "manual": not bool(spec)}
                        for mid, spec in cfg.model_specs.items()
                    ],
                    "candidates": [c.model_id for c in pm.cloud.get_candidates(name)],
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
        """POST /admin/cloud/provider-models — 模型策展（v6.1 Phase 1）。

        body: {provider, action: enable|disable|add|remove, model}
        - enable:  勾选发现候选 → 白名单 + 可路由
        - disable: 取消勾选 → 隐藏（spec/manual 同名仍在 spec 侧）
        - add:     手填模型名 → 空 spec 复用 model_specs 管道（恒可路由）
        - remove:  移除手填模型（不存在 → 404）
        每次操作后 save_config() 持久化 + 刷新内存注册表。
        """
        data = self._read_body()
        if data is None:
            return
        provider = (data.get("provider") or "").strip()
        action = (data.get("action") or "").strip()
        model = (data.get("model") or "").strip()
        valid_actions = {"enable", "disable", "add", "remove"}
        if not provider or not action or not model:
            self._send_json({"error": "Missing provider/action/model"}, 400)
            return
        if action not in valid_actions:
            self._send_json({"error": f"Invalid action '{action}' — expected "
                                      f"{'/'.join(sorted(valid_actions))}"}, 400)
            return
        try:
            if action == "enable":
                count = pm.cloud.enable_model(provider, model)
            elif action == "disable":
                count = pm.cloud.disable_model(provider, model)
            elif action == "add":
                count = pm.cloud.add_manual_model(provider, model)
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

    - If _ADMIN_TOKEN is empty and PROXY_HOST is not localhost → raise RuntimeError
    - If _ADMIN_TOKEN is empty and PROXY_HOST is localhost → warn (acceptable)
    """
    if not _ADMIN_TOKEN:
        if PROXY_HOST in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "Admin token is empty — control-plane routes are open. "
                "This is acceptable for localhost-only binding but insecure for network access. "
                "Set IFF_ADMIN_TOKEN to a secure value."
            )
        else:
            raise RuntimeError(
                f"Admin token is empty but PROXY_HOST={PROXY_HOST!r} is not localhost. "
                "This would expose control-plane routes without authentication. "
                "Set IFF_ADMIN_TOKEN to a secure value or set PROXY_HOST to 127.0.0.1."
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

    _validate_admin_token_safety()

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    # Start runtime health watchdog
    watchdog = ModelWatchdog(mgr.mgr, check_interval=30, auto_restart=True)
    watchdog.start()

    server = _create_server()
    server.proxy_mgr = mgr
    server.watchdog = watchdog

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
