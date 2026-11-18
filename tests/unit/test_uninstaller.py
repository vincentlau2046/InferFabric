"""uninstaller — 项目域卸载（issue #3 L2，CLI 先行）。

契约：
- 引擎引用计数是唯一可删判据：env/image 被任一 models.d 配置引用 → blocked，
  零副作用；extra_sources 可注入 fork 侧引用源（路径假设不写死）。
- 镜像双重门禁：引用计数 + 台账（L3b）——用户自拉镜像永不触碰；
  永不发 docker image prune。
- env 前缀必须落在 CONDA_ENVS 内；conda 优先，缺 conda 时 --force 直删；
  `~/miniconda3/pkgs` 共享缓存永不触碰（Q5）。
- 模型卸载：停服务失败即中止（不留"活进程 + 删配置"的孤儿）；池候选
  引用默认 blocked，--purge-pools 同批清除；权重属用户资产，只报告不删。
- 所有破坏性动作 dry-run 零副作用（不调 stop/不写盘/不调 docker）。

全部纯逻辑（tmp_path / fake run / 构造 config），无 docker/conda/GPU 依赖。
"""

import yaml
import pytest

from inferfabric.image_manager import LEDGER_FILENAME, ManagedImageLedger
from inferfabric import uninstaller as un
from inferfabric.uninstaller import (
    EngineRefs,
    collect_engine_refs,
    find_model,
    find_model_yaml,
    pool_refs,
    purge_pool_candidates,
    remove_env,
    remove_image,
    remove_model,
)


# ── 构造替身 ──────────────────────────────────────────────────

class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _model(served_name="", vllm=None, sglang=None, ninfer=None,
           comfyui=None, tts=None, asr=None):
    m = _NS(vllm=vllm, sglang=sglang, ninfer=ninfer,
            comfyui=comfyui, tts=tts, asr=asr)
    m.served_name = served_name
    return m


def _run_map(responses, digests=None):
    """fake run：按 argv 关键字匹配预设 (rc, stderr)；digests 模拟 digest inspect。"""
    calls = []

    class R:
        def __init__(self, rc, stderr="", stdout=""):
            self.returncode = rc
            self.stderr = stderr
            self.stdout = stdout

    def run(cmd, **kw):
        calls.append(list(cmd))
        if "--format" in cmd:
            return R(0, "", (digests or {}).get(cmd[-1], ""))
        for key, (rc, err) in responses.items():
            if key in cmd:
                return R(rc, err)
        return R(0)

    run.calls = calls
    return run


# ── 引用索引 ──────────────────────────────────────────────────

class TestCollectEngineRefs:

    def test_scans_all_engine_attrs(self):
        models = {
            "a": _model(vllm=_NS(conda_env="v-1", docker_image="vllm:v1")),
            "b": _model(sglang=_NS(conda_env="", docker_image="sgl:v1")),
            "c": _model(ninfer=_NS(docker_image="ninfer:v1")),
            "d": _model(comfyui=_NS(conda_env="comfyui")),
            "e": _model(tts=_NS(conda_env="tts-env")),
            "f": _model(asr=_NS(conda_env="asr-env")),
            "g": _model(),
        }
        refs = collect_engine_refs(models)
        assert set(refs.conda_envs) == {"v-1", "comfyui", "tts-env", "asr-env"}
        assert refs.conda_envs["v-1"] == {"a"}
        assert set(refs.images) == {"vllm:v1", "sgl:v1", "ninfer:v1"}

    def test_extra_sources_merge_callables_and_values(self):
        models = {"a": _model(vllm=_NS(conda_env="v-1"))}
        extra = EngineRefs(conda_envs={"pack-env": {"pack:x"}},
                           images={"img:p": {"pack:x"}})
        refs = collect_engine_refs(
            models, extra_sources=[extra, lambda: EngineRefs(conda_envs={"v-1": {"lock:y"}})])
        assert refs.conda_envs["v-1"] == {"a", "lock:y"}   # 合并同一 env 的 owners
        assert refs.conda_envs["pack-env"] == {"pack:x"}
        assert refs.images["img:p"] == {"pack:x"}


# ── 模型定位 ──────────────────────────────────────────────────

class TestFindModel:

    def test_by_key_and_served_name(self):
        models = {"qwen3": _model(served_name="fabric-qwen3")}
        assert find_model(models, "qwen3")[0] == "qwen3"
        assert find_model(models, "fabric-qwen3")[0] == "qwen3"
        assert find_model(models, "nope") == (None, None)

    def test_yaml_by_stem_then_name_field(self, tmp_path):
        (tmp_path / "a.yaml").write_text("name: a\ntype: vllm\n")
        (tmp_path / "b.yaml").write_text("name: b\ntype: vllm\n")
        assert find_model_yaml(tmp_path, "a").name == "a.yaml"
        assert find_model_yaml(tmp_path, "b").name == "b.yaml"
        assert find_model_yaml(tmp_path, "missing") is None


# ── 池候选 ────────────────────────────────────────────────────

def _write_pools(path):
    path.write_text(yaml.safe_dump({
        "schema_version": 1, "revision": 4,
        "pools": {
            "fast": {"allowed_locations": ["local"], "candidates": [
                {"id": "a", "priority": 10, "kind": "local", "model_name": "m1"},
                {"id": "b", "priority": 20, "kind": "cloud",
                 "provider": "p", "model_id": "x"},
            ]},
            "slow": {"allowed_locations": ["local"], "candidates": [
                {"id": "c", "priority": 10, "kind": "local", "model_name": "m2"},
            ]},
        }}))
    return path


class TestPoolCandidates:

    def test_pool_refs_only_local_matches(self, tmp_path):
        p = _write_pools(tmp_path / "pools.yaml")
        rows = pool_refs(p, {"m1"})
        assert rows == [{"pool": "fast", "candidate_id": "a", "model_name": "m1"}]
        assert pool_refs(tmp_path / "absent.yaml", {"m1"}) == []

    def test_purge_removes_matching_keeps_rest_and_bumps_revision(self, tmp_path):
        p = _write_pools(tmp_path / "pools.yaml")
        removed = purge_pool_candidates(p, {"m1"})
        assert removed == [{"pool": "fast", "candidate_id": "a", "model_name": "m1"}]
        raw = yaml.safe_load(p.read_text())
        assert raw["revision"] == 5
        ids = [c["id"] for pool in raw["pools"].values() for c in pool["candidates"]]
        assert ids == ["b", "c"]            # 云候选与他池候选原样保留

    def test_purge_no_match_is_noop_no_write(self, tmp_path):
        p = _write_pools(tmp_path / "pools.yaml")
        before = p.read_text()
        assert purge_pool_candidates(p, {"nope"}) == []
        assert p.read_text() == before


# ── 模型卸载 ──────────────────────────────────────────────────

def _model_env(tmp_path):
    """一个 vllm 模型 + 其 models.d YAML。"""
    models = {"m1": _model(served_name="fabric-m1",
                           vllm=_NS(conda_env="v-1", model_dir="Qwen3"))}
    mdir = tmp_path / "models.d"
    mdir.mkdir()
    (mdir / "m1.yaml").write_text("name: m1\ntype: vllm\n")
    return models, mdir


class TestRemoveModel:

    def test_dry_run_zero_side_effects(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        stops = []
        report = remove_model("m1", models=models, models_dir=mdir,
                              stop_fn=lambda: stops.append(1),
                              is_running_fn=lambda: True, dry_run=True)
        assert report["status"] == "would-remove"
        assert stops == []
        assert (mdir / "m1.yaml").exists()
        assert any(a["action"] == "would-stop" for a in report["actions"])
        assert report["weights_kept"]                    # 报告不删
        assert any(p.endswith("Qwen3") for p in report["weights_kept"])

    def test_execute_stops_then_deletes(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        stops = []
        report = remove_model("fabric-m1", models=models, models_dir=mdir,
                              stop_fn=lambda: stops.append(1) or {"status": "stopped"},
                              is_running_fn=lambda: True)
        assert report["status"] == "removed"
        assert stops == [1]
        assert not (mdir / "m1.yaml").exists()

    def test_stop_failure_aborts_and_keeps_config(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        report = remove_model(
            "m1", models=models, models_dir=mdir,
            stop_fn=lambda: {"status": "error", "message": "boom"},
            is_running_fn=lambda: True)
        assert report["status"] == "error"
        assert (mdir / "m1.yaml").exists()     # 不留活进程 + 删配置的孤儿

    def test_not_running_skips_stop(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        stops = []
        report = remove_model("m1", models=models, models_dir=mdir,
                              stop_fn=lambda: stops.append(1) or {"status": "stopped"},
                              is_running_fn=lambda: False)
        assert stops == []
        assert any(a.get("status") == "skipped" for a in report["actions"])

    def test_pool_reference_blocks_without_purge(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        pools = _write_pools(tmp_path / "pools.yaml")
        report = remove_model("m1", models=models, models_dir=mdir,
                              pools_path=pools, is_running_fn=lambda: False)
        assert report["status"] == "blocked"
        assert report["pool_refs"][0]["pool"] == "fast"
        assert (mdir / "m1.yaml").exists()
        assert len(yaml.safe_load(pools.read_text())["pools"]["fast"]["candidates"]) == 2

    def test_purge_pools_clears_candidates_then_removes(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        pools = _write_pools(tmp_path / "pools.yaml")
        report = remove_model("m1", models=models, models_dir=mdir,
                              pools_path=pools, purge_pools=True,
                              is_running_fn=lambda: False)
        assert report["status"] == "removed"
        assert not (mdir / "m1.yaml").exists()
        raw = yaml.safe_load(pools.read_text())
        assert [c["id"] for c in raw["pools"]["fast"]["candidates"]] == ["b"]

    def test_unknown_model_error(self, tmp_path):
        assert remove_model("nope", models={}, models_dir=tmp_path)["status"] == "error"


# ── env 卸载 ──────────────────────────────────────────────────

class TestRemoveEnv:

    def _root(self, tmp_path):
        root = tmp_path / "envs"
        (root / "v-1").mkdir(parents=True)
        (root / "orphan").mkdir()
        return root

    def test_referenced_env_blocked_no_calls(self, tmp_path):
        root = self._root(tmp_path)
        run = _run_map({})
        report = remove_env("v-1", models={"a": _model(vllm=_NS(conda_env="v-1"))},
                            envs_root=root, run=run, conda_bin="/usr/bin/conda")
        assert report["status"] == "blocked"
        assert report["referenced_by"] == ["a"]
        assert run.calls == []
        assert (root / "v-1").exists()

    def test_dry_run_no_calls(self, tmp_path):
        root = self._root(tmp_path)
        run = _run_map({})
        report = remove_env("orphan", models={}, envs_root=root,
                            run=run, conda_bin="/usr/bin/conda", dry_run=True)
        assert report["status"] == "would-remove"
        assert run.calls == []

    def test_conda_path_removes(self, tmp_path):
        root = self._root(tmp_path)
        run = _run_map({})
        report = remove_env("orphan", models={}, envs_root=root,
                            run=run, conda_bin="/usr/bin/conda")
        assert report["status"] == "removed"
        assert run.calls == [["/usr/bin/conda", "env", "remove",
                              "-p", str((root / "orphan").resolve()), "-y"]]
        assert "pkgs" in report["note"]      # Q5：共享缓存明确不碰

    def test_conda_failure_fail_visible(self, tmp_path):
        root = self._root(tmp_path)
        run = _run_map({"-p": (1, "CondaError: in use")})
        report = remove_env("orphan", models={}, envs_root=root,
                            run=run, conda_bin="/usr/bin/conda")
        assert report["status"] == "error"
        assert "in use" in report["message"]
        assert (root / "orphan").exists()

    def test_missing_conda_requires_force(self, tmp_path):
        root = self._root(tmp_path)
        report = remove_env("orphan", models={}, envs_root=root, conda_bin=None)
        assert report["status"] == "error"
        assert "--force" in report["message"]
        assert (root / "orphan").exists()

    def test_force_rmtree_when_conda_missing(self, tmp_path):
        root = self._root(tmp_path)
        (root / "orphan" / "bin").mkdir()
        report = remove_env("orphan", models={}, envs_root=root,
                            conda_bin=None, force=True)
        assert report["status"] == "removed"
        assert not (root / "orphan").exists()

    def test_path_traversal_rejected(self, tmp_path):
        root = self._root(tmp_path)
        report = remove_env("../escape", models={}, envs_root=root)
        assert report["status"] == "error"
        assert "invalid env name" in report["message"]

    def test_missing_prefix_error(self, tmp_path):
        root = self._root(tmp_path)
        report = remove_env("ghost", models={}, envs_root=root)
        assert report["status"] == "error"
        assert "does not exist" in report["message"]


# ── 镜像卸载 ──────────────────────────────────────────────────

class TestRemoveImage:

    def test_unmanaged_image_blocked(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        run = _run_map({})
        report = remove_image("user/img:1", models={}, ledger=led, run=run)
        assert report["status"] == "blocked"
        assert "ledger" in report["message"]
        assert run.calls == []

    def test_referenced_image_blocked(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:v1", now=1.0)
        report = remove_image("vllm:v1", models={"a": _model(vllm=_NS(docker_image="vllm:v1"))},
                              ledger=led)
        assert report["status"] == "blocked"
        assert report["referenced_by"] == ["a"]
        assert led.refs() == {"vllm:v1"}

    def test_no_digest_blocked_unverifiable(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", now=1.0)          # 无 digest 的旧条目
        run = _run_map({})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run)
        assert report["status"] == "blocked"
        assert "digest" in report["message"]
        assert not any("rmi" in c for c in run.calls)
        assert led.refs() == {"vllm:v1"}

    def test_digest_changed_refused(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:old", now=1.0)
        run = _run_map({}, digests={"vllm:v1": "repo@sha256:new"})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run)
        assert report["status"] == "blocked"
        assert "digest changed" in report["message"]
        assert not any("rmi" in c for c in run.calls)

    def test_dry_run_no_calls(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:v1", now=1.0)
        run = _run_map({})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run, dry_run=True)
        assert report["status"] == "would-remove"
        assert run.calls == []

    def test_rmi_removes_and_forgets(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:v1", now=1.0)
        run = _run_map({}, digests={"vllm:v1": "repo@sha256:v1"})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run)
        assert report["status"] == "removed"
        assert ["docker", "rmi", "vllm:v1"] in run.calls
        assert led.refs() == set()

    def test_rmi_failure_fail_visible_ref_stays(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:v1", now=1.0)
        run = _run_map({"rmi": (1, "image is in use")},
                       digests={"vllm:v1": "repo@sha256:v1"})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run)
        assert report["status"] == "error"
        assert "in use" in report["message"]
        assert led.refs() == {"vllm:v1"}


# ── CLI 接线 ──────────────────────────────────────────────────

class TestCmdRemove:

    def test_usage_error(self):
        from inferfabric.cli import cmd_remove
        with pytest.raises(SystemExit):
            cmd_remove(["bogus"])

    def test_unknown_model_exits_1(self, monkeypatch, tmp_path, capsys):
        import inferfabric.config as cfg
        from inferfabric.cli import cmd_remove
        monkeypatch.setattr(cfg, "load_models", lambda *a, **k: {})
        monkeypatch.setattr(cfg, "MODELS_DIR", tmp_path)
        with pytest.raises(SystemExit) as ei:
            cmd_remove(["model", "ghost"])
        assert ei.value.code == 1
        assert "unknown model" in capsys.readouterr().out