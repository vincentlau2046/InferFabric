"""升级原子切换与旧版回退窗口 — PK2（Task 11）。

契约（计划 Interfaces）：
- PackInstaller.upgrade(entry) -> InstallResult：新版本目录走完整事务
  （含健康检查），全部成功才切换；旧版本保留 UPGRADE_KEEP_PREVIOUS=1 个
  回退窗口，切成功后清理更早版本；任一阶段失败 → 旧版不动、staging 清理。
"""

import hashlib
from pathlib import Path

import pytest

from inferfabric.packs.catalog import CatalogEntry
from inferfabric.packs.installer import PackInstaller, UPGRADE_KEEP_PREVIOUS
from inferfabric.packs.policy import PolicyStore
from inferfabric.packs.sandbox import NoopProvider, probe_sandbox
from inferfabric.packs.schema import parse_manifest
from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML

PID = "llm.qwen3-4b-instruct-2507"

FILES = {"model": b"M" * 128, "engine": b"E" * 64}


def _entry(version="1.0.0"):
    d = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    d["pack_version"] = version
    arts = []
    for i, aid in enumerate(FILES):
        a = parse_manifest(MINIMAL_MANIFEST_YAML).artifacts[i]
        arts.append({"id": aid, "kind": a.kind, "source": f"https://x/{aid}",
                     "sha256": hashlib.sha256(FILES[aid]).hexdigest()})
    d["artifacts"] = arts
    d["resources"]["download_mb"] = 1
    d["resources"]["disk_mb"] = 1
    return CatalogEntry(manifest=parse_manifest(d), display_name="T",
                        summary="s", tags=(), risk_level="verified",
                        builtin=False)


def make_installer(tmp_path, **hooks):
    return PackInstaller(
        root=tmp_path / "packs",
        fetcher=type("F", (), {"fetch": staticmethod(
            lambda a, dest: dest.write_bytes(FILES[a.id]))})(),
        policy=PolicyStore(tmp_path / "policy.json"),
        sandbox_provider=NoopProvider(),
        catalog_probe=lambda: probe_sandbox(mac_version=(27, 2),
                                            has_sbpl_api=True),
        **hooks)


def test_upgrade_is_install_when_no_previous(tmp_path):
    inst = make_installer(tmp_path)
    res = inst.upgrade(_entry("1.0.0"))
    assert res.ok and res.stage == "installed"
    assert (tmp_path / "packs" / PID / "1.0.0").exists()


def test_upgrade_keeps_old_until_new_healthy(tmp_path):
    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("healthcheck timeout")

    inst = make_installer(tmp_path, run_healthcheck=Boom())
    assert inst.upgrade(_entry("1.0.0")).ok or True   # 首装也失败无妨
    # 先成功装 1.0.0
    inst_ok = make_installer(tmp_path)
    assert inst_ok.upgrade(_entry("1.0.0")).ok
    old_files = list((tmp_path / "packs" / PID).glob("*"))
    # 升级 1.1.0 健康检查失败 → 旧版 1.0.0 完好
    res = inst.upgrade(_entry("1.1.0"))
    assert not res.ok and res.stage == "isolated_healthcheck"
    assert [d.name for d in (tmp_path / "packs" / PID).glob("*")] == ["1.0.0"]
    assert (tmp_path / "packs" / PID / "1.0.0" / "models" / "model").exists()
    assert not (tmp_path / "packs" / "_staging" / PID).exists()


def test_upgrade_atomic_switch_and_cleanup(tmp_path):
    inst = make_installer(tmp_path)
    assert inst.upgrade(_entry("1.0.0")).ok
    assert inst.upgrade(_entry("1.1.0")).ok
    versions = sorted(d.name for d in (tmp_path / "packs" / PID).glob("*"))
    assert versions == ["1.0.0", "1.1.0"]   # 上一版本保留（回退窗口）
    assert inst.upgrade(_entry("1.2.0")).ok
    versions = sorted(d.name for d in (tmp_path / "packs" / PID).glob("*"))
    # 仅保留上一版本（UPGRADE_KEEP_PREVIOUS=1）+ 新版本
    assert versions == ["1.1.0", "1.2.0"]
    assert UPGRADE_KEEP_PREVIOUS == 1


def test_upgrade_failed_leaves_no_staging(tmp_path):
    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("download boom")

    inst = make_installer(tmp_path)
    assert inst.upgrade(_entry("1.0.0")).ok
    # fetcher 损坏 → 下载失败
    inst_bad = make_installer(tmp_path)
    inst_bad._fetcher = type("F", (), {"fetch": staticmethod(
        lambda a, dest: (_ for _ in ()).throw(RuntimeError("net down")))})()
    res = inst_bad.upgrade(_entry("1.1.0"))
    assert not res.ok and res.stage == "downloading"
    assert sorted(d.name for d in (tmp_path / "packs" / PID).glob("*")) == \
        ["1.0.0"]
    assert not (tmp_path / "packs" / "_staging" / PID).exists()
