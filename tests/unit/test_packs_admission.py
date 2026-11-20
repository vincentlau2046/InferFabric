"""能力包准入评估器 — 阶段 1 Task 2（specs/packs-platform-adaptation §3）。

契约：evaluate_pack(manifest, facts, template, runtime, resources, *,
operation) -> PackDecision(platform, isolation, resources, allowed,
reason_code)。三维度判定：平台 compatible/incompatible/unknown、隔离
verified/degraded/unverified/unavailable、资源 ready/busy/insufficient/unknown。
"""

import pytest
import yaml

from inferfabric.platform.pack_probe import PackHostFacts
from inferfabric.packs import admission
from inferfabric.packs.admission import (
    ResourceEvidence,
    RuntimeEvidence,
    RuntimeTemplate,
    evaluate_pack,
)
from inferfabric.packs.schema import parse_manifest
from tests.unit.tests_fixtures_packs import A64 as _A64


# ── 测试夹具 ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _registry():
    """隔离注册表：每个测试从干净状态开始。"""
    admission.reset_registry()
    yield
    admission.reset_registry()


def _facts(os="linux", arch="x86_64", accelerator="nvidia", cc=(12, 0)):
    return PackHostFacts(os=os, arch=arch, accelerator=accelerator,
                         compute_capability=cc)


def _ninfer_template(**kw):
    defaults = dict(name="ninfer-nvfp4-linux-v1",
                    platforms=("linux-nvidia",), isolation="verified",
                    allow_degraded=False, resource_profile="legacy_default",
                    expected_artifacts=())
    defaults.update(kw)
    return RuntimeTemplate(**defaults)


def _manifest_yaml(*, template="ninfer-nvfp4-linux-v1", params=27,
                   gpu_role="exclusive", mem=24000, degraded=False,
                   platforms=None):
    plat = f"\nplatforms: {platforms}" if platforms is not None else ""
    return f"""
pack_id: llm.qwen3-27b-nvfp4
pack_version: 1.0.0
capability: llm{plat}
model: {{id: qwen3-27b, parameter_count_b: {params}, quantization: nvfp4, license: apache-2.0}}
runtime: {{engine: ninfer, runtime_template: {template}}}
resources: {{download_mb: 30000, disk_mb: 32000, peak_memory_mb: {mem}, gpu_role: {gpu_role}}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/m.nf4", sha256: "{_A64}"}}
execution: {{model_config_template: ninfer_chat_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: {str(degraded).lower()}}}
"""


def _manifest(**kw):
    return parse_manifest(_manifest_yaml(**kw))


def _runtime(isolation="verified"):
    return RuntimeEvidence(isolation=isolation)


def _resources(**kw):
    defaults = dict(disk_ok=True, ram_ok=True, vram_ok=True, gpu_busy=False)
    defaults.update(kw)
    return ResourceEvidence(**defaults)


def _reviewed_profile(identity_sha=None, max_params_b=27, max_mem_mb=26000):
    sha = identity_sha or (_A64,)
    admission.register_reviewed_profile(
        pack_id="llm.qwen3-27b-nvfp4", pack_version="1.0.0",
        template_name="ninfer-nvfp4-linux-v1", artifact_identities=sha,
        max_params_b=max_params_b, max_mem_mb=max_mem_mb)


# ── 平台维度：manifest 不能扩大模板范围 ─────────────────────────

def test_template_cannot_be_widened_by_manifest():
    # 旧 Mac 模板 + Linux 主机 → platform_mismatch；manifest 申明
    # linux-nvidia 也无法把 darwin-arm 模板扩成跨平台
    from inferfabric.packs.admission import get_template
    mac = get_template("llama-cpp-macos-arm64-v1")
    assert mac is not None, "Mac 模板必须预登记（已知平台，绝不变成跨平台）"
    d = evaluate_pack(_manifest(template="llama-cpp-macos-arm64-v1",
                                params=4, gpu_role="shared", mem=4800),
                      _facts(), mac, _runtime("degraded"), _resources(),
                      operation="install")
    assert (d.platform, d.allowed, d.reason_code) == \
        ("incompatible", False, "platform_mismatch")
    # 未知模板 → platform_unverified（unknown，不可新装）
    d2 = evaluate_pack(_manifest(template="totally-unknown-v9"),
                       _facts(), None, _runtime(), _resources(),
                       operation="install")
    assert (d2.platform, d2.allowed, d2.reason_code) == \
        ("unknown", False, "platform_unverified")


# ── 隔离维度：runtime 缺失不降级 ────────────────────────────────

def test_docker_outage_does_not_fall_back():
    d = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                      _runtime("unavailable"), _resources(vram_ok=None),
                      operation="install")
    assert (d.isolation, d.allowed, d.reason_code) == \
        ("unavailable", False, "runtime_unavailable")


def test_native_degraded_requires_template_and_manifest_consent():
    tpl = _ninfer_template(name="llama-cpp-bge-cpu-linux-v1",
                           platforms=("linux-nvidia", "linux-cpu"),
                           isolation="degraded", allow_degraded=True)
    admission.register_template(tpl)
    m = _manifest(template="llama-cpp-bge-cpu-linux-v1", params=1,
                  gpu_role="none", mem=1200, degraded=True)
    facts = _facts(accelerator="none", cc=None)
    ok = evaluate_pack(m, facts, tpl, _runtime("degraded"),
                       _resources(vram_ok=None), operation="install")
    assert (ok.isolation, ok.allowed, ok.reason_code) == \
        ("degraded", True, None)
    # manifest 不同意降级 → 拒绝
    m2 = _manifest(template="llama-cpp-bge-cpu-linux-v1", params=1,
                   gpu_role="none", mem=1200, degraded=False)
    no = evaluate_pack(m2, facts, tpl, _runtime("degraded"),
                       _resources(vram_ok=None), operation="install")
    assert (no.allowed, no.reason_code) == \
        (False, "isolation_degraded_not_allowed")


# ── 资源维度：27B 例外必须精确匹配审核登记 ─────────────────────

def test_ninfer_27b_exception_requires_exact_reviewed_identity():
    # 未登记 27B：legacy 4B 门槛 → resource_insufficient
    d = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                      _runtime(), _resources(), operation="install")
    assert (d.resources, d.allowed, d.reason_code) == \
        ("insufficient", False, "resource_insufficient")

    # 已登记审核 profile + VRAM 遥测缺失 → resource_unknown（不盲放行）
    _reviewed_profile()
    d2 = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                       _runtime(), _resources(vram_ok=None),
                       operation="install")
    assert (d2.resources, d2.allowed, d2.reason_code) == \
        ("unknown", False, "resource_unknown")

    # 版本不匹配的审核 profile 不生效
    admission.reset_registry()
    admission.register_reviewed_profile(
        pack_id="llm.qwen3-27b-nvfp4", pack_version="9.9.9",
        template_name="ninfer-nvfp4-linux-v1", artifact_identities=(_A64,),
        max_params_b=27, max_mem_mb=26000)
    d3 = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                       _runtime(), _resources(), operation="install")
    assert (d3.resources, d3.reason_code) == \
        ("insufficient", "resource_insufficient")


def test_reviewed_profile_full_admission_path():
    _reviewed_profile()
    d = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                      _runtime(), _resources(), operation="start")
    assert (d.platform, d.isolation, d.resources) == \
        ("compatible", "verified", "ready")
    assert d.allowed is True and d.reason_code is None


def test_gpu_busy_returns_resource_busy():
    _reviewed_profile()
    d = evaluate_pack(_manifest(), _facts(), _ninfer_template(),
                      _runtime(), _resources(gpu_busy=True),
                      operation="start")
    assert (d.resources, d.reason_code) == ("busy", "resource_busy")


# ── 目录/执行器同因序列化 + 目录零执行 ──────────────────────────

def test_catalog_and_executor_serialize_same_reason():
    _reviewed_profile()
    m, facts = _manifest(), _facts()
    runtime, resources = _runtime(), _resources(gpu_busy=True)
    as_catalog = evaluate_pack(m, facts, _ninfer_template(), runtime,
                               resources, operation="install")
    as_executor = evaluate_pack(m, facts, _ninfer_template(), runtime,
                                resources, operation="start")
    assert as_catalog.to_dict() == as_executor.to_dict()
    assert as_catalog.reason_code == "resource_busy"


def test_catalog_preview_never_executes(tmp_path):
    """目录渲染只吃注入事实：fetcher 被调用即失败（不下载、不启动、不探测）。"""
    from inferfabric.proxy.pack_handlers import PackManager

    class _Boom:
        def fetch(self, *a, **kw):
            raise AssertionError("catalog preview must not download")

    cat_path = tmp_path / "catalog.yaml"
    cat_path.write_text(yaml.safe_dump({
        "packs": [{"pack_id": "llm.qwen3-27b-nvfp4",
                   "display_name": "NInfer 27B", "summary": "s",
                   "tags": ["llm"], "risk_level": "verified",
                   "builtin": False,
                   "manifest": yaml.safe_load(_manifest_yaml())}]}),
        encoding="utf-8")
    admission.register_template(_ninfer_template())
    pm = PackManager(tmp_path, tmp_path / "models.d", catalog_path=cat_path,
                     fetcher=_Boom())
    payload = pm.catalog_payload(
        facts=_facts(), runtime=_runtime(), resources=_resources())
    entry = payload["packs"][0]
    # 附加式决策：不替换既有元数据
    assert entry["decision"]["allowed"] is False
    assert entry["decision"]["reason_code"] == "resource_insufficient"
    assert entry["display_name"] == "NInfer 27B"
    assert entry["manifest"]["pack_id"] == "llm.qwen3-27b-nvfp4"
