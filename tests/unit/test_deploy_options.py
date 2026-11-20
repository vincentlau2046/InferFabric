"""deploy_wizard.deploy_options — 能力×引擎×平台收窄（部署向导计划 Task 2）。

收窄在 Python 侧完成且不可绕过（向导内不出被收窄掉的选项）：
- darwin/arm64 → llama_cpp（Metal 全卸载）
- linux + nvidia-smi → vllm/sglang/llama_cpp
- 无 NVIDIA（含探测失败）→ 剔除 vllm/sglang（Review Focus 2）
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.proxy import deploy_wizard as dw


import pytest


@pytest.fixture(autouse=True)
def _stub_engine_probe(monkeypatch):
    """全文件打桩真实引擎探测：_default_engine_available 会经
    get_pack_manager() 创建 packs 单例（真实 IFF_DATA_DIR），污染依赖
    MACFABRIC_*_DIR 环境注入的后续用例（test_packs_api.
    test_singleton_defaults_from_env）。可用性语义由显式注入用例覆盖。"""
    monkeypatch.setattr(dw, "_default_engine_available", lambda eid: True)


class TestPlatformNarrowing:
    def test_darwin_arm64_narrows_to_llama_cpp(self):
        opts = dw.deploy_options(os_name="darwin", arch="arm64", has_nvidia=False)
        assert [e["id"] for e in opts["engines"]] == ["llama_cpp"]
        assert opts["platform"] == {"os": "darwin", "arch": "arm64", "nvidia": False}

    def test_linux_nvidia_keeps_all_engines(self):
        opts = dw.deploy_options(os_name="linux", arch="x86_64", has_nvidia=True)
        ids = [e["id"] for e in opts["engines"]]
        assert set(ids) == {"vllm", "sglang", "llama_cpp"}

    def test_linux_no_nvidia_drops_gpu_engines(self):
        opts = dw.deploy_options(os_name="linux", arch="x86_64", has_nvidia=False)
        ids = [e["id"] for e in opts["engines"]]
        assert "vllm" not in ids and "sglang" not in ids
        assert "llama_cpp" in ids

    def test_probe_failure_treated_as_no_nvidia(self, monkeypatch):
        # Review Focus 2：探测失真按「无 NVIDIA」收窄，绝不出 vllm 选项。
        # PR #21 实测评审 I1：has_nvidia=None 语义是「执行真实探测」而非
        # 「探测失败」—— 有卡的机器上该写法恒红。改为在 subprocess 层
        # 模拟探测失败（nvidia-smi 挂起），走缺省探测路径验证收窄契约。
        def _hang(*a, **k):
            raise TimeoutError("nvidia-smi hung")
        monkeypatch.setattr(dw.subprocess, "run", _hang)
        opts = dw.deploy_options(os_name="linux", arch="x86_64")
        ids = [e["id"] for e in opts["engines"]]
        assert "vllm" not in ids and "sglang" not in ids
        assert opts["platform"]["nvidia"] is False

    def test_darwin_x86_still_narrows_to_llama_cpp(self):
        # 平台收窄裁决只对 darwin/arm64 放开——但 darwin 任何情况无 vllm/sglang
        opts = dw.deploy_options(os_name="darwin", arch="x86_64", has_nvidia=False)
        assert [e["id"] for e in opts["engines"]] == ["llama_cpp"]


class TestShape:
    def test_capabilities_and_modes(self):
        opts = dw.deploy_options(os_name="darwin", arch="arm64", has_nvidia=False)
        assert opts["capabilities"] == ["llm", "embedding", "tts", "asr", "ocr"]
        assert opts["deployment_modes"]["llama_cpp"] == ["local-gguf", "packs-catalog"]
        assert opts["deployment_modes"]["vllm"] == ["local-dir"]

    def test_engine_unavailable_flag_via_probe(self):
        # 可用性注入：探测 False → available=False（前端灰置 deep-link packs）
        opts = dw.deploy_options(os_name="darwin", arch="arm64", has_nvidia=False,
                                 engine_available=lambda eid: False)
        assert opts["engines"][0]["available"] is False
        assert opts["engines"][0]["label"] == "llama.cpp"

    def test_default_probe_reads_system(self, monkeypatch):
        monkeypatch.setattr(dw.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(dw.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(dw.shutil, "which", lambda p: None)
        # GPU 探测走 nvidia-smi -L 子进程（评审 F7：二进制存在 ≠ 有可用 GPU）
        monkeypatch.setattr(dw.subprocess, "run",
                            lambda *a, **k: _raise(TimeoutError("slow")))
        # 引擎真实探测已由 autouse _stub_engine_probe 打桩（避免 packs 单例污染）
        opts = dw.deploy_options()
        assert [e["id"] for e in opts["engines"]] == ["llama_cpp"]

    def test_nvidia_probe_requires_device_list(self, monkeypatch):
        # nvidia-smi 在 PATH 但无 GPU 设备（容器）→ 按无 NVIDIA 收窄
        import types
        monkeypatch.setattr(dw.platform, "system", lambda: "Linux")
        monkeypatch.setattr(dw.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(dw.shutil, "which", lambda p: "/usr/bin/nvidia-smi")
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: ok)
        opts = dw.deploy_options()
        assert "vllm" not in [e["id"] for e in opts["engines"]]
        # 有设备清单 → 放开
        ok2 = types.SimpleNamespace(returncode=0,
                                    stdout="GPU 0: Tesla (uuid...)\n", stderr="")
        monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: ok2)
        opts = dw.deploy_options()
        assert "vllm" in [e["id"] for e in opts["engines"]]

    def test_nvidia_probe_nonzero_exit_is_no_gpu(self, monkeypatch):
        import types
        monkeypatch.setattr(dw.platform, "system", lambda: "Linux")
        monkeypatch.setattr(dw.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(dw.shutil, "which", lambda p: "/usr/bin/nvidia-smi")
        bad = types.SimpleNamespace(returncode=9, stdout="", stderr="driver err")
        monkeypatch.setattr(dw.subprocess, "run", lambda *a, **k: bad)
        opts = dw.deploy_options()
        assert "vllm" not in [e["id"] for e in opts["engines"]]


def _raise(exc):
    raise exc


class TestRouteWiring:
    def test_route_registered_with_admin_gate(self):
        from inferfabric.proxy import handler
        assert "/admin/deploy/options" in handler._GET_ROUTES
