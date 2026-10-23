"""inferfabric/config_reloader.py — Unified config hot-reload (v5.2).

Replaces: SIGHUP handler in handler.py + config_watcher.detect_drift().
Single entry point for model/auth/cloud/dashboard config reload.
"""

import logging
import signal
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inferfabric.manager import ModelManager
    from inferfabric.proxy.auth import AuthManager
    from inferfabric.cloud_discovery import CloudDiscovery
    from inferfabric.dashboard import invalidate_cache

log = logging.getLogger("inferfabric.config_reloader")


def build_config_reloader(mgr, auth=None, cloud=None) -> "ConfigReloader":
    """装配 ConfigReloader（含 agents / model_pools 域 registry）。threaded
    main() 与 async start_async() 两条服务路径共用，防止任一路径漏接
    agent_registry / pool_registry 而分叉。

    mgr: ProxyManager（.mgr/.auth/.cloud/.agent_registry/.pool_registry 属性）。
    """
    return ConfigReloader(mgr.mgr, auth=auth, cloud=cloud,
                          registry=getattr(mgr, "agent_registry", None),
                          pool_registry=getattr(mgr, "pool_registry", None))


class ConfigReloader:
    """Unified config hot-reload with signal handling (v5.2).

    Usage:
        reloader = ConfigReloader(mgr, auth, cloud)
        reloader.setup()
    """

    def __init__(self, mgr: "ModelManager", auth: "AuthManager" = None,
                 cloud: "CloudDiscovery" = None, registry=None,
                 pool_registry=None):
        self._mgr = mgr
        self._auth = auth
        self._cloud = cloud
        self._registry = registry
        self._pool_registry = pool_registry
        self._last_reload = 0.0
        self._cooldown = 5.0  # seconds

    def setup(self):
        """Register SIGHUP / SIGUSR1 handlers."""
        try:
            signal.signal(signal.SIGHUP, self._on_signal)
            signal.signal(signal.SIGUSR1, self._on_signal)
            log.info("ConfigReloader: SIGHUP/SIGUSR1 handlers registered")
        except ValueError:
            # Not in main thread — skip signal registration
            pass

    def _on_signal(self, signum, frame):
        if signum == signal.SIGHUP:
            self.reload_all()
        elif signum == signal.SIGUSR1:
            self.reload_models()

    def reload_all(self):
        """Full reload: models + auth + cloud + dashboard cache.

        Returns: list of failed domain names (["models"], ["cloud"], ...) —
        empty list means all domains reloaded successfully. Callers
        (/reload-config) report the truth instead of an unconditional
        "reloaded" (B2: cloud reload 曾静默失效但端点仍声称成功).
        """
        now = time.time()
        if now - self._last_reload < self._cooldown:
            return []
        self._last_reload = now
        failed = []
        try:
            self._mgr.reload_models()
            log.info("ConfigReloader: models reloaded")
        except Exception as e:
            failed.append("models")
            log.error("ConfigReloader: model reload failed: %s", e)
        if self._auth:
            try:
                self._auth.reload()
            except Exception as e:
                failed.append("auth")
                log.error("ConfigReloader: auth reload failed: %s", e)
        if self._cloud:
            try:
                self._cloud.reload()
            except Exception as e:
                failed.append("cloud")
                log.error("ConfigReloader: cloud reload failed: %s", e)
        if self._registry:
            try:
                if not self._registry.reload():
                    failed.append("agents")
                    log.error("ConfigReloader: agent registry reload FAILED")
            except Exception as e:
                failed.append("agents")
                log.error("ConfigReloader: agent registry reload failed: %s", e)
        # 池重载最后执行（评审 #20）：候选解析依赖 models.d 与 cloud provider
        # 的最新状态（known_local / known_providers / provider base），先重载
        # 依赖域再编译池快照，否则外部编辑会被旧的依赖视图拒绝或错判
        if self._pool_registry:
            try:
                # 三态（终审 #19）：True=已应用；None=无文件/未变化（正常）；
                # False=文件损坏/不可读 → 报告失败域，保留上一份好快照
                result = self._pool_registry.reload_from_disk()
                if result is False:
                    failed.append("model_pools")
                    log.error("ConfigReloader: model_pools reload rejected "
                              "invalid file — keeping last good snapshot")
            except Exception as e:
                failed.append("model_pools")
                log.error("ConfigReloader: model_pools reload failed: %s", e)
        # Invalidate dashboard cache
        try:
            from inferfabric.dashboard import invalidate_cache
            invalidate_cache()
        except Exception:
            log.warning("Dashboard cache invalidation failed")
        if failed:
            log.warning("ConfigReloader: full reload complete with failures: %s", failed)
        else:
            log.info("ConfigReloader: full reload complete")
        return failed

    def reload_models(self):
        """Reload models only (SIGUSR1)."""
        try:
            self._mgr.reload_models()
            log.info("ConfigReloader: models reloaded (SIGUSR1)")
            from inferfabric.dashboard import invalidate_cache
            invalidate_cache()
        except Exception as e:
            log.error("ConfigReloader: model reload failed: %s", e)