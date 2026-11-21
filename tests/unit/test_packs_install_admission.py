"""安装期三维准入（真机首发现：start_install 不做平台判定）。

契约（specs/packs-platform-adaptation §3 + §「其他平台可查看、不可执行」）：
- start_install 必须吃真实主机事实（probe_pack_host 可注入）：平台维度
  非 compatible（incompatible / unknown 含未知模板）→ 422 + 决策载荷，
  不落 store、不开下载线程。
- 资源维度中可实测且无歧义的部分（磁盘余量、可用内存）不足 → 422
  resource_insufficient。
- 隔离维（#23 联合裁决 A1）：unavailable / 未双同意 degraded /
  unverified → 422；证据按平台分派（darwin=PK0 探针三态，linux=degraded
  兜底常真）。
- VRAM 遥测缺失（resource_unknown）不阻塞安装（#23 联合裁决 B1′：
  VRAM 仅启动闸）——安装不启动进程、不占 GPU，GPU busy 不构成安装拒绝。
"""

import hashlib
import time
from pathlib import Path

import pytest
import yaml

from inferfabric.platform.pack_probe import PackHostFacts
from inferfabric.proxy import pack_handlers


class InstantFetcher:
    def __init__(self, files):
        self.files = files

    def fetch(self, artifact, dest):
        dest.write_bytes(self.files[artifact.id])


@pytest.fixture()
def env(tmp_path):
    from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML
    from inferfabric.packs.schema import parse_manifest
    m = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    m["resources"]["download_mb"] = 1
    m["resources"]["disk_mb"] = 1
    files = {"model": b"M" * 128, "engine": b"E" * 64}
    arts = []
    for i, aid in enumerate(files):
        a = parse_manifest(MINIMAL_MANIFEST_YAML).artifacts[i]
        arts.append({"id": aid, "kind": a.kind, "source": f"https://x/{aid}",
                     "sha256": hashlib.sha256(files[aid]).hexdigest()})
    m["artifacts"] = arts
    from inferfabric.packs.catalog import Catalog
    manifest = parse_manifest(m)
    cat_path = tmp_path / "catalog.yaml"
    cat_path.write_text(yaml.safe_dump({"packs": [{
        "pack_id": manifest.pack_id, "display_name": "T", "summary": "s",
        "tags": [], "risk_level": "verified", "builtin": False,
        "manifest": m}]}, sort_keys=False))
    return {"tmp": tmp_path, "cat_path": cat_path, "files": files,
            "pack_id": manifest.pack_id}


def _mgr(env, *, facts, disk_free_mb=None, sandbox_probe=None):
    return pack_handlers.PackManager(
        root=env["tmp"] / "packs", models_d=env["tmp"] / "models.d",
        catalog_path=env["cat_path"], fetcher=InstantFetcher(env["files"]),
        reload_gateway=lambda: None,
        prepare_runtime=lambda pd, m: None,
        run_healthcheck=lambda pd, m: None,
        memory_available_mb=lambda: 65536,
        pack_host_facts=facts,
        sandbox_probe=sandbox_probe,
        disk_free_mb=disk_free_mb or (lambda: 1 << 40))


DARWIN_ARM = lambda: PackHostFacts(os="darwin", arch="arm64",
                                   accelerator="none", compute_capability=None)
LINUX_NVIDIA = lambda: PackHostFacts(os="linux", arch="x86_64",
                                     accelerator="nvidia",
                                     compute_capability=(12, 0))


class TestInstallAdmission:
    def test_install_wrong_platform_rejected(self, env):
        """darwin-arm 模板包在 linux-nvidia 主机 → 422，不落盘不开线程。"""
        code, payload = _mgr(env, facts=LINUX_NVIDIA).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == "platform_mismatch"
        assert payload["decision"]["platform"] == "incompatible"

    def test_install_unknown_template_rejected(self, env, tmp_path):
        """未知模板 = platform unknown（platform_unverified）→ 422。"""
        import yaml as _y
        cat = _y.safe_load(env["cat_path"].read_text())
        cat["packs"][0]["manifest"]["runtime"] = {
            "engine": "llama_cpp", "runtime_template": "nope-v9"}
        env["cat_path"].write_text(_y.safe_dump(cat, sort_keys=False))
        code, payload = _mgr(env, facts=DARWIN_ARM).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == "platform_unverified"

    def test_install_disk_insufficient_rejected(self, env):
        code, payload = _mgr(env, facts=DARWIN_ARM,
                             disk_free_mb=lambda: 0).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == "resource_insufficient"

    def test_install_matching_platform_proceeds(self, env):
        """平台匹配 + 资源充足 → 202 正常事务（degraded 放行不阻塞）。"""
        from inferfabric.packs.sandbox import SandboxMode, SandboxProbe
        probe = lambda: SandboxProbe(SandboxMode.DEGRADED, "test probe")
        mgr = _mgr(env, facts=DARWIN_ARM, sandbox_probe=probe)
        code, payload = mgr.start_install(env["pack_id"])
        assert code == 202, payload
        deadline = time.time() + 5
        rec = None
        while time.time() < deadline:
            rec = mgr.store.get(env["pack_id"])
            if rec and rec.status == "installed":
                break
            time.sleep(0.05)
        assert rec and rec.status == "installed"

    def test_install_isolation_unavailable_rejected(self, env):
        """A1 分级收紧（#23 联合裁决）：darwin 探测 unavailable → 422
        runtime_unavailable，不进下载事务。"""
        from inferfabric.packs.sandbox import SandboxMode, SandboxProbe
        probe = lambda: SandboxProbe(SandboxMode.UNAVAILABLE, "test probe")
        code, payload = _mgr(env, facts=DARWIN_ARM,
                             sandbox_probe=probe).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == "runtime_unavailable"

    def test_install_linux_isolation_degraded_proceeds(self, env,
                                                       monkeypatch):
        """Linux 侧隔离证据 = NoopProvider 进程组兜底恒可用 → degraded
        （不冒充 verified）；模板/manifest 双同意 → 安装照常。

        installer 的 catalog_probe 也一并密闭化（评审 I1）：记录上的
        sandbox_degraded 标记来自 installer 探针，macOS 真探针会返回
        FULL 导致本断言在 Mac 机器翻转——注入 DEGRADED 后双端同断言。"""
        from inferfabric.packs import admission
        from inferfabric.packs.sandbox import SandboxMode, SandboxProbe
        import yaml as _y
        admission.register_template(admission.RuntimeTemplate(
            name="llama-cpp-linux-v1", platforms=("linux-nvidia",),
            isolation="degraded", allow_degraded=True,
            resource_profile="legacy_default", expected_artifacts=()))
        try:
            cat = _y.safe_load(env["cat_path"].read_text())
            cat["packs"][0]["manifest"]["runtime"] = {
                "engine": "llama_cpp", "runtime_template":
                    "llama-cpp-linux-v1"}
            env["cat_path"].write_text(_y.safe_dump(cat, sort_keys=False))
            probe = lambda: SandboxProbe(SandboxMode.DEGRADED, "sealed")
            monkeypatch.setattr(pack_handlers, "probe_sandbox", probe)
            mgr = _mgr(env, facts=LINUX_NVIDIA, sandbox_probe=probe)
            code, payload = mgr.start_install(env["pack_id"])
            assert code == 202, payload
            deadline = time.time() + 5
            rec = None
            while time.time() < deadline:
                rec = mgr.store.get(env["pack_id"])
                if rec and rec.status == "installed":
                    break
                time.sleep(0.05)
            assert rec and rec.status == "installed"
            assert rec.sandbox_degraded is True, "degraded 放行必须显式标记"
        finally:
            admission.reset_registry()

    def test_isolation_evidence_bogus_probe_mode_fails_closed(self, env):
        """注入探针返回未知 mode.value → fail closed（unverified 拒绝），
        不得 KeyError 炸 500（评审 M6）。"""
        from types import SimpleNamespace
        probe = lambda: SimpleNamespace(mode=SimpleNamespace(value="warp9"))
        code, payload = _mgr(env, facts=DARWIN_ARM,
                             sandbox_probe=probe).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == "isolation_unverified"

    def test_install_degraded_not_allowed_rejected(self, env):
        """degraded 需模板与 manifest 双同意：manifest 关闭降级 → 422
        isolation_degraded_not_allowed。"""
        import yaml as _y
        cat = _y.safe_load(env["cat_path"].read_text())
        cat["packs"][0]["manifest"]["sandbox"][
            "degraded_install_allowed"] = False
        env["cat_path"].write_text(_y.safe_dump(cat, sort_keys=False))
        from inferfabric.packs.sandbox import SandboxMode, SandboxProbe
        probe = lambda: SandboxProbe(SandboxMode.DEGRADED, "test probe")
        code, payload = _mgr(env, facts=DARWIN_ARM,
                             sandbox_probe=probe).start_install(
            env["pack_id"])
        assert code == 422, payload
        assert payload["decision"]["reason_code"] == \
            "isolation_degraded_not_allowed"

    def test_default_real_probe_invoked(self, env, monkeypatch):
        """未注入 facts 时走真实 probe_pack_host（密闭化为模块级可打桩）。"""
        called = []
        def fake_probe(**kwargs):
            called.append(kwargs)
            return PackHostFacts(os="linux", arch="x86_64",
                                 accelerator="nvidia",
                                 compute_capability=(12, 0))
        monkeypatch.setattr(pack_handlers, "probe_pack_host", fake_probe)
        code, payload = pack_handlers.PackManager(
            root=env["tmp"] / "packs", models_d=env["tmp"] / "models.d",
            catalog_path=env["cat_path"], fetcher=InstantFetcher(env["files"]),
            reload_gateway=lambda: None,
            prepare_runtime=lambda pd, m: None,
            run_healthcheck=lambda pd, m: None,
            memory_available_mb=lambda: 65536).start_install(env["pack_id"])
        assert called, "默认路径必须调用真实主机事实探测"
        assert code == 422
        assert payload["decision"]["reason_code"] == "platform_mismatch"
