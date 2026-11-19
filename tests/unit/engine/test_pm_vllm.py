# tests/unit/engine/test_pm_vllm.py
"""VLLMProcessManager — start_vllm_docker 启动器（deployment: docker，镜像 start_sglang/start_ninfer）。"""
from unittest.mock import MagicMock


def _make_pm(tmp_path):
    from inferfabric.process_manager.vllm import VLLMProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "v.db")
    return VLLMProcessManager(state, tmp_path)


class TestStartVLLMDocker:
    def test_start_vllm_docker_tracks_pid_and_health(self, monkeypatch, tmp_path):
        """start_vllm_docker(cfg, container_name): Popen docker run, track vllm_pid (PGID), health → healthy。"""
        import subprocess
        from inferfabric.config import VLLMConfig
        pm = _make_pm(tmp_path)
        cfg = VLLMConfig(model_dir="/m", served_name="foo", conda_env="vllm",
                        port=8000, max_model_len=4096, gpu_memory_utilization=0.9)

        def fake_popen(cmd, **kw):
            return MagicMock(pid=555, poll=lambda: None, returncode=None)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr("inferfabric.process_manager.vllm.os.environ", {"PATH": "/x"})
        monkeypatch.setattr("inferfabric.process_manager.vllm.time.sleep", lambda *a: None)
        monkeypatch.setattr("inferfabric.process_manager.vllm.check_http_status", lambda *a, **k: "✅")
        # issue #8-③ fail-fast 接线后，这里显式注入“拉取成功”，
        # 使本测试继续聚焦 pid/cmd 契约（原 fake run 并未模拟 docker inspect）。
        monkeypatch.setattr("inferfabric.process_manager.vllm.ensure_image_pulled",
                            lambda image, **k: {"status": "ok", "pulled": False, "image": image})

        result = pm.start_vllm_docker(cfg, container_name="vllm-foo")
        assert result["status"] == "healthy", f"应 healthy: {result}"
        assert pm.vllm_pid == 555  # start_new_session → PID == PGID

    def test_start_vllm_docker_builds_cmd_with_container_name(self, monkeypatch, tmp_path):
        """start_vllm_docker 传 container_name 给 build_docker_cmd（--name 用参数）。"""
        import subprocess
        from inferfabric.config import VLLMConfig
        pm = _make_pm(tmp_path)
        cfg = VLLMConfig(model_dir="/m", served_name="foo", conda_env="vllm",
                        port=8000, max_model_len=4096, gpu_memory_utilization=0.9)
        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            return MagicMock(pid=555, poll=lambda: None, returncode=None)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr("inferfabric.process_manager.vllm.os.environ", {"PATH": "/x"})
        monkeypatch.setattr("inferfabric.process_manager.vllm.time.sleep", lambda *a: None)
        monkeypatch.setattr("inferfabric.process_manager.vllm.check_http_status", lambda *a, **k: "✅")
        # issue #8-③ fail-fast 接线后，这里显式注入“拉取成功”，
        # 使本测试继续聚焦 pid/cmd 契约（原 fake run 并未模拟 docker inspect）。
        monkeypatch.setattr("inferfabric.process_manager.vllm.ensure_image_pulled",
                            lambda image, **k: {"status": "ok", "pulled": False, "image": image})

        pm.start_vllm_docker(cfg, container_name="vllm-custom")
        cmd = captured["cmd"]
        assert "--name" in cmd
        assert cmd[cmd.index("--name") + 1] == "vllm-custom"

    def test_start_vllm_docker_immediate_exit_returns_error(self, monkeypatch, tmp_path):
        """容器立即退出（poll 非 None）：返回 error + 清 vllm_pid。"""
        import subprocess
        from inferfabric.config import VLLMConfig
        pm = _make_pm(tmp_path)
        cfg = VLLMConfig(model_dir="/m", served_name="foo", conda_env="vllm",
                        port=8000, max_model_len=4096, gpu_memory_utilization=0.9)

        def fake_popen(cmd, **kw):
            return MagicMock(pid=555, poll=lambda: 1, returncode=1)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr("inferfabric.process_manager.vllm.os.environ", {"PATH": "/x"})
        monkeypatch.setattr("inferfabric.process_manager.vllm.time.sleep", lambda *a: None)

        result = pm.start_vllm_docker(cfg, container_name="vllm-foo")
        assert result["status"] == "error"
        assert pm.vllm_pid is None  # 清状态
