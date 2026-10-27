"""
inferfabric/watchdog.py — Runtime health watchdog for active models.

Periodically checks /health endpoints of active models.
Consecutive failures trigger alerts and optional auto-restart.
"""

import threading
import time
import logging
from typing import Optional

log = logging.getLogger("inferfabric")


class ModelWatchdog:
    """Background thread that monitors active model health.

    Config:
      - check_interval: seconds between health checks (default 30)
      - fail_threshold_alert: consecutive failures before alert (default 3)
      - fail_threshold_restart: consecutive failures before auto-restart (default 5)
      - auto_restart: whether to attempt auto-restart on persistent failure (default True)
      - max_restarts: max consecutive restarts per model before giving up (default 3)
      - restart_cooldown_step: base cooldown in seconds, grows exponentially (default 30)
    """

    def __init__(
        self,
        manager,  # ModelManager instance
        check_interval: float = 30.0,
        fail_threshold_alert: int = 3,
        fail_threshold_restart: int = 5,
        auto_restart: bool = True,
        max_restarts: int = 3,
        restart_cooldown_step: float = 30.0,
        fence_timeout: float = 5.0,
    ):
        self._manager = manager
        self._check_interval = check_interval
        self._fail_threshold_alert = fail_threshold_alert
        self._fail_threshold_restart = fail_threshold_restart
        self._auto_restart = auto_restart
        self._max_restarts = max_restarts
        self._restart_cooldown_step = restart_cooldown_step
        self._fence_timeout = fence_timeout
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._fail_counts: dict[str, int] = {}  # model_name → consecutive_failures
        self._restarting: set[str] = set()  # models currently being restarted
        self._total_restarts: dict[str, int] = {}  # model_name → total restart attempts
        self._restart_cooldowns: dict[str, float] = {}  # model_name → cooldown deadline (monotonic)
        self._lock = threading.Lock()
        # PR-20 评审修复 4：关停围栏。已派发的重启 worker 不受监控线程 join
        # 管辖 —— 没有围栏它们可在关停后继续 reconcile/switch/stop_service，
        # 把重启要保全的引擎杀掉。
        self._workers: set[threading.Thread] = set()
        self._mutation_lock = threading.Lock()

    def start(self):
        """Start the watchdog background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="iff-watchdog")
        self._thread.start()
        log.info("Watchdog started (interval=%.0fs, alert=%d, restart=%d)",
                 self._check_interval, self._fail_threshold_alert, self._fail_threshold_restart)

    def stop(self):
        """Stop the watchdog: monitor thread + fence all restart workers.

        PR-20 评审修复 4：先置 _stop_event，再以 _mutation_lock 作屏障。
        屏障语义：新变更不可能在检查与调用之间溜进（worker 的 check+mutate
        在同一把锁下原子执行）；已在途的单次变更要么在 fence_timeout 内
        完成，要么由进程硬退出截断。stop() 返回后：无在途 worker 变更，
        也不会有新变更开始。
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        # 屏障：等出在途变更（有界）。拿不到锁 = 有变更在途，放弃等待 ——
        # 该变更由硬退出截断；其后续变更会在围栏检查处放弃。
        if self._mutation_lock.acquire(timeout=self._fence_timeout):
            self._mutation_lock.release()
        with self._lock:
            workers = list(self._workers)
        for w in workers:
            w.join(timeout=self._fence_timeout)
        log.info("Watchdog stopped (%d restart worker(s) drained)", len(workers))

    def _fenced(self, fn, *args):
        """评审修复 4：在关停围栏内执行一次 manager 变更。

        check+mutate 在 _mutation_lock 下原子：stop() 先置 _stop_event 再
        用同一把锁作屏障，屏障之后不会有变更通过检查。返回 (ok, result)；
        ok=False = 关停中，变更被拦截未执行。
        """
        with self._mutation_lock:
            if self._stop_event.is_set():
                log.info("Watchdog: shutdown fence — skip %s",
                         getattr(fn, "__name__", fn))
                return False, None
            return True, fn(*args)

    def _run(self):
        """Main watchdog loop."""
        while not self._stop_event.wait(self._check_interval):
            try:
                self._check_all()
            except Exception as e:
                log.error("Watchdog check error: %s", e)

    def _check_all(self):
        """Check health of all active models."""
        from .health import check_http_status
        from .state import ServiceState

        active = list(self._manager.active_services)
        if not active:
            return

        for svc_name in active:
            model = self._manager.get_model(svc_name)
            if not model or not model.port:
                continue

            with self._lock:
                # Skip if a restart is already in progress for this model
                # P2-2: Add timeout — if a model stays in _restarting > 120s,
                # treat as stuck and allow retry.
                if svc_name in self._restarting:
                    _restart_deadline = getattr(self, "_restart_started", {}).get(svc_name, 0)
                    if _restart_deadline and (time.time() - _restart_deadline < 120):
                        continue
                    log.warning("Watchdog: restart of %s appears stuck (>120s), retrying", svc_name)
                    self._restarting.discard(svc_name)

            health_url = getattr(model, 'health_url', None) or f"http://localhost:{model.port}/health"
            status = check_http_status(health_url)

            with self._lock:
                if status == "✅":
                    old_count = self._fail_counts.get(svc_name, 0)
                    if old_count >= self._fail_threshold_alert:
                        log.info("Watchdog: %s recovered after %d consecutive failures", svc_name, old_count)
                    self._fail_counts[svc_name] = 0
                    self._total_restarts.pop(svc_name, None)  # reset on recovery
                    self._restart_cooldowns.pop(svc_name, None)
                elif status in ("⏳", "❌"):
                    self._fail_counts[svc_name] = self._fail_counts.get(svc_name, 0) + 1
                    count = self._fail_counts[svc_name]

                    if count >= self._fail_threshold_restart and self._auto_restart:
                        # P2-2: Check cooldown
                        now = time.monotonic()
                        cooldown_deadline = self._restart_cooldowns.get(svc_name, 0)
                        if cooldown_deadline and now < cooldown_deadline:
                            remaining = int(cooldown_deadline - now)
                            log.debug("Watchdog: %s in cooldown (%ds remaining), skipping restart", svc_name, remaining)
                            continue

                        # P2-2: Check max_restarts
                        total = self._total_restarts.get(svc_name, 0)
                        if total >= self._max_restarts:
                            log.error(
                                "Watchdog: %s exceeded max_restarts (%d) — giving up",
                                svc_name, self._max_restarts,
                            )
                            self._manager.state.set("profile_state", ServiceState.ERROR)
                            continue

                        log.warning("Watchdog: %s failed %d times (attempt %d/%d) — auto-restarting",
                                    svc_name, count, total + 1, self._max_restarts)
                        self._restarting.add(svc_name)
                        self._total_restarts[svc_name] = total + 1
                        # P2-2: Set cooldown with exponential backoff
                        cooldown = min(300, self._restart_cooldown_step * (2 ** total))
                        self._restart_cooldowns[svc_name] = now + cooldown
                        log.info("Watchdog: %s cooldown set to %ds", svc_name, cooldown)
                        # 关停后不再派发新 worker（评审修复 4）
                        if self._stop_event.is_set():
                            continue
                        # Trigger restart in a separate thread to avoid blocking watchdog
                        worker = threading.Thread(
                            target=self._restart_model,
                            args=(svc_name,),
                            daemon=True,
                            name=f"iff-watchdog-restart-{svc_name}",
                        )
                        # 已在 _check_all 的 with self._lock 块内 —— 直接注册，
                        # 不得重入非可重入 Lock（自死锁）
                        self._workers.add(worker)
                        worker.start()
                    elif count >= self._fail_threshold_alert:
                        log.warning("Watchdog: %s failed %d times — alerting (profile_state=ERROR)", svc_name, count)
                        self._manager.state.set("profile_state", ServiceState.ERROR)

    def _restart_model(self, name: str):
        """Attempt to restart a failed model.

        P2-2: When invoked from a restart-triggered context, "already_active"
        from switch() is NOT a success — it means reconcile() failed to clean
        up a stale entry, or the process was still registered but non-responsive.
        Only "switched" counts as a successful restart.

        PR-20 评审修复 4：每个 manager 变更经 _fenced 围栏 —— 关停开始后
        worker 在变更边界处放弃（不得杀掉重启要保全的引擎/毒化状态）。
        """
        try:
            with self._lock:
                if not hasattr(self, "_restart_started"):
                    self._restart_started = {}
                self._restart_started[name] = time.time()
            # Skip if a user-triggered switch is already in progress
            switching = self._manager.state.get("switching_target") or ""
            if switching and switching != name:
                log.info("Watchdog: %s restart skipped — %s is switching", name, switching)
                return
            log.info("Watchdog: reconciling before restart of %s", name)
            ok, _ = self._fenced(self._manager.reconcile)
            if not ok:
                return  # Clean up dead service entries（关停围栏拦截）
            if name not in self._manager.active_services:
                # Reconcile removed it — now switch will actually deploy
                log.info("Watchdog: deploying %s after reconcile", name)
                ok, result = self._fenced(self._manager.switch, name)
                if not ok:
                    return
                if result.get("status") == "switched":
                    log.info("Watchdog: %s restart succeeded", name)
                    with self._lock:
                        self._fail_counts[name] = 0
                else:
                    # P2-2: "already_active" in restart context = stale entry.
                    # Do not clear fail count — let the next cycle retry.
                    if result.get("status") == "already_active":
                        log.warning(
                            "Watchdog: %s returned already_active after reconcile — "
                            "stale entry, will force-clean and retry", name
                        )
                        # Stop the process first to avoid orphan / port conflict,
                        # then remove the stale active_services entry.
                        if not self._fenced_quiet_stop(name):
                            return
                        ok, _ = self._fenced(self._manager.state.remove_active_service, name)
                        if not ok:
                            return
                    else:
                        log.error("Watchdog: %s restart failed: %s", name, result)
            else:
                # P2-2: Still active after reconcile — could be a zombie.
                # Stop the process first to avoid orphan, then force-clean state.
                log.warning(
                    "Watchdog: %s still in active_services after reconcile — "
                    "force-stopping and retrying in next cycle", name
                )
                if not self._fenced_quiet_stop(name):
                    return
                ok, _ = self._fenced(self._manager.state.remove_active_service, name)
                if not ok:
                    return
        except Exception as e:
            log.error("Watchdog: %s restart exception: %s", name, e)
        finally:
            with self._lock:
                self._restarting.discard(name)
                self._workers.discard(threading.current_thread())

    def _fenced_quiet_stop(self, name: str) -> bool:
        """围栏内 stop_service，保留原有吞异常语义（P2-2: stop 失败也清条目）。"""
        def _stop():
            try:
                self._manager.stop_service(name)
            except Exception:
                pass
        ok, _ = self._fenced(_stop)
        return ok

    @property
    def fail_counts(self) -> dict[str, int]:
        """Current failure counts (for dashboard/status)."""
        with self._lock:
            return dict(self._fail_counts)

    @property
    def running(self) -> bool:
        """Whether the watchdog thread is currently running."""
        return self._thread is not None and self._thread.is_alive()
