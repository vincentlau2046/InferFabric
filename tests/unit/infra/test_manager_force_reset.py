"""
unit/infra/test_manager_force_reset.py — Task 2.4: manager-level force_reset hoist

ModelManager.force_reset 把「停所有 active 服务」的编排提升到协调层（coordinator）：
  1. ModelLifecycle._stop_all_active(include_none=True) — 统一 metadata-driven
     优雅停止（关闭 R6 窗口：vllm/tts/asr/sglang/llama_cpp/comfyui 不再直接
     落到 SIGKILL，全部先经 adapter 优雅停）
  2. GpuStateMachine.force_reset — 降级为 kill+clear-only（force_kill_all 仍是
     SIGKILL 兜底安全网）

测试手法：与 test_auto_switch.py 一致 — MagicMock 管理器 + 非绑定类方法调用，
避免构造真实 ModelManager（会触碰 StateDB / ProcessManager / models.d）。
"""
from unittest.mock import MagicMock


def test_force_reset_graceful_stops_via_lifecycle():
    """manager.force_reset 必须经 _lifecycle._stop_all_active(include_none=True) 优雅停。"""
    from inferfabric.manager import ModelManager

    mgr = MagicMock()

    ModelManager.force_reset(mgr)

    mgr._lifecycle._stop_all_active.assert_called_once_with(include_none=True)


def test_force_reset_delegates_to_gpu_state_and_returns_its_result():
    """manager.force_reset 委托 _gpu_state.force_reset 并原样返回其结果。"""
    from inferfabric.manager import ModelManager

    mgr = MagicMock()
    mgr._gpu_state.force_reset.return_value = {"status": "reset", "gpu_mode": "idle"}

    result = ModelManager.force_reset(mgr)

    mgr._gpu_state.force_reset.assert_called_once()
    assert result == {"status": "reset", "gpu_mode": "idle"}


def test_force_reset_graceful_stop_precedes_kill():
    """顺序：先优雅停（_stop_all_active），后 kill+clear（gpu_state.force_reset）。"""
    from inferfabric.manager import ModelManager

    mgr = MagicMock()
    calls = []
    mgr._lifecycle._stop_all_active.side_effect = lambda **k: calls.append("graceful_stop")
    mgr._gpu_state.force_reset.side_effect = lambda: calls.append("kill_clear")

    ModelManager.force_reset(mgr)

    assert calls == ["graceful_stop", "kill_clear"], \
        f"优雅停必须先于 kill+clear，got {calls}"
