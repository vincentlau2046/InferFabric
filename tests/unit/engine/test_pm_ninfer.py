# tests/unit/engine/test_pm_ninfer.py
"""NInferProcessManager — start/stop 生命周期（Task 1，D1）。

镜像 vllm/sglang PM 的 mock-subprocess 测试模式。start_ninfer 的
container_name 必须来自参数（不重推 cfg.container_name or f"ninfer-{port}"）。
PR-20 ②：三态收养 — 重启网关不杀健在引擎（P0 实测 15min 冷启元凶）。
"""
import time as _time
from datetime import datetime, timezone
from unittest.mock import MagicMock


def _make_pm(tmp_path):
    from inferfabric.process_manager.ninfer import NInferProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return NInferProcessManager(state, tmp_path)


def _make_cfg():
    from inferfabric.config import NInferConfig
    return NInferConfig(
        port=8007, container_name="iff-ninfer-qwen38",
        weight_path="/w/model.bin", docker_image="ninfer:latest",
        model_id="m1", max_concurrency=8, max_context=615000,
        kv_capacity=0, default_max_tokens=8192, pending_timeout_ms=300000,
        kv_dtype="nvfp4", prefill_chunk=8192,
    )


class TestStartNInferContainerName:
    """PR-22A 后启动 = subprocess.run("docker run -d")。参数名契约不变；
    pid/container 追踪契约由 TestStartDetached 覆盖（pid 恒 None）。"""

    def test_start_uses_param_container_name_not_rederive(self, monkeypatch, tmp_path):
        """start_ninfer(cfg, container_name) 必须用参数名，不重推 cfg.container_name。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        # 参数给一个与 cfg.container_name 不同的名，验证用的是参数
        runs = []

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                runs.append(args)
                return MagicMock(returncode=0, stdout="cid123", stderr="")
            if args[:2] == ["docker", "inspect"]:
                if "\t" in args[3]:
                    # 3 字段身份探测（收养判定）→ 容器不存在，走创建路径
                    return MagicMock(returncode=1, stdout="", stderr="")
                # PR-22A 1 字段存活探测（detached 启动确认）→ 在跑
                return MagicMock(returncode=0, stdout="true", stderr="")
            return MagicMock(returncode=0, stdout="", stderr=b"")

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.time.sleep", lambda s: None)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", lambda url, timeout=120: True)

        pm.start_ninfer(cfg, container_name="custom-name-from-param")
        assert runs, "必须有一次 docker run（detached）"
        cmd = runs[0]
        idx = cmd.index("--name")
        assert cmd[idx + 1] == "custom-name-from-param", \
            f"应用参数名，实际 docker run --name: {cmd[idx + 1]}"


class TestStopNInferGraceful:
    def test_stop_uses_docker_stop_not_kill(self, monkeypatch, tmp_path):
        """stop_ninfer 必须用 docker stop（优雅），不调 docker kill（D3）。"""
        pm = _make_pm(tmp_path)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        result = pm.stop_ninfer("ninfer-8007")
        assert result["status"] == "ok"
        assert len(calls) == 1
        cmd = calls[0]
        assert "docker" in cmd and "stop" in cmd, f"应调 docker stop: {cmd}"
        assert "kill" not in cmd, f"不应调 docker kill: {cmd}"
        assert "ninfer-8007" in cmd

    def test_stop_none_container_name_returns_warning(self, tmp_path):
        """None container_name：不触子进程，直接 warning（守卫）。"""
        pm = _make_pm(tmp_path)
        result = pm.stop_ninfer("")
        assert result["status"] == "warning"
        assert "container_name" in result["message"]

    def test_stop_nonzero_rc_returns_warning(self, monkeypatch, tmp_path):
        """非零 rc：warning + stderr 片段。"""
        pm = _make_pm(tmp_path)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=1, stderr=b"container not found"))
        result = pm.stop_ninfer("ninfer-8007")
        assert result["status"] == "warning"
        assert "exit 1" in result["message"]

    def test_stop_clears_pid_and_container_state(self, monkeypatch, tmp_path):
        """stop 成功后清 PID + container state。"""
        pm = _make_pm(tmp_path)
        pm._set_ninfer_pid(123)
        pm._set_ninfer_container("ninfer-8007")
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=0, stderr=b""))
        pm.stop_ninfer("ninfer-8007")
        assert pm.ninfer_pid is None
        assert pm.ninfer_container is None


def _docker_ts(seconds_ago: float) -> str:
    """RFC3339Nano 风格 StartedAt（docker 实际输出 9 位纳秒小数）。"""
    dt = datetime.fromtimestamp(_time.time() - seconds_ago, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}789Z"


class TestStartDetached:
    """PR-22A：`docker run -d` — 客户端即退，引擎不随 proxy 停机陪葬。

    生产实证（2026-10-28 21:12）：attached 客户端是 proxy 子进程（同 cgroup），
    KillMode=control-group 的停机 SIGTERM 经 sig-proxy 转发进容器 → 引擎死 +
    --rm 销毁 → 收养无对象（PR-20 ② 从未在生产跑通）。detached 后无长驻
    子进程；日志走 docker logs（--log-opt 旋转防长驻容器无界）。
    """

    NAME = "iff-ninfer-qwen38"

    def _patch_detached(self, monkeypatch, *, run_rc=0, run_stdout="abc123",
                        run_stderr="", liveness="true", wait_ok=True,
                        logs_rc=0, logs_stdout="engine boot failed: boom",
                        logs_stderr="", logs_exc=False,
                        launched_image="ninfer:latest",
                        inspect_info=None):
        """detached 全套替身。

        launched_image：`docker inspect -f {{.Config.Image}}` 应答（Task 4
        受管身份核对替身）；与 cfg.docker_image 不一致时启动后身份核对失败。

        inspect_info：在运行的容器的收养身份探测应答
        （{"image": str, "label": str}，终审 F1 的 managed-pack label 列）；
        None = 无在跑容器（rc=1）。

        liveness：单值或逐轮序列，每轮 —
          "true"  → 存活探测 rc=0 Running=true
          "false" → rc=0 Running=false（已退出、--rm 未及移除）
          None    → rc=1（容器已被 --rm 移除）
          "EXC"   → 探测抛 OSError（daemon 卡死 / docker 缺失，评审 M-1）
        logs_rc/logs_stdout/logs_stderr/logs_exc：docker logs 应答（诊断文本源）。
        收养身份探测（3 字段格式）恒 rc=1 → 走创建路径。
        """
        rounds = list(liveness) if isinstance(liveness, (list, tuple)) else None
        calls = {"run": [], "popen": 0, "logs": 0, "inspect": 0, "stop": 0}

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                calls["run"].append(args)
                return MagicMock(returncode=run_rc, stdout=run_stdout,
                                 stderr=run_stderr)
            if args[:2] == ["docker", "inspect"]:
                if args[3] == "{{.Config.Image}}":
                    calls["inspect"] += 1
                    return MagicMock(returncode=0, stdout=launched_image,
                                     stderr="")
                if "\t" in args[3]:
                    # 收养身份探测（含终审 F1 的第 4 列 managed-pack label）
                    if inspect_info is not None:
                        return MagicMock(
                            returncode=0,
                            stdout="\t".join([
                                "true", "2026-11-20T10:00:00Z",
                                inspect_info.get("image", ""),
                                inspect_info.get("label", "")]),
                            stderr="")
                    return MagicMock(returncode=1, stdout="", stderr="")
                calls["inspect"] += 1
                v = rounds[calls["inspect"] - 1] if rounds else liveness
                if v == "EXC":
                    raise OSError("docker daemon wedged")
                if v is None:
                    return MagicMock(returncode=1, stdout="", stderr="")
                return MagicMock(returncode=0, stdout=v, stderr="")
            if args[:2] == ["docker", "logs"]:
                calls["logs"] += 1
                if logs_exc:
                    raise OSError("docker daemon wedged")
                return MagicMock(returncode=logs_rc, stdout=logs_stdout,
                                 stderr=logs_stderr)
            if args[:2] == ["docker", "stop"]:
                calls["stop"] += 1
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=0, stdout="", stderr=b"")

        def no_popen(*a, **k):
            calls["popen"] += 1
            raise AssertionError("detached 路径不得遗留 Popen 长驻客户端")

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen", no_popen)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.time.sleep",
                            lambda s: None)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http",
                            lambda url, timeout=120: wait_ok)
        return calls

    def test_detached_flag_and_log_rotation(self, monkeypatch, tmp_path):
        """docker run 必须带 -d + 日志旋转 opt；不得有长驻 Popen 客户端。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch_detached(monkeypatch)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "healthy"
        assert calls["popen"] == 0
        cmd = calls["run"][0]
        assert "-d" in cmd, "必须 docker run -d（detached）"
        assert "max-size=50m" in cmd and "max-file=3" in cmd, \
            "长驻容器日志必须 --log-opt 旋转"

    def test_detached_healthy_watches_full_exit_window(self, monkeypatch, tmp_path):
        """评审 I-1：退出观察窗必须满 6 轮（~3s），不得首轮 true 即断 —
        否则 0.3–3s 快退带漏检 → 烧满 startup_timeout 误报 timeout。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch_detached(monkeypatch)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "healthy"
        assert calls["inspect"] == 6, \
            f"健康启动应观察满 6 轮探活（实际 {calls['inspect']}）"

    def test_detached_healthy_tracks_container_pid_none(self, monkeypatch, tmp_path):
        """成功启动：container 入 state，pid=None（无本进程子 PID，与收养同构）。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_detached(monkeypatch)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "healthy"
        assert result["pid"] is None
        assert pm.ninfer_pid is None
        assert pm.ninfer_container == self.NAME

    def test_detached_run_failure_returns_error(self, monkeypatch, tmp_path):
        """docker run -d 非零 rc → error + stderr 片段。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_detached(monkeypatch, run_rc=1,
                             run_stderr="docker: port is already allocated")
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "error"
        assert "port is already allocated" in result["message"]

    def test_detached_exit_within_window_recovers_engine_logs(self, monkeypatch, tmp_path):
        """评审 I-1 主修：0.3–3s 快退带 — 探活见 Running=false（--rm 未及
        移除）→ 立即断出抢 docker logs 取真实报错，不等 120s 假超时。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch_detached(monkeypatch, liveness=["true", "false"])
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "error"
        assert "boom" in result["message"], "必须抢在 --rm 移除前取回引擎真实报错"
        assert calls["logs"] == 1
        assert calls["inspect"] == 2, "见 false 即断，不必烧满窗口"
        assert pm.ninfer_container is None, "失败路径必须回滚容器追踪"

    def test_detached_immediate_exit_removed_container(self, monkeypatch, tmp_path):
        """启动即退且 --rm 移除竞态已过（探活恒 rc=1）→ error；docker logs
        亦不可得 → 诊断文本为 daemon 错误（实测可观察行为，非机制）+ 回滚。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch_detached(
            monkeypatch, liveness=None, logs_rc=1,
            logs_stderr="Error response from daemon: No such container: iff-ninfer-qwen38")
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "error"
        assert "No such container" in result["message"]
        assert calls["logs"] >= 1
        assert pm.ninfer_container is None, "失败路径必须回滚容器追踪"

    def test_detached_probe_error_failsoft(self, monkeypatch, tmp_path):
        """评审 M-1：探活 6 轮全异常（daemon 卡死/docker 缺失）→ 不抛，
        走失败路径；docker logs 同抛 → 诊断含不可用提示 + 回滚。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_detached(monkeypatch, liveness="EXC", logs_exc=True)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "error"
        assert "docker logs unavailable" in result["message"]
        assert pm.ninfer_container is None


class TestStartNInferAdopt:
    """PR-20 ② 三态收养：健在健康 → 收养；年轻未就绪 → 等宽限；否则重建。"""

    NAME = "iff-ninfer-qwen38"

    def _patch(self, monkeypatch, *, inspect_running=None, inspect_age=None,
               inspect_image="ninfer:latest", health="✅", wait_ok=True,
               grace=None):
        """docker inspect / stop / run / 健康探测全套替身。

        inspect_running=None → 身份探测 rc=1（容器不存在）。
        PR-22A：inspect 双格式 — 3 字段身份探测（收养判定）按参数应答；
        1 字段存活探测（detached 启动确认）恒「在跑」（刚 docker run 完）。
        重建路径 = docker run -d（subprocess.run，非 Popen）。
        返回 calls 字典收集 stop / run / wait_timeout 证据。
        """
        calls = {"stop": 0, "run": [], "wait_timeouts": [], "popen": 0}

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "inspect"]:
                if "\t" not in args[3]:
                    return MagicMock(returncode=0, stdout="true", stderr="")
                if inspect_running is None:
                    return MagicMock(returncode=1, stdout="", stderr="")
                stdout = (f"true\t{_docker_ts(inspect_age)}\t{inspect_image}"
                          if inspect_running else "false\t\t")
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if args[:2] == ["docker", "run"]:
                calls["run"].append(args)
                return MagicMock(returncode=0, stdout="cid123", stderr="")
            if args[:2] == ["docker", "logs"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["docker", "stop"]:
                calls["stop"] += 1
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=0, stdout="", stderr=b"")

        def no_popen(*a, **k):
            calls["popen"] += 1
            raise AssertionError("PR-22A 后启动只有 docker run -d，不得遗留 Popen 客户端")

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen", no_popen)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.time.sleep",
                            lambda s: None)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.check_http_status",
                            lambda url, timeout=2: health)
        # wait_ok 可为序列：第 i 次调用取 waits[i]（收养等待与重建后健康
        # 检查是两次独立 wait_http 调用），越界取末值
        waits = list(wait_ok) if isinstance(wait_ok, (list, tuple)) else [wait_ok]
        state = {"i": 0}

        def fake_wait(url, timeout=120):
            calls["wait_timeouts"].append(timeout)
            i = state["i"]
            state["i"] += 1
            return waits[i] if i < len(waits) else waits[-1]

        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", fake_wait)
        monkeypatch.delenv("IFF_NINFER_ADOPT_GRACE", raising=False)
        if grace is not None:
            monkeypatch.setenv("IFF_NINFER_ADOPT_GRACE", str(grace))
        return calls

    def test_adopt_healthy_container_no_stop_no_run(self, monkeypatch, tmp_path):
        """健在且健康 → 直接收养：不 docker stop、不 docker run、不 sleep。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()  # docker_image="ninfer:latest" 与 inspect 一致
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=3600)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "healthy"
        assert result["adopted"] is True
        assert result["port"] == cfg.port
        assert calls["stop"] == 0
        assert calls["run"] == []

    def test_adopt_records_container_without_pid(self, monkeypatch, tmp_path):
        """收养后记录容器名，PID 置空（容器不是本进程的子进程）。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=3600)
        pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert pm.ninfer_container == self.NAME
        assert pm.ninfer_pid is None

    def test_adopt_wait_young_loading_container(self, monkeypatch, tmp_path):
        """年轻容器未就绪（加载中）→ 等宽限剩余时间，就绪即收养（不重建）。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=100,
                            health="❌", wait_ok=True)
        result = pm.start_ninfer(cfg, container_name=self.NAME)
        assert result["status"] == "healthy"
        assert result["adopted"] is True
        assert calls["stop"] == 0
        assert calls["run"] == []
        # 等待上限 = 宽限剩余（默认 1200 - 100），不是 startup_timeout=120
        assert calls["wait_timeouts"] and 1000 <= calls["wait_timeouts"][0] <= 1200

    def test_adopt_wait_expired_rebuilds(self, monkeypatch, tmp_path):
        """宽限内未就绪 → 回落重建（docker stop + run）。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=100,
                            health="❌", wait_ok=[False, True])
        result = pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert result["status"] == "healthy"  # 重建后正常启动
        assert "adopted" not in result
        assert calls["stop"] == 1
        assert len(calls["run"]) == 1

    def test_rebuild_old_unhealthy_container(self, monkeypatch, tmp_path):
        """年老不健康（疑似挂死）→ 立即重建，不等宽限。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=99999,
                            health="❌")
        result = pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert result["status"] == "healthy"
        assert "adopted" not in result
        assert calls["stop"] == 1
        assert len(calls["run"]) == 1
        # 只有重建后的健康检查（timeout=startup_timeout=120），没走 adopt-wait
        assert calls["wait_timeouts"] == [120]

    def test_rebuild_on_image_mismatch(self, monkeypatch, tmp_path):
        """容器镜像 ≠ cfg.docker_image → 不收养（镜像升级需重建生效）。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=60,
                            inspect_image="ninfer:old", health="✅")
        result = pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert "adopted" not in result
        assert calls["stop"] == 1
        assert len(calls["run"]) == 1

    def test_create_when_container_absent(self, monkeypatch, tmp_path):
        """容器不存在 → 原创建路径（inspect rc=1；docker stop 为无害 no-op）。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=None)
        result = pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert result["status"] == "healthy"
        assert "adopted" not in result
        assert calls["stop"] == 1
        assert len(calls["run"]) == 1

    def test_grace_env_override(self, monkeypatch, tmp_path):
        """IFF_NINFER_ADOPT_GRACE 可调：age 500 < grace 600 → 仍走收养等待。"""
        pm = _make_pm(tmp_path)
        calls = self._patch(monkeypatch, inspect_running=True, inspect_age=500,
                            health="❌", wait_ok=True, grace=600)
        result = pm.start_ninfer(_make_cfg(), container_name=self.NAME)
        assert result["adopted"] is True
        assert calls["wait_timeouts"] and 90 <= calls["wait_timeouts"][0] <= 100


class TestParseDockerTs:
    def test_nano_fraction_truncated_for_py310(self):
        from inferfabric.process_manager.ninfer import _parse_docker_ts
        ts = _parse_docker_ts("2026-10-27T04:27:58.123456789Z")
        assert ts == datetime(2026, 10, 27, 4, 27, 58, 123456,
                              tzinfo=timezone.utc).timestamp()

    def test_garbage_returns_none(self):
        from inferfabric.process_manager.ninfer import _parse_docker_ts
        assert _parse_docker_ts("not-a-time") is None
        assert _parse_docker_ts("") is None


class TestContainerExists:
    """评审修复 3：非变更性身份探测 container_exists（lifecycle 守卫跳过依据）。

    只回答「这个模型自己的容器是否在跑且镜像匹配」—— 健康与否、年轻与否
    的三态决策仍归 start_ninfer。绝不能 docker stop/run 或写 state。
    """

    NAME = "iff-ninfer-qwen38"

    def _patch_inspect(self, monkeypatch, *, running=None, image="ninfer:latest"):
        calls = {"stop": 0, "popen": []}

        def fake_run(args, **kwargs):
            if args[:2] == ["docker", "inspect"]:
                if running is None:
                    return MagicMock(returncode=1, stdout="", stderr="")
                stdout = f"true\t{_docker_ts(60)}\t{image}" if running else "false\t\t"
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if args[:2] == ["docker", "stop"]:
                calls["stop"] += 1
                return MagicMock(returncode=0, stderr=b"")
            return MagicMock(returncode=0, stdout="", stderr=b"")

        def fake_popen(cmd, **kw):
            calls["popen"].append(cmd)
            return MagicMock(pid=1, poll=lambda: None)

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", fake_run)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen", fake_popen)
        return calls

    def test_running_image_match_true_non_mutating(self, monkeypatch, tmp_path):
        """运行中 + 镜像匹配 → True；不健康也算在场（加载中容器同样要跳守卫）。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch_inspect(monkeypatch, running=True, image="ninfer:latest")
        assert pm.container_exists(cfg, container_name=self.NAME) is True
        assert calls["stop"] == 0
        assert calls["popen"] == []
        assert pm.ninfer_pid is None       # 不写 state
        assert pm.ninfer_container is None

    def test_image_mismatch_false(self, monkeypatch, tmp_path):
        """镜像不匹配（models.d 换镜像）→ 不是本模型的引擎 → False。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_inspect(monkeypatch, running=True, image="ninfer:old")
        assert pm.container_exists(cfg, container_name=self.NAME) is False

    def test_absent_container_false(self, monkeypatch, tmp_path):
        """容器不存在（inspect rc=1）→ False。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_inspect(monkeypatch, running=None)
        assert pm.container_exists(cfg, container_name=self.NAME) is False

    def test_not_running_false(self, monkeypatch, tmp_path):
        """容器存在但 State.Running=false（--rm 移除竞态窗口）→ False。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        self._patch_inspect(monkeypatch, running=False)
        assert pm.container_exists(cfg, container_name=self.NAME) is False

    def test_cfg_without_image_false(self, monkeypatch, tmp_path):
        """cfg.docker_image 为空 → 无法验证身份 → 不得认领（False）。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        cfg.docker_image = ""
        self._patch_inspect(monkeypatch, running=True, image="ninfer:latest")
        assert pm.container_exists(cfg, container_name=self.NAME) is False


class TestManagedNInferLaunch:
    """Task 4（specs/packs-platform-adaptation §4）：受管启动强制审核范围。

    受管 managed=ResolvedRuntime 与普通启动走同一 launcher，但 docker run
    策略被收紧：loopback 发布、设备白名单、包域可写缓存、身份钉住；
    外来身份一律 runtime_identity_mismatch 拒绝，绝不收养/重建（不 stop
    外来容器）。legacy（managed=None）路径零变化。
    """

    NAME = "iff-ninfer-pilot"

    def _managed(self, tmp_path, *, gpu_devices="0", pack_dir=None):
        from inferfabric.packs.managed_runtime import ResolvedRuntime
        return ResolvedRuntime(
            pack_id="llm.pilot-ninfer-27b", pack_version="1.0.0",
            template_name="ninfer-nvfp4-linux-v1",
            artifact_identities=("a" * 64,), role="owned",
            model_name="pilot",
            marker={"marker_version": 1, "pack_id": "llm.pilot-ninfer-27b"},
            gpu_devices=gpu_devices,
            pack_dir=pack_dir if pack_dir is not None
            else str(tmp_path / "packs_root" / "llm.pilot-ninfer-27b" / "1.0.0"))

    def _make_managed_cfg(self, tmp_path):
        cfg = _make_cfg()
        # 权重位于包产物目录内（受管约束：包域内只读挂载）
        cfg.weight_path = str(tmp_path / "packs_root" / "llm.pilot-ninfer-27b"
                              / "1.0.0" / "models" / "model")
        return cfg

    _patch = TestStartDetached._patch_detached   # 复用 detached 全套替身

    def test_managed_ninfer_flags_enforce_reviewed_scope(self, monkeypatch, tmp_path):
        """受管启动：loopback 发布 / 设备白名单 / 无 host 命名空间与特权 /
        权重只读 / 包域可写缓存 —— 临时与正常启动同一策略（launcher 层
        唯一路径，无旁路）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        managed = self._managed(tmp_path)
        calls = self._patch(monkeypatch)
        result = pm.start_ninfer(cfg, container_name=self.NAME, managed=managed)
        assert result["status"] == "healthy", result
        cmd = calls["run"][0]
        # host 侧仅 loopback 可达（宿主外无法访问监听口）
        assert "-p" in cmd and \
            cmd[cmd.index("-p") + 1] == f"127.0.0.1:{cfg.port}:8080"
        # 无 host IPC / 网络 / PID / 特权（不自动扩大网络范围）
        for forbidden in ("--privileged", "--network", "--ipc", "--pid"):
            assert forbidden not in cmd, f"受管启动不得使用 {forbidden}"
        # 设备白名单（不是 --gpus all）
        assert "--gpus" in cmd and \
            cmd[cmd.index("--gpus") + 1] == "device=0"
        # 权重只读（宿主权重不可被容器写）
        assert cmd[cmd.index("-v") + 1].endswith(":/workspace:ro")
        # 包域可写缓存：容器可写路径钉在包目录内，不落在权重/宿主任意处
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        assert any(m.endswith(":/workspace/cache") and
                   m.startswith(managed.pack_dir) for m in mounts), mounts
        # 身份钉住：健康判定前有一次 .Config.Image 身份核对
        assert calls["inspect"] >= 7, "受管启动必须在健康等待前核对容器镜像身份"

    def test_managed_ninfer_rejects_healthy_wrong_container(
            self, monkeypatch, tmp_path):
        """HTTP 健康但镜像 ≠ 钉住身份 → runtime_identity_mismatch；
        绝不收养、绝不 stop 重建（外来容器零接触）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        adopt = TestStartNInferAdopt()
        calls = adopt._patch(monkeypatch, inspect_running=True, inspect_age=3600,
                             inspect_image="ninfer:foreign", health="✅")
        result = pm.start_ninfer(cfg, container_name=self.NAME,
                                 managed=self._managed(tmp_path))
        assert result["status"] == "error"
        assert result["reason_code"] == "runtime_identity_mismatch"
        assert calls["stop"] == 0, "外来容器绝不能被 stop"
        assert calls["run"] == []

    def test_managed_ninfer_invalid_device_spec_rejected(self, monkeypatch, tmp_path):
        """设备域非纯数字/逗号清单 → 拒绝启动，不透传不可信
        docker 选项（manifest/YAML 不暴露任何 docker 形态参数）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch)
        result = pm.start_ninfer(
            cfg, container_name=self.NAME,
            managed=self._managed(tmp_path, gpu_devices="--privileged"))
        assert result["status"] == "error"
        assert "gpu_devices" in result["message"]
        assert calls["run"] == []

    def test_legacy_ninfer_publish_loopback(self, monkeypatch, tmp_path):
        """managed=None：legacy 与 managed 同契约，发布 127.0.0.1
        （issue #64：引擎端口不得对非 loopback 网卡开放）+ --gpus all，
        无包域缓存挂载。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        calls = self._patch(monkeypatch)
        result = pm.start_ninfer(cfg, container_name="iff-ninfer-qwen38")
        assert result["status"] == "healthy"
        cmd = calls["run"][0]
        assert cmd[cmd.index("-p") + 1] == f"127.0.0.1:{cfg.port}:8080"
        assert cmd[cmd.index("--gpus") + 1] == "all"
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        assert all(not m.endswith(":/workspace/cache") for m in mounts)
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
        assert all(not m.endswith(":/workspace/cache") for m in mounts)


class TestManagedContainerOwnership:
    """终审 F1/F6：容器所有权绑定 + 设备域权威。

    镜像相等 ≠ 所有权证明 —— 在场容器必须携带本包的
    `inferfabric.managed-pack=<pack_id>` label 才可收养/复用；否则
    runtime_identity_mismatch，零接触（绝不 stop、绝不重建）。设备
    白名单只认持久化所有权段的 gpu_devices，YAML marker 不可提权。
    """

    NAME = "iff-ninfer-pilot"
    _patch = TestStartDetached._patch_detached
    _managed = TestManagedNInferLaunch._managed
    _make_managed_cfg = TestManagedNInferLaunch._make_managed_cfg

    def test_same_image_unlabeled_container_zero_contact(
            self, monkeypatch, tmp_path):
        """同镜像但无本包 label 的在跑容器 = 外来 → mismatch，绝不收养/
        stop/重建（镜像相等不构成所有权证明）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch,
                            inspect_info={"image": cfg.docker_image,
                                          "label": ""})
        result = pm.start_ninfer(cfg, container_name=self.NAME,
                                 managed=self._managed(tmp_path))
        assert result["status"] == "error"
        assert result["reason_code"] == "runtime_identity_mismatch"
        assert calls["stop"] == 0, "外来容器绝不能被 stop"
        assert calls["run"] == []

    def test_other_pack_label_zero_contact(self, monkeypatch, tmp_path):
        """label 指向别的包 → mismatch，零接触。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch,
                            inspect_info={"image": cfg.docker_image,
                                          "label": "llm.other-pack"})
        result = pm.start_ninfer(cfg, container_name=self.NAME,
                                 managed=self._managed(tmp_path))
        assert result["status"] == "error"
        assert result["reason_code"] == "runtime_identity_mismatch"
        assert calls["stop"] == 0
        assert calls["run"] == []

    def test_own_labeled_healthy_container_adopted(
            self, monkeypatch, tmp_path):
        """本包 label + 钉住镜像 + 健康 → 收养复用（不 stop、不重建）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch,
                            inspect_info={"image": cfg.docker_image,
                                          "label": "llm.pilot-ninfer-27b"})
        monkeypatch.setattr(
            "inferfabric.process_manager.ninfer.check_http_status",
            lambda *a, **k: "✅")
        result = pm.start_ninfer(cfg, container_name=self.NAME,
                                 managed=self._managed(tmp_path))
        assert result["status"] == "healthy", result
        assert result.get("adopted") is True
        assert calls["stop"] == 0
        assert calls["run"] == []

    def test_managed_run_stamps_pack_label(self, monkeypatch, tmp_path):
        """受管 docker run 必须打上 `inferfabric.managed-pack=<pack_id>`
        label（所有权的容器侧凭据）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch)
        result = pm.start_ninfer(cfg, container_name=self.NAME,
                                 managed=self._managed(tmp_path))
        assert result["status"] == "healthy", result
        cmd = calls["run"][0]
        assert "inferfabric.managed-pack=llm.pilot-ninfer-27b" in cmd

    def test_missing_device_scope_rejected(self, monkeypatch, tmp_path):
        """持久化设备域缺失（空）→ 拒绝启动（绝不静默默认 device 0）。"""
        pm = _make_pm(tmp_path)
        cfg = self._make_managed_cfg(tmp_path)
        calls = self._patch(monkeypatch)
        result = pm.start_ninfer(
            cfg, container_name=self.NAME,
            managed=self._managed(tmp_path, gpu_devices=""))
        assert result["status"] == "error"
        assert "gpu_devices" in result["message"]
        assert calls["run"] == []
