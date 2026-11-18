"""Docker-first 隔离硬化（issue #3）：权重挂载一律 :ro + deployment: docker 选路。

契约：
- vllm/sglang build_docker_cmd：模型权重目录与 MODEL_BASE 挂载只读（容器不得
  写宿主权重）；HF cache 保持可写（tokenizer 加载落 lock 文件）。
- ninfer start_ninfer：weight_dir 挂载只读。
- ModelConfig.resolved_deployment：顶层 deployment: docker 显式声明优先于
  引擎类型推导（YAML 写 deployment: docker 即启用 docker 路径）。

全部纯逻辑（fake subprocess / 构造 config），无网络/GPU 依赖。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inferfabric.config import MODEL_BASE, SGLangConfig, VLLMConfig


def _mounts(cmd):
    """从 docker run argv 提取 -v 挂载列表。"""
    return [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]


class TestVllmDockerRoMounts:

    def test_weight_and_model_base_mounts_readonly(self, tmp_path, monkeypatch):
        cfg = VLLMConfig(model_dir="m", served_name="foo", conda_env="v",
                         port=8000, max_model_len=4096,
                         gpu_memory_utilization=0.9)
        cmd = cfg.build_docker_cmd("vllm-test")
        ms = _mounts(cmd)
        model_ro = f"{MODEL_BASE / 'm'}:{MODEL_BASE / 'm'}:ro"
        base_ro = f"{MODEL_BASE}:/models:ro"
        assert model_ro in ms
        assert base_ro in ms
        # 不得出现无 :ro 的权重挂载
        assert f"{MODEL_BASE / 'm'}:{MODEL_BASE / 'm'}" not in ms

    def test_hf_cache_mount_stays_writable(self, tmp_path):
        cfg = VLLMConfig(model_dir="m", served_name="foo", conda_env="v",
                         port=8000, max_model_len=4096,
                         gpu_memory_utilization=0.9)
        cmd = cfg.build_docker_cmd("vllm-test")
        hf = f"{Path.home() / '.cache/huggingface'}:/root/.cache/huggingface"
        assert hf in _mounts(cmd)  # 无 :ro 后缀 = 可写


class TestSglangDockerRoMounts:

    def test_weight_and_model_base_mounts_readonly(self):
        cfg = SGLangConfig(model_dir="m", served_name="foo", port=8006,
                           conda_env="")
        cmd = cfg.build_docker_cmd("sglang-test")
        ms = _mounts(cmd)
        assert f"{MODEL_BASE / 'm'}:{MODEL_BASE / 'm'}:ro" in ms
        assert f"{MODEL_BASE}:/models:ro" in ms


class TestNinferRoMount:

    def test_weight_dir_mount_readonly(self, monkeypatch, tmp_path):
        from inferfabric.config import NInferConfig
        from inferfabric.process_manager.ninfer import (
            NInferProcessManager,
        )
        from inferfabric.state import StateDB

        pm = NInferProcessManager(StateDB(tmp_path / "s.db"), tmp_path)
        cfg = NInferConfig(
            port=8007, container_name="iff-ninfer-ro",
            weight_path=str(tmp_path / "weights" / "model.np4"),
            docker_image="ninfer:latest", model_id="m1",
            max_concurrency=8, max_context=615000, kv_capacity=0,
            default_max_tokens=8192, pending_timeout_ms=300000,
        )
        runs = []

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                runs.append(args)
                return MagicMock(returncode=0, stdout="cid123", stderr="")
            if args[:2] == ["docker", "inspect"]:
                if "\t" in args[3]:
                    return MagicMock(returncode=1, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="true", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(
            "inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        monkeypatch.setattr(
            "inferfabric.process_manager.ninfer.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "inferfabric.process_manager.ninfer.wait_http",
            lambda url, timeout=120: True)

        pm.start_ninfer(cfg, container_name="ninfer-ro-test")
        assert runs, "必须有一次 docker run（detached）"
        ms = _mounts(runs[0])
        assert f"{tmp_path / 'weights'}:/workspace:ro" in ms
        # 不得出现无 :ro 的权重挂载
        assert f"{tmp_path / 'weights'}:/workspace" not in ms


class TestDeploymentExplicit:

    def test_yaml_deployment_docker_wins(self):
        """顶层 deployment: docker 显式声明 → docker 路径（conda_env 并存仅回滚用）。"""
        from inferfabric.config import ModelConfig
        cfg = ModelConfig(
            name="pilot", description="docker pilot",
            vllm=VLLMConfig(
                model_dir="m", served_name="pilot", conda_env="qwen36-35b-vl",
                port=8008, max_model_len=163840,
                gpu_memory_utilization=0.85),
            deployment="docker")
        assert cfg.resolved_deployment == "docker"
