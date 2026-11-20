"""ModelManager.install_model — /deploy 断链修复（部署向导计划 Task 1）。

上游 main 的 /deploy 处理器调用 pm.mgr.install_model，但该方法只存在于
fork manager.py — main 上为 AttributeError（潜在断链）。本移植对齐 fork
语义：装载=生成 models.d YAML + 热重载，**不启动服务**（Mac 裁决：
模型不默认启动，启动留给首请求唤醒或显式 switch）。
"""
from pathlib import Path

import pytest

from inferfabric.manager import ModelManager


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
