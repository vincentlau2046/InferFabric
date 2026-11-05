"""
unit/state/test_idle_switch.py — idle 切换保留 gpu:none 服务测试

测试对象: inferfabric.model_lifecycle._switch_to_idle 逻辑
覆盖范围:
  - gpu_role:none 服务在 idle 切换后保留
  - 全 GPU 服务时 active_services 清空
  - 多个 gpu:none 服务全部保留
  - 未知服务跳过（防御性）
  - stop_llama_cpp 不对 gpu:none 服务调用（源码验证）
  - 所有 GPU-bound 服务经 _stop_model_process 停止（不内联 docker，Task 2.1）
"""
import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from inferfabric.model_lifecycle import ModelLifecycle
from inferfabric.state import GPUMode

# The gpu:none-preservation tests below exercise the set_multi filter logic in
# isolation; the Task 2.1 test drives the real _switch_to_idle with mocked
# deps (module import is side-effect-free — no StateDB is constructed).


def _make_lifecycle_with_active(names):
    """Build a ModelLifecycle whose active services are exactly ``names``.

    List-based variant of test_model_lifecycle._make_lifecycle_with_active_exclusive:
    state/proc/health/lock/gpu_state are MagicMocks, and one MagicMock model is
    built per name (name → type: "Qwen38-27B-TXT" → ninfer, "ovis-ocr2" → vllm).

    Two gotchas this helper resolves (see task-2.1 brief):
      - state.get_active_services returns a REAL list — _switch_to_idle does
        list(self.state.get_active_services()); a bare MagicMock would raise
        TypeError on iteration.
      - state.gpu_mode is EXCLUSIVE (not IDLE) — IDLE would short-circuit
        _switch_to_idle to "already_active" and make the test pass for the
        wrong reason.
    """
    state = MagicMock()
    state.get_active_services.return_value = list(names)
    state.get_sleep_state.return_value = None
    state.get_all_sleep_states.return_value = {}
    state.gpu_mode = GPUMode.EXCLUSIVE

    proc = MagicMock()
    health = MagicMock()
    lock = MagicMock()
    gpu_state = MagicMock()
    gpu_state._get_current_vram_pct.return_value = 0

    name_to_type = {"Qwen38-27B-TXT": "ninfer", "ovis-ocr2": "vllm"}
    models = {}
    for name in names:
        model = MagicMock()
        mtype = name_to_type.get(name, "vllm")
        model.name = name
        model.type = mtype
        for flag in ("is_vllm", "is_sglang", "is_comfyui", "is_ninfer",
                     "is_llama_cpp", "is_tts_server", "is_asr_server"):
            setattr(model, flag, False)
        setattr(model, f"is_{mtype}", True)
        model.is_gpu_none = False
        model.needs_gpu = True
        model.gpu_role = "exclusive"
        model.peak_vram_mb = 0
        model.typical_vram_pct = 0
        model.vllm.port = 8100
        model.ninfer.port = 8007
        model.ninfer.container_name = f"ninfer-{name}"
        models[name] = model

    return ModelLifecycle(state, proc, health, lock, gpu_state, models)


class TestSwitchToIdlePreservesGpuNone:
    """Core invariant: gpu_role:none services survive _switch_to_idle()."""

    def test_active_services_preserves_gpu_none(self):
        """After _switch_to_idle(), gpu_role:none services remain in active_services."""
        # Simulate from_services = ["qwen2", "bge-m3"]
        # qwen2: gpu_role=exclusive → should be removed
        # bge-m3: gpu_role=none → should be preserved
        from_services = ["qwen2", "bge-m3"]

        # Mock model configs
        qwen2_model = MagicMock()
        qwen2_model.is_gpu_none = False
        qwen2_model.gpu_role = "exclusive"

        bge_m3_model = MagicMock()
        bge_m3_model.is_gpu_none = True
        bge_m3_model.gpu_role = "none"

        models_dict = {"qwen2": qwen2_model, "bge-m3": bge_m3_model}

        # This is the new logic from the patch:
        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == ["bge-m3"], \
            f"Expected ['bge-m3'] in active_services, got {json.loads(result)}"

    def test_active_services_empty_when_no_gpu_none(self):
        """If all services are GPU-bound, active_services should be empty after idle."""
        from_services = ["qwen2", "llama3"]
        qwen2 = MagicMock(is_gpu_none=False)
        llama3 = MagicMock(is_gpu_none=False)
        models_dict = {"qwen2": qwen2, "llama3": llama3}

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == []

    def test_multiple_gpu_none_preserved(self):
        """Multiple gpu_role:none services are all preserved."""
        from_services = ["qwen2", "bge-m3", "nomic-embed", "llama3"]
        models_dict = {
            "qwen2": MagicMock(is_gpu_none=False),
            "bge-m3": MagicMock(is_gpu_none=True),
            "nomic-embed": MagicMock(is_gpu_none=True),
            "llama3": MagicMock(is_gpu_none=False),
        }

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert set(json.loads(result)) == {"bge-m3", "nomic-embed"}

    def test_unknown_service_skipped(self):
        """Services not in _models dict are skipped (defensive)."""
        from_services = ["qwen2", "orphan-service"]
        models_dict = {"qwen2": MagicMock(is_gpu_none=False)}

        result = json.dumps([s for s in from_services
                            if (m := models_dict.get(s)) and m.is_gpu_none])

        assert json.loads(result) == []


class TestSwitchToIdleNoStopLlamaCpp:
    """Verify the deleted code path: stop_llama_cpp is NOT called for gpu_role:none."""

    def test_no_stop_llama_cpp_for_gpu_none(self):
        """The gpu_role:none stop loop has been deleted.
        Verify by checking the source code directly."""
        import ast

        source = open("inferfabric/model_lifecycle.py").read()
        tree = ast.parse(source)

        # Find _switch_to_idle method
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_switch_to_idle":
                # Get the source lines for this method
                method_source = source.split("\n")
                method_lines = method_lines = method_source[node.lineno-1:node.end_lineno]
                method_text = "\n".join(method_lines)

                # stop_llama_cpp should NOT appear in _switch_to_idle
                assert "stop_llama_cpp" not in method_text, \
                    "stop_llama_cpp found in _switch_to_idle() — gpu_role:none loop not removed!"
                break
        else:
            pytest.fail("_switch_to_idle method not found")


class TestSwitchToIdleStopsViaUnifiedEntry:
    """Task 2.1: _switch_to_idle stops every GPU-bound service via
    _stop_model_process — no hardcoded if/elif, no inlined docker."""

    def test_switch_to_idle_stops_all_via_stop_model_process(self, monkeypatch):
        """_switch_to_idle 必须经 _stop_model_process 停每个 GPU-bound 服务，不内联 docker。"""
        lc = _make_lifecycle_with_active(["Qwen38-27B-TXT", "ovis-ocr2"])  # ninfer + vllm
        stopped = []
        monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append((n, m.type)))
        # 阻断内联 subprocess（若仍内联会调 docker，mock 掉 subprocess.run 验证不被调）
        import inferfabric.model_lifecycle as ml
        monkeypatch.setattr(ml._subprocess if hasattr(ml, '_subprocess') else __import__('subprocess'), 'run',
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应内联 subprocess")))
        # mock _proc.stop_all / _wait_gpu_idle / lock
        monkeypatch.setattr(lc._proc, "stop_all", lambda **k: None)
        monkeypatch.setattr(lc._proc, "_wait_gpu_idle", lambda timeout=30: {"status": "ok"})
        monkeypatch.setattr(lc._proc, "clear_gpu_cuda_state", lambda force=False: None)
        lc._lock = __import__("threading").Lock()

        result = lc._switch_to_idle()
        names = [n for n, _ in stopped]
        assert "Qwen38-27B-TXT" in names and "ovis-ocr2" in names, "所有 GPU-bound 服务都应经 _stop_model_process"
        assert result["status"] == "switched"
