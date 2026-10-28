# tests/unit/proxy/test_proxy_manager_shutdown.py
"""ProxyManager.begin_shutdown — PR-20 ①③ 关停序列第一步。

SIGTERM 第一时间调用：封印 StateDB（垂死进程禁写）+ 停后台状态写入者
（health_monitor 60s 周期 reconcile 是 P0 毒写源头）+ telemetry 采集线程。
幂等；单点失败不阻断关停。
"""
from unittest.mock import MagicMock

from inferfabric.proxy_manager import ProxyManager


def _pm():
    """绕过重型 __init__（拉起 HealthMonitor/telemetry/模型加载），
    只装配 begin_shutdown 依赖的属性。"""
    pm = ProxyManager.__new__(ProxyManager)
    pm._shutdown_begun = False
    pm.mgr = MagicMock()
    pm.health_monitor = MagicMock()
    pm.telemetry = MagicMock()
    return pm


class TestBeginShutdown:
    def test_seals_statedb_with_reason(self):
        pm = _pm()
        pm.begin_shutdown("proxy shutdown")
        pm.mgr.state.seal.assert_called_once_with("proxy shutdown")

    def test_stops_background_writers(self):
        pm = _pm()
        pm.begin_shutdown("x")
        pm.health_monitor.stop.assert_called_once()
        pm.telemetry.token_collector.stop.assert_called_once()
        pm.telemetry.power_sampler.stop.assert_called_once()

    def test_idempotent(self):
        pm = _pm()
        pm.begin_shutdown("first")
        pm.begin_shutdown("second")
        pm.mgr.state.seal.assert_called_once_with("first")
        pm.health_monitor.stop.assert_called_once()

    def test_seal_failure_does_not_block_stops(self):
        """封印抛异常也必须继续停线程——关停序列不能被单点失败卡死。"""
        pm = _pm()
        pm.mgr.state.seal.side_effect = RuntimeError("db gone")
        pm.begin_shutdown("x")
        pm.health_monitor.stop.assert_called_once()
        pm.telemetry.token_collector.stop.assert_called_once()

    def test_monitor_stop_failure_does_not_block_rest(self):
        pm = _pm()
        pm.health_monitor.stop.side_effect = RuntimeError("join hang")
        pm.begin_shutdown("x")
        pm.mgr.state.seal.assert_called_once_with("x")
        pm.telemetry.power_sampler.stop.assert_called_once()
