"""
unit/engine/test_tts_lifecycle.py — TTS 服务生命周期测试

测试对象: inferfabric.model_lifecycle.ModelLifecycle, process_manager.ProcessManager
覆盖范围:
  - switch_to_idle 时 TTS 经统一入口停止（Task 2.1，stop_all 不再收引擎特定端口）
  - force_kill_all 杀 TTS 进程组（SIGKILL、fuser 端口清理）
  - 孤儿 PID 检测与恢复（TTS / ComfyUI / vLLM）
  - 防御性测试（无 tts_config、无 tts_port、空 tts_block）
  - deploy_model TTS 失败后端口 & PID 清理
  - deploy_model 失败清理经 _stop_model_process 统一入口（Task 2.2，不内联 docker）
  - modality 回归测试
  - config_hash 排除字段测试
"""

import json
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ═══════════════════════════════════════════════════════════════════
# 1. _switch_to_idle() tts_port full chain
# ═══════════════════════════════════════════════════════════════════

class TestSwitchToIdleTtsPort:
    """Verify _switch_to_idle stops services via the unified entry (Task 2.1).

    Engine stops go through the adapter (proc.stop_tts_server / proc.stop_vllm);
    stop_all is a thin call that only receives active_services — no
    engine-specific ports/cfgs (those were removed in Task 2.1)."""

    def _make_lc(self, models=None):
        """Create ModelLifecycle with mocked dependencies."""
        from inferfabric.model_lifecycle import ModelLifecycle
        from inferfabric.config import ModelConfig, TTSConfig

        mock_state = MagicMock()
        mock_proc = MagicMock()
        mock_proc._wait_gpu_idle.return_value = {"status": "ok"}
        mock_health = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_gpu = MagicMock()
        mock_gpu.reconcile.return_value = {"actions": []}

        lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu, models or {})
        return lc, mock_state, mock_proc

    def test_switch_to_idle_tts_only(self):
        """When only TTS is active, it is stopped via its adapter (proc.stop_tts_server);
        stop_all only receives active_services."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        lc, mock_state, mock_proc = self._make_lc({"tts-qwen3": tts_model})
        mock_state.gpu_mode = "shared"
        mock_state.get_active_services.return_value = ["tts-qwen3"]
        mock_state.get.return_value = ""

        lc._switch_to_idle()

        # TTS stopped via the adapter (Task 2.1: no engine-specific kwargs to stop_all)
        mock_proc.stop_tts_server.assert_called_once_with(port=8880)
        mock_proc.stop_all.assert_called_once()
        kwargs = mock_proc.stop_all.call_args[1]
        assert kwargs == {"active_services": ["tts-qwen3"]}

    def test_switch_to_idle_vllm_and_tts(self):
        """When both vLLM and TTS are active, each is stopped via its adapter."""
        from inferfabric.config import ModelConfig, TTSConfig, VLLMConfig

        vllm_model = ModelConfig(
            name="qwen35-9b-vl", description="test", type="vllm",
            gpu_role="shared",
            vllm=VLLMConfig(
                port=11441, served_name="qwen35-9b-vl", model_dir="/fake",
                conda_env="test", max_model_len=4096, gpu_memory_utilization=0.9,
            ),
        )
        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        lc, mock_state, mock_proc = self._make_lc({
            "qwen35-9b-vl": vllm_model,
            "tts-qwen3": tts_model,
        })
        mock_state.gpu_mode = "shared"
        mock_state.get_active_services.return_value = ["qwen35-9b-vl", "tts-qwen3"]
        mock_state.get.return_value = ""

        lc._switch_to_idle()

        mock_proc.stop_vllm.assert_called_once_with(port=11441)
        mock_proc.stop_tts_server.assert_called_once_with(port=8880)
        kwargs = mock_proc.stop_all.call_args[1]
        assert kwargs == {"active_services": ["qwen35-9b-vl", "tts-qwen3"]}

    def test_switch_to_idle_no_tts(self):
        """When no TTS is active, stop_tts_server is not called and stop_all
        carries no engine-specific kwargs."""
        from inferfabric.config import ModelConfig, VLLMConfig

        vllm_model = ModelConfig(
            name="qwen35-9b-vl", description="test", type="vllm",
            gpu_role="exclusive",
            vllm=VLLMConfig(
                port=11441, served_name="qwen35-9b-vl", model_dir="/fake",
                conda_env="test", max_model_len=4096, gpu_memory_utilization=0.9,
            ),
        )
        lc, mock_state, mock_proc = self._make_lc({"qwen35-9b-vl": vllm_model})
        mock_state.gpu_mode = "exclusive"
        mock_state.get_active_services.return_value = ["qwen35-9b-vl"]
        mock_state.get.return_value = ""

        lc._switch_to_idle()

        mock_proc.stop_tts_server.assert_not_called()
        mock_proc.stop_vllm.assert_called_once_with(port=11441)
        kwargs = mock_proc.stop_all.call_args[1]
        assert kwargs == {"active_services": ["qwen35-9b-vl"]}

    def test_switch_to_idle_clears_tts_pid(self):
        """_switch_to_idle clears tts_pid in state."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        lc, mock_state, mock_proc = self._make_lc({"tts-qwen3": tts_model})
        mock_state.gpu_mode = "shared"
        mock_state.get_active_services.return_value = ["tts-qwen3"]
        mock_state.get.return_value = ""

        lc._switch_to_idle()

        # Check set_multi was called with tts_pid=""
        set_multi_calls = mock_state.set_multi.call_args_list
        assert len(set_multi_calls) > 0
        last_call_kwargs = set_multi_calls[-1][0][0]
        assert "tts_pid" in last_call_kwargs
        assert last_call_kwargs["tts_pid"] == ""

    def test_switch_to_idle_unknown_service_skipped(self):
        """Unknown service name in active_services is safely skipped by the stop loop."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        lc, mock_state, mock_proc = self._make_lc({"tts-qwen3": tts_model})
        mock_state.gpu_mode = "shared"
        # "phantom-svc" is in active_services but not in models dict
        mock_state.get_active_services.return_value = ["tts-qwen3", "phantom-svc"]
        mock_state.get.return_value = ""

        lc._switch_to_idle()

        # Only the known service gets an adapter stop; stop_all still sees the full list
        mock_proc.stop_tts_server.assert_called_once_with(port=8880)
        kwargs = mock_proc.stop_all.call_args[1]
        assert kwargs == {"active_services": ["tts-qwen3", "phantom-svc"]}


# ═══════════════════════════════════════════════════════════════════
# 2. force_kill_all() TTS cleanup
# ═══════════════════════════════════════════════════════════════════

class TestForceKillAllTts:
    """Verify force_kill_all correctly kills TTS processes and cleans state."""

    def _make_pm(self, tmpdir):
        from inferfabric.process_manager import ProcessManager
        from inferfabric.state import StateDB
        state = StateDB(Path(tmpdir) / "state.db")
        pm = ProcessManager(state, log_dir=Path(tmpdir))
        return pm, state

    def test_force_kill_all_kills_tts_pgid(self):
        """force_kill_all sends SIGKILL to TTS process group."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())
        state.set("tts_pid", "55555")

        with patch("os.killpg") as mock_killpg, \
             patch("subprocess.run") as mock_run, \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            result = pm.force_kill_all()

        # SIGKILL sent to tts pgid
        mock_killpg.assert_any_call(55555, signal.SIGKILL)
        assert result["status"] == "ok"

    def test_force_kill_all_pkill_tts_pattern(self):
        """force_kill_all uses targeted pkill pattern for TTS."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())

        with patch("os.killpg"), \
             patch("subprocess.run") as mock_run, \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            pm.force_kill_all()

        # Find the pkill call for TTS
        pkill_calls = [
            c for c in mock_run.call_args_list
            if "pkill" in str(c) and "Qwen3-TTS" in str(c)
        ]
        assert len(pkill_calls) == 1, f"Expected 1 TTS pkill, got: {pkill_calls}"
        call_args = pkill_calls[0][0][0]
        assert "Qwen3-TTS-Openai-Fastapi/api" in call_args

    def test_force_kill_all_fuser_tts_port(self):
        """force_kill_all uses fuser for port-based TTS cleanup."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())

        with patch("os.killpg"), \
             patch("subprocess.run") as mock_run, \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            pm.force_kill_all()

        # Find the fuser call
        fuser_calls = [
            c for c in mock_run.call_args_list
            if "fuser" in str(c) and "8880" in str(c)
        ]
        assert len(fuser_calls) == 1, f"Expected 1 fuser call, got: {[str(c) for c in mock_run.call_args_list]}"
        call_args = fuser_calls[0][0][0]
        assert "fuser" in call_args
        assert "8880/tcp" in call_args

    def test_force_kill_all_clears_tts_pid(self):
        """force_kill_all clears tts_pid after cleanup."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())
        state.set("tts_pid", "55555")

        with patch("os.killpg"), \
             patch("subprocess.run"), \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            pm.force_kill_all()

        assert pm.tts_pid is None

    def test_force_kill_all_no_tts_pid(self):
        """force_kill_all is safe when no TTS PID tracked."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())

        with patch("os.killpg") as mock_killpg, \
             patch("subprocess.run"), \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            result = pm.force_kill_all()

        # No SIGKILL sent for tts (no pid)
        for c in mock_killpg.call_args_list:
            # Should not have a tts pgid call (55555 etc)
            assert c[0][0] != 55555
        assert result["status"] == "ok"

    def test_force_kill_all_tts_pid_process_gone(self):
        """force_kill_all handles ProcessLookupError gracefully for TTS."""
        import subprocess
        pm, state = self._make_pm(tempfile.mkdtemp())
        state.set("tts_pid", "55555")

        def killpg_side_effect(pgid, sig):
            if pgid == 55555:
                raise ProcessLookupError("No such process")

        with patch("os.killpg", side_effect=killpg_side_effect), \
             patch("subprocess.run"), \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            result = pm.force_kill_all()

        assert result["status"] == "ok"
        assert pm.tts_pid is None

    def test_force_kill_all_cleanup_pid_files(self):
        """force_kill_all removes tts_server.pid file."""
        import subprocess
        tmpdir = tempfile.mkdtemp()
        pm, state = self._make_pm(tmpdir)

        # Create pid file
        pid_file = Path(tmpdir) / "tts_server.pid"
        pid_file.write_text("55555")

        with patch("os.killpg"), \
             patch("subprocess.run"), \
             patch.object(pm, "_reap_zombies"), \
             patch.object(pm, "_wait_gpu_idle", return_value={"status": "ok"}), \
             patch("time.sleep"):
            pm.force_kill_all()

        assert not pid_file.exists()


# ═══════════════════════════════════════════════════════════════════
# 3. Orphan PID detection for tts_pid / comfyui_pid
# ═══════════════════════════════════════════════════════════════════

class TestOrphanPidDetection:
    """Test that orphan PID detection covers tts_pid and comfyui_pid.

    After v4.7.0 refactor, _detect_orphan_pids and _restore_dead_pids
    are generic across vllm_pid, comfyui_pid, and tts_pid.
    """

    def _make_gpu_state(self, models=None):
        from inferfabric.gpu_state import GpuStateMachine
        from inferfabric.state import StateDB

        tmpdir = tempfile.mkdtemp()
        state = StateDB(Path(tmpdir) / "state.db")
        mock_proc = MagicMock()
        mock_health = MagicMock()
        mock_lock = MagicMock()

        gpu = GpuStateMachine(state, mock_proc, mock_health, mock_lock, models or {})
        return gpu, state, mock_proc

    def test_orphan_tts_pid_dead_process_cleared(self):
        """If tts_pid points to a dead process, reconcile clears it."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        gpu, state, mock_proc = self._make_gpu_state({"tts-qwen3": tts_model})
        mock_proc.tts_pid = 99999  # non-existent PID
        mock_proc.vllm_pid = None
        mock_proc.comfyui_pid = None
        state.set("tts_pid", "99999")
        state.set("gpu_mode", "shared")
        state.set_active_services(["tts-qwen3"])

        # _port_pid should return None (no process on port 8880)
        with patch.object(gpu, "_port_pid", return_value=None):
            result = gpu.reconcile()

        assert state.get("tts_pid") == "", \
            f"Orphan tts_pid should be cleared, got '{state.get('tts_pid')}'"
        assert any("tts_pid" in a and "dead" in a for a in result["actions"]), \
            f"Expected orphan tts_pid action, got {result['actions']}"

    def test_orphan_comfyui_pid_dead_process_cleared(self):
        """If comfyui_pid points to a dead process, reconcile clears it."""
        from inferfabric.config import ModelConfig, ComfyUIConfig

        comfy_model = ModelConfig(
            name="comfyui", description="test", type="comfyui",
            gpu_role="shared", comfyui=ComfyUIConfig(conda_env="test", port=8188),
        )
        gpu, state, mock_proc = self._make_gpu_state({"comfyui": comfy_model})
        mock_proc.comfyui_pid = 88888  # non-existent PID
        mock_proc.vllm_pid = None
        mock_proc.tts_pid = None
        state.set("comfyui_pid", "88888")
        state.set("gpu_mode", "shared")
        state.set_active_services(["comfyui"])

        with patch.object(gpu, "_port_pid", return_value=None):
            result = gpu.reconcile()

        assert state.get("comfyui_pid") == "", \
            f"Orphan comfyui_pid should be cleared, got '{state.get('comfyui_pid')}'"
        assert any("comfyui_pid" in a and "dead" in a for a in result["actions"])

    def test_orphan_vllm_pid_dead_process_cleared(self):
        """Regression: vllm_pid orphan detection still works after refactor."""
        from inferfabric.config import ModelConfig, VLLMConfig

        vllm_model = ModelConfig(
            name="test-vl", description="test", type="vllm",
            vllm=VLLMConfig(
                port=11441, served_name="test-vl", model_dir="/fake",
                conda_env="test", max_model_len=4096, gpu_memory_utilization=0.9,
            ),
        )
        gpu, state, mock_proc = self._make_gpu_state({"test-vl": vllm_model})
        mock_proc.vllm_pid = 77777
        mock_proc.comfyui_pid = None
        mock_proc.tts_pid = None
        state.set("vllm_pid", "77777")
        state.set("gpu_mode", "exclusive")
        state.set_active_services(["test-vl"])

        with patch.object(gpu, "_port_pid", return_value=None):
            result = gpu.reconcile()

        assert state.get("vllm_pid") == ""
        assert any("vllm_pid" in a and "dead" in a for a in result["actions"])

    def test_stale_tts_pid_with_no_active_services(self):
        """If tts_pid exists but no active services and port is free, PID is cleared."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        gpu, state, mock_proc = self._make_gpu_state({"tts-qwen3": tts_model})
        mock_proc.tts_pid = 99999
        mock_proc.vllm_pid = None
        mock_proc.comfyui_pid = None
        state.set("tts_pid", "99999")
        state.set("gpu_mode", "idle")
        state.set_active_services([])

        # Port 8880 has no process (stale PID, no services)
        with patch.object(gpu, "_port_pid", return_value=None):
            result = gpu.reconcile()

        assert state.get("tts_pid") == ""
        assert any("tts_pid" in a and "Stale" in a for a in result["actions"])

    def test_tts_pid_kept_if_port_occupied(self):
        """If tts_pid is alive and port is occupied, PID is kept (false negative)."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        gpu, state, mock_proc = self._make_gpu_state({"tts-qwen3": tts_model})
        mock_proc.tts_pid = 99999
        mock_proc.vllm_pid = None
        mock_proc.comfyui_pid = None
        state.set("tts_pid", "99999")
        state.set("gpu_mode", "idle")
        state.set_active_services([])

        # Process alive (os.kill(pid,0) succeeds) but no active services
        # Port 8880 still occupied
        with patch("os.kill", return_value=None), \
             patch.object(gpu, "_port_pid", return_value=99999), \
             patch.object(gpu._health, "check_model", return_value="❌"):
            result = gpu.reconcile()

        # PID kept because port is occupied (health check false negative)
        assert state.get("tts_pid") == "99999"
        assert any("still owns port" in a for a in result["actions"])

    def test_restore_tts_pid_via_fuser(self):
        """If TTS port is occupied but tts_pid not tracked, restore via fuser."""
        from inferfabric.config import ModelConfig, TTSConfig

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )
        gpu, state, mock_proc = self._make_gpu_state({"tts-qwen3": tts_model})
        mock_proc.tts_pid = None  # not tracked
        mock_proc.vllm_pid = None
        mock_proc.comfyui_pid = None
        state.set("tts_pid", "")
        state.set("gpu_mode", "shared")
        state.set_active_services(["tts-qwen3"])

        # Mock health check to say TTS is healthy, and fuser finds PID 55555
        with patch.object(gpu, "_port_pid", return_value=55555), \
             patch.object(gpu._health, "check_model", return_value="✅"):
            result = gpu.reconcile()

        assert state.get("tts_pid") == "55555"
        assert any("Recovered tts_pid=55555" in a for a in result["actions"])

    def test_restore_comfyui_pid_via_fuser(self):
        """If ComfyUI port is occupied but comfyui_pid not tracked, restore via fuser."""
        from inferfabric.config import ModelConfig, ComfyUIConfig

        comfy_model = ModelConfig(
            name="comfyui", description="test", type="comfyui",
            gpu_role="shared", comfyui=ComfyUIConfig(conda_env="test", port=8188),
        )
        gpu, state, mock_proc = self._make_gpu_state({"comfyui": comfy_model})
        mock_proc.comfyui_pid = None
        mock_proc.vllm_pid = None
        mock_proc.tts_pid = None
        state.set("comfyui_pid", "")
        state.set("gpu_mode", "shared")
        state.set_active_services(["comfyui"])

        with patch.object(gpu, "_port_pid", return_value=44444), \
             patch.object(gpu._health, "check_model", return_value="✅"):
            result = gpu.reconcile()

        assert state.get("comfyui_pid") == "44444"
        assert any("Recovered comfyui_pid=44444" in a for a in result["actions"])

    def test_force_reset_clears_tts_pid(self):
        """force_reset() clears tts_pid unconditionally."""
        gpu, state, mock_proc = self._make_gpu_state()
        state.set("tts_pid", "99999")
        state.set("comfyui_pid", "88888")
        state.set("gpu_mode", "shared")
        state.set_active_services(["tts-qwen3"])

        with patch.object(gpu._proc, "stop_all"), \
             patch.object(gpu._proc, "force_kill_all"), \
             patch("inferfabric.gpu_state.wait_gpu_free", return_value=True), \
             patch("inferfabric.gpu_state.gpu_used_mb", return_value=500):
            result = gpu.force_reset()

        assert state.get("tts_pid") == ""
        assert state.get("comfyui_pid") == ""

    def test_force_reset_clears_comfyui_pid(self):
        """force_reset() clears comfyui_pid."""
        gpu, state, mock_proc = self._make_gpu_state()
        state.set("comfyui_pid", "12345")
        state.set("gpu_mode", "exclusive")

        with patch.object(gpu._proc, "stop_all"), \
             patch.object(gpu._proc, "force_kill_all"), \
             patch("inferfabric.gpu_state.wait_gpu_free", return_value=True), \
             patch("inferfabric.gpu_state.gpu_used_mb", return_value=500):
            gpu.force_reset()

        assert state.get("comfyui_pid") == ""


# ═══════════════════════════════════════════════════════════════════
# 4. Defensive: type=tts_server but tts=None
# ═══════════════════════════════════════════════════════════════════

class TestTtsServerDefensive:
    """Test defensive behavior when tts_server config is incomplete or malformed."""

    def test_type_tts_server_without_tts_config(self):
        """is_tts_server returns False when type=tts_server but tts=None."""
        from inferfabric.config import ModelConfig

        m = ModelConfig(
            name="broken-tts",
            description="tts_server type but no tts block",
            type="tts_server",
            # tts is None by default
        )
        # is_tts_server requires BOTH type=="tts_server" AND tts is not None
        assert m.is_tts_server is False, \
            "is_tts_server should be False when tts config is missing"

    def test_type_tts_server_without_tts_port_returns_none(self):
        """port returns None when type=tts_server but tts=None."""
        from inferfabric.config import ModelConfig

        m = ModelConfig(
            name="broken-tts",
            description="tts_server type but no tts block",
            type="tts_server",
        )
        assert m.port is None, \
            "port should be None when tts config is missing"

    def test_type_tts_server_without_tts_served_name_fallback(self):
        """served_name returns self.name when tts=None (same as other non-vllm types)."""
        from inferfabric.config import ModelConfig

        m = ModelConfig(
            name="broken-tts",
            description="tts_server type but no tts block",
            type="tts_server",
        )
        # served_name goes through: vllm? no → ollama? no → ollama_cpp? no → return self.name
        assert m.served_name == "broken-tts"

    def test_load_models_missing_tts_block_raises_on_switch(self):
        """A tts_server YAML without tts_server block gets tts=None → is_tts_server=False.
        Attempting to switch to it would fail at _start_model dispatch."""
        from inferfabric.config import load_models

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir)
            yaml_content = """
name: tts-no-block
type: tts_server
gpu_role: shared
# No tts_server: block, no top-level conda_env
"""
            (models_dir / "tts-no-block.yaml").write_text(yaml_content)
            models = load_models(models_dir)
            m = models.get("tts-no-block")
            assert m is not None
            # tts_cfg is None because neither tts_server: block nor top-level fields
            assert m.tts is None, "tts should be None without config block"
            assert m.is_tts_server is False, \
                "is_tts_server should be False without tts config"

    def test_load_models_empty_tts_block(self):
        """A tts_server YAML with empty tts_server: block still gets TTSConfig."""
        from inferfabric.config import load_models

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir)
            yaml_content = """
name: tts-empty
type: tts_server
gpu_role: shared
tts_server:
  conda_env: minimal-env
"""
            (models_dir / "tts-empty.yaml").write_text(yaml_content)
            models = load_models(models_dir)
            m = models.get("tts-empty")
            assert m is not None
            assert m.tts is not None
            assert m.tts.conda_env == "minimal-env"
            assert m.is_tts_server is True

class TestDeployModelFailureTts:
    """Verify _deploy_model failure cleanup handles tts_port + tts_pid."""

    def test_deploy_tts_failure_stops_tts_port(self):
        """When TTS deployment fails, the failed model is stopped via
        _stop_model_process (adapter → proc.stop_tts_server); stop_all only
        receives active_services (PID cleanup, Task 2.3 demotes the facade)."""
        from inferfabric.model_lifecycle import ModelLifecycle
        from inferfabric.config import ModelConfig, TTSConfig, load_models

        mock_state = MagicMock()
        mock_state.get_active_services.return_value = []
        mock_proc = MagicMock()
        # Simulate TTS start failure
        mock_proc.start_tts_server.return_value = {"status": "error", "message": "crashed"}
        mock_proc._wait_gpu_idle.return_value = {"status": "ok"}
        mock_health = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_gpu = MagicMock()
        mock_gpu.reconcile.return_value = {"actions": []}

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )

        lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu,
                            {"tts-qwen3": tts_model})

        # Mock load_models to return the same dict
        with patch("inferfabric.model_lifecycle.load_models", return_value={"tts-qwen3": tts_model}):
            result = lc._deploy_model(tts_model, "shared")

        assert result["status"] == "error"

        # Metadata-driven: the failed model is stopped via _stop_model_process →
        # TTSAdapter.stop → proc.stop_tts_server(port=8880)
        mock_proc.stop_tts_server.assert_called_once_with(port=8880)
        # stop_all is a thin PID-cleanup call — no engine-specific kwargs
        stop_all_calls = mock_proc.stop_all.call_args_list
        assert len(stop_all_calls) == 1
        kwargs = stop_all_calls[0][1]
        assert kwargs == {"active_services": []}, \
            f"stop_all should receive only active_services, got {kwargs}"

    def test_deploy_tts_failure_clears_tts_pid(self):
        """When TTS deployment fails, tts_pid is cleared in state."""
        from inferfabric.model_lifecycle import ModelLifecycle
        from inferfabric.config import ModelConfig, TTSConfig, load_models

        mock_state = MagicMock()
        mock_proc = MagicMock()
        mock_proc.start_tts_server.return_value = {"status": "error", "message": "crashed"}
        mock_proc._wait_gpu_idle.return_value = {"status": "ok"}
        mock_health = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_gpu = MagicMock()
        mock_gpu.reconcile.return_value = {"actions": []}

        tts_model = ModelConfig(
            name="tts-qwen3", description="test", type="tts_server",
            gpu_role="shared", tts=TTSConfig(conda_env="test", port=8880),
        )

        lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu,
                            {"tts-qwen3": tts_model})

        with patch("inferfabric.model_lifecycle.load_models", return_value={"tts-qwen3": tts_model}):
            lc._deploy_model(tts_model, "shared")

        # Check set_multi was called with tts_pid=""
        set_multi_calls = mock_state.set_multi.call_args_list
        found_tts_pid_clear = False
        for c in set_multi_calls:
            args = c[0][0] if c[0] else {}
            if "tts_pid" in args and args["tts_pid"] == "":
                found_tts_pid_clear = True
        assert found_tts_pid_clear, "tts_pid should be cleared on deployment failure"


class TestDeployModelFailureNinfer:
    """Task 2.2: _deploy_model failure-cleanup stops the failed model via
    _stop_model_process (metadata-driven) — no inlined docker stop, and
    stop_all receives only active_services (PID cleanup; Task 2.3 demotes
    the facade)."""

    def test_deploy_ninfer_failure_stops_via_stop_model_process(self):
        """When NInfer deployment fails, cleanup routes through _stop_model_process
        (not an inlined docker stop) and stop_all gets only active_services."""
        import subprocess as _subprocess
        from inferfabric.model_lifecycle import ModelLifecycle
        from inferfabric.config import ModelConfig, NInferConfig
        from inferfabric.engine_adapter.ninfer import NInferAdapter

        mock_state = MagicMock()
        mock_state.get_active_services.return_value = []
        mock_proc = MagicMock()
        mock_health = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_gpu = MagicMock()
        mock_gpu.reconcile.return_value = {"actions": []}

        ninfer_model = ModelConfig(
            name="qwen38-ninfer", description="test", type="ninfer",
            gpu_role="shared",
            ninfer=NInferConfig(port=8007, container_name="ninfer-qwen38",
                                 weight_path="/tmp/weights",
                                 docker_image="ninfer:test"),
        )

        lc = ModelLifecycle(mock_state, mock_proc, mock_health, mock_lock, mock_gpu,
                            {"qwen38-ninfer": ninfer_model})

        # Simulate NInfer start failure — patch the adapter's start so no real
        # docker/Popen/sleep runs. Spy on _stop_model_process so the real
        # adapter.stop (which shells out to docker) is not executed here.
        with patch("inferfabric.model_lifecycle.load_models",
                   return_value={"qwen38-ninfer": ninfer_model}), \
             patch.object(NInferAdapter, "start",
                          return_value={"status": "error", "message": "simulated"}), \
             patch.object(lc, "_stop_model_process") as stop_spy, \
             patch.object(_subprocess, "run") as run_spy:
            result = lc._deploy_model(ninfer_model, "shared")

        assert result["status"] == "error"

        # Unified entry: _stop_model_process(model, model.name)
        stop_spy.assert_called_once_with(ninfer_model, "qwen38-ninfer")
        # No inlined docker/subprocess in the failure-cleanup path
        run_spy.assert_not_called()
        # stop_all is a thin PID-cleanup call — no engine-specific kwargs
        assert mock_proc.stop_all.call_count == 1
        kwargs = mock_proc.stop_all.call_args[1]
        assert kwargs == {"active_services": []}, \
            f"stop_all should receive only active_services, got {kwargs}"


# ═══════════════════════════════════════════════════════════════════
# 6. MODEL_TYPE_TO_MODALITY regression
# ═══════════════════════════════════════════════════════════════════

class TestModalityRegression:
    """Ensure rerank/infra/tts modality mappings are correct after audit fix."""

    def test_infra_modality(self):
        from inferfabric.config import MODEL_TYPE_TO_MODALITY
        assert MODEL_TYPE_TO_MODALITY["infra"] == "infra"

    def test_rerank_modality(self):
        from inferfabric.config import MODEL_TYPE_TO_MODALITY
        assert MODEL_TYPE_TO_MODALITY["rerank"] == "rerank"

    def test_tts_modality(self):
        from inferfabric.config import MODEL_TYPE_TO_MODALITY
        assert MODEL_TYPE_TO_MODALITY["tts"] == "tts"

    def test_ollama_daemon_resolved_modality(self):
        """ollama-daemon with model_type=infra resolves correctly."""
        from inferfabric.config import load_models
        models = load_models()
        od = models.get("ollama-daemon")
        assert od is not None
        assert od.model_type == "infra"
        assert od.resolved_modality == "infra"


# ═══════════════════════════════════════════════════════════════════
# 7. config_hash health_check_timeout exclusion
# ═══════════════════════════════════════════════════════════════════

class TestConfigHashExclusion:
    """Verify runtime-only fields are excluded from config_hash drift detection."""

    def test_health_check_timeout_no_drift(self):
        """Changing health_check_timeout should NOT trigger drift."""
        from inferfabric.config import ModelConfig, TTSConfig

        m1 = ModelConfig(
            name="tts-test", description="test", type="tts_server",
            tts=TTSConfig(conda_env="test", health_check_timeout=60),
        )
        m2 = ModelConfig(
            name="tts-test", description="test", type="tts_server",
            tts=TTSConfig(conda_env="test", health_check_timeout=300),
        )
        assert m1.config_hash() == m2.config_hash(), \
            "health_check_timeout change should not cause config hash drift"

    def test_conda_env_does_drift(self):
        """Changing conda_env SHOULD trigger drift."""
        from inferfabric.config import ModelConfig, TTSConfig

        m1 = ModelConfig(
            name="tts-test", description="test", type="tts_server",
            tts=TTSConfig(conda_env="test-v1"),
        )
        m2 = ModelConfig(
            name="tts-test", description="test", type="tts_server",
            tts=TTSConfig(conda_env="test-v2"),
        )
        assert m1.config_hash() != m2.config_hash(), \
            "conda_env change should cause config hash drift"

    def test_vllm_startup_timeout_no_drift(self):
        """Changing vllm startup_timeout should NOT trigger drift (existing behavior)."""
        from inferfabric.config import ModelConfig, VLLMConfig

        base = dict(
            model_dir="/fake", served_name="test", port=11441,
            conda_env="test", max_model_len=4096, gpu_memory_utilization=0.9,
        )
        m1 = ModelConfig(
            name="vllm-test", description="test", type="vllm",
            vllm=VLLMConfig(**base, startup_timeout=120),
        )
        m2 = ModelConfig(
            name="vllm-test", description="test", type="vllm",
            vllm=VLLMConfig(**base, startup_timeout=480),
        )
        assert m1.config_hash() == m2.config_hash(), \
            "startup_timeout change should not cause config hash drift"
