"""
inferfabric/proxy_manager.py — Model switching, health check, and request routing.

Extracted from proxy.py for modularity.
"""

import itertools
import json as _json
import uuid
import logging
import os
import threading
import time
from http.client import HTTPConnection
from typing import Optional

import yaml as _yaml

from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode
from inferfabric.config import MODELS_DIR, ConfigError
from inferfabric.proxy.auth import AuthManager
from inferfabric.agent_registry import AgentRegistry
from inferfabric.cloud_discovery import CloudDiscovery, CloudModel
from inferfabric.ratelimit import DualGateLimiter, RateLimiterV2
from inferfabric.metrics_aggregator import CloudModelPrice

# R5: 添加 _deps 到路径（cachetools 等额外依赖）
_deps_path = os.path.join(os.path.dirname(__file__), "..", "_deps")
if os.path.isdir(_deps_path):
    import sys as _sys
    _sys.path.insert(0, _deps_path)
from pathlib import Path as _Path

# IFF data directory (consistent with config.py / token_stats.py)
from inferfabric.config import IFF_DATA_DIR

log = logging.getLogger("inferfabric.proxy_manager")


# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
# Default ON (v6.0.0): request-driven auto-switching is the expected UX for a
# personal single-GPU inference OS — a request to a known-but-inactive model
# should auto-start it. Set EDGE_AUTO_SWITCH=0 to disable (e.g. to prevent a
# stray request from displacing the active exclusive model).
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20


class ProxyManager:
    """Manages model switching + request routing (v4.0: model-plugin)."""

    def __init__(self, mgr: Optional["ModelManager"] = None, models_dir: str | None = None):
        self.mgr = mgr if mgr is not None else ModelManager(models_dir or str(MODELS_DIR))
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        # PR-A: Auth manager
        self.auth = AuthManager(IFF_DATA_DIR / "api_keys.yaml")
        # v6.5: 客户端 Agent 识别引擎（agents.d 双目录；用户目录 ~/.inferfabric/agents.d）
        self.agent_registry = AgentRegistry(
            builtin_dir=_Path(__file__).parent / "agents.d",
            user_dir=IFF_DATA_DIR / "agents.d",
        )
        # PR-D: Cloud discovery (must init before aggregator for price config)
        self.cloud = CloudDiscovery(IFF_DATA_DIR / "cloud_provider.yaml")
        self._cloud_discovered = False
        # v4.6.2: Runtime config (iff.yaml overrides)
        self._runtime_config = self._load_runtime_config()
        # v4.6.3: DualGateLimiter — 可配置流控 (PR-G4/G1/G3)
        rate_cfg = self._runtime_config.get("rate_limit", {})
        rate_mode = rate_cfg.get("mode", "observe")
        server_rpm = rate_cfg.get("server_rpm", 0)
        model_rpm_default = rate_cfg.get("model_rpm_default", 0)
        rate_timeout = rate_cfg.get("timeout", 5)
        max_concurrent_cfg = rate_cfg.get("max_concurrent", "auto")
        if max_concurrent_cfg == "auto":
            max_concurrent = self._compute_max_concurrent()
        else:
            max_concurrent = int(max_concurrent_cfg)
        global_max_concurrent = int(rate_cfg.get("global_max_concurrent", 0))
        self.dual_gate = DualGateLimiter(
            rpm_limiter=RateLimiterV2(
                server_rpm=server_rpm,
                model_rpm_default=model_rpm_default,
                timeout=rate_timeout,
            ),
            max_concurrent=max_concurrent,
            mode=rate_mode,
            timeout=rate_timeout,
            global_max_concurrent=global_max_concurrent,
        )
        log.info(
            "Rate limit: mode=%s server_rpm=%s model_rpm_default=%s max_concurrent=%d global_max=%d timeout=%ds",
            rate_mode, server_rpm, model_rpm_default, max_concurrent, global_max_concurrent, rate_timeout,
        )
        # v5.2: HealthMonitor — delegated health checking
        from inferfabric.health_monitor import HealthMonitor
        self.health_monitor = HealthMonitor(self.mgr, self.mgr.state)
        self.health_monitor.start()
        from inferfabric.telemetry import TelemetryHub
        self.telemetry = TelemetryHub(IFF_DATA_DIR, self._runtime_config)
        self.logger = self.telemetry.logger
        self.metrics = self.telemetry.metrics
        # R9: AnomalyCollector — 结构化异常事件（线程安全环形缓冲）
        from inferfabric.anomaly_collector import AnomalyCollector
        self.anomalies = AnomalyCollector()
        # R5: ResponseCache — 精确匹配响应缓存（IFF_CACHE_ENABLED 环境变量可覆盖 YAML）
        _cache_env = os.environ.get("IFF_CACHE_ENABLED")
        if _cache_env is not None:
            _cache_enabled = _cache_env.lower() in ("1", "true", "yes")
        else:
            _cache_enabled = self._runtime_config.get("cache", {}).get("enabled", True)
        if _cache_enabled:
            from inferfabric.proxy.response_cache import ResponseCache
            self.response_cache = ResponseCache(maxsize=self._runtime_config.get("cache", {}).get("max_entries", 500))
        else:
            self.response_cache = None
        # R-AS: Auto Switch 实例态 — 读取优先级: 显式 env > iff.yaml > 默认开
        self.auto_switch, self._auto_switch_source = self._resolve_auto_switch(self._runtime_config)
        # D-2: Build served_name → friendly_name mapping for dashboard
        self._metrics_name_map = {}
        for m in self.mgr._models.values():
            sn = m.served_name
            if sn and sn != m.name:
                self._metrics_name_map[sn] = m.name
        self.telemetry.update_metrics_name_map(self._metrics_name_map)
        # PR-B: Helper to create request context
        self._req_counter = itertools.count()
        # R7: 多副本选择器缓存
        self._selectors: dict = {}

    def new_request_id(self) -> str:
        """Generate a unique, thread-safe request ID for logging.

        Format: {8-hex-counter}-{8-hex-uuid} (e.g. ``00000001-a3b4f2c1``).
        The atomic counter guarantees uniqueness across threads; the random
        suffix further eliminates any risk of collision across process restarts.
        """
        return f"{next(self._req_counter):08x}-{uuid.uuid4().hex[:8]}"

    def ensure_cloud_discovered(self):
        """首次请求时触发云端模型发现（懒加载）+ 启动后台轮询。"""
        if not self._cloud_discovered and self.cloud.providers:
            self.cloud.discover_all()
            self._cloud_discovered = True
            log.info("Cloud discovery completed: %d models", len(self.cloud.cloud_models))
            # Start background polling for model updates
            self.cloud.start_polling()
            # G-2: Update price config now that cloud models are available
            self.metrics.update_prices(self._load_price_config())

    @staticmethod
    def _resolve_auto_switch(runtime_config: dict) -> tuple:
        """R-AS: 解析 auto_switch → (value, source)。

        优先级: 显式 env EDGE_AUTO_SWITCH > iff.yaml auto_switch.enabled > 默认开。
        source ∈ {"env", "file", "default"} 供 UI snapshot 展示来源。
        """
        _as_env = os.environ.get("EDGE_AUTO_SWITCH")
        if _as_env is not None:
            return _as_env == "1", "env"
        _as_cfg = (runtime_config or {}).get("auto_switch", {})
        if isinstance(_as_cfg, dict) and "enabled" in _as_cfg:
            return bool(_as_cfg["enabled"]), "file"
        return True, "default"

    def set_auto_switch(self, enabled: bool) -> dict:
        """R-AS: 切换自动切换（立即生效，无需重启 proxy）。

        读取优先级: 显式 env > iff.yaml > 默认开。本写入:
          1. 实例态 auto_switch（handler/chat_handlers 的活读点）
          2. 模块级 AUTO_SWITCH 镜像（保住函数作用域 import 读点的正确性）
          3. iff.yaml auto_switch.enabled 持久化（重启后无 env 锁时按此恢复）
        env 显式设置（env_locked）时本次写入只作用于当前进程生命周期，
        重启后回到 env 值 —— hint 返回给 UI 提示。env_locked 按调用时实时
        env 判定（非 import 期冻结值），避免 systemd/环境变化后语义漂移。
        """
        enabled = bool(enabled)
        env_locked = os.environ.get("EDGE_AUTO_SWITCH") is not None
        self.auto_switch = enabled
        import inferfabric.proxy_manager as _self_mod
        _self_mod.AUTO_SWITCH = enabled
        self._persist_auto_switch(enabled)
        log.info("Auto switch → %s (source=%s, env_locked=%s)",
                 enabled, self._auto_switch_source, env_locked)
        return {
            "auto_switch": enabled,
            "source": self._auto_switch_source,
            "env_locked": env_locked,
            "hint": ("环境变量 EDGE_AUTO_SWITCH 已显式设置，重启 proxy 后将回到 env 值"
                     if env_locked else None),
        }

    def _persist_auto_switch(self, enabled: bool):
        """把 auto_switch.enabled 写入 iff.yaml（文本级合并，保留注释与其他键；幂等）。"""
        import re
        path = IFF_DATA_DIR / "iff.yaml"
        text = path.read_text() if path.exists() else ""
        block = "auto_switch:\n  enabled: %s\n" % str(bool(enabled)).lower()
        if re.search(r"^auto_switch:\s*$", text, re.M):
            # 替换已有块: 从 'auto_switch:' 行到下一个顶层行（非空白开头）或文件尾
            pattern = re.compile(r"^auto_switch:.*?(?=^\S|\Z)", re.M | re.S)
            text = pattern.sub(block, text, count=1)
        else:
            text = text.rstrip("\n")
            if text:
                text += "\n"
            text += ("\n# R-AS: 自动切换（推理 tab UI 开关；优先级: 显式 env > 本文件 > 默认开）\n"
                    + block)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except Exception:
            log.error("Failed to persist auto_switch into iff.yaml", exc_info=True)

    def _compute_max_concurrent(self) -> int:
        """从 vLLM 模型配置中取 max_num_seqs 最大值作为并发上限。

        确保 IFF 的并发限制不低于 vLLM 的处理能力，避免人为瓶颈。
        仅考虑本地 vLLM 模型（cloud 模型不走本地并发门）。
        """
        max_seqs = 4  # 保守默认
        for model in self.mgr._models.values():
            if model.is_vllm and model.vllm:
                max_seqs = max(max_seqs, model.vllm.max_num_seqs)
        log.debug("Computed max_concurrent=%d from vLLM configs", max_seqs)
        return max_seqs

    def _validate_runtime_config(self, config: dict):
        """Validate iff.yaml schema. Raises ConfigError on invalid values.

        Required fields and constraints:
          - rate_limit.mode: "observe" or "reject"
          - rate_limit.timeout: int > 0
          - rate_limit.server_rpm: int >= 0
          - rate_limit.model_rpm_default: int >= 0
          - rate_limit.max_concurrent: "auto" or int > 0
          - access_log_jsonl: bool
          - request_log_retention_days: int > 0
          - tts.enabled: bool (optional)
          - tts.port: int > 0 (optional)
          - asr.enabled: bool (optional)
          - asr.port: int > 0 (optional)
        """
        from inferfabric.config import ConfigError

        rate_cfg = config.get("rate_limit", {})
        if not isinstance(rate_cfg, dict):
            raise ConfigError("rate_limit must be a mapping")

        mode = rate_cfg.get("mode", "observe")
        if mode not in ("observe", "reject"):
            raise ConfigError(f"rate_limit.mode must be 'observe' or 'reject', got {mode!r}")

        timeout = rate_cfg.get("timeout", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError(f"rate_limit.timeout must be int > 0, got {timeout!r}")

        server_rpm = rate_cfg.get("server_rpm", 0)
        if isinstance(server_rpm, bool) or not isinstance(server_rpm, int) or server_rpm < 0:
            raise ConfigError(f"rate_limit.server_rpm must be int >= 0, got {server_rpm!r}")

        model_rpm_default = rate_cfg.get("model_rpm_default", 0)
        if isinstance(model_rpm_default, bool) or not isinstance(model_rpm_default, int) or model_rpm_default < 0:
            raise ConfigError(f"rate_limit.model_rpm_default must be int >= 0, got {model_rpm_default!r}")

        max_concurrent = rate_cfg.get("max_concurrent", "auto")
        if isinstance(max_concurrent, bool):
            raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int > 0, got {max_concurrent!r}")
        elif isinstance(max_concurrent, str):
            if max_concurrent != "auto":
                raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int > 0, got {max_concurrent!r}")
        elif isinstance(max_concurrent, int):
            if max_concurrent <= 0:
                raise ConfigError(f"rate_limit.max_concurrent must be int > 0, got {max_concurrent}")
        else:
            raise ConfigError(f"rate_limit.max_concurrent must be 'auto' or int, got {type(max_concurrent).__name__}")

        if "access_log_jsonl" in config:
            if not isinstance(config["access_log_jsonl"], bool):
                raise ConfigError(f"access_log_jsonl must be bool, got {config['access_log_jsonl']!r}")

        if "request_log_retention_days" in config:
            rd = config["request_log_retention_days"]
            if isinstance(rd, bool) or not isinstance(rd, int) or rd <= 0:
                raise ConfigError(f"request_log_retention_days must be int > 0, got {rd!r}")

        # Validate asr/tts local service config
        for svc_name in ("asr", "tts"):
            svc_cfg = config.get(svc_name)
            if svc_cfg is None:
                continue
            if not isinstance(svc_cfg, dict):
                raise ConfigError(f"{svc_name} must be a mapping, got {type(svc_cfg).__name__}")
            if "enabled" in svc_cfg and not isinstance(svc_cfg["enabled"], bool):
                raise ConfigError(f"{svc_name}.enabled must be bool, got {svc_cfg['enabled']!r}")
            if "port" in svc_cfg:
                p = svc_cfg["port"]
                if isinstance(p, bool) or not isinstance(p, int) or p <= 0:
                    raise ConfigError(f"{svc_name}.port must be int > 0, got {p!r}")

        # R-AS: auto_switch.enabled: bool (optional, 默认开)
        as_cfg = config.get("auto_switch")
        if as_cfg is not None:
            if not isinstance(as_cfg, dict):
                raise ConfigError(f"auto_switch must be a mapping, got {type(as_cfg).__name__}")
            if "enabled" in as_cfg and not isinstance(as_cfg["enabled"], bool):
                raise ConfigError(f"auto_switch.enabled must be bool, got {as_cfg['enabled']!r}")

    def _load_runtime_config(self) -> dict:
        """从 iff.yaml 加载运行时配置，不存在时返回空 dict。

        支持的配置项:
          - access_log_jsonl: bool (默认 True)
          - request_log_retention_days: int (默认 90)
          - rate_limit.mode: "observe" | "reject" (默认 observe)
          - rate_limit.server_rpm: int (默认 0=不限流)
          - rate_limit.model_rpm_default: int (默认 0=不限流)
          - rate_limit.max_concurrent: "auto" | int (默认 auto)
          - rate_limit.timeout: int (默认 5)
          - auto_switch.enabled: bool (默认 True; 优先级低于显式 env EDGE_AUTO_SWITCH)
        """
        config_path = IFF_DATA_DIR / "iff.yaml"
        if not config_path.exists():
            return {}
        try:
            with open(config_path) as f:
                cfg = _yaml.safe_load(f)
            if not cfg or not isinstance(cfg, dict):
                return {}
            self._validate_runtime_config(cfg)
            return cfg
        except ConfigError as e:
            log.warning("Invalid iff.yaml configuration: %s — using defaults", e)
            return {}
        except Exception:
            log.warning("Failed to load iff.yaml — using defaults", exc_info=True)
            return {}

    def _load_price_config(self) -> dict[str, CloudModelPrice]:
        """从 cloud_provider.yaml 加载价格配置（cloud_models + provider model_specs）"""
        prices = {}
        try:
            if hasattr(self, 'cloud') and self.cloud:
                # 优先从 CloudModel 实例读取（含 spec-only 注册模型）
                for model_id, model in self.cloud.cloud_models.items():
                    if "/" in model_id:
                        continue  # 跳过 provider-prefixed 键
                    if model.price_input > 0 or model.price_output > 0:
                        prices[model_id] = CloudModelPrice(
                            price_input=model.price_input,
                            price_output=model.price_output,
                        )
                # 回退：从 provider model_specs 补充（尚未注册为 CloudModel 的）
                for _pname, pcfg in self.cloud._providers.items():
                    for mid, spec in pcfg.model_specs.items():
                        if mid in prices:
                            continue
                        pi = spec.get("price_input", 0)
                        po = spec.get("price_output", 0)
                        if pi > 0 or po > 0:
                            prices[mid] = CloudModelPrice(
                                price_input=float(pi),
                                price_output=float(po),
                            )
        except Exception as e:
            log.warning("Failed to load price config: %s", e)
        return prices

    @property
    def current(self) -> str:
        """Current active service or 'idle'."""
        return self.mgr.current_service

    def model_to_service(self, model_name: str):
        """Map served_model_name to model config name."""
        m = self.mgr.find_model_by_served_name(model_name)
        if m:
            log.debug("model_to_service: %s → %s", model_name, m.name)
            return m.name
        return None

    def _wait_healthy(self, target: str, timeout: float = 180) -> bool:
        """Wait for a model to become healthy after switch."""
        model = self.mgr.get_model(target)
        if not model:
            return False
        port = model.port
        if not port:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = None
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    conn.close()
                    log.info("Model %s healthy on :%d", target, port)
                    return True
                resp.close()
            except Exception:
                pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(2)
        log.warning("Model %s not healthy after %.0fs", target, timeout)
        return False

    def ensure_service(self, target: str) -> bool:
        """Ensure a model is running, auto-switch if needed.

        Uses _switch_lock only for switch initiation (seconds), not health
        wait (up to 500s).  manager.switch() owns the authoritative
        switching_target gate; this method only has a same-target fast-path
        that waits for the already-in-progress switch to complete.
        """
        if target in self.mgr.active_services:
            return True
        if self.mgr.state.is_manually_stopped(target):
            log.info("Auto-switch to %s blocked: manually stopped by user", target)
            return False

        # -- Fast-path: already switching to the same target → wait --
        switching = self.mgr.state.get("switching_target") or ""
        if switching == target:
            log.info("Auto-switch to %s -- already switching, waiting", target)
            return self._wait_healthy(target)

        # -- Core: hold _switch_lock only for switch initiation --
        # (manager.switch() enforces the authoritative switching_target gate)
        if not self._switch_lock.acquire(timeout=0):
            log.warning("Switch already in progress, rejecting")
            return None
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch → %s", target)
            self.mgr.state.set("switching_target", target)
            try:
                result = self.mgr.switch(target)
            except Exception:
                # M3: mgr.switch raised → clear switching_target so other
                # auto-switches aren't 503-blocked until a reconcile. Re-raise
                # to preserve the original exception-propagation semantics.
                log.warning("Auto-switch to %s raised; clearing switching_target", target)
                self.mgr.state.set("switching_target", "")
                raise
            ok = result["status"] == "switched"
            if ok:
                self._last_switch = time.time()
            else:
                # R0: a FAILED switch also arms the cooldown, so the next
                # ensure_service call skips (returns False) instead of retrying
                # an in-flight/failed switch forever (infinite 503 storm).
                self._last_switch = time.time()
                self.mgr.state.set("switching_target", "")
                return result["status"] in ("switched", "already_active")
        finally:
            self._switch_lock.release()

        # -- Health wait OUTSIDE lock (up to 500s) --
        healthy = self._wait_healthy(target)
        self.mgr.state.set("switching_target", "")
        return healthy

    def get_target_port(self, model_name: str):
        """Get port for a served_model_name (R7: uses ReplicaSelector if replicas configured)."""
        m = self.mgr.find_model_by_served_name(model_name)
        if not m:
            return None
        port = getattr(m, 'port', None)
        replicas = getattr(m, 'replicas', None)
        if isinstance(replicas, (list, tuple)) and replicas:
            selector = self._get_selector(m.name)
            if selector:
                return selector.select()
        return port

    def release_port(self, model_name: str, port: int):
        """释放副本并发计数（R7）。"""
        selector = self._selectors.get(model_name)
        if selector:
            selector.release(port)

    def _get_selector(self, model_name: str):
        """懒创建 ReplicaSelector。"""
        from inferfabric.proxy.replica_selector import ReplicaInfo, ReplicaSelector
        if model_name not in self._selectors:
            m = self.mgr.get_model(model_name)
            replicas = getattr(m, 'replicas', None) if m else None
            if not isinstance(replicas, (list, tuple)) or not replicas:
                return None
            strategy = self._runtime_config.get("load_balance", {}).get("strategy", "least_busy")
            self._selectors[model_name] = ReplicaSelector(
                [ReplicaInfo(port=p) for p in replicas], strategy)
        return self._selectors.get(model_name)

    def make_conn(self, port: int, timeout: int = 300) -> HTTPConnection:
        """Create new HTTP connection per request — no pool (thread-safe).

        Each thread gets its own connection to vLLM, avoiding race conditions.
        vLLM handles concurrent connections natively.
        """
        return HTTPConnection("127.0.0.1", port, timeout=timeout)

    def health_check(self):
        return self.health_monitor.health_check()

    def _clean_manual_stops(self):
        self.health_monitor._clean_manual_stops()
