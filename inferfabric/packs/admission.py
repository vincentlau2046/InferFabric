"""能力包准入评估器 — 三维度契约（specs/packs-platform-adaptation §3）。

纯函数 + 服务端注册表，无 I/O：目录渲染与执行闸共用同一 evaluate_pack()，
目录只吃已测事实（RuntimeEvidence / ResourceEvidence 携带实测态，不是
客户端声明）。判定规则：

- 平台  compatible / incompatible / unknown —— 模板求交；manifest.platforms
  只能收窄模板范围；未知模板 = unknown（platform_unverified，不可新装）。
- 隔离  verified / degraded / unverified / unavailable —— 只认实测证据；
  降级须模板与 manifest 双同意；无证据不得标 verified。
- 资源  ready / busy / insufficient / unknown —— legacy 4B / 8192 MiB 默认
  门槛；超出须精确匹配审核登记（pack_id/version/模板/产物标识）；缺 VRAM
  遥测不盲放行（resource_unknown）。

成功 reason_code=None；优先级 平台 > 隔离 > 资源。
"""
from __future__ import annotations

from dataclasses import dataclass

from inferfabric.platform.pack_probe import PackHostFacts, platform_key
from inferfabric.packs.schema import PLATFORM_KEYS, PackManifest

__all__ = [
    "RuntimeTemplate", "RuntimeEvidence", "ResourceEvidence", "PackDecision",
    "evaluate_pack", "get_template", "register_template", "reset_registry",
    "register_reviewed_profile", "LEGACY_MAX_PARAMS_B",
    "LEGACY_MAX_PEAK_MEM_MB",
]

LEGACY_MAX_PARAMS_B = 4
LEGACY_MAX_PEAK_MEM_MB = 8192

# legacy 参数门槛只约束对话类能力（与 packs.policy 的 llm/vl 门一致）
_PARAM_GATED = frozenset({"llm", "vl"})


@dataclass(frozen=True)
class RuntimeTemplate:
    """服务端登记的运行时模板 —— 准入范围的权威，manifest 不能扩大。"""

    name: str
    platforms: tuple[str, ...]            # 允许的平台键（schema.PLATFORM_KEYS 词汇）
    isolation: str                        # 模板可提供的隔离级别（证据确认后生效）
    allow_degraded: bool                  # 模板是否同意降级运行
    resource_profile: str                 # "legacy_default" | 审核资源档名
    expected_artifacts: tuple[str, ...] = ()  # 不可变产物标识（sha256）
    # GPU 规格下限（spec §3：NInfer 旗舰另要求已验证的 SM 12.x）；
    # None = 不设下限（compute_capability 不参与判定）
    min_compute_capability: tuple[int, int] | None = None


@dataclass(frozen=True)
class RuntimeEvidence:
    """已实测的运行时隔离态（不是声明）。"""

    isolation: str  # verified | degraded | unverified | unavailable


@dataclass(frozen=True)
class ResourceEvidence:
    """已实测的资源态。None = 遥测缺失（unknown，不盲放行 GPU 包）。"""

    disk_ok: bool | None
    ram_ok: bool | None
    vram_ok: bool | None
    gpu_busy: bool = False


@dataclass(frozen=True)
class PackDecision:
    platform: str      # compatible | incompatible | unknown
    isolation: str     # verified | degraded | unverified | unavailable
    resources: str     # ready | busy | insufficient | unknown
    allowed: bool
    reason_code: str | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {"platform": self.platform, "isolation": self.isolation,
                "resources": self.resources, "allowed": self.allowed,
                "reason_code": self.reason_code, "detail": self.detail}


# ── 服务端注册表（模板 + 审核资源档）────────────────────────────

_TEMPLATES: dict[str, RuntimeTemplate] = {}
_REVIEWED: dict[tuple, dict] = {}


def _seed_defaults() -> None:
    # 旧 Mac llama.cpp 模板：平台明确 darwin-arm，进程组级隔离最多 degraded；
    # 绝不因它在目录里就变成跨平台（spec §3）。
    _TEMPLATES["llama-cpp-macos-arm64-v1"] = RuntimeTemplate(
        name="llama-cpp-macos-arm64-v1", platforms=("darwin-arm",),
        isolation="degraded", allow_degraded=True,
        resource_profile="legacy_default", expected_artifacts=())


def reset_registry() -> None:
    """测试隔离：清空并重置为出厂默认模板。"""
    _TEMPLATES.clear()
    _REVIEWED.clear()
    _seed_defaults()


def register_template(t: RuntimeTemplate) -> None:
    _TEMPLATES[t.name] = t


def get_template(name: str) -> RuntimeTemplate | None:
    return _TEMPLATES.get(name)


def register_reviewed_profile(*, pack_id: str, pack_version: str,
                              template_name: str,
                              artifact_identities: tuple[str, ...],
                              max_params_b: int, max_mem_mb: int) -> None:
    """登记审核资源档：精确 (pack_id, version, 模板, 产物标识) 才生效。

    私有试点（spec §4）专用；manifest / 自定义包 / force 均不可触达。
    """
    key = (pack_id, pack_version, template_name,
           tuple(sorted(artifact_identities)))
    _REVIEWED[key] = {"max_params_b": max_params_b,
                      "max_mem_mb": max_mem_mb}


def _find_reviewed(manifest: PackManifest) -> dict | None:
    identities = tuple(sorted(a.sha256 for a in manifest.artifacts))
    return _REVIEWED.get((manifest.pack_id, manifest.pack_version,
                          manifest.runtime_template, identities))


def reviewed_profile_for(manifest: PackManifest) -> dict | None:
    """公开只读访问：安装事务 preflight 复用同一审核登记（Task 3）。"""
    return _find_reviewed(manifest)


# ── 三维度判定 ──────────────────────────────────────────────────

def _platform_dim(manifest: PackManifest, facts: PackHostFacts,
                  template: RuntimeTemplate | None) -> tuple[str, str | None]:
    if template is None:
        return "unknown", "platform_unverified"
    key = platform_key(facts)
    if key not in PLATFORM_KEYS:
        # darwin-x86 / linux-unknown / unknown —— 事实不足，不是不兼容
        return "unknown", "platform_unverified"
    effective = template.platforms
    if manifest.platforms is not None:
        # 申明只收窄：与模板求交，绝不能扩大
        effective = tuple(p for p in manifest.platforms
                          if p in template.platforms)
    if key not in effective:
        return "incompatible", "platform_mismatch"
    # GPU 规格下限（终审 F7）：实测 compute_capability 缺失 → unknown
    # （事实不足，不是不兼容，也绝不放行）；低于下限 → incompatible。
    if template.min_compute_capability is not None:
        cc = facts.compute_capability
        if cc is None:
            return "unknown", "platform_unverified"
        if cc < template.min_compute_capability:
            return "incompatible", "platform_mismatch"
    return "compatible", None


def _isolation_dim(template: RuntimeTemplate | None,
                   runtime: RuntimeEvidence,
                   manifest: PackManifest) -> tuple[str, bool, str | None]:
    if template is None:
        # 未知模板：即使 runtime 证据可用也不得宣称隔离已验证
        return "unverified", False, None
    iso = runtime.isolation
    if iso == "unavailable":
        return "unavailable", False, "runtime_unavailable"
    if iso == "unverified":
        return "unverified", False, "isolation_unverified"
    if iso == "degraded":
        if template.allow_degraded and manifest.degraded_install_allowed:
            return "degraded", True, None
        return "degraded", False, "isolation_degraded_not_allowed"
    if iso == "verified":
        return "verified", True, None
    return "unverified", False, "isolation_unverified"


def _resource_dim(manifest: PackManifest, resources: ResourceEvidence) \
        -> tuple[str, bool, str | None]:
    reviewed = _find_reviewed(manifest)
    max_b = reviewed["max_params_b"] if reviewed else LEGACY_MAX_PARAMS_B
    max_mem = reviewed["max_mem_mb"] if reviewed else LEGACY_MAX_PEAK_MEM_MB
    gpu_pack = manifest.capability in _PARAM_GATED \
        and manifest.gpu_role != "none"
    if gpu_pack and resources.vram_ok is None:
        # 缺遥测不为 GPU 包盲放行（含旗舰审核档）
        return "unknown", False, "resource_unknown"
    if manifest.capability in _PARAM_GATED and (
            manifest.parameter_count_b > max_b
            or manifest.peak_memory_mb > max_mem):
        return "insufficient", False, "resource_insufficient"
    if resources.disk_ok is False or resources.ram_ok is False \
            or resources.vram_ok is False:
        return "insufficient", False, "resource_insufficient"
    if manifest.gpu_role != "none" and resources.gpu_busy:
        # 临时验证与正式启动同规：GPU 被占用一律 busy，不挤占现役
        return "busy", False, "resource_busy"
    if resources.disk_ok is None or resources.ram_ok is None:
        return "unknown", False, "resource_unknown"
    return "ready", True, None


def evaluate_pack(manifest: PackManifest, facts: PackHostFacts,
                  template: RuntimeTemplate | None, runtime: RuntimeEvidence,
                  resources: ResourceEvidence, *, operation: str) \
        -> PackDecision:
    """三维度准入判定；operation ('install'|'start') 走同一规则。"""
    del operation  # 安装期临时验证与正式启动同规（spec §4）
    platform, p_reason = _platform_dim(manifest, facts, template)
    isolation, i_ok, i_reason = _isolation_dim(template, runtime, manifest)
    res, r_ok, r_reason = _resource_dim(manifest, resources)
    p_ok = p_reason is None
    allowed = p_ok and i_ok and r_ok
    reason = p_reason or i_reason or r_reason
    return PackDecision(platform=platform, isolation=isolation,
                        resources=res, allowed=allowed, reason_code=reason)


# 模块导入即播种默认模板（reset_registry 回到同一状态）
_seed_defaults()
