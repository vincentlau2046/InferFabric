"""
unit/infra/test_watchdog.py — 模型看门狗测试

测试对象: inferfabric.watchdog.ModelWatchdog
覆盖范围:
  - start/stop 生命周期（守护线程、重复 start no-op）
  - fail_counts: 初始为空、返回副本安全
  - _check_all: 空服务 no-op、健康重置计数、故障递增、alert 阈值设 ERROR、无模型跳过
  - auto_restart: 阈值触发重启线程 + reconcile 调用、禁用时不触发
"""

import time
import threading
from unittest.mock import MagicMock, patch

import pytest


class TestWatchdogLifecycle:
    """start/stop 生命周期"""

    def test_start_creates_thread(self):
        """start() 创建守护线程。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        mgr.active_services = []
        mgr.get_model.return_value = None

        wd = ModelWatchdog(mgr, check_interval=0.1)
        wd.start()

        assert wd.running
        assert wd._thread is not None
        assert wd._thread.daemon

        wd.stop()

    def test_stop_joins_thread(self):
        """stop() 等待线程结束。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        mgr.active_services = []

        wd = ModelWatchdog(mgr, check_interval=0.1)
        wd.start()
        time.sleep(0.2)
        wd.stop()

        assert not wd.running

    def test_double_start_is_noop(self):
        """重复 start() 不创建新线程。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        mgr.active_services = []

        wd = ModelWatchdog(mgr, check_interval=0.1)
        wd.start()
        t1 = wd._thread
        wd.start()
        t2 = wd._thread

        assert t1 is t2
        wd.stop()


class TestWatchdogFailCounts:
    """故障计数"""

    def test_initial_fail_counts_empty(self):
        """初始 fail_counts 为空。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        wd = ModelWatchdog(mgr)

        assert wd.fail_counts == {}

    def test_fail_counts_returns_copy(self):
        """fail_counts 返回副本，不影响内部状态。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        wd = ModelWatchdog(mgr)
        wd._fail_counts["test"] = 3

        counts = wd.fail_counts
        counts["test"] = 0  # 修改副本

        assert wd._fail_counts["test"] == 3  # 内部不变


class TestWatchdogCheckAll:
    """_check_all 健康检查

    check_http_status 在 _check_all 内部通过 from .health import 引入，
    需要 patch inferfabric.health.check_http_status。
    """

    def test_empty_active_services_noop(self):
        """无活跃服务时 _check_all 不执行任何操作。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        mgr.active_services = []

        wd = ModelWatchdog(mgr)
        wd._check_all()

        assert wd.fail_counts == {}

    def test_healthy_model_resets_count(self):
        """健康模型的 fail_count 被重置为 0。"""
        from inferfabric.watchdog import ModelWatchdog

        model = MagicMock()
        model.port = 8000
        model.health_url = None

        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.get_model.return_value = model

        wd = ModelWatchdog(mgr)
        wd._fail_counts["test-model"] = 2

        with patch("inferfabric.health.check_http_status", return_value="✅"):
            wd._check_all()

        assert wd.fail_counts["test-model"] == 0

    def test_unhealthy_model_increments_count(self):
        """不健康模型的 fail_count 递增。"""
        from inferfabric.watchdog import ModelWatchdog

        model = MagicMock()
        model.port = 8000
        model.health_url = None

        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.get_model.return_value = model

        wd = ModelWatchdog(mgr, fail_threshold_alert=10, fail_threshold_restart=20,
                           auto_restart=False)

        with patch("inferfabric.health.check_http_status", return_value="❌"):
            wd._check_all()

        assert wd.fail_counts["test-model"] == 1

    def test_alert_threshold_sets_error_state(self):
        """达到 alert 阈值时设置 ERROR 状态。"""
        from inferfabric.watchdog import ModelWatchdog

        model = MagicMock()
        model.port = 8000
        model.health_url = None

        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.get_model.return_value = model
        mgr.state = MagicMock()

        wd = ModelWatchdog(mgr, fail_threshold_alert=2, fail_threshold_restart=10,
                           auto_restart=False)
        wd._fail_counts["test-model"] = 1  # 已有 1 次失败

        with patch("inferfabric.health.check_http_status", return_value="⏳"):
            wd._check_all()  # 第 2 次失败，达到阈值

        mgr.state.set.assert_called_once_with("profile_state", "error")

    def test_no_model_skipped(self):
        """get_model 返回 None 时跳过该服务。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = MagicMock()
        mgr.active_services = ["ghost-model"]
        mgr.get_model.return_value = None

        wd = ModelWatchdog(mgr)

        with patch("inferfabric.health.check_http_status") as mock_health:
            wd._check_all()
            mock_health.assert_not_called()


class TestWatchdogAutoRestart:
    """自动重启"""

    def test_restart_triggered_on_threshold(self):
        """达到 restart 阈值时触发重启线程并调用 reconcile。"""
        from inferfabric.watchdog import ModelWatchdog

        model = MagicMock()
        model.port = 8000
        model.health_url = None

        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.get_model.return_value = model
        mgr.state = MagicMock()
        mgr.state.get.return_value = ""  # no switching_target
        mgr.reconcile.return_value = {"actions": []}
        mgr.switch.return_value = {"status": "switched"}

        wd = ModelWatchdog(mgr, fail_threshold_alert=3, fail_threshold_restart=3,
                           auto_restart=True, check_interval=60)
        wd._fail_counts["test-model"] = 2  # 已有 2 次

        with patch("inferfabric.health.check_http_status", return_value="❌"):
            wd._check_all()  # 第 3 次，触发重启

        # 等待重启线程完成
        time.sleep(1.5)
        # 重启线程应调用 reconcile
        mgr.reconcile.assert_called_once()
        # 重启线程结束后 _restarting 应被清除
        assert "test-model" not in wd._restarting

    def test_auto_restart_disabled_no_restart(self):
        """auto_restart=False 时不触发重启。"""
        from inferfabric.watchdog import ModelWatchdog

        model = MagicMock()
        model.port = 8000
        model.health_url = None

        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.get_model.return_value = model
        mgr.state = MagicMock()

        wd = ModelWatchdog(mgr, fail_threshold_alert=10, fail_threshold_restart=3,
                           auto_restart=False)
        wd._fail_counts["test-model"] = 2

        with patch("inferfabric.health.check_http_status", return_value="❌"):
            wd._check_all()  # 第 3 次失败

        # 不应进入 restarting
        assert "test-model" not in wd._restarting


class TestWatchdogShutdownFence:
    """PR-20 评审修复 4：关停围栏 —— 重启 worker 不得在关停后变更生命周期。

    P0 场景：SIGTERM 后 watchdog.stop() 只 join 监控线程，已派发的
    _restart_model 线程不受管 —— 可在 StateDB 封印后继续 reconcile/
    switch/stop_service，把重启要保全的引擎杀掉。围栏语义：
      - 每次 manager 变更在 _mutation_lock 下 check+mutate 原子执行；
      - stop() 先置 _stop_event 再以同一把锁作屏障 —— 屏障之后不可能
        有新变更溜进检查与调用之间（单次布尔检查关不住的竞态）；
      - 在途变更要么在 fence_timeout 内完成，要么由进程硬退出截断。
    """

    def _mgr(self):
        mgr = MagicMock()
        mgr.active_services = ["test-model"]
        mgr.state.get.return_value = ""  # switching_target 为空
        model = MagicMock()
        model.port = 8000
        model.health_url = None
        mgr.get_model.return_value = model
        return mgr

    def test_fenced_mutation_skips_after_stop_event(self):
        """stop_event 置位后 _fenced 拦截变更，不执行 fn。"""
        from inferfabric.watchdog import ModelWatchdog

        wd = ModelWatchdog(self._mgr())
        wd._stop_event.set()
        called = []

        ok, result = wd._fenced(lambda: called.append(1) or "r")

        assert ok is False
        assert result is None
        assert called == []

    def test_stop_waits_for_inflight_mutation(self):
        """在途变更未完成时 stop() 不得返回（屏障），完成后才返回。"""
        import threading
        from inferfabric.watchdog import ModelWatchdog

        wd = ModelWatchdog(self._mgr(), fence_timeout=5.0)
        started, release = threading.Event(), threading.Event()

        def slow_mutation():
            started.set()
            release.wait(5)

        t = threading.Thread(target=lambda: wd._fenced(slow_mutation), daemon=True)
        t.start()
        assert started.wait(2)

        stop_done = threading.Event()
        stopper = threading.Thread(target=lambda: (wd.stop(), stop_done.set()),
                                   daemon=True)
        stopper.start()
        # 在途变更未释放 → stop() 必须还在屏障上等
        assert not stop_done.wait(0.3), "stop() 在在途变更未完成时提前返回"
        release.set()
        assert stop_done.wait(3), "stop() 在在途变更完成后应返回"

    def test_stop_bounded_when_mutation_stuck(self):
        """变更卡死时 stop() 也必须在 fence_timeout 内有界返回。"""
        import threading
        import time as _t
        from inferfabric.watchdog import ModelWatchdog

        wd = ModelWatchdog(self._mgr(), fence_timeout=0.2)
        started, release = threading.Event(), threading.Event()

        def stuck_mutation():
            started.set()
            release.wait(10)

        t = threading.Thread(target=lambda: wd._fenced(stuck_mutation), daemon=True)
        t.start()
        assert started.wait(2)

        t0 = _t.monotonic()
        wd.stop()
        elapsed = _t.monotonic() - t0
        release.set()
        assert elapsed < 2.0, f"stop() 应有界返回（fence_timeout=0.2），实际 {elapsed:.1f}s"

    def test_restart_worker_aborts_between_mutations(self):
        """评审场景：reconcile 中途开始关停 → worker 不得再 switch/stop_service。"""
        import threading
        from inferfabric.watchdog import ModelWatchdog

        mgr = self._mgr()
        wd = ModelWatchdog(mgr, fence_timeout=5.0)
        in_reconcile, release = threading.Event(), threading.Event()

        def slow_reconcile():
            in_reconcile.set()
            release.wait(5)

        mgr.reconcile.side_effect = slow_reconcile

        worker = threading.Thread(target=wd._restart_model, args=("test-model",),
                                  daemon=True)
        worker.start()
        assert in_reconcile.wait(2)

        wd._stop_event.set()  # SIGTERM：关停开始，worker 还停在 reconcile 里
        release.set()
        worker.join(3)

        mgr.switch.assert_not_called()
        mgr.stop_service.assert_not_called()
        assert not worker.is_alive()

    def test_stop_joins_spawned_restart_worker(self):
        """经 _check_all 派发的 worker 必须注册并在 stop() 返回前 join。"""
        import threading
        from inferfabric.watchdog import ModelWatchdog

        mgr = self._mgr()
        wd = ModelWatchdog(mgr, fail_threshold_alert=1, fail_threshold_restart=1,
                           auto_restart=True, check_interval=60, fence_timeout=5.0)
        wd._fail_counts["test-model"] = 0
        in_reconcile, release = threading.Event(), threading.Event()

        def slow_reconcile():
            in_reconcile.set()
            release.wait(5)

        mgr.reconcile.side_effect = slow_reconcile

        with patch("inferfabric.health.check_http_status", return_value="❌"):
            wd._check_all()

        assert in_reconcile.wait(2)
        with wd._lock:
            workers = list(wd._workers)
        assert len(workers) == 1, "重启 worker 必须注册进 _workers"

        stop_done = threading.Event()
        stopper = threading.Thread(target=lambda: (wd.stop(), stop_done.set()),
                                   daemon=True)
        stopper.start()
        release.set()
        assert stop_done.wait(3)
        assert not workers[0].is_alive(), "stop() 返回后 worker 线程必须已结束"

    def test_check_all_does_not_spawn_worker_after_stop(self):
        """stop() 后 _check_all 不得再派发新重启线程。"""
        from inferfabric.watchdog import ModelWatchdog

        mgr = self._mgr()
        wd = ModelWatchdog(mgr, fail_threshold_alert=1, fail_threshold_restart=1,
                           auto_restart=True, check_interval=60)
        wd._stop_event.set()

        with patch("inferfabric.health.check_http_status", return_value="❌"):
            wd._check_all()

        with wd._lock:
            assert wd._workers == set(), "关停后不得派发新 worker"
