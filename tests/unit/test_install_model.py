"""ModelManager.install_model — /deploy 断链修复（部署向导计划 Task 1）。

上游 main 的 /deploy 处理器调用 pm.mgr.install_model，但该方法只存在于
fork manager.py — main 上为 AttributeError（潜在断链）。本移植对齐 fork
语义：装载=生成 models.d YAML + 热重载，**不启动服务**（Mac 裁决：
模型不默认启动，启动留给首请求唤醒或显式 switch）。
"""
from pathlib import Path

import pytest

from inferfabric.manager import ModelManager


def _patch_linux(monkeypatch):
    """install_yaml 的平台分支打桩：强制走非 macOS 分支（本机为 macOS）。"""
    from inferfabric.platform import probe as _probe
    monkeypatch.setattr(_probe, "IS_MACOS", False)


def _mgr(tmp_path: Path) -> ModelManager:
    # 两层布局（<ws>/models.d）：install_yaml 的 GGUF 查找目录 = parents[1]/models
    # （生产语义：<workspace>/models.d → <workspace>/models）
    models_d = tmp_path / "ws" / "models.d"
    models_d.mkdir(parents=True, exist_ok=True)   # 生产中 models.d 恒存在
    return ModelManager(models_dir=str(models_d))


def test_install_model_writes_yaml_and_reloads(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    # install_yaml（macOS llama_cpp 分支）要求 GGUF 已在本地——权重下载不在
    # install 语义内（与 fork 一致；ModelScope 下载通道是否入本期=独立裁决）。
    ws_models = Path(mgr.models_dir).parents[1] / "models"
    ws_models.mkdir(parents=True, exist_ok=True)
    (ws_models / "m1.gguf").write_bytes(b"GGUF")
    calls = []
    monkeypatch.setattr(mgr, "reload_models", lambda: calls.append(1))
    result = mgr.install_model("m1", "llama_cpp")
    assert result["status"] == "installed"
    assert (Path(mgr.models_dir) / "m1.yaml").exists()
    assert calls == [1], "installed 必须热重载一次"


def test_install_model_missing_gguf_is_actionable_error(tmp_path):
    mgr = _mgr(tmp_path)
    result = mgr.install_model("nope", "llama_cpp")
    assert result["status"] == "error"
    assert "GGUF" in result["message"]


def test_install_model_existing_yaml_is_already_configured(tmp_path):
    mgr = _mgr(tmp_path)
    ydir = Path(mgr.models_dir)
    ydir.mkdir(parents=True, exist_ok=True)
    (ydir / "m1.yaml").write_text("name: m1\ntype: llama_cpp\n", encoding="utf-8")
    result = mgr.install_model("m1", "llama_cpp")
    assert result["status"] == "already_configured"


class TestInstallNameValidation:
    """评审 F1（Critical）：服务端文件系统边界 —— 浏览器正则不是边界。

    断言错误消息含「无效名称」：校验必须先于 GGUF 查找（否则 macOS
    分支的 GGUF-glob 巧合会掩盖穿越名，在其他平台/类型下不设防）。
    """

    def test_rejects_path_traversal_and_absolute(self, tmp_path):
        mgr = _mgr(tmp_path)
        for bad in ("../evil", "/abs/evil", "a/b", "..", "sub/dir"):
            result = mgr.install_model(bad, "llama_cpp")
            assert result["status"] == "error", bad
            assert "无效名称" in result["message"], bad
            assert not list(Path(mgr.models_dir).parents[1].rglob("*evil*"))

    def test_rejects_yaml_ambiguous_names(self, tmp_path):
        # name: on 会被 yaml 解析为布尔 True，破坏后续加载（评审 F1）
        mgr = _mgr(tmp_path)
        for bad in ("on", "true", "yes", "null", "off"):
            result = mgr.install_model(bad, "llama_cpp")
            assert result["status"] == "error", bad
            assert "无效名称" in result["message"], bad

    def test_rejects_empty_and_overlong(self, tmp_path):
        mgr = _mgr(tmp_path)
        assert mgr.install_model("", "llama_cpp")["status"] == "error"
        assert mgr.install_model("x" * 65, "llama_cpp")["status"] == "error"


class TestInstallYamlLinuxGgufContract:
    """PR #21 实测评审 B1（Blocking）：GGUF 查找+缺失报错必须平台无关。

    Linux 实测：非 macOS 分支不做 glob 校验，无条件写 `~/models/{name}/`
    → 一键链「假成功」（YAML model_path 指向不存在目录，首请求必败）。
    Metal 字段（ledger_mode/binary_path/gpu_layers=99）保留平台分支。
    """

    def test_linux_llama_cpp_resolves_downloaded_gguf(self, tmp_path, monkeypatch):
        _patch_linux(monkeypatch)
        mgr = _mgr(tmp_path)
        ws_models = Path(mgr.models_dir).parents[1] / "models" / "m"
        ws_models.mkdir(parents=True)
        gguf = ws_models / "m-q4_k_m.gguf"
        gguf.write_bytes(b"GGUF")
        monkeypatch.setattr(mgr, "reload_models", lambda: None)
        result = mgr.install_model("m", "llama_cpp")
        assert result["status"] == "installed", result
        yaml_text = (Path(mgr.models_dir) / "m.yaml").read_text(encoding="utf-8")
        assert "model_path: ~/models/m/" not in yaml_text, \
            "B1：不得写不存在的字面量目录（假成功）"
        assert f"model_path: {gguf}" in yaml_text, "model_path 须指向真实 GGUF"

    def test_linux_llama_cpp_missing_gguf_is_error(self, tmp_path, monkeypatch):
        _patch_linux(monkeypatch)
        mgr = _mgr(tmp_path)
        monkeypatch.setattr(mgr, "reload_models", lambda: None)
        result = mgr.install_model("nope", "llama_cpp")
        assert result["status"] == "error"
        assert "GGUF" in result["message"]
        assert not (Path(mgr.models_dir) / "nope.yaml").exists(), \
            "B1：失败不得落 YAML（UI 显示已装载的假成功源头）"
