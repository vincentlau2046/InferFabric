# 统一模型启动流程（元数据驱动）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把模型 **start** 路径元数据化，与已完成的 `unified-stop-metadata-driven` 对称——`adapter.start()` 读 `model.resolved_deployment` / `model.container_name`（和 stop 读同一份元数据），facade start 签名透传 `container_name`，container_name 单一来源（`ModelConfig.container_name` property）。同时给 ninfer 补完整 ProcessManager launcher（start+stop），把 sglang stop 统一到优雅 `docker stop`。

**Architecture:**
dispatch 层 `_start_model`（model_lifecycle.py:50-65）已对称——`get_adapter(model.type).start(model)`，不动。不对称在 dispatch 以下：adapter.start 体不读元数据（ninfer 内联 `docker run` 重推 container_name、sglang PM 重推 `f"sglang-{served_name}"`、vllm 无 docker 分支）。本计划下沉到 adapter.start 体 + PM start 签名 + 新 ninfer launcher：① 新建 `process_manager/ninfer.py`（`NInferProcessManager`，迁 ninfer adapter 内联 docker run/stop）；② sglang PM start 签名加 `container_name`、stop 改 `docker stop`（删 kill/rm）；③ vllm.start 按 deployment 分派（docker 分支报错 + validate 拦截）。Approach A：adapter 读元数据透传值，PM 不碰 ModelConfig。

**Tech Stack:** Python 3.10+ dataclass（config.py）、subprocess（docker）、pytest + monkeypatch（回归，镜像 stop 的 mock 模式）。无新依赖。

**Spec:** `docs/superpowers/specs/2026-09-20-unified-start-metadata-driven-design.md`（权威依据，D1-D4 锁定决策）。

## Global Constraints

- **Python 3.10+**，不升级 vLLM 0.24。
- **Frozen-API**：不动 proxy 转发核心路径（PR-14）；不新增/修改后端 HTTP 端点。
- **Sandbox-first**：所有改动在 sandbox/ 开发 → pytest → smoke → diff review → merge（CLAUDE.md governance）。本计划代码位置以生产路径 `inferfabric/` 描述。
- **向后兼容**：现有 models.d/*.yaml 不能因改动失效——container_name property 不变（已是 stop 的单一来源），YAML 不显式写 `deployment` 时行为与现状一致。
- **start 行为不回退**：ninfer/sglang 的 `docker run` 命令体（参数、挂载、env）迁移到 PM 时逐行保留，只改 container_name 来源（参数 vs 重推）；vllm conda start 路径不动。
- **stop 行为保留**：ninfer.stop 迁到 PM launcher 时保留 `docker stop` + 完整守卫 + fix-1 的 `log.warning`（行为字节一致）；sglang stop 改优雅是 D3 显式决策（kill→stop），非回退。
- **串行执行**：用户绑定约束——不并行 dispatch implementer，三步串行完成。
- **commit 归属**：每条 commit 消息以 `Co-Authored-By: Claude Code <noreply@anthropic.com>` 结尾。

---

## 文件结构总览

| 文件 | 责任 | 本计划改动 |
|---|---|---|
| **新建 `inferfabric/process_manager/ninfer.py`** | NInfer PM launcher | **Task 1**：`NInferProcessManager`（start_ninfer/stop_ninfer/is_ninfer_alive + 状态跟踪） |
| `inferfabric/process_manager/sglang.py` | SGLang PM | **Task 2**：start_sglang 加 container_name 参、删重推；stop kill→stop、删 rm |
| `inferfabric/process_manager/facade.py` | PM 门面 | **Task 3**：注册 NInferProcessManager、start_sglang/start_ninfer/stop_ninfer 透传、ninfer 状态层、stop_all 清 ninfer |
| `inferfabric/engine_adapter/ninfer.py` | NInfer 适配器 | **Task 4**：start→委托 start_ninfer(cfg, container_name)；stop→委托 stop_ninfer（迁出 base helper）；删内联 docker + import |
| `inferfabric/engine_adapter/sglang.py` | SGLang 适配器 | **Task 5**：start→委托 start_sglang(cfg, model.container_name) |
| `inferfabric/engine_adapter/vllm.py` | vLLM 适配器 | **Task 6**：start 按 resolved_deployment 分派（docker 报错+log.warning）；validate 拦截 docker |
| `inferfabric/config.py` | ModelConfig | **Task 5**：SGLangConfig.build_docker_cmd 加 container_name 参 |
| `inferfabric/process_manager/base.py` | PM 基类 | **Task 4**：_stop_docker_container docstring 改"vllm-docker stop" |
| `tests/unit/engine/test_pm_ninfer.py` | ninfer PM 测试 | **Task 1**：新建 |
| `tests/unit/engine/test_engine_adapters.py` | 适配器测试 | **Task 4/5/6**：加 start 委托 + vllm 分派测试 |

---

## Step 1: PM launchers（ninfer 新建 + sglang 改优雅）

### Task 1: 新建 `NInferProcessManager`（start + stop，D1）

**Files:**
- Create: `inferfabric/process_manager/ninfer.py`
- Test: `tests/unit/engine/test_pm_ninfer.py`（新建）

**Interfaces:**
- Consumes: `BaseProcessManager`（`process_manager/base.py:20`，提供 `_pkill_by_port`/`_validate_pid`/`_cleanup_pid_files`/`_wait_gpu_idle`/`_reap_zombies`）；`NInferConfig`（`config.py`，字段：port/container_name/weight_path/docker_image/model_id/max_concurrency/max_context/kv_capacity/default_max_tokens/pending_timeout_ms/kv_dtype/prefill_chunk/enable_mtp/draft_tokens/enable_lm_head_draft/log_file/startup_timeout）；`inferfabric.health.wait_http`
- Produces: `NInferProcessManager` 类，方法 `start_ninfer(cfg: NInferConfig, container_name: str) -> dict`、`stop_ninfer(container_name: str) -> dict`、`is_ninfer_alive(port: int) -> bool`；状态 property `ninfer_pid`/`ninfer_container` + setter `_set_ninfer_pid`/`_set_ninfer_container`。后被 Task 3 的 facade 注册、Task 4 的 adapter 委托。

**迁移来源（逐行保留逻辑，只改 container_name 来源）：** `engine_adapter/ninfer.py:63-130` 的内联 `docker run`（含 L73-75 前置 `docker stop` 旧容器 + 2s settle）、`base.py:66-100` 的 `_stop_docker_container` 守卫（None-name/Timeout/FileNotFound/非零rc + log.warning）。

- [ ] **Step 1: 写失败测试——start_ninfer 用 container_name 参数，不重推**

```python
# tests/unit/engine/test_pm_ninfer.py
"""NInferProcessManager — start/stop 生命周期（Task 1，D1）。

镜像 vllm/sglang PM 的 mock-subprocess 测试模式。start_ninfer 的
container_name 必须来自参数（不重推 cfg.container_name or f"ninfer-{port}"）。
"""
from unittest.mock import MagicMock, patch
from pathlib import Path


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
    def test_start_uses_param_container_name_not_rederive(self, monkeypatch, tmp_path):
        """start_ninfer(cfg, container_name) 必须用参数名，不重推 cfg.container_name。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        # 参数给一个与 cfg.container_name 不同的名，验证用的是参数
        passed_names = []

        def fake_popen(cmd, **kw):
            # 捕获 --name 后的值
            if "--name" in cmd:
                idx = cmd.index("--name")
                passed_names.append(cmd[idx + 1])
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None  # 不立即退出
            return proc

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen", fake_popen)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run", lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", lambda url, timeout=120: True)

        pm.start_ninfer(cfg, container_name="custom-name-from-param")
        assert passed_names == ["custom-name-from-param"], \
            f"应用参数名，实际 docker run --name: {passed_names}"

    def test_start_tracks_pid_and_container(self, monkeypatch, tmp_path):
        """start_ninfer 成功后记录 PID + container 到 state。"""
        pm = _make_pm(tmp_path)
        cfg = _make_cfg()
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.Popen",
                            lambda cmd, **kw: MagicMock(pid=999, poll=lambda: None))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http", lambda url, timeout=120: True)
        pm.start_ninfer(cfg, container_name="ninfer-8007")
        assert pm.ninfer_pid == 999
        assert pm.ninfer_container == "ninfer-8007"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_pm_ninfer.py::TestStartNInferContainerName -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'inferfabric.process_manager.ninfer'`

- [ ] **Step 3: 写 NInferProcessManager（迁 ninfer adapter 内联 docker run，container_name 改参数）**

```python
# inferfabric/process_manager/ninfer.py
"""
inferfabric/process_manager/ninfer.py — NInfer Docker container lifecycle.

Mirrors VLLMProcessManager / SGLangProcessManager structure (D1).
start_ninfer / stop_ninfer migrated from the inline docker run/stop in
engine_adapter/ninfer.py — container_name now a parameter (single source
of truth = ModelConfig.container_name, threaded by the adapter).
"""

import os
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.health import wait_http, check_http_status
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class NInferProcessManager(BaseProcessManager):
    """NInfer Docker container lifecycle: start, stop. Mirrors SGLangProcessManager."""

    # ─── PID / container accessors ──────────────────────────────

    @property
    def ninfer_pid(self):
        pid_str = self._state.get("ninfer_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_ninfer_pid(self, pid):
        self._state.set("ninfer_pid", str(pid) if pid else "")

    @property
    def ninfer_container(self):
        c = self._state.get("ninfer_container")
        return c or None

    def _set_ninfer_container(self, name):
        self._state.set("ninfer_container", name or "")

    # ─── start ──────────────────────────────────────────────────

    def start_ninfer(self, cfg, container_name: str) -> dict:
        """Start NInfer via Docker container.

        Migrated from engine_adapter/ninfer.py:63-124. container_name is now
        a parameter (was cfg.container_name or f"ninfer-{cfg.port}"). Preserves
        the pre-start cleanup (docker stop stale same-name + 2s settle).
        """
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}

        # Pre-start cleanup: stop any stale container with the same name (避免重名冲突)
        subprocess.run(["docker", "stop", container_name], timeout=10,
                       capture_output=True, check=False)
        time.sleep(2)

        weight_path = Path(cfg.weight_path).expanduser()
        weight_dir = str(weight_path.parent)

        cmd = [
            "docker", "run", "--gpus", "all", "--rm",
            "-v", f"{weight_dir}:/workspace",
            "-p", f"{cfg.port}:8080",
            "--name", container_name,
            "-e", "NVIDIA_DISABLE_REQUIRE=1",
            cfg.docker_image,
            "ninfer-serve", weight_path.name,
            "--model-id", cfg.model_id,
            "--host", "0.0.0.0", "--port", "8080",
            "--max-concurrency", str(cfg.max_concurrency),
            "--max-context", str(cfg.max_context),
            "--kv-capacity", "auto" if cfg.kv_capacity == 0 else str(cfg.kv_capacity),
            "--default-max-tokens", str(cfg.default_max_tokens),
            "--pending-timeout-ms", str(cfg.pending_timeout_ms),
            "--kv-dtype", cfg.kv_dtype,
            "--prefill-chunk", str(cfg.prefill_chunk),
        ]
        if cfg.enable_mtp:
            cmd.extend(["--spec", "mtp", "--draft-tokens", str(cfg.draft_tokens)])
        if cfg.enable_lm_head_draft:
            cmd.append("--lm-head-draft")

        log.info("Starting NInfer: %s", " ".join(cmd))
        log_file = Path(cfg.log_file or f"/tmp/ninfer-{cfg.port}.log")
        log_file.write_text("")

        try:
            proc = subprocess.Popen(
                cmd, stdout=log_file.open("a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start NInfer: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_ninfer_pid(pgid)
        self._set_ninfer_container(container_name)
        log.info("NInfer docker PID=%s container=%s", proc.pid, container_name)

        # Immediate-exit detection (保留 ninfer adapter:117-120 的 6 轮 poll)
        for _ in range(6):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("NInfer exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_ninfer_pid(None)
                self._set_ninfer_container(None)
                return {"status": "error", "message": f"NInfer exited with code {ret}",
                        "log": str(log_file)}
            time.sleep(0.5)

        timeout = cfg.startup_timeout or 120
        healthy = wait_http(f"http://localhost:{cfg.port}/v1/models", timeout=timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        # 健康超时 → 清理
        self.stop_ninfer(container_name)
        return {"status": "timeout",
                "message": f"NInfer didn't become healthy within {timeout}s"}

    # ─── stop ───────────────────────────────────────────────────

    def stop_ninfer(self, container_name: str, timeout: int = 30) -> dict:
        """Stop NInfer container via `docker stop` (graceful SIGTERM→SIGKILL).

        Migrated from base._stop_docker_container — preserves the full guard
        (None-name / TimeoutExpired / FileNotFoundError / non-zero rc) and the
        fix-1 log.warning on each failure branch. No `docker rm`: ninfer's
        `docker run --rm` auto-removes on stop.
        """
        if not container_name:
            msg = "docker deployment has no container_name — cannot stop"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        log.info("Stopping NInfer container: %s", container_name)
        try:
            result = subprocess.run(
                ["docker", "stop", container_name],
                timeout=timeout, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            msg = f"docker stop {container_name} timed out"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        except FileNotFoundError:
            msg = "docker not found on PATH"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        if result.returncode == 0:
            self._set_ninfer_pid(None)
            self._set_ninfer_container(None)
            return {"status": "ok", "message": f"Container {container_name} stopped"}
        stderr_fragment = result.stderr.decode()[:200] if result.stderr else ""
        msg = f"docker stop exit {result.returncode}: {stderr_fragment}"
        log.warning(msg)
        self._set_ninfer_pid(None)
        self._set_ninfer_container(None)
        return {"status": "warning", "message": msg}

    # ─── liveness ───────────────────────────────────────────────

    def is_ninfer_alive(self, port: int) -> bool:
        return check_http_status(f"http://localhost:{port}/v1/models", timeout=2) == "✅"
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_pm_ninfer.py::TestStartNInferContainerName -v --tb=short`
Expected: PASS（2 passed）

- [ ] **Step 5: 加 stop_ninfer 测试（docker stop 非 kill，完整守卫）**

```python
# 追加到 tests/unit/engine/test_pm_ninfer.py

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
```

- [ ] **Step 6: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_pm_ninfer.py -v --tb=short`
Expected: PASS（6 passed）

- [ ] **Step 7: smoke + commit**

```bash
python3 -c "import inferfabric; from inferfabric.process_manager.ninfer import NInferProcessManager; print('OK')"
git add inferfabric/process_manager/ninfer.py tests/unit/engine/test_pm_ninfer.py
git commit -m "feat(pm): NInferProcessManager launcher (start_ninfer + stop_ninfer, D1)

Migrates the inline docker run/stop from engine_adapter/ninfer.py into a
real PM launcher mirroring VLLM/SGLang PMs. container_name is now a
parameter (single source = ModelConfig.container_name, threaded later by
the adapter). stop_ninfer preserves the graceful docker stop + full guard
+ fix-1 log.warning migrated from base._stop_docker_container.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: sglang PM — start 加 container_name 参、stop 改优雅（D3 + 单一来源）

**Files:**
- Modify: `inferfabric/process_manager/sglang.py:46-126`
- Test: `tests/unit/engine/test_pm_sglang.py`（新建，若无既有 sglang PM 测试则建；先查）

**Interfaces:**
- Consumes: `SGLangConfig.build_docker_cmd(container_name)`（Task 5 改签名后）；现有 `sglang_pid`/`sglang_container` 状态跟踪（sglang.py:24-43）
- Produces: `start_sglang(cfg, container_name: str)` 新签名（删 L49 `f"sglang-{cfg.served_name}"` 重推）；`stop_sglang` 改 `docker stop`（删 L113 kill + L115 rm）。后被 Task 5 的 adapter 委托。

**注意 build_docker_cmd 依赖：** Task 2 改 `start_sglang` 调 `cfg.build_docker_cmd(container_name)`，但 `build_docker_cmd` 签名在 Task 5 才改。为避免 Task 2 暂时 RED（签名不匹配），**Task 2 的 start_sglang 改造中先不改 build_docker_cmd 调用方式**——保持 `cfg.build_docker_cmd()` 无参调用，只在 PM 内用 `container_name` 参数做 PID/container 跟踪 + 日志（build_docker_cmd 内部仍硬编码 `--name`，Task 5 统一改）。这样 Task 2 独立可测（测 stop 优雅 + start 签名加参），Task 5 闭合 build_docker_cmd 的 `--name` 来源。

- [ ] **Step 1: 先查既有 sglang PM 测试**

Run: `grep -rn "start_sglang\|stop_sglang\|SGLangProcessManager" tests/unit/engine/`
Expected: 确认是否有 test_pm_sglang.py；若无，新建。

- [ ] **Step 2: 写失败测试——stop_sglang 用 docker stop 非 kill**

```python
# tests/unit/engine/test_pm_sglang.py
"""SGLangProcessManager — stop 改优雅 docker stop（D3），start 加 container_name 参。"""
from unittest.mock import MagicMock
from pathlib import Path


def _make_pm(tmp_path):
    from inferfabric.process_manager.sglang import SGLangProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "s.db")
    return SGLangProcessManager(state, tmp_path)


class TestStopSGLangGraceful:
    def test_stop_uses_docker_stop_not_kill(self, monkeypatch, tmp_path):
        """stop_sglang 改优雅：调 docker stop，不调 docker kill，不调 docker rm（D3）。"""
        import subprocess
        pm = _make_pm(tmp_path)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0, stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = pm.stop_sglang(container_name="sglang-foo")
        assert result["status"] == "ok"
        # 只应有一次 docker stop（无 kill、无 rm）
        stop_calls = [c for c in calls if "stop" in c]
        kill_calls = [c for c in calls if "kill" in c]
        rm_calls = [c for c in calls if "rm" in c]
        assert len(stop_calls) == 1, f"应只一次 docker stop: {calls}"
        assert len(kill_calls) == 0, f"不应 docker kill: {calls}"
        assert len(rm_calls) == 0, f"不应 docker rm（--rm 自动移除）: {calls}"
        assert "sglang-foo" in stop_calls[0]
```

- [ ] **Step 3: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_pm_sglang.py::TestStopSGLangGraceful -v --tb=short`
Expected: FAIL — `assert len(kill_calls) == 0`（当前 stop_sglang 调 docker kill）

- [ ] **Step 4: 改 stop_sglang——kill→stop、删 rm、保留 fallback 扫描**

修改 `inferfabric/process_manager/sglang.py:83-126`。**保留** L91-111 的 container-name fallback 扫描逻辑（无 container_name 时扫描 `sglang-*` 容器）。**改** L113 `docker kill`→`docker stop`，**删** L115-116 `docker rm -f`（`--rm` 自动移除）。docstring 改"docker stop（graceful）"。

```python
    def stop_sglang(self, port: Optional[int] = None, container_name: Optional[str] = None) -> dict:
        """Stop SGLang container via `docker stop` (graceful SIGTERM→SIGKILL).

        D3: unified to graceful docker stop (was docker kill). SGLang's
        `docker run --rm` auto-removes on stop, so no separate docker rm.
        Falls back to scanning running containers if no name is tracked.
        """
        if not container_name:
            container_name = self.sglang_container
        if not container_name:
            # Fallback: scan for sglang-* containers and stop any on this port
            try:
                r = subprocess.run(
                    ["docker", "ps", "--filter", "name=sglang-", "--format", "{{.Names}}:{{.Ports}}"],
                    timeout=10, capture_output=True, text=True, check=False
                )
                for line in r.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        cname, cports = parts
                        if str(port) in cports:
                            container_name = cname
                            log.info("Discovered SGLang container %s (port %d)", container_name, port)
                            break
            except Exception as e:
                log.debug("Docker scan fallback failed: %s", e)
        if container_name:
            subprocess.run(["docker", "stop", container_name],
                          timeout=10, check=False, capture_output=True)
            log.info("SGLang container %s stopped (graceful)", container_name)

        if port:
            try:
                wait_gpu_free()
            except Exception as e:
                log.error("GPU did not free after SGLang stop (port %s): %s", port, e)
        self._set_sglang_pid(None)
        self._set_sglang_container(None)
        return {"status": "ok", "message": "SGLang container stopped"}
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_pm_sglang.py::TestStopSGLangGraceful -v --tb=short`
Expected: PASS

- [ ] **Step 6: 加 start_sglang 签名测试（container_name 参，不重推）**

```python
# 追加到 tests/unit/engine/test_pm_sglang.py

class TestStartSGLangContainerName:
    def test_start_accepts_container_name_param(self, monkeypatch, tmp_path):
        """start_sglang(cfg, container_name) 接受参数，用于状态跟踪（不重推 L49）。"""
        import subprocess
        pm = _make_pm(tmp_path)
        from inferfabric.config import SGLangConfig
        cfg = SGLangConfig(model_dir="/m", served_name="foo", port=8100,
                          mem_fraction=0.9, context_length=4096)

        def fake_popen(cmd, **kw):
            return MagicMock(pid=555, poll=lambda: None, returncode=None)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        # sglang.py:63 调 os.getpgid(proc.pid) — fake pid 555 不存在会抛
        # ProcessLookupError，必须 patch
        monkeypatch.setattr("inferfabric.process_manager.sglang.os.getpgid", lambda pid: 555)
        monkeypatch.setattr("inferfabric.process_manager.sglang.os.environ", {"PATH": "/x"})
        monkeypatch.setattr("inferfabric.process_manager.sglang.time.sleep", lambda *a: None)
        monkeypatch.setattr("inferfabric.process_manager.sglang.check_http_status", lambda *a, **k: "✅")

        pm.start_sglang(cfg, container_name="sglang-foo")
        # 容器名记录到 state（来自参数，非重推）
        assert pm.sglang_container == "sglang-foo"
        assert pm.sglang_pid == 555
```

- [ ] **Step 7: 改 start_sglang 签名——加 container_name 参、删 L49 重推**

修改 `inferfabric/process_manager/sglang.py:46-66`。签名加 `container_name: str`，删 L49 `container_name = f"sglang-{cfg.served_name}"`。**build_docker_cmd 暂不改**（Task 5 统一），保持 `cfg.build_docker_cmd()` 无参。

```python
    def start_sglang(self, cfg, container_name: str) -> dict:
        """Start SGLang via Docker container.

        container_name threaded from ModelConfig.container_name (single source).
        """
        log_file = self._log_dir / f"sglang_{cfg.served_name}.log"

        cmd = cfg.build_docker_cmd()
        env = os.environ.copy()

        log.info("Starting SGLang container %s: %s", container_name, " ".join(cmd))
        with open(log_file, "w") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        pgid = os.getpgid(proc.pid)
        self._set_sglang_pid(pgid)
        self._set_sglang_container(container_name)
        log.info("SGLang docker PID=%s PGID=%s container=%s", proc.pid, pgid, container_name)

        health_timeout = cfg.startup_timeout or HEALTH_CHECK_TIMEOUT
        start = time.time()
        while time.time() - start < health_timeout:
            time.sleep(2)
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                self._set_sglang_pid(None)
                self._set_sglang_container(None)
                return {"status": "error", "message": "SGLang container exited", "log": str(log_file)}
            if check_http_status(f"http://localhost:{cfg.port}/health", timeout=2) == "✅":
                return {"status": "healthy", "message": f"SGLang healthy on port {cfg.port}"}
        self.stop_sglang(port=cfg.port, container_name=container_name)
        return {"status": "timeout", "message": f"SGLang didn't become healthy within {health_timeout}s"}
```

- [ ] **Step 8: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_pm_sglang.py -v --tb=short`
Expected: PASS（2 passed）

- [ ] **Step 9: 回归 + smoke + commit**

```bash
python3 -m pytest tests/unit/engine/test_engine_adapters.py -q --tb=short   # sglang adapter 测试不应破
python3 -c "import inferfabric"
git add inferfabric/process_manager/sglang.py tests/unit/engine/test_pm_sglang.py
git commit -m "fix(pm): sglang stop unified to graceful docker stop (D3) + start container_name param

stop_sglang: docker kill → docker stop (graceful SIGTERM→SIGKILL, unifying
with vllm+ninfer so in-flight requests survive every engine's stop). Deletes
docker rm -f (--rm auto-removes). Preserves container-name fallback scan.
start_sglang: signature gains container_name param (deletes L49 re-derive);
build_docker_cmd --name source closed in Task 5.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Step 2: facade 注册 + adapter 委托（ninfer 迁出 base helper）

### Task 3: facade 注册 NInferProcessManager + 透传 + 状态层 + stop_all 清理

**Files:**
- Modify: `inferfabric/process_manager/facade.py:38-46`（__init__）、`:80-123`（状态层）、`:147-151`（start_sglang/stop_sglang 透传）、`:236-310`（stop_all）、新增 start_ninfer/stop_ninfer 方法
- Test: `tests/unit/engine/test_engine_adapters.py`（facade 委托断言，或新建 test_pm_facade.py——先查既有）

**Interfaces:**
- Consumes: `NInferProcessManager`（Task 1）、`SGLangProcessManager.start_sglang(cfg, container_name)`（Task 2）
- Produces: facade `start_ninfer(cfg, container_name)`、`stop_ninfer(container_name)`、`start_sglang(cfg, container_name)` 透传；`ninfer_pid`/`ninfer_container` property + setter；`stop_all` 清 ninfer 状态。后被 Task 4/5 的 adapter 委托。

- [ ] **Step 1: 写失败测试——facade start_ninfer/stop_ninfer 透传 container_name**

```python
# tests/unit/engine/test_pm_facade.py（新建，若无既有 facade 测试；先 grep 确认）
"""ProcessManager facade — ninfer 注册 + 透传 + 状态层（Task 3）。"""
from unittest.mock import MagicMock
from pathlib import Path


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
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_pm_facade.py -v --tb=short`
Expected: FAIL — `AttributeError: 'ProcessManager' object has no attribute 'start_ninfer'`（或 `_ninfer`）

- [ ] **Step 3: 改 facade——注册 + 状态层 + 透传 + stop_all 清理**

修改 `inferfabric/process_manager/facade.py`：

(a) `__init__`（L38-46）加 `self._ninfer = NInferProcessManager(state, log_dir)` + import（L26 区加 `from inferfabric.process_manager.ninfer import NInferProcessManager`）：

```python
    def __init__(self, state, log_dir: Path = DEFAULT_LOG_DIR):
        super().__init__(state, log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._vllm = VLLMProcessManager(state, log_dir)
        self._sglang = SGLangProcessManager(state, log_dir)
        self._comfyui = ComfyUIProcessManager(state, log_dir)
        self._tts = TTSProcessManager(state, log_dir)
        self._asr = ASRProcessManager(state, log_dir)
        self._ollama = LlamaCppProcessManager(state, log_dir)
        self._ninfer = NInferProcessManager(state, log_dir)
```

(b) 状态层（L80-123 区，sglang_container property 之后 + setter 区）加 ninfer 对称项：

```python
    @property
    def ninfer_pid(self) -> Optional[int]:
        pid_str = self._state.get("ninfer_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def ninfer_container(self) -> Optional[str]:
        c = self._state.get("ninfer_container")
        return c or None

    # 在 setter 区（L116 _set_sglang_container 之后）加：
    def _set_ninfer_pid(self, pid: Optional[int]):
        self._state.set("ninfer_pid", str(pid) if pid else "")

    def _set_ninfer_container(self, name: Optional[str]):
        self._state.set("ninfer_container", name or "")
```

(c) start_sglang（L147-148）改签名透传 + 加 start_ninfer/stop_ninfer 方法（L151 stop_sglang 之后）：

```python
    def start_sglang(self, cfg, container_name: str) -> dict:
        return self._sglang.start_sglang(cfg, container_name)

    def stop_sglang(self, port: Optional[int] = None, container_name: Optional[str] = None) -> dict:
        return self._sglang.stop_sglang(port=port, container_name=container_name)

    def start_ninfer(self, cfg, container_name: str) -> dict:
        return self._ninfer.start_ninfer(cfg, container_name)

    def stop_ninfer(self, container_name: str) -> dict:
        return self._ninfer.stop_ninfer(container_name)
```

(d) `stop_all`（L304-310 active_services 分支）加 ninfer 清理：

```python
        if active_services is not None:
            self._set_vllm_pid(None)
            self._set_sglang_pid(None)
            self._set_comfyui_pid(None)
            self._set_tts_pid(None)
            self._set_asr_pid(None)
            self._set_sglang_container(None)
            self._set_ninfer_pid(None)
            self._set_ninfer_container(None)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_pm_facade.py -v --tb=short`
Expected: PASS（4 passed）

- [ ] **Step 5: 回归 + smoke + commit**

```bash
python3 -m pytest tests/unit/engine/ -q --tb=short
python3 -c "import inferfabric; from inferfabric.process_manager.facade import ProcessManager; print('OK')"
git add inferfabric/process_manager/facade.py tests/unit/engine/test_pm_facade.py
git commit -m "feat(facade): register NInferProcessManager + thread container_name + stop_all ninfer clear

facade gains start_ninfer/stop_ninfer/start_sglang(cfg, container_name)
delegation, ninfer_pid/ninfer_container state tracking (mirrors sglang),
and stop_all clears ninfer state alongside sglang/vllm/comfyui/tts/asr.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: ninfer adapter——start/stop 委托 PM（迁出 base helper + 内联 docker）

**Files:**
- Modify: `inferfabric/engine_adapter/ninfer.py:63-130`（start + stop 体）、删内联 import（`subprocess`/`time`/`Path` 迁到 PM）
- Modify: `inferfabric/process_manager/base.py:67`（docstring 改"vllm-docker stop"）
- Test: `tests/unit/engine/test_engine_adapters.py`（加 TestNInferAdapterStartContainerName；既有 TestNInferAdapterStopContainerName 改断言——stop 现在走 PM.stop_ninfer 而非 base helper）

**Interfaces:**
- Consumes: `facade.start_ninfer(cfg, container_name)` + `facade.stop_ninfer(container_name)`（Task 3）；`ModelConfig.container_name` property（config.py:464）
- Produces: `NInferAdapter.start` 委托 PM（不再内联 docker run）；`NInferAdapter.stop` 委托 PM（不再走 base helper）。base._stop_docker_container 此后只服务 vllm-docker。

- [ ] **Step 1: 写失败测试——start 委托 start_ninfer(cfg, model.container_name)**

```python
# 追加到 tests/unit/engine/test_engine_adapters.py

class TestNInferAdapterStartContainerName:
    """NInferAdapter.start 委托 PM.start_ninfer(cfg, model.container_name)（Task 4，D1）。"""

    def _make_model(self, container_name="", port=8007):
        from inferfabric.config import ModelConfig, NInferConfig
        cfg = NInferConfig(
            port=port, container_name=container_name,
            weight_path="/w/model.bin", docker_image="ninfer:latest",
            model_id="m1", max_concurrency=8, max_context=615000,
            kv_capacity=0, default_max_tokens=8192, pending_timeout_ms=300000,
            kv_dtype="nvfp4", prefill_chunk=8192,
        )
        return ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)

    def test_start_delegates_start_ninfer_with_container_name(self):
        """start 调 PM.start_ninfer(cfg, model.container_name)，container_name 来自 property。"""
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        adapter = NInferAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(container_name="iff-ninfer-qwen38")
        assert model.container_name == "iff-ninfer-qwen38"  # property
        adapter.start(model)
        pm.start_ninfer.assert_called_once()
        # 第二个位置参 = container_name（来自 property）
        args = pm.start_ninfer.call_args
        assert args[0][1] == "iff-ninfer-qwen38" or args.kwargs.get("container_name") == "iff-ninfer-qwen38"

    def test_start_uses_derived_container_name_when_empty(self):
        """无显式 container_name：start 传推导名 ninfer-{port}（来自 property，不重推）。"""
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        adapter = NInferAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(container_name="", port=8007)
        assert model.container_name == "ninfer-8007"  # property derives
        adapter.start(model)
        args = pm.start_ninfer.call_args
        assert args[0][1] == "ninfer-8007" or args.kwargs.get("container_name") == "ninfer-8007"

    def test_start_no_proc_raises(self):
        """无 PM：start 抛 RuntimeError（和 stop 一致）。"""
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        adapter = NInferAdapter()
        import pytest
        model = self._make_model()
        with pytest.raises(RuntimeError):
            adapter.start(model)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestNInferAdapterStartContainerName -v --tb=short`
Expected: FAIL — start 当前内联 docker run（不调 pm.start_ninfer），`pm.start_ninfer.assert_called_once()` 失败

- [ ] **Step 3: 改 NInferAdapter.start——委托 PM，删内联 docker run + import**

修改 `inferfabric/engine_adapter/ninfer.py:63-124`。start 体改为委托，删 `import logging`/`import subprocess`/`import time`/`Path`（迁到 PM，adapter 不再需要）：

```python
    def start(self, model: ModelConfig) -> dict:
        """Start NInfer via ProcessManager delegation (D1).

        container_name threaded from ModelConfig.container_name (single source).
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.start_ninfer(cfg, model.container_name)

    def stop(self, model: ModelConfig) -> dict:
        """Stop NInfer via ProcessManager delegation (migrated off base helper, D1)."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = model.ninfer
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}
        return self._proc.stop_ninfer(model.container_name)
```

注意：`fetch_engine_metrics`（ninfer.py:139-149）仍用 `Path`/`re`——保留这些 import（`from pathlib import Path` 在文件顶部，不删；只删 start 体内的局部 `import subprocess`/`import time`/`import logging`）。先读 ninfer.py 顶部确认哪些是模块级、哪些是局部 import，避免误删 fetch_engine_metrics 依赖的。

- [ ] **Step 4: 改 base.py docstring**

修改 `inferfabric/process_manager/base.py:67`：`"""docker stop <model.container_name>. Shared by docker-deployed adapters.` → `"""docker stop <model.container_name>. Used by vllm-docker stop (ninfer migrated to PM launcher).`

- [ ] **Step 5: 改既有 TestNInferAdapterStopContainerName——stop 现在走 PM.stop_ninfer**

既有测试（test_engine_adapters.py:304-345）patch `subprocess.run` 测 base helper。stop 迁到 PM 后，adapter.stop 调 `pm.stop_ninfer`——改测为 mock PM 验证委托（和 start 测试对称）：

```python
class TestNInferAdapterStopContainerName:
    """NInferAdapter.stop 委托 PM.stop_ninfer(model.container_name)（Task 4，D1）。"""

    def test_stop_delegates_stop_ninfer_with_explicit_name(self):
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        from inferfabric.config import ModelConfig, NInferConfig
        adapter = NInferAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        cfg = NInferConfig(port=8007, container_name="iff-ninfer-qwen38")
        model = ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)
        adapter.stop(model)
        pm.stop_ninfer.assert_called_once_with("iff-ninfer-qwen38")

    def test_stop_delegates_stop_ninfer_with_derived_name(self):
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        from inferfabric.config import ModelConfig, NInferConfig
        adapter = NInferAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        cfg = NInferConfig(port=8007, container_name="")
        model = ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)
        adapter.stop(model)
        pm.stop_ninfer.assert_called_once_with("ninfer-8007")

    def test_stop_no_proc_raises(self):
        from inferfabric.engine_adapter.ninfer import NInferAdapter
        from inferfabric.config import ModelConfig, NInferConfig
        import pytest
        adapter = NInferAdapter()
        cfg = NInferConfig(port=8007)
        model = ModelConfig(name="t", description="d", type="ninfer", ninfer=cfg)
        with pytest.raises(RuntimeError):
            adapter.stop(model)
```

- [ ] **Step 6: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestNInferAdapterStartContainerName tests/unit/engine/test_engine_adapters.py::TestNInferAdapterStopContainerName -v --tb=short`
Expected: PASS（6 passed）

- [ ] **Step 7: 回归 + smoke + commit**

```bash
python3 -m pytest tests/unit/engine/ tests/integration/test_engine_lifecycle.py -q --tb=short
python3 -c "import inferfabric"
git add inferfabric/engine_adapter/ninfer.py inferfabric/process_manager/base.py tests/unit/engine/test_engine_adapters.py
git commit -m "refactor(adapter): NInferAdapter start/stop delegate to PM launcher (D1)

start: inline docker run → self._proc.start_ninfer(cfg, model.container_name).
stop: base._stop_docker_container → self._proc.stop_ninfer(model.container_name).
container_name now from the single ModelConfig.container_name property (no
re-derive). base helper docstring narrowed to vllm-docker (only remaining user).

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: sglang adapter + config——start 委托 + build_docker_cmd 闭合 container_name 来源

**Files:**
- Modify: `inferfabric/engine_adapter/sglang.py:48-53`（start 委托）
- Modify: `inferfabric/config.py:175-197`（build_docker_cmd 加 container_name 参，L190 --name 用参）
- Test: `tests/unit/engine/test_engine_adapters.py`（加 TestSGLangAdapterStartContainerName）

**Interfaces:**
- Consumes: `facade.start_sglang(cfg, container_name)`（Task 3）；`ModelConfig.container_name`（config.py:464，sglang 分支 L475 `f"sglang-{self.sglang.served_name}"`）
- Produces: `SGLangAdapter.start` 委托 PM（读 model.container_name）；`SGLangConfig.build_docker_cmd(container_name)` 用参数做 `--name`。闭合 Task 2 留的 build_docker_cmd 来源。

- [ ] **Step 1: 写失败测试——start 委托 + build_docker_cmd 用 container_name**

```python
# 追加到 tests/unit/engine/test_engine_adapters.py

class TestSGLangAdapterStartContainerName:
    """SGLangAdapter.start 委托 start_sglang(cfg, model.container_name)（Task 5）。"""

    def _make_model(self, served_name="foo", port=8100):
        from inferfabric.config import ModelConfig, SGLangConfig
        cfg = SGLangConfig(model_dir="/m", served_name=served_name, port=port,
                          mem_fraction=0.9, context_length=4096)
        return ModelConfig(name="t", description="d", type="sglang", sglang=cfg)

    def test_start_delegates_with_container_name(self):
        from inferfabric.engine_adapter.sglang import SGLangAdapter
        adapter = SGLangAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(served_name="foo")
        assert model.container_name == "sglang-foo"  # property
        adapter.start(model)
        args = pm.start_sglang.call_args
        assert args[0][1] == "sglang-foo" or args.kwargs.get("container_name") == "sglang-foo"

    def test_build_docker_cmd_uses_container_name_param(self):
        """build_docker_cmd(container_name) 用参数做 --name，不硬编码 sglang-{served_name}。"""
        from inferfabric.config import SGLangConfig
        cfg = SGLangConfig(model_dir="/m", served_name="foo", port=8100,
                          mem_fraction=0.9, context_length=4096)
        cmd = cfg.build_docker_cmd("custom-container-name")
        assert "--name" in cmd
        idx = cmd.index("--name")
        assert cmd[idx + 1] == "custom-container-name", \
            f"build_docker_cmd 应用 container_name 参数: {cmd[idx:idx+2]}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestSGLangAdapterStartContainerName -v --tb=short`
Expected: FAIL — start 当前 `start_sglang(cfg)` 不传 container_name；build_docker_cmd 无参签名

- [ ] **Step 3: 改 SGLangAdapter.start——委托 + 传 container_name**

修改 `inferfabric/engine_adapter/sglang.py:48-53`：

```python
    def start(self, model: ModelConfig) -> dict:
        """Start sglang via ProcessManager delegation.

        container_name threaded from ModelConfig.container_name (single source).
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        cfg = getattr(model, 'sglang')
        return self._proc.start_sglang(cfg, model.container_name)
```

- [ ] **Step 4: 改 SGLangConfig.build_docker_cmd——加 container_name 参**

修改 `inferfabric/config.py:175-197`。签名加 `container_name: str`，L190 `--name` 用参数：

```python
    def build_docker_cmd(self, container_name: str) -> list[str]:
        """Build docker run command for SGLang serving.

        container_name threaded from ModelConfig.container_name (single source).
        """
        import shlex
        model_path = MODEL_BASE / self.model_dir
        container_cmd = self.build_cmd()
        docker_flags = [
            "docker", "run", "--rm",
            "--gpus", "all",
            "--ipc=host",
            "--ulimit", "memlock=-1",
            "--ulimit", "stack=67108864",
            "-p", f"{self.port}:{self.port}",
            "-v", f"{model_path}:{model_path}",
            "-v", f"{MODEL_BASE}:/models",
            "-v", f"{Path.home() / '.cache/huggingface'}:/root/.cache/huggingface",
            "--name", container_name,
        ]
        if self.extra_env:
            for k, v in self.extra_env.items():
                docker_flags.extend(["-e", f"{k}={v}"])
        # Replace sglang binary path with full path inside container
        container_cmd[0] = "/usr/local/bin/sglang"
        return docker_flags + [self.docker_image] + container_cmd
```

- [ ] **Step 5: 改 sglang PM start_sglang——build_docker_cmd 传 container_name（闭合 Task 2）**

修改 `inferfabric/process_manager/sglang.py` 的 `start_sglang`（Task 2 改过的）：`cmd = cfg.build_docker_cmd()` → `cmd = cfg.build_docker_cmd(container_name)`。这样 `--name` 来自参数（单一来源闭合）。

- [ ] **Step 6: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestSGLangAdapterStartContainerName tests/unit/engine/test_pm_sglang.py -v --tb=short`
Expected: PASS

- [ ] **Step 7: 回归 + smoke + commit**

```bash
python3 -m pytest tests/unit/engine/ tests/integration/test_engine_lifecycle.py -q --tb=short
python3 -c "import inferfabric"
git add inferfabric/engine_adapter/sglang.py inferfabric/config.py inferfabric/process_manager/sglang.py tests/unit/engine/test_engine_adapters.py
git commit -m "feat(adapter): SGLangAdapter start delegates + build_docker_cmd container_name (single source)

start: self._proc.start_sglang(cfg, model.container_name) — reads the
unified property, no re-derive. build_docker_cmd gains container_name param
(--name uses it), closing the single-source-of-truth loop opened in Task 2.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Step 3: vllm 脚手架（D2）

### Task 6: vllm.start 按 deployment 分派 + validate 拦截 docker

**Files:**
- Modify: `inferfabric/engine_adapter/vllm.py:35-56`（validate_config 加 docker 拦截 + start 分派）
- Test: `tests/unit/engine/test_engine_adapters.py`（加 TestVLLMAdapterStartDispatch，镜像既有 TestVLLMAdapterStopDispatch:154-204）

**Interfaces:**
- Consumes: `ModelConfig.resolved_deployment`（config.py:488）；现有 `facade.start_vllm(cfg)`（conda 路径不动）
- Produces: `VLLMAdapter.start` 按 resolved_deployment 分派（conda→start_vllm，docker→error+log.warning）；`validate_config` 拒绝 docker。镜像 `vllm.stop:58-65` 结构。

- [ ] **Step 1: 写失败测试——start 按 deployment 分派**

```python
# 追加到 tests/unit/engine/test_engine_adapters.py

class TestVLLMAdapterStartDispatch:
    """VLLMAdapter.start 按 resolved_deployment 分派（Task 6，D2，镜像 stop:58-65）。"""

    def _make_model(self, deployment="", **vllm_overrides):
        from inferfabric.config import ModelConfig, VLLMConfig
        defaults = dict(model_dir="/m", served_name="t", conda_env="vllm",
                        port=8000, max_model_len=4096, gpu_memory_utilization=0.9)
        defaults.update(vllm_overrides)
        return ModelConfig(
            name="t", description="d", type="vllm", deployment=deployment,
            gpu_role="exclusive", vllm=VLLMConfig(**defaults),
        )

    def test_start_conda_calls_start_vllm(self):
        """conda 部署：start 调 PM.start_vllm（现有路径不动）。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(deployment="")  # 推导 conda
        assert model.resolved_deployment == "conda"
        adapter.start(model)
        pm.start_vllm.assert_called_once()

    def test_start_docker_returns_error(self, caplog):
        """docker 部署：start 返回 error + log.warning（D2 脚手架，未实现）。"""
        import logging
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        pm = MagicMock()
        adapter.set_process_manager(pm)
        model = self._make_model(deployment="docker", conda_env="")
        assert model.resolved_deployment == "docker"
        with caplog.at_level(logging.WARNING, logger="inferfabric.vllm_adapter"):
            result = adapter.start(model)
        assert result["status"] == "error"
        assert "docker" in result["message"].lower()
        pm.start_vllm.assert_not_called()

    def test_start_no_proc_raises(self):
        """无 PM：start 抛 RuntimeError（和 stop 一致）。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        import pytest
        adapter = VLLMAdapter()
        model = self._make_model()
        with pytest.raises(RuntimeError):
            adapter.start(model)

    def test_validate_rejects_docker_deployment(self):
        """validate_config 拦截 deployment:docker（D2，防止 start 硬失败陷阱）。"""
        from inferfabric.engine_adapter.vllm import VLLMAdapter
        adapter = VLLMAdapter()
        model = self._make_model(deployment="docker", conda_env="")
        issues = adapter.validate_config(model)
        assert any("docker" in i.lower() and "conda" in i.lower() for i in issues), \
            f"validate 应拦截 vllm docker: {issues}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestVLLMAdapterStartDispatch -v --tb=short`
Expected: FAIL — start 当前无条件 `start_vllm(cfg)`（docker 不报错）；validate 不拦截 docker

- [ ] **Step 3: 改 VLLMAdapter——start 分派 + validate 拦截**

修改 `inferfabric/engine_adapter/vllm.py`：

(a) `validate_config`（L43 之后加 docker 拦截）：

```python
        if model.resolved_deployment == "conda" and not model.vllm.conda_env:
            issues.append("vllm.conda_env is empty (required for conda deployment)")
        if model.resolved_deployment == "docker":
            issues.append("vllm docker start not yet supported — use deployment: conda")
```

(b) `start`（L51-56 改分派，镜像 stop:58-65）：

```python
    def start(self, model: ModelConfig) -> dict:
        """Start vllm, dispatching by deployment: docker → not-implemented error, conda → PM.

        D2: docker start is scaffolded (error + log.warning) but not implemented;
        validate_config rejects deployment:docker to prevent the trap.
        """
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        if model.resolved_deployment == "docker":
            log.warning("vllm docker start not implemented for %s — set deployment: conda", model.name)
            return {"status": "error",
                    "message": "vllm docker start not implemented — set deployment: conda"}
        cfg = getattr(model, 'vllm')
        return self._proc.start_vllm(cfg)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python3 -m pytest tests/unit/engine/test_engine_adapters.py::TestVLLMAdapterStartDispatch -v --tb=short`
Expected: PASS（4 passed）

- [ ] **Step 5: 全量回归 + smoke + commit**

```bash
python3 -m pytest tests/unit/ tests/integration/ -q --tb=short
python3 -c "import inferfabric"
git add inferfabric/engine_adapter/vllm.py tests/unit/engine/test_engine_adapters.py
git commit -m "feat(adapter): vllm start dispatch by deployment + validate docker gate (D2)

start mirrors stop:58-65 structure — conda → start_vllm (unchanged), docker →
clear error + log.warning (scaffolded, not implemented). validate_config
rejects deployment:docker for vllm, closing the validate-passes-but-start-
hard-fails trap. No speculative build_docker_cmd (YAGNI — no vllm-docker model).

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review（计划写完后自查，对照 spec）

**1. Spec coverage:**
- D1（ninfer PM launcher start+stop）→ Task 1（PM）+ Task 3（facade）+ Task 4（adapter 迁出 base helper）✓
- D2（vllm 脚手架+validate）→ Task 6 ✓
- D3（sglang 优雅 stop）→ Task 2 ✓
- D4（Approach A 透传值）→ Task 3/4/5 全部 `start_*(cfg, container_name)` 签名 ✓
- container_name 单一来源 → Task 5 闭合 build_docker_cmd ✓
- base helper docstring → Task 4 Step 4 ✓
- stop_all 清 ninfer → Task 3 Step 3(d) ✓
- 改造清单 8 文件全覆盖 ✓

**2. Placeholder scan:** 无 TBD/TODO/"add error handling"/"similar to Task N"。每个 code step 有完整代码块。✓

**3. Type consistency:**
- `start_ninfer(cfg, container_name: str)` — Task 1 定义，Task 3 facade 透传，Task 4 adapter 调用，签名一致 ✓
- `start_sglang(cfg, container_name: str)` — Task 2 定义，Task 3 facade 透传，Task 5 adapter 调用 ✓
- `stop_ninfer(container_name: str)` — Task 1 定义，Task 3 透传，Task 4 调用 ✓
- `build_docker_cmd(container_name: str)` — Task 5 定义 + 调用 ✓
- `ninfer_pid`/`ninfer_container`/`_set_ninfer_pid`/`_set_ninfer_container` — Task 1 定义，Task 3 facade 镜像，Task 3 stop_all 清理 ✓

无类型不一致。✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-20-unified-start-metadata-driven.md`. 

**执行方式已由用户预先绑定：串行 Subagent-Driven Development**（"选择1,但三个环节串行完成，不要并性"）。故跳过选择问询，直接进入串行 SDD：

- **REQUIRED SUB-SKILL:** superpowers:subagent-driven-development
- Fresh subagent per task（Task 1→2→3→4→5→6 串行），review between tasks，broad final review at end。
- 串行：不并行 dispatch implementer，前一个任务 review 通过后再 dispatch 下一个。
