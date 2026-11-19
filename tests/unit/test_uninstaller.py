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

import urllib.error

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
    weight_paths,
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


def _no_proxy(monkeypatch):
    """force the no-live-registry path (connection refused semantics)."""
    def _boom(*a, **k):
        raise urllib.error.URLError("proxy down")
    monkeypatch.setattr(un, "_admin_request", _boom)


def _run_map(responses, digests=None, ids=None):
    """fake run：按 argv 关键字匹配预设；digests/ids 模拟 inspect 的 --format 输出。"""
    calls = []

    class R:
        def __init__(self, rc, stderr="", stdout=""):
            self.returncode = rc
            self.stderr = stderr
            self.stdout = stdout

    def run(cmd, **kw):
        calls.append(list(cmd))
        if "--format" in cmd:
            if "{{.Id}}" in cmd:
                return R(0, "", (ids or {}).get(cmd[-1], ""))
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

    def test_purge_removes_matching_keeps_rest_and_bumps_revision(
            self, tmp_path, monkeypatch):
        _no_proxy(monkeypatch)
        p = _write_pools(tmp_path / "pools.yaml")
        res = purge_pool_candidates(p, {"m1"})
        assert res["removed"] == [{"pool": "fast", "candidate_id": "a", "model_name": "m1"}]
        assert res["reload"]["status"] == "pending"
        raw = yaml.safe_load(p.read_text())
        assert raw["revision"] == 5
        ids = [c["id"] for pool in raw["pools"].values() for c in pool["candidates"]]
        assert ids == ["b", "c"]            # 云候选与他池候选原样保留

    def test_purge_no_match_is_noop_no_write(self, tmp_path, monkeypatch):
        _no_proxy(monkeypatch)
        p = _write_pools(tmp_path / "pools.yaml")
        before = p.read_text()
        res = purge_pool_candidates(p, {"nope"})
        assert res["removed"] == [] and res["reload"]["status"] == "noop"
        assert p.read_text() == before

    def test_unreadable_pool_config_fails_closed(self, tmp_path):
        from inferfabric.uninstaller import PoolsConfigError
        p = tmp_path / "pools.yaml"
        p.write_text("pools: [unclosed\n")
        with pytest.raises(PoolsConfigError):
            pool_refs(p, {"m1"})
        with pytest.raises(PoolsConfigError):
            purge_pool_candidates(p, {"m1"})

    def test_non_mapping_pool_config_fails_closed(self, tmp_path):
        from inferfabric.uninstaller import PoolsConfigError
        p = tmp_path / "pools.yaml"
        p.write_text("- just\n- a list\n")
        with pytest.raises(PoolsConfigError):
            pool_refs(p, {"m1"})

    def test_bad_admin_json_fails_closed_no_local_write(
            self, tmp_path, monkeypatch):
        from inferfabric.uninstaller import PoolsConfigError
        p = _write_pools(tmp_path / "pools.yaml")
        before = p.read_text()
        monkeypatch.setattr(
            un, "_admin_request",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad json")))
        with pytest.raises(PoolsConfigError):
            purge_pool_candidates(p, {"m1"})
        assert p.read_text() == before      # 活 registry 在跑时绝不本地偷写

    def test_purge_uses_live_registry_cas(self, tmp_path, monkeypatch):
        p = _write_pools(tmp_path / "pools.yaml")
        calls = []

        def fake_admin(method, path, **kw):
            calls.append((method, kw.get("payload")))
            if method == "GET":
                return 200, {"revision": 4, "raw": yaml.safe_load(p.read_text())}
            return 200, {"status": "updated", "revision": 5}

        monkeypatch.setattr(un, "_admin_request", fake_admin)
        res = purge_pool_candidates(p, {"m1"})
        assert res["removed"][0]["candidate_id"] == "a"
        assert res["reload"] == {"status": "applied-live", "revision": 5}
        put_payload = calls[-1][1]
        assert put_payload["expected_revision"] == 4     # CAS 基点=live revision
        assert "revision" not in put_payload   # registry 管理 revision，不落盘旧值
        # live 模式不本地写盘：文件仍由 proxy 的 registry 持久化
        assert len(yaml.safe_load(p.read_text())["pools"]["fast"]["candidates"]) == 2
        # 更新请求必须是 POST：同步 server 只路由 GET/POST（无 do_PUT），
        # PR #7 曾发 PUT → 501，活 registry 下 purge 必挂（MacFabric dashboard
        # 真实浏览器验收发现）。
        assert calls[-1][0] == "POST"

    def test_purge_cas_conflict_refreshes_once(self, tmp_path, monkeypatch):
        p = _write_pools(tmp_path / "pools.yaml")
        state = {"gets": 0, "puts": 0}

        def fake_admin(method, path, **kw):
            if method == "GET":
                state["gets"] += 1
                return 200, {"revision": 4 + state["gets"] - 1,
                             "raw": yaml.safe_load(p.read_text())}
            state["puts"] += 1
            if state["puts"] == 1:
                raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
            assert kw["payload"]["expected_revision"] == 5
            return 200, {"status": "updated", "revision": 6}

        monkeypatch.setattr(un, "_admin_request", fake_admin)
        res = purge_pool_candidates(p, {"m1"})
        assert res["reload"] == {"status": "applied-live", "revision": 6}
        assert state == {"gets": 2, "puts": 2}


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

    def test_weights_kept_covers_llama_cpp_model_path(self, tmp_path):
        """llama_cpp 的 GGUF 是显式文件路径（model_path），预览必须列出
        （MacFabric 验收发现：包引擎模型移除预览缺权重条目）。"""
        from inferfabric.config import ModelConfig, LlamaCppConfig
        cfg = ModelConfig(name="gguf-m", type="llama_cpp", description="d",
                          llama_cpp=LlamaCppConfig(
                              model_path="/data/gguf/qwen3-q4.gguf"))
        paths = weight_paths(cfg)
        assert paths == ["/data/gguf/qwen3-q4.gguf"]

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

    def test_purge_pools_clears_candidates_then_removes(self, tmp_path, monkeypatch):
        _no_proxy(monkeypatch)
        models, mdir = _model_env(tmp_path)
        pools = _write_pools(tmp_path / "pools.yaml")
        report = remove_model("m1", models=models, models_dir=mdir,
                              pools_path=pools, purge_pools=True,
                              is_running_fn=lambda: False)
        assert report["status"] == "removed"
        assert not (mdir / "m1.yaml").exists()
        raw = yaml.safe_load(pools.read_text())
        assert [c["id"] for c in raw["pools"]["fast"]["candidates"]] == ["b"]
        reload_action = [a for a in report["actions"] if a["action"] == "pool-reload"]
        assert reload_action and reload_action[0]["status"] == "pending"

    def test_unreadable_pools_abort_before_any_side_effect(self, tmp_path):
        models, mdir = _model_env(tmp_path)
        pools = tmp_path / "pools.yaml"
        pools.write_text("pools: [unclosed\n")
        stops = []
        report = remove_model("m1", models=models, models_dir=mdir,
                              pools_path=pools, purge_pools=True,
                              stop_fn=lambda: stops.append(1) or {"status": "stopped"},
                              is_running_fn=lambda: True)
        assert report["status"] == "error"
        assert "pool reference gate unavailable" in report["message"]
        assert stops == [] and (mdir / "m1.yaml").exists()

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

    def test_missing_conda_requires_force(self, tmp_path, monkeypatch):
        monkeypatch.setattr(un.shutil, "which", lambda *a, **k: None)
        root = self._root(tmp_path)
        report = remove_env("orphan", models={}, envs_root=root, conda_bin=None)
        assert report["status"] == "error"
        assert "--force" in report["message"]
        assert (root / "orphan").exists()

    def test_force_rmtree_when_conda_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(un.shutil, "which", lambda *a, **k: None)
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

    def test_default_root_is_project_env_dir_not_user_conda(
            self, tmp_path, monkeypatch):
        """issue #3：删的是 <IFF_DATA_DIR>/envs 下的项目前缀；用户级
        ~/miniconda3/envs 只读遗留。"""
        import inferfabric.config as cfg
        data = tmp_path / "data"
        (data / "envs" / "orphan").mkdir(parents=True)
        monkeypatch.setattr(cfg, "IFF_DATA_DIR", data)
        run = _run_map({})
        report = remove_env("orphan", models={}, run=run,
                            conda_bin="/usr/bin/conda")
        assert report["status"] == "removed"
        assert ["/usr/bin/conda", "env", "remove", "-p",
                str(data / "envs" / "orphan"), "-y"] in run.calls

    def test_user_level_conda_env_is_not_removable(self, tmp_path, monkeypatch):
        import inferfabric.config as cfg
        data = tmp_path / "data"
        data.mkdir()
        legacy = tmp_path / "miniconda3" / "envs" / "orphan"
        legacy.mkdir(parents=True)
        monkeypatch.setattr(cfg, "IFF_DATA_DIR", data)
        run = _run_map({})
        report = remove_env("orphan", models={}, run=run,
                            conda_bin="/usr/bin/conda")
        assert report["status"] == "error"
        assert "read-only legacy" in report["message"]
        assert legacy.exists() and run.calls == []


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
        assert "identity" in report["message"]
        assert not any("rmi" in c for c in run.calls)
        assert led.refs() == {"vllm:v1"}

    def test_dry_run_unverifiable_is_blocked_not_would_remove(self, tmp_path):
        """dry-run 必须是忠实计划：执行会 blocked 的条目不得报 would-remove。"""
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", now=1.0)
        run = _run_map({})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run, dry_run=True)
        assert report["status"] == "blocked"
        assert run.calls == []

    def test_locally_built_image_removable_by_image_id(self, tmp_path):
        """本地构建镜像无 RepoDigests → 台账记 .Id，仍可在复核后删除。"""
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("ninfer:local", digest="sha256:localid", now=1.0)
        run = _run_map({}, ids={"ninfer:local": "sha256:localid"})
        report = remove_image("ninfer:local", models={}, ledger=led, run=run)
        assert report["status"] == "removed"
        assert ["docker", "rmi", "ninfer:local"] in run.calls

    def test_local_image_id_changed_refused(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("ninfer:local", digest="sha256:oldid", now=1.0)
        run = _run_map({}, ids={"ninfer:local": "sha256:newid"})
        report = remove_image("ninfer:local", models={}, ledger=led, run=run)
        assert report["status"] == "blocked"
        assert not any("rmi" in c for c in run.calls)

    def test_digest_changed_refused(self, tmp_path):
        led = ManagedImageLedger(tmp_path / LEDGER_FILENAME)
        led.record("vllm:v1", digest="repo@sha256:old", now=1.0)
        run = _run_map({}, digests={"vllm:v1": "repo@sha256:new"})
        report = remove_image("vllm:v1", models={}, ledger=led, run=run)
        assert report["status"] == "blocked"
        assert "identity changed" in report["message"]
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

class TestCmdRemoveFlow:
    """两段式 CLI：--dry-run 退出 0、无 --yes 退出 1、--yes 执行。"""

    def _setup(self, monkeypatch, tmp_path):
        import inferfabric.cli as cli
        import inferfabric.config as cfg
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "m1.yaml").write_text("name: m1\ntype: vllm\n")
        models = {"m1": _model(served_name="m1")}

        class FakeMgr:
            active_services = {}
            def stop_service(self, name):
                raise AssertionError("dry-run must not stop services")

        monkeypatch.setattr(cfg, "load_models", lambda *a, **k: models)
        monkeypatch.setattr(cfg, "MODELS_DIR", mdir)
        monkeypatch.setattr(cfg, "IFF_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cli, "ModelManager", FakeMgr)
        return cli, mdir

    def test_dry_run_exits_0_without_side_effects(self, monkeypatch, tmp_path):
        cli, mdir = self._setup(monkeypatch, tmp_path)
        cli.cmd_remove(["model", "m1", "--dry-run"])   # 正常返回 ⇒ exit 0
        assert (mdir / "m1.yaml").exists()

    def test_plan_without_yes_exits_1_without_side_effects(self, monkeypatch, tmp_path):
        cli, mdir = self._setup(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as ei:
            cli.cmd_remove(["model", "m1"])
        assert ei.value.code == 1
        assert (mdir / "m1.yaml").exists()

    def test_yes_executes(self, monkeypatch, tmp_path):
        cli, mdir = self._setup(monkeypatch, tmp_path)

        class FakeMgr:
            active_services = {"m1": True}
            def stop_service(self, name):
                self.stopped = name
                return {"status": "stopped"}

        monkeypatch.setattr(cli, "ModelManager", FakeMgr)
        cli.cmd_remove(["model", "m1", "--yes"])
        assert not (mdir / "m1.yaml").exists()


# ── 场景漂移检测（issue #8-②，report-only）─────────────────────

class TestFindScenarioDrift:

    def _models(self, served="fabric-m1"):
        return {"m1": _model(served_name=served)}

    def test_no_drift_when_all_keys_exist(self, tmp_path):
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text("m1:\n  short-ctx: {}\n")
        applied = tmp_path / "active_scenarios.yaml"
        applied.write_text("m1:\n  active_preset: short-ctx\n")
        from inferfabric.uninstaller import find_scenario_drift
        assert find_scenario_drift(self._models(), mdir, applied) == []

    def test_served_name_keyed_sidecar_is_drift(self, tmp_path):
        """loader 严格按 config 名取侧车（models.get()）——served_name 键
        是 loader 会警告并忽略的死条目，必须上报为漂移（评审 B1）。"""
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text(
            "gone-model:\n  short-ctx: {}\nfabric-m1:\n  short-ctx: {}\n")
        from inferfabric.uninstaller import find_scenario_drift
        drift = find_scenario_drift(self._models(), mdir, None)
        assert {"file": "scenarios.yaml", "model": "gone-model"} in drift
        assert {"file": "scenarios.yaml", "model": "fabric-m1"} in drift

    def test_applied_overlay_drift_reported(self, tmp_path):
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        applied = tmp_path / "active_scenarios.yaml"
        applied.write_text("gone-model:\n  active_preset: x\n")
        from inferfabric.uninstaller import find_scenario_drift
        drift = find_scenario_drift(self._models(), mdir, applied)
        assert drift == [{"file": "active_scenarios.yaml", "model": "gone-model"}]

    def test_unreadable_sidecar_flagged_not_crash(self, tmp_path):
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text("pools: [unclosed\n")
        from inferfabric.uninstaller import find_scenario_drift
        drift = find_scenario_drift(self._models(), mdir, None)
        assert drift == [{"file": "scenarios.yaml", "status": "unreadable"}]

    def test_missing_sidecars_no_drift(self, tmp_path):
        from inferfabric.uninstaller import find_scenario_drift
        assert find_scenario_drift(self._models(), tmp_path / "models.d", None) == []

    def test_non_dict_sidecar_shapes_skipped(self, tmp_path):
        """顶层 YAML list / 空文件：loader 同样忽略，静默跳过（评审 N3）。"""
        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text("- a\n- b\n")
        applied = tmp_path / "active_scenarios.yaml"
        applied.write_text("")
        from inferfabric.uninstaller import find_scenario_drift
        assert find_scenario_drift(self._models(), mdir, applied) == []


class TestCmdModelsDriftWarning:

    def test_drift_printed_as_warning(self, monkeypatch, tmp_path, capsys):
        import inferfabric.cli as cli

        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text("gone-model:\n  short-ctx: {}\n")

        class FakeMgr:
            models_dir = mdir
            _models = {"m1": _model(served_name="m1")}
            def list_models(self):
                return [{"name": "m1", "mode": "idle", "type": "vllm",
                         "active": False, "description": "d"}]

        monkeypatch.setattr(cli, "ModelManager", FakeMgr)
        # 密闭：cmd_models 在调用时从 config 读 APPLIED_SCENARIOS_FILE，
        # 必须指向 tmp——否则真机上的 ~/.inferfabric/active_scenarios.yaml
        # 会泄漏进 FakeMgr 的模型集合（评审 B2）。
        monkeypatch.setattr("inferfabric.config.APPLIED_SCENARIOS_FILE",
                            tmp_path / "active_scenarios.yaml")
        cli.cmd_models([])
        out = capsys.readouterr().out
        assert "场景侧车漂移" in out and "gone-model" in out

    def test_both_sidecars_drifting_reported_together(self, monkeypatch, tmp_path, capsys):
        import inferfabric.cli as cli

        mdir = tmp_path / "models.d"
        mdir.mkdir()
        (mdir / "scenarios.yaml").write_text("gone-a:\n  short-ctx: {}\n")
        applied = tmp_path / "active_scenarios.yaml"
        applied.write_text("gone-b:\n  active_preset: x\n")

        class FakeMgr:
            models_dir = mdir
            _models = {"m1": _model(served_name="m1")}
            def list_models(self):
                return [{"name": "m1", "mode": "idle", "type": "vllm",
                         "active": False, "description": "d"}]

        monkeypatch.setattr(cli, "ModelManager", FakeMgr)
        monkeypatch.setattr("inferfabric.config.APPLIED_SCENARIOS_FILE", applied)
        cli.cmd_models([])
        out = capsys.readouterr().out
        assert "gone-a" in out and "gone-b" in out
        assert "scenarios.yaml" in out and "active_scenarios.yaml" in out

    def test_no_drift_no_warning(self, monkeypatch, tmp_path, capsys):
        import inferfabric.cli as cli

        mdir = tmp_path / "models.d"
        mdir.mkdir()

        class FakeMgr:
            models_dir = mdir
            _models = {"m1": _model(served_name="m1")}
            def list_models(self):
                return [{"name": "m1", "mode": "idle", "type": "vllm",
                         "active": False, "description": "d"}]

        monkeypatch.setattr(cli, "ModelManager", FakeMgr)
        monkeypatch.setattr("inferfabric.config.APPLIED_SCENARIOS_FILE",
                            tmp_path / "active_scenarios.yaml")
        cli.cmd_models([])
        assert "漂移" not in capsys.readouterr().out
