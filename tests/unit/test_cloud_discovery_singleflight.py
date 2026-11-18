"""云发现单飞后台化 — PLAN-2026-11-15 v2 Task 5（P2）。

契约（计划 Interfaces）：
- ensure_cloud_discovered(background=True)：池路径不阻塞请求关键线程；
  并发冷请求只启一个发现任务；失败可重试；成功后衔接轮询与价格刷新；
  不唤醒本地、不扩大本地白名单。
- background=False（默认）保持旧同步行为。
"""

import threading
import time
from types import SimpleNamespace

import pytest

from inferfabric.proxy_manager import ProxyManager


def make_pm():
    pm = ProxyManager.__new__(ProxyManager)
    pm._cloud_discovered = False
    pm._discovery_lock = threading.Lock()
    pm._discovery_thread = None
    calls = {"discover": 0, "poll": 0, "prices": 0}

    gate = threading.Event()

    def discover_all():
        calls["discover"] += 1
        gate.wait(timeout=10)  # 慢发现：测试控制放行
        if calls.get("fail"):
            raise RuntimeError("discovery boom")
        return {}

    pm.cloud = SimpleNamespace(
        providers={"p1": object()},
        discover_all=discover_all,
        start_polling=lambda: calls.__setitem__("poll", calls["poll"] + 1),
        cloud_models={})
    pm.metrics = SimpleNamespace(
        update_prices=lambda cfg: calls.__setitem__("prices", calls["prices"] + 1))
    pm._load_price_config = lambda: {}
    pm.calls = calls
    pm.gate = gate
    return pm


class TestSlowDiscoveryNotOnPoolPath:

    def test_background_returns_before_discovery_finishes(self):
        pm = make_pm()
        t0 = time.monotonic()
        pm.ensure_cloud_discovered(background=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0            # 不等慢发现
        assert pm.calls["discover"] == 1  # 已在后台启动
        assert pm._cloud_discovered is False
        assert pm.calls["poll"] == 0    # 尚未衔接轮询
        pm.gate.set()                   # 放行发现
        pm._discovery_thread.join(timeout=5)
        assert pm._cloud_discovered is True
        assert pm.calls["poll"] == 1
        assert pm.calls["prices"] == 1

    # 注：test_dispatch_pool_path_uses_background（池 dispatch 调用侧断言）
    # 不在本批次 — dispatch.py 的 background=True 调用改造随池 v2（pool-v2）
    # 批次摘取，见 CHERRY-PICK-MANIFEST F5。

    def test_sync_default_unchanged(self):
        pm = make_pm()
        pm.ensure_cloud_discovered()  # 默认同步：返回时发现已完成
        assert pm._cloud_discovered is True
        assert pm.calls["poll"] == 1
        assert pm.calls["prices"] == 1

    def test_no_providers_noop(self):
        pm = make_pm()
        pm.cloud = SimpleNamespace(providers={}, discover_all=lambda: {},
                                   start_polling=lambda: None, cloud_models={})
        pm.ensure_cloud_discovered(background=True)
        assert pm.calls["discover"] == 0


class TestConcurrentDiscoverySingleflight:

    def test_only_one_thread_discovers(self):
        pm = make_pm()
        threads = [threading.Thread(target=lambda: pm.ensure_cloud_discovered(
            background=True)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        time.sleep(0.05)
        assert pm.calls["discover"] == 1  # 单飞
        pm.gate.set()
        pm._discovery_thread.join(timeout=5)
        assert pm.calls["poll"] == 1

    def test_failure_is_retryable(self):
        pm = make_pm()
        pm.calls["fail"] = 1
        pm.ensure_cloud_discovered(background=True)
        pm.gate.set()
        pm._discovery_thread.join(timeout=5)
        assert pm._cloud_discovered is False  # 失败不置位
        # 可重试：下次请求再触发
        pm.calls["fail"] = 0
        pm.ensure_cloud_discovered(background=True)
        pm.gate.set()
        pm._discovery_thread.join(timeout=5)
        assert pm._cloud_discovered is True

    def test_completion_does_not_wake_local(self):
        pm = make_pm()
        woken = []
        pm.mgr = SimpleNamespace(
            active_services=set(),
            wake=lambda name: woken.append(name))
        pm.ensure_cloud_discovered(background=True)
        pm.gate.set()
        pm._discovery_thread.join(timeout=5)
        assert woken == []  # 发现完成不唤醒本地模型
