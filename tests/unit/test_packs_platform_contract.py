"""能力包平台契约 — 阶段 1 Task 1（host facts + platforms 解析）。

契约（specs/packs-platform-adaptation §3 + 计划 Task 1）：
- probe_pack_host() -> PackHostFacts(os, arch, accelerator, compute_capability)；
  加速器探测失败/超时 → 'unknown'（绝不当 'none'），命令缺失 → 'none'。
- PackManifest.platforms 可选申明，只收窄审核过的模板范围；缺省 None
  （按模板推断）；'any' 通配被禁止（spec §3：禁止新明确通配）。
"""

import pytest

from inferfabric.packs.schema import ManifestError, parse_manifest
from tests.unit.tests_fixtures_packs import A64 as _A64
from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML as MINIMAL_YAML


# ── platforms 解析 ──────────────────────────────────────────────

def test_absent_platforms_remain_unspecified():
    m = parse_manifest(MINIMAL_YAML)
    assert m.platforms is None
    # roundtrip：未申明 platforms 的旧 manifest 不新增该键
    assert "platforms" not in m.to_dict()
    m2 = parse_manifest(m.to_dict_yaml())
    assert m2.platforms is None


def test_invalid_platforms_rejected():
    def _with_platforms(value) -> str:
        return MINIMAL_YAML.replace("pack_version: 1.0.0",
                                    f"pack_version: 1.0.0\nplatforms: {value}")

    for bad, value in [
        ("empty", "[]"),
        ("duplicate", '["linux-nvidia", "linux-nvidia"]'),
        ("unknown", '["linux-gpu"]'),
        ("explicit any", '["any"]'),
        ("any combined", '["any", "linux-cpu"]'),
        ("non-string", "[1]"),
    ]:
        with pytest.raises(ManifestError) as ei:
            parse_manifest(_with_platforms(value))
        assert ei.value.field == "platforms", bad

    # 合法值通过且保持申明顺序
    m = parse_manifest(_with_platforms('["linux-nvidia", "linux-cpu"]'))
    assert m.platforms == ("linux-nvidia", "linux-cpu")
    assert m.to_dict()["platforms"] == ["linux-nvidia", "linux-cpu"]


# ── host facts 探测 ─────────────────────────────────────────────

def test_intel_mac_not_apple_silicon():
    from inferfabric.platform.pack_probe import PackHostFacts, platform_key, probe_pack_host

    facts = probe_pack_host(os_name="darwin", arch="x86_64")
    assert isinstance(facts, PackHostFacts)
    assert facts.os == "darwin" and facts.arch == "x86_64"
    # x86_64 Mac 不是 Apple Silicon —— 平台键绝不落进 darwin-arm
    assert platform_key(facts) != "darwin-arm"
    assert platform_key(facts) == "darwin-x86"


def test_arch_normalization():
    from inferfabric.platform.pack_probe import probe_pack_host

    assert probe_pack_host(os_name="linux", arch="aarch64").arch == "arm64"
    assert probe_pack_host(os_name="linux", arch="ARM64").arch == "arm64"
    assert probe_pack_host(os_name="linux", arch="AMD64").arch == "x86_64"


def test_nvidia_probe_success_reports_compute_capability():
    from inferfabric.platform.pack_probe import probe_pack_host

    def run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "12.0\n"
        return R()

    facts = probe_pack_host(os_name="linux", arch="x86_64", run=run)
    assert facts.accelerator == "nvidia"
    assert facts.compute_capability == (12, 0)


def test_nvidia_probe_timeout_is_unknown():
    from inferfabric.platform.pack_probe import probe_pack_host

    def run(cmd, **kw):
        raise TimeoutError("nvidia-smi timed out")

    facts = probe_pack_host(os_name="linux", arch="x86_64", run=run)
    # 探测失败 ≠ CPU 正常态：必须是 unknown，不是 none
    assert facts.accelerator == "unknown"
    assert facts.compute_capability is None


def test_nvidia_probe_missing_binary_is_none():
    from inferfabric.platform.pack_probe import probe_pack_host

    def run(cmd, **kw):
        raise FileNotFoundError("nvidia-smi")

    facts = probe_pack_host(os_name="linux", arch="x86_64", run=run)
    assert facts.accelerator == "none"
    assert facts.compute_capability is None


def test_nvidia_probe_garbage_output_is_unknown():
    from inferfabric.platform.pack_probe import probe_pack_host

    def run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "N/A\n"
        return R()

    facts = probe_pack_host(os_name="linux", arch="x86_64", run=run)
    assert facts.accelerator == "unknown"


def test_darwin_skips_nvidia_probe():
    from inferfabric.platform.pack_probe import probe_pack_host

    def run(cmd, **kw):  # 不应被调用
        raise AssertionError("darwin must not probe nvidia-smi")

    facts = probe_pack_host(os_name="darwin", arch="arm64", run=run)
    assert facts.accelerator == "none"
    assert facts.compute_capability is None
