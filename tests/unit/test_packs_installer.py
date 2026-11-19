"""安装事务 staging/校验/回滚 — PK1（Task 5）。

契约（计划 Interfaces）：
- PackInstaller(root, fetcher, policy, sandbox_provider, **hooks).install(entry)
  -> InstallResult(ok, stage, error, pack_dir, sandbox_degraded)。
- 事务规则（spec §7.1）：preflight（磁盘/参数门禁/沙箱准入）→ 下载落
  _staging/<pack_id>/ → 逐 artifact sha256（TOCTOU 安全：staging 校验后才
  移动）→ prepare_runtime → write_model_config → run_healthcheck →
  reload_gateway → commit。任一阶段失败删 staging，不动已安装包，无半成品
  models.d。崩溃残留 staging 下次安装先清理。
- INSTALL_STAGES 常量供 store/API 引用。
"""

import hashlib
import shutil
from pathlib import Path

import pytest

import inferfabric.packs.installer as installer_mod
from inferfabric.packs.catalog import CatalogEntry
from inferfabric.packs.installer import INSTALL_STAGES, PackInstaller
from inferfabric.packs.policy import PackPolicy, PolicyStore
from inferfabric.packs.sandbox import NoopProvider, SandboxMode, probe_sandbox
from inferfabric.packs.schema import parse_manifest
from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML

ROOT_KEY = "llm.qwen3-4b-instruct-2507"


def _manifest_with_artifacts(artifacts):
    d = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    d["artifacts"] = [{"id": a.id, "kind": a.kind, "source": a.source,
                       "sha256": a.sha256} for a in artifacts]
    return parse_manifest(d)


def _fixture_entry(tmp_path, corrupt=False):
    model_bytes = b"FAKE-GGUF-CONTENT"
    engine_bytes = b"FAKE-ENGINE-BINARY"
    if corrupt:
        engine_bytes += b"-tampered"
    files = {"model": model_bytes, "engine": engine_bytes}
    arts = []
    for aid, data in files.items():
        art = parse_manifest(MINIMAL_MANIFEST_YAML).artifacts[0]
        arts.append(type(art)(id=aid, kind="model" if aid == "model" else "runtime",
                              source=f"https://example.com/{aid}",
                              sha256=hashlib.sha256(
                                  data if not corrupt or aid == "model"
                                  else engine_bytes).hexdigest()))
    # corrupt 场景：fetcher 会写出被篡改内容，manifest hash 仍是原始值
    m = _manifest_with_artifacts(arts)
    entry = CatalogEntry(manifest=m, display_name="T", summary="s",
                         tags=(), risk_level="verified", builtin=False)
    return entry, files


class LocalFetcher:
    """测试 fetcher：把 fixture 内容写到 dest（模拟下载 .part→rename）。"""

    def __init__(self, files, corrupt_ids=()):
        self.files = files
        self.corrupt_ids = corrupt_ids

    def fetch(self, artifact, dest: Path) -> None:
        data = self.files[artifact.id]
        if artifact.id in self.corrupt_ids:
            data += b"-tampered"
        dest.write_bytes(data)


class BrokenStep:
    def __init__(self, stage_name):
        self.stage_name = stage_name

    def __call__(self, *a, **k):
        raise RuntimeError(f"boom at {self.stage_name}")


def make_installer(tmp_path, files=None, corrupt_ids=(), **hooks):
    entry, _files = _fixture_entry(tmp_path)
    fetcher = LocalFetcher(files if files is not None else _files,
                           corrupt_ids=corrupt_ids)
    inst = PackInstaller(
        root=tmp_path / "packs", fetcher=fetcher,
        policy=PolicyStore(tmp_path / "policy.json"),
        sandbox_provider=NoopProvider(),
        catalog_probe=lambda: probe_sandbox(mac_version=(27, 2),
                                            has_sbpl_api=True),
        **hooks)
    return inst, entry


def test_stage_constants_match_spec():
    assert INSTALL_STAGES == ("preflight", "staging", "downloading",
                              "verifying", "preparing_runtime",
                              "writing_config", "isolated_healthcheck",
                              "gateway_reload", "installed")


def test_happy_path_commits(tmp_path):
    inst, entry = make_installer(tmp_path)
    res = inst.install(entry)
    assert res.ok and res.stage == "installed", res.error
    assert res.pack_dir is not None
    assert (res.pack_dir / "models" / "model").exists()
    assert not (tmp_path / "packs" / "_staging" / ROOT_KEY).exists()


def test_hash_mismatch_blocks_commit(tmp_path):
    inst, entry = make_installer(tmp_path, corrupt_ids=("engine",))
    res = inst.install(entry)
    assert not res.ok and res.stage == "verifying"
    assert "artifact_sha256_mismatch:engine" in res.error
    assert not (tmp_path / "packs" / ROOT_KEY).exists()
    assert not (tmp_path / "packs" / "_staging" / ROOT_KEY).exists()


def test_preflight_rejects_param_gate(tmp_path):
    inst, entry = make_installer(tmp_path)
    d = entry.manifest.to_dict()
    d["model"]["parameter_count_b"] = 8
    big = CatalogEntry(manifest=parse_manifest(d), display_name="T",
                       summary="s", tags=(), risk_level="verified",
                       builtin=False)
    res = inst.install(big)
    assert not res.ok and res.stage == "preflight"
    assert "parameter_count_b" in res.error


def test_preflight_rejects_low_disk(tmp_path, monkeypatch):
    from collections import namedtuple
    DU = namedtuple("usage", "total used free")
    inst, entry = make_installer(tmp_path)
    monkeypatch.setattr(installer_mod.shutil, "disk_usage",
                        lambda p: DU(0, 0, 1024 * 1024))  # free = 1MB
    res = inst.install(entry)
    assert not res.ok and res.stage == "preflight"
    assert "disk" in res.error


def test_sandbox_gate_rejects_unverified_when_degraded(tmp_path):
    # degraded_install_allowed=False + DEGRADED 探测 → 拒绝
    inst, entry = make_installer(tmp_path)
    inst._catalog_probe = lambda: probe_sandbox(mac_version=(27, 2),
                                                has_sbpl_api=False)
    d = entry.manifest.to_dict()
    d["sandbox"]["degraded_install_allowed"] = False
    strict = CatalogEntry(manifest=parse_manifest(d), display_name="T",
                          summary="s", tags=(), risk_level="verified",
                          builtin=False)
    res = inst.install(strict)
    assert not res.ok and res.stage == "preflight"
    assert "sandbox" in res.error


def test_sandbox_degraded_install_marks_flag(tmp_path):
    # degraded_install_allowed=True + DEGRADED → 可安装，显式标记（spec §8.3）
    inst, entry = make_installer(tmp_path)
    inst._catalog_probe = lambda: probe_sandbox(mac_version=(27, 2),
                                                has_sbpl_api=False)
    res = inst.install(entry)
    assert res.ok and res.sandbox_degraded is True


def test_runtime_failure_rolls_back_no_residue(tmp_path):
    inst, entry = make_installer(tmp_path,
                                 prepare_runtime=BrokenStep("preparing_runtime"))
    res = inst.install(entry)
    assert not res.ok and res.stage == "preparing_runtime"
    assert "boom" in res.error
    assert not (tmp_path / "packs" / ROOT_KEY).exists()
    assert not (tmp_path / "packs" / "_staging" / ROOT_KEY).exists()


def test_healthcheck_failure_rolls_back(tmp_path):
    inst, entry = make_installer(tmp_path,
                                 run_healthcheck=BrokenStep("isolated_healthcheck"))
    res = inst.install(entry)
    assert not res.ok and res.stage == "isolated_healthcheck"
    assert not (tmp_path / "packs" / ROOT_KEY).exists()


def test_reload_failure_not_installed(tmp_path):
    inst, entry = make_installer(tmp_path, reload_gateway=BrokenStep("gateway_reload"))
    res = inst.install(entry)
    assert not res.ok and res.stage == "gateway_reload"
    # 包文件已就位但状态未提交（store 由 Task 6 记录）；此处断言不冒充 installed
    assert res.ok is False


def test_stale_staging_cleaned_on_next_install(tmp_path):
    inst, entry = make_installer(tmp_path)
    stale = tmp_path / "packs" / "_staging" / ROOT_KEY
    stale.mkdir(parents=True)
    (stale / "junk.part").write_bytes(b"x")
    res = inst.install(entry)
    assert res.ok
    assert not (stale / "junk.part").exists()


def test_failed_retry_leaves_existing_pack_dir_untouched(tmp_path):
    # 终审 F1：已存在的 pack_dir（已装/unknown_origin）在下载失败时不得被删
    inst, entry = make_installer(tmp_path)
    res = inst.install(entry)
    assert res.ok
    sentinel = res.pack_dir / "models" / "model"

    class ExplodingFetcher:
        def fetch(self, artifact, dest):
            raise OSError("network down")

    inst._fetcher = ExplodingFetcher()
    res2 = inst.install(entry)
    assert not res2.ok and res2.stage == "downloading"
    assert sentinel.exists()


def test_config_write_rolled_back_on_healthcheck_failure(tmp_path):
    # 终审 F2：writing_config 之后失败 → 本次写入的受控 YAML 一并回滚
    written = []

    def write_cfg(pack_dir, manifest):
        p = tmp_path / "models.d" / "managed.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("managed_by_pack: x")
        written.append(p)
        return [p]

    inst, entry = make_installer(
        tmp_path, write_model_config=write_cfg,
        run_healthcheck=BrokenStep("isolated_healthcheck"))
    res = inst.install(entry)
    assert not res.ok and res.stage == "isolated_healthcheck"
    assert written and not written[0].exists()


def test_preflight_memory_gate_rejects(tmp_path):
    # 终审 F3：可用内存 - 系统保留 < peak_memory_mb → preflight 拒绝
    # fixture：peak_memory_mb=4800，默认保留 8192；注入可用 5000 → 预算负
    inst, entry = make_installer(tmp_path, memory_available_mb=lambda: 5000)
    res = inst.install(entry)
    assert not res.ok and res.stage == "preflight"
    assert "peak_memory_mb" in res.error


def test_preflight_memory_gate_passes_with_headroom(tmp_path):
    inst, entry = make_installer(tmp_path, memory_available_mb=lambda: 20000)
    res = inst.install(entry)
    assert res.ok, res.error


def test_builtin_entry_skips_download(tmp_path):
    # 系统内置（spec §3.5）：无 artifact，跳过下载/校验/prepare_runtime
    d = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    d["pack_id"] = "ocr.macos-vision"
    d["capability"] = "ocr"
    d["artifacts"] = []
    entry = CatalogEntry(manifest=parse_manifest(d), display_name="V",
                         summary="s", tags=(), risk_level="verified",
                         builtin=True)
    fetcher_calls = []

    class SpyFetcher:
        def fetch(self, artifact, dest):
            fetcher_calls.append(artifact.id)

    inst = PackInstaller(root=tmp_path / "packs", fetcher=SpyFetcher(),
                         policy=PolicyStore(tmp_path / "policy.json"),
                         sandbox_provider=NoopProvider(),
                         catalog_probe=lambda: probe_sandbox(
                             mac_version=(27, 2), has_sbpl_api=True))
    res = inst.install(entry)
    assert res.ok and res.stage == "installed"
    assert fetcher_calls == []


def test_preflight_rejects_unimplemented_template(tmp_path):
    # 终审 deferred #5：未实现模板 preflight 即拒（早于下载，分阶段错误）
    inst, entry = make_installer(tmp_path)
    d = entry.manifest.to_dict()
    d["execution"]["model_config_template"] = "asr_server_v1"
    bad = CatalogEntry(manifest=parse_manifest(d), display_name="T",
                       summary="s", tags=(), risk_level="verified",
                       builtin=False)

    class NoFetch:
        def fetch(self, artifact, dest):
            raise AssertionError("must not download before preflight")

    inst._fetcher = NoFetch()
    res = inst.install(bad)
    assert not res.ok and res.stage == "preflight"
    assert "asr_server_v1" in res.error


def test_preflight_delegates_param_gate(tmp_path, monkeypatch):
    # 终审 deferred #7：预检参数门禁复用 policy.check_param_gate（单一事实源）
    from inferfabric.packs import policy as policy_mod
    inst, entry = make_installer(tmp_path)
    monkeypatch.setattr(policy_mod, "check_param_gate",
                        lambda m, p: "injected_violation")
    res = inst.install(entry)
    assert not res.ok and res.stage == "preflight"
    assert "injected_violation" in res.error


def test_install_reports_stage_progress(tmp_path):
    # PK4 生产验收发现：App 阶段文案流转依赖安装进度上报 — installer 需
    # on_stage(pack_id, stage) 回调，在各阶段起点触发
    stages = []
    inst, entry = make_installer(
        tmp_path, on_stage=lambda pid, st: stages.append(st))
    assert inst._report is not None
    res = inst.install(entry)
    assert res.ok and res.stage == "installed", res.error
    assert stages == ["preflight", "downloading", "verifying",
                      "preparing_runtime", "writing_config",
                      "isolated_healthcheck", "gateway_reload"], stages
