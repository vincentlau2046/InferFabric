"""image_manager — 管理镜像台账 + 作用域 prune（issue #3 L3b）。

契约：
- 容器 label：vllm/sglang build_docker_cmd 与 ninfer start 的 docker run
  一律带 --label com.inferfabric.managed=true（网关所启容器可辨 →
  docker container prune --filter label=… 作用域清理可用）。
- 台账：record 幂等 + last_seen 刷新；损坏文件容忍（空台账，不抛）；
  IFF_DATA_DIR 定位；引擎启动路径（显式 pull + record）接线。
- prune 纪律（不变量）：只 rmi 台账内且无配置引用的镜像；被引用必留；
  rmi 失败 fail-visible 且引用保留在台账；永不发 docker image prune。

全部纯逻辑（fake run / 构造 config / tmp_path），无网络/GPU/docker 依赖。
"""

import json
from pathlib import Path

import pytest

from inferfabric.image_manager import (
    LEDGER_FILENAME,
    MANAGED_LABEL,
    ManagedImageLedger,
    collect_image_refs,
    default_ledger_path,
    ensure_image_pulled,
    prune_managed,
    record_image,
)
from inferfabric.config import MODEL_BASE, SGLangConfig, VLLMConfig


# ── Ledger ──────────────────────────────────────────────────────

class TestManagedImageLedger:

    def test_record_idempotent_with_last_seen_refresh(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm/vllm-openai:v0.24.0", now=100.0)
        led.record("vllm/vllm-openai:v0.24.0", now=200.0)
        e = led.entries()["vllm/vllm-openai:v0.24.0"]
        assert e["first_seen"] == 100.0          # first_seen 不被刷新
        assert e["last_seen"] == 200.0
        assert led.refs() == {"vllm/vllm-openai:v0.24.0"}

    def test_persistence_across_instances(self, tmp_path):
        p = tmp_path / LEDGER_FILENAME
        ManagedImageLedger(p).record("img:a", now=1.0)
        led2 = ManagedImageLedger(p)
        assert led2.refs() == {"img:a"}

    def test_corrupt_file_tolerated_as_empty(self, tmp_path):
        p = tmp_path / LEDGER_FILENAME
        p.write_text("{not json")
        led = ManagedImageLedger(p)
        assert led.refs() == set()               # 不抛，空台账起步
        led.record("img:b", now=1.0)             # 且可继续写入
        assert ManagedImageLedger(p).refs() == {"img:b"}

    def test_forget_removes_and_persists(self, tmp_path):
        p = tmp_path / LEDGER_FILENAME
        led = ManagedImageLedger(p)
        led.record("img:a", now=1.0)
        led.record("img:b", now=1.0)
        led.forget("img:a")
        assert ManagedImageLedger(p).refs() == {"img:b"}

    def test_default_ledger_path_uses_iff_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IFF_DATA_DIR", str(tmp_path))
        assert default_ledger_path() == tmp_path / LEDGER_FILENAME

    def test_digest_recorded_and_kept_on_digestless_rerecord(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:a", digest="repo@sha256:aaa", now=1.0)
        assert led.digest("img:a") == "repo@sha256:aaa"
        led.record("img:a", now=2.0)              # 本次拿不到 digest ≠ 忘记 digest
        assert led.digest("img:a") == "repo@sha256:aaa"
        led.record("img:a", digest="repo@sha256:bbb", now=3.0)
        assert led.digest("img:a") == "repo@sha256:bbb"

    def test_revision_persisted_and_bumped_per_write(self, tmp_path):
        p = tmp_path / LEDGER_FILENAME
        led = ManagedImageLedger(p)
        assert led.revision == 0
        led.record("img:a", now=1.0)
        assert led.revision == 1
        led.record("img:b", now=1.0)
        led.forget("img:a")
        assert led.revision == 3
        assert ManagedImageLedger(p).revision == 3   # 持久化
        led.forget("img:absent")                     # 无变化 → 不写、不推进
        assert led.revision == 3

    def test_external_writer_advance_is_resynced_not_clobbered(self, tmp_path):
        p = tmp_path / LEDGER_FILENAME
        led_a = ManagedImageLedger(p)
        led_b = ManagedImageLedger(p)            # 两个写者各自加载
        led_a.record("img:a", now=1.0)           # 磁盘 revision=1
        led_b.record("img:b", now=2.0)           # 写前同步 → 不覆盖 a
        assert ManagedImageLedger(p).refs() == {"img:a", "img:b"}


class TestRecordImage:

    def test_record_image_writes_default_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IFF_DATA_DIR", str(tmp_path))
        record_image("sglang:latest")
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        assert led.refs() == {"sglang:latest"}

    def test_record_image_never_raises_on_ledger_failure(self, tmp_path, monkeypatch):
        # 台账目录不可写 → 记账失败仅告警（引擎启动不得依赖台账）
        monkeypatch.setenv("IFF_DATA_DIR", str(tmp_path / "nope" / "nope"))
        (tmp_path / "nope").write_text("not a dir")  # mkdir 将失败
        record_image("img:x")                        # 不抛
        assert True


# ── ensure_image_pulled ─────────────────────────────────────────

def _run_map(responses, digests=None):
    """fake subprocess.run：按命令首段（inspect/pull）返回预设 CompletedProcess。

    digests: image ref → `docker image inspect --format …` 的 RepoDigests 输出
    （None/缺省 → 空串，模拟拿不到 digest）。
    """
    calls = []

    class R:
        def __init__(self, returncode, stderr="", stdout=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = stdout

    def run(cmd, **kw):
        calls.append(list(cmd))
        if "--format" in cmd:
            return R(0, "", (digests or {}).get(cmd[-1], ""))
        key = "inspect" if "inspect" in cmd else "pull"
        rc, err = responses[key]
        return R(rc, err)

    run.calls = calls
    return run


class TestEnsureImagePulled:

    def test_image_present_skips_pull_and_records(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        run = _run_map({"inspect": (0, ""), "pull": (0, "")},
                       digests={"img:1": "repo@sha256:1"})
        res = ensure_image_pulled("img:1", ledger=led, run=run)
        assert res == {"status": "ok", "pulled": False, "image": "img:1"}
        assert not any("pull" in c for c in run.calls)
        assert led.refs() == {"img:1"}
        assert led.digest("img:1") == "repo@sha256:1"   # digest 一并记账

    def test_image_missing_pulls_and_records(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        run = _run_map({"inspect": (1, ""), "pull": (0, "")},
                       digests={"img:2": "repo@sha256:2"})
        res = ensure_image_pulled("img:2", ledger=led, run=run)
        assert res["pulled"] is True and res["status"] == "ok"
        assert any("pull" in c for c in run.calls)
        assert led.refs() == {"img:2"}
        assert led.digest("img:2") == "repo@sha256:2"

    def test_pull_failure_fail_visible_not_recorded(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        run = _run_map({"inspect": (1, ""), "pull": (1, "network down")})
        res = ensure_image_pulled("img:3", ledger=led, run=run)
        assert res["status"] == "error"
        assert led.refs() == set()               # 拉取失败不记账（镜像不在盘上）


# ── collect_image_refs / prune_managed ─────────────────────────

class _Sub:
    def __init__(self, image):
        self.docker_image = image


class _Model:
    def __init__(self, vllm=None, sglang=None, ninfer=None):
        self.vllm = vllm
        self.sglang = sglang
        self.ninfer = ninfer


class TestCollectImageRefs:

    def test_scans_three_engine_configs(self):
        models = {"a": _Model(vllm=_Sub("vllm:v1")),
                  "b": _Model(sglang=_Sub("sgl:v1")),
                  "c": _Model(ninfer=_Sub("ninfer:v1")),
                  "d": _Model()}
        assert collect_image_refs(models) == {"vllm:v1", "sgl:v1", "ninfer:v1"}


class TestPruneManaged:

    def test_referenced_kept_unreferenced_removed(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:keep", digest="repo@sha256:keep", now=1.0)
        led.record("img:gone", digest="repo@sha256:gone", now=1.0)
        run = _run_map({"inspect": (0, ""), "pull": (0, "")},
                       digests={"img:gone": "repo@sha256:gone"})
        report = prune_managed(led, {"img:keep"}, run=run)
        actions = {r["ref"]: r["action"] for r in report}
        assert actions == {"img:keep": "kept", "img:gone": "removed"}
        assert led.refs() == {"img:keep"}
        assert ["docker", "rmi", "img:gone"] in run.calls

    def test_digest_mismatch_skipped_and_kept(self, tmp_path):
        # tag 被重指：台账 digest 与当前 digest 不符 → 不 rmi，条目保留
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:moved", digest="repo@sha256:old", now=1.0)
        run = _run_map({"inspect": (0, ""), "pull": (0, "")},
                       digests={"img:moved": "repo@sha256:new"})
        report = prune_managed(led, set(), run=run)
        assert report[0]["action"] == "skipped"
        assert "digest changed" in report[0]["reason"]
        assert not any("rmi" in c for c in run.calls)
        assert led.refs() == {"img:moved"}

    def test_no_digest_recorded_skipped_unverifiable(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:legacy", now=1.0)        # 无 digest（旧条目/取不到）
        run = _run_map({"inspect": (0, ""), "pull": (0, "")})
        report = prune_managed(led, set(), run=run)
        assert report[0]["action"] == "skipped"
        assert "unverifiable" in report[0]["reason"]
        assert not any("rmi" in c for c in run.calls)
        assert led.refs() == {"img:legacy"}

    def test_rmi_failure_fail_visible_ref_stays(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:stuck", digest="repo@sha256:s", now=1.0)

        def failing_rmi(cmd, **kw):
            class R:
                returncode = 0 if "--format" in cmd else 1
                stderr = "image is in use"
                stdout = "repo@sha256:s" if "--format" in cmd else ""
            return R()

        report = prune_managed(led, set(), run=failing_rmi)
        assert report[0]["action"] == "error"
        assert "in use" in report[0]["reason"]
        assert led.refs() == {"img:stuck"}       # 删不掉仍是我们的责任，台账保留

    def test_dry_run_touches_nothing(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:d", digest="repo@sha256:d", now=1.0)
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            class R:
                returncode = 0
                stderr = ""
                stdout = ""
            return R()

        report = prune_managed(led, set(), run=run, dry_run=True)
        assert report[0]["action"] == "would-remove"
        assert calls == []                        # 零 docker 调用（执行时才复核 digest）
        assert led.refs() == {"img:d"}

    def test_never_issues_global_image_prune(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("img:x", digest="repo@sha256:x", now=1.0)
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            class R:
                returncode = 0
                stderr = ""
                stdout = "repo@sha256:x" if "--format" in cmd else ""
            return R()

        prune_managed(led, set(), run=run)
        for c in calls:
            assert "prune" not in c, f"全局 prune 出现: {c}"


def _make_ninfer_fake_run(runs):
    """统一 fake subprocess.run：ninfer 启动路径 + image_manager 共用。

    注意：monkeypatch 针对的是 stdlib subprocess 模块属性（全局唯一对象），
    因此只能打一次补丁 —— 拆分 ninfer / image_manager 两个假 run 会互相覆盖。
    """

    class R:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kw):
        runs.append(list(cmd))
        if cmd[:2] == ["docker", "run"]:
            return R(stdout="container-id\n")
        if cmd[:2] == ["docker", "inspect"]:
            # 区分两种 inspect：_container_info（含 \t，容器不存在 → rc=1）
            # 与 _probe_container_running（单字段 → 在跑 → "true"）
            if len(cmd) > 3 and "\t" in cmd[3]:
                return R(returncode=1, stderr="no such object")
            return R(stdout="true\n")
        return R()  # docker stop / image inspect / pull …

    return fake_run


# ── 容器 label 注入 ────────────────────────────────────────────

class TestManagedLabelInjected:

    def test_vllm_docker_cmd_carries_label(self):
        cfg = VLLMConfig(model_dir="m", served_name="foo", conda_env="v",
                         port=8000, max_model_len=4096,
                         gpu_memory_utilization=0.9)
        cmd = cfg.build_docker_cmd("vllm-test")
        i = cmd.index("--label")
        assert cmd[i + 1] == MANAGED_LABEL

    def test_sglang_docker_cmd_carries_label(self):
        cfg = SGLangConfig(model_dir="m", served_name="foo", port=8006,
                           conda_env="")
        cmd = cfg.build_docker_cmd("sglang-test")
        i = cmd.index("--label")
        assert cmd[i + 1] == MANAGED_LABEL

    def test_ninfer_docker_run_carries_label(self, monkeypatch, tmp_path):
        from inferfabric.process_manager.ninfer import NInferProcessManager
        from inferfabric.state import StateDB
        from inferfabric.config import NInferConfig

        monkeypatch.setenv("IFF_DATA_DIR", str(tmp_path))
        pm = NInferProcessManager(StateDB(tmp_path / "s.db"), tmp_path)
        cfg = NInferConfig(
            port=8007, container_name="iff-ninfer-qwen38",
            weight_path="/w/model.bin", docker_image="ninfer:latest",
            model_id="m1", max_concurrency=8, max_context=615000,
            kv_capacity=0, default_max_tokens=8192, pending_timeout_ms=300000,
            kv_dtype="nvfp4", prefill_chunk=8192,
        )
        runs = []

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            _make_ninfer_fake_run(runs))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.time.sleep", lambda s: None)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http",
                            lambda url, timeout=120: True)

        result = pm.start_ninfer(cfg, "ninfer-test")
        assert result.get("status") == "healthy", result
        run_cmd = next(c for c in runs if c[:2] == ["docker", "run"])
        i = run_cmd.index("--label")
        assert run_cmd[i + 1] == MANAGED_LABEL


# ── 启动路径接线（显式 pull + record）─────────────────────────

class TestStartPathsRecordManagedImage:

    def test_ninfer_start_records_image(self, monkeypatch, tmp_path):
        from inferfabric.process_manager.ninfer import NInferProcessManager
        from inferfabric.state import StateDB
        from inferfabric.config import NInferConfig

        monkeypatch.setenv("IFF_DATA_DIR", str(tmp_path))
        pm = NInferProcessManager(StateDB(tmp_path / "s.db"), tmp_path)
        cfg = NInferConfig(
            port=8007, container_name="iff-ninfer-qwen38",
            weight_path="/w/model.bin", docker_image="ninfer:latest",
            model_id="m1", max_concurrency=8, max_context=615000,
            kv_capacity=0, default_max_tokens=8192, pending_timeout_ms=300000,
            kv_dtype="nvfp4", prefill_chunk=8192,
        )

        runs = []

        monkeypatch.setattr("inferfabric.process_manager.ninfer.subprocess.run",
                            _make_ninfer_fake_run(runs))
        monkeypatch.setattr("inferfabric.process_manager.ninfer.time.sleep", lambda s: None)
        monkeypatch.setattr("inferfabric.process_manager.ninfer.wait_http",
                            lambda url, timeout=120: True)

        pm.start_ninfer(cfg, "ninfer-test")
        assert ManagedImageLedger(tmp_path / LEDGER_FILENAME).refs() == {"ninfer:latest"}
