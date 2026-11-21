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
        pm._ninfer.start_ninfer.assert_called_once_with(
            cfg, "iff-ninfer-qwen38", managed=None)

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
class TestFacadeNInferManagedForwarding:
    """PR #27 同类遗漏（真机首发现）：engine_adapter/ninfer.py 总是以
    managed= kwarg 调 facade.start_ninfer（legacy 传 None），facade 签名缺参
    = 一切 NInfer adapter 冷启动 TypeError——主 TXT 引擎不可启动。
    同样补 facade 层透传回归。"""

    def test_start_ninfer_forwards_managed(self, tmp_path):
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.start_ninfer.return_value = {"status": "healthy"}
        cfg = MagicMock()
        managed = object()
        r = pm.start_ninfer(cfg, "c-name", managed=managed)
        pm._ninfer.start_ninfer.assert_called_once_with(
            cfg, "c-name", managed=managed)
        assert r == {"status": "healthy"}

    def test_start_ninfer_legacy_none_managed(self, tmp_path):
        """legacy 路径（managed=None）同样必须可达——回归本体是 kwarg 缺参。"""
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.start_ninfer.return_value = {"status": "healthy"}
        cfg = MagicMock()
        r = pm.start_ninfer(cfg, "c-name")
        pm._ninfer.start_ninfer.assert_called_once_with(
            cfg, "c-name", managed=None)
        assert r == {"status": "healthy"}

    def test_ninfer_adapter_start_through_facade_no_typeerror(self, tmp_path):
        """回归现场：NInferAdapter.start(model) → facade → launcher 不 TypeError。"""
        from types import SimpleNamespace
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        pm = _make_facade(tmp_path)
        pm._ninfer = MagicMock()
        pm._ninfer.start_ninfer.return_value = {"status": "healthy"}
        adapter = NInferAdapter()
        adapter._proc = pm
        model = SimpleNamespace(ninfer=MagicMock(), container_name="c-name")
        r = adapter.start(model)          # managed 缺省 = None（legacy）
        assert r == {"status": "healthy"}
        r2 = adapter.start(model, managed=object())  # 受管路径
        assert r2 == {"status": "healthy"}


class TestFacadeLlamaCppManagedChatProbeDispatch:
    """Mac chat 包（#23）：探针分派在 facade 全链路生效 —— 真实 sub-pm
    （非 mock）走 facade.start_llama_cpp → launcher，openai_chat_ready_v1
    包必须被 chat 探针放行、绝不被 embeddings 探针误杀（Linux 教训：
    只测 launcher 层会漏链路回归）。"""

    def test_chat_probe_dispatch_through_facade(self, tmp_path, monkeypatch):
        import inferfabric.process_manager.llama_cpp as llm_mod
        pm = _make_facade(tmp_path)

        pack_dir = tmp_path / "packs_root" / "llm.qwen3-4b-mac" / "1.0.0"
        (pack_dir / "runtime").mkdir(parents=True)
        (pack_dir / "runtime" / "engine").write_text("binary")
        (pack_dir / "models").mkdir(parents=True)
        (pack_dir / "models" / "model").write_text("gguf")
        from inferfabric.packs.managed_runtime import ResolvedRuntime
        inst = ResolvedRuntime(
            pack_id="llm.qwen3-4b-mac", pack_version="1.0.0",
            template_name="llama-cpp-macos-arm64-v1",
            artifact_identities=(), role="owned", model_name="qmac",
            marker={"marker_version": 1, "pack_id": "llm.qwen3-4b-mac",
                    "pack_version": "1.0.0",
                    "runtime_template": "llama-cpp-macos-arm64-v1",
                    "artifact_identities": [], "role": "owned"},
            pack_dir=str(pack_dir), healthcheck="openai_chat_ready_v1")
        from inferfabric.config import LlamaCppConfig
        cfg = LlamaCppConfig(model_path=str(pack_dir / "models" / "model"),
                             binary_path=str(pack_dir / "runtime" / "engine"),
                             served_name="qmac", port=8130, threads=4,
                             context_size=8192, gpu_layers=99)

        popens, chat_probes, emb_probes = [], [], []

        class _FakeProc:
            pid = 4321
            def poll(self):
                return None

        monkeypatch.setattr(llm_mod.subprocess, "Popen",
                            lambda cmd, **kw:
                            (popens.append(cmd), _FakeProc())[1])
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(llm_mod, "wait_http",
                            lambda url, timeout=120: True)
        monkeypatch.setattr(llm_mod.LlamaCppProcessManager,
                            "_probe_chat_ready",
                            staticmethod(
                                lambda port, timeout=2:
                                (chat_probes.append(port), (True, ""))[1]))
        monkeypatch.setattr(llm_mod.LlamaCppProcessManager,
                            "_probe_embeddings",
                            staticmethod(
                                lambda port, timeout=10:
                                (emb_probes.append(port),
                                 (False, "must not be called"))[1]))

        r = pm.start_llama_cpp(cfg, "llm", managed=inst)
        assert r["status"] == "healthy", r
        assert chat_probes == [8130] and emb_probes == []
