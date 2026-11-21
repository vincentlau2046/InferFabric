"""ProcessManager facade — ninfer 注册 + 透传 + 状态层（Task 3）。"""
from unittest.mock import MagicMock


def _make_facade(tmp_path):
    from inferfabric.process_manager.facade import ProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return ProcessManager(state, tmp_path)


class TestFacadeNInferDelegation:
    def test_start_ninfer_threads_container_name(self, tmp_path):
        """facade.start_ninfer(cfg, container_name) 透传给 NInferProcessManager。"""
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.start_ninfer.return_value = {"status": "healthy"}
        cfg = MagicMock()
        result = pm.start_ninfer(cfg, container_name="iff-ninfer-qwen38")
        pm._ninfer.start_ninfer.assert_called_once_with(cfg, "iff-ninfer-qwen38")

    def test_stop_ninfer_threads_container_name(self, tmp_path):
        """facade.stop_ninfer(container_name) 透传。"""
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.stop_ninfer.return_value = {"status": "ok"}
        result = pm.stop_ninfer("ninfer-8007")
        pm._ninfer.stop_ninfer.assert_called_once_with("ninfer-8007")

    def test_start_sglang_threads_container_name(self, tmp_path):
        """facade.start_sglang(cfg, container_name) 透传（Task 2 签名）。"""
        pm = _make_facade(tmp_path)
        pm._sglang = MagicMock()
        pm._sglang.start_sglang.return_value = {"status": "healthy"}
        cfg = MagicMock()
        pm.start_sglang(cfg, container_name="sglang-foo")
        pm._sglang.start_sglang.assert_called_once_with(cfg, "sglang-foo")

    def test_stop_all_clears_ninfer_state(self, tmp_path):
        """stop_all(active_services=[]) 必须清 ninfer PID + container（与 sglang 对称）。"""
        pm = _make_facade(tmp_path)
        pm._set_ninfer_pid(123)
        pm._set_ninfer_container("ninfer-8007")
        pm.stop_all(active_services=[])
        assert pm.ninfer_pid is None
        assert pm.ninfer_container is None


class TestFacadeLlamaCppManagedForwarding:
    """安全复审 HIGH（availability regression）：engine_adapter/llama_cpp.py
    总是以 managed= kwarg 调 facade.start_llama_cpp（legacy 传 None），facade
    签名缺参 = 一切 llama_cpp adapter 冷启动 TypeError。集成试点测试直打
    LlamaCppProcessManager 绕过了 facade，故此处补 facade 层透传回归。"""

    def test_start_llama_cpp_forwards_managed(self, tmp_path):
        pm = _make_facade(tmp_path)
        pm._ollama = MagicMock()
        pm._ollama.start_llama_cpp.return_value = {"status": "healthy"}
        cfg = MagicMock()
        managed = object()
        r = pm.start_llama_cpp(cfg, "embedding", managed=managed)
        pm._ollama.start_llama_cpp.assert_called_once_with(
            cfg, "embedding", managed=managed)
        assert r == {"status": "healthy"}

    def test_start_llama_cpp_legacy_none_managed(self, tmp_path):
        """legacy 路径（managed=None）同样必须可达——回归本体是 kwarg 缺参。"""
        pm = _make_facade(tmp_path)
        pm._ollama = MagicMock()
        pm._ollama.start_llama_cpp.return_value = {"status": "healthy"}
        cfg = MagicMock()
        r = pm.start_llama_cpp(cfg, "")
        pm._ollama.start_llama_cpp.assert_called_once_with(cfg, "", managed=None)
        assert r == {"status": "healthy"}

    def test_adapter_start_through_facade_no_typeerror(self, tmp_path):
        """回归现场：adapter.start(model) → facade → launcher 全链路不 TypeError。"""
        from types import SimpleNamespace
        from inferfabric.engine_adapter.llama_cpp import LlamaCppAdapter
        pm = _make_facade(tmp_path)
        pm._ollama = MagicMock()
        pm._ollama.start_llama_cpp.return_value = {"status": "healthy"}
        adapter = LlamaCppAdapter()
        adapter._proc = pm
        model = SimpleNamespace(llama_cpp=MagicMock(), model_type="embedding")
        r = adapter.start(model)          # managed 缺省 = None（legacy）
        assert r == {"status": "healthy"}
        r2 = adapter.start(model, managed=object())  # 受管路径
        assert r2 == {"status": "healthy"}
