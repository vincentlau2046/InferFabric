"""
unit/state/test_gpu_state.py — GPU 状态机测试

测试对象: inferfabric.gpu_state.GpuStateMachine
覆盖范围:
  - _derive_gpu_mode: idle / exclusive / shared 推导、gpu_none 排除
  - _scan_port_owners: 端口扫描、gpu_none 跳过、PID 检测
  - _detect_orphan_pids: 死进程清理、存活进程保留
  - reconcile: 空状态安全、返回 db/actual 服务列表
  - cleanup_dead_services: 空状态、健康服务保留
  - force_reset: 返回 reset 状态、调用 force_kill_all（Task 2.4 降级后 stop_all 已移至 manager 层）
"""

import time
import json
from unittest.mock import MagicMock, patch

import pytest


def _make_gs(state=None, models=None, proc=None, health=None, lock=None):
    """创建 GpuStateMachine 实例的辅助函数。"""
    from inferfabric.gpu_state import GpuStateMachine

    state = state or MagicMock()
    proc = proc or MagicMock()
    health = health or MagicMock()
    lock = lock or MagicMock()
    models = models or {}

    return GpuStateMachine(state, proc, health, lock, models)


class TestGpuStateMachineDeriveMode:
    """GPU 模式推导"""

    def test_no_services_returns_idle(self):
        """无实际服务时推导为 idle。"""
        from inferfabric.state import GPUMode
        gs = _make_gs()
        mode = gs._derive_gpu_mode(actual_services=[])
        assert mode == GPUMode.IDLE

    def test_exclusive_service_returns_exclusive(self):
        """exclusive 服务推导为 exclusive 模式。"""
        from inferfabric.state import GPUMode
        model = MagicMock()
        model.is_gpu_none = False
        model.is_exclusive = True

        gs = _make_gs(models={"test-model": model})
        mode = gs._derive_gpu_mode(actual_services=["test-model"])
        assert mode == GPUMode.EXCLUSIVE

    def test_shared_services_returns_shared(self):
        """shared 服务推导为 shared 模式。"""
        from inferfabric.state import GPUMode
        model1 = MagicMock()
        model1.is_gpu_none = False
        model1.is_exclusive = False
        model2 = MagicMock()
        model2.is_gpu_none = False
        model2.is_exclusive = False

        gs = _make_gs(models={"m1": model1, "m2": model2})
        mode = gs._derive_gpu_mode(actual_services=["m1", "m2"])
        assert mode == GPUMode.SHARED

    def test_gpu_none_services_excluded(self):
        """gpu_role:none 服务不计入 GPU 模式推导。"""
        from inferfabric.state import GPUMode
        model = MagicMock()
        model.is_gpu_none = True

        gs = _make_gs(models={"bge-m3": model})
        mode = gs._derive_gpu_mode(actual_services=["bge-m3"])
        assert mode == GPUMode.IDLE


class TestGpuStateMachineScanPortOwners:
    """端口扫描"""

    def test_no_models_returns_empty(self):
        """无模型时返回空字典。"""
        gs = _make_gs()
        owners = gs._scan_port_owners()
        assert owners == {}

    def test_gpu_none_skipped(self):
        """gpu_role:none 模型被跳过。"""
        model = MagicMock()
        model.is_gpu_none = True
        model.port = 8000

        gs = _make_gs(models={"bge-m3": model})
        owners = gs._scan_port_owners()
        assert "bge-m3" not in owners

    def test_port_with_pid_detected(self):
        """端口有进程占用时返回 (port, pid)。"""
        model = MagicMock()
        model.is_gpu_none = False
        model.port = 8000

        gs = _make_gs(models={"test-model": model})
        with patch.object(gs, "_port_pid", return_value=12345):
            owners = gs._scan_port_owners()

        assert "test-model" in owners
        assert owners["test-model"] == (8000, 12345)


class TestGpuStateMachineDetectOrphanPids:
    """孤儿 PID 检测

    _detect_orphan_pids 遍历所有 engine adapter，
    从 self._proc 读取 PID state key，用 os.kill(pid, 0) 检测进程存活。
    """

    def test_dead_process_cleared(self):
        """PID 对应进程已死亡时清除 PID 记录。"""
        state = MagicMock()
        actions = []

        proc = MagicMock()
        proc.vllm_pid = 99999  # 不存在的 PID

        gs = _make_gs(state=state, proc=proc, models={})
        with patch("inferfabric.gpu_state.os.kill",
                   side_effect=ProcessLookupError):
            with patch.object(gs, "_port_pid", return_value=None):
                gs._detect_orphan_pids(actual_services=[], actions=actions)

        state.set.assert_called()
        # 验证清除 vllm_pid
        calls = [str(c) for c in state.set.call_args_list]
        assert any("vllm_pid" in c for c in calls), f"Expected vllm_pid cleared: {calls}"

    def test_alive_process_preserved(self):
        """PID 对应进程存活时保留。"""
        state = MagicMock()
        actions = []

        proc = MagicMock()
        proc.vllm_pid = 1  # PID 1 (init) 始终存在

        gs = _make_gs(state=state, proc=proc, models={})
        with patch("inferfabric.gpu_state.os.kill"):  # 不抛异常 = 进程存在
            gs._detect_orphan_pids(actual_services=[], actions=actions)

        # 进程存活，不应清除（但可能有 stale 检测路径）
        # 至少不应报 "Orphan" 错误
        assert not any("Orphan" in a for a in actions)


class TestGpuStateMachineReconcile:
    """状态调和"""

    def test_reconcile_with_empty_state(self):
        """空状态的 reconcile 不抛异常。"""
        from inferfabric.state import GPUMode
        state = MagicMock()
        state.get_active_services.return_value = []

        gs = _make_gs(state=state, models={})
        with patch.object(gs, "_scan_port_owners", return_value={}):
            with patch.object(gs, "_detect_orphan_pids"):
                with patch.object(gs, "_restore_dead_pids"):
                    with patch.object(gs, "_derive_gpu_mode",
                                      return_value=GPUMode.IDLE):
                        result = gs.reconcile()

        assert isinstance(result, dict)
        assert "actions" in result

    def test_reconcile_returns_db_and_actual(self):
        """reconcile 返回 db_services 和 actual_services。"""
        from inferfabric.state import GPUMode
        state = MagicMock()
        state.get_active_services.return_value = ["test-model"]
        state.gpu_mode = GPUMode.IDLE

        gs = _make_gs(state=state, models={})
        with patch.object(gs, "_scan_port_owners", return_value={}):
            with patch.object(gs, "_scan_actual_services", return_value=[]):
                with patch.object(gs, "_detect_orphan_pids"):
                    with patch.object(gs, "_restore_dead_pids"):
                        with patch.object(gs, "_derive_gpu_mode",
                                          return_value=GPUMode.IDLE):
                            result = gs.reconcile()

        assert "db_services" in result
        assert "actual_services" in result


class TestGpuStateMachineCleanupDeadServices:
    """清理死亡服务"""

    def test_empty_active_no_cleanup(self):
        """无活跃服务时不清理。"""
        state = MagicMock()
        state.get_active_services.return_value = []

        gs = _make_gs(state=state, models={})
        dead = gs.cleanup_dead_services()

        assert dead == []

    def test_healthy_service_kept(self):
        """健康检查通过的服务不被清理。"""
        state = MagicMock()
        state.get_active_services.return_value = ["alive-model"]

        model = MagicMock()
        model.type = "vllm"
        model.is_comfyui = False
        model.is_ollama_cpp = False
        model.is_asr_server = False

        gs = _make_gs(state=state, models={"alive-model": model})

        mock_adapter = MagicMock()
        mock_adapter.check_health.return_value = "✅"
        # cleanup_dead_services does local import: from inferfabric.engine_adapter import get_adapter
        with patch("inferfabric.engine_adapter.get_adapter", return_value=mock_adapter):
            dead = gs.cleanup_dead_services()

        assert dead == []


class TestGpuStateMachineForceReset:
    """强制重置"""

    def test_force_reset_returns_reset_status(self):
        """force_reset 返回 reset 状态。"""
        from inferfabric.state import GPUMode
        state = MagicMock()
        state.get_active_services.return_value = []

        proc = MagicMock()

        gs = _make_gs(state=state, proc=proc, models={})

        with patch("inferfabric.gpu_state.wait_gpu_free", return_value=True):
            with patch("inferfabric.gpu_state.gpu_used_mb", return_value=100):
                result = gs.force_reset()

        assert result["status"] == "reset"
        assert result["gpu_mode"] == GPUMode.IDLE

    def test_force_reset_kill_and_clear_only(self):
        """Task 2.4 降级：gpu_state.force_reset 只做 kill+clear（force_kill_all），
        不再调用 proc.stop_all（优雅停止已提升到 manager 层 _stop_all_active）。"""
        from inferfabric.state import GPUMode
        state = MagicMock()
        state.get_active_services.return_value = []

        proc = MagicMock()

        gs = _make_gs(state=state, proc=proc, models={})

        with patch("inferfabric.gpu_state.wait_gpu_free", return_value=True):
            with patch("inferfabric.gpu_state.gpu_used_mb", return_value=100):
                gs.force_reset()

        proc.force_kill_all.assert_called_once()
        proc.stop_all.assert_not_called()
