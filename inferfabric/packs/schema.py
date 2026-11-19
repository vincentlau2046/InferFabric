"""Pack Manifest 严格解析 — PK0 契约（Task 1）。

Manifest 是安装事务的不可变输入、卸载所有权的依据（spec §6.2）。
安全设计：
- 未知键任何层级拒绝 — Manifest 不允许携带任意安装脚本/钩子字段；
  运行时准备只能调用平台已审核的模板动作。
- network.listen 仅接受 loopback — 包进程只经网关 loopback API 被调用。
- sha256 必须 64 位 hex，供安装事务 staging 校验（Task 5）。
"""

import re
from dataclasses import dataclass

import yaml

CAPABILITIES = frozenset(
    {"llm", "vl", "asr", "tts", "embedding", "rerank", "ocr"})
ARTIFACT_KINDS = frozenset({"model", "runtime", "resource"})
GPU_ROLES = frozenset({"exclusive", "shared", "none"})
LIFECYCLE_MODES = frozenset({"on_demand", "resident"})
PACK_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*$")

# (section, keys) — 各段必填键；顶层键由此推导
_TOP = "pack_id pack_version capability model runtime resources lifecycle " \
       "network artifacts execution sandbox".split()
_MODEL_KEYS = ("id", "parameter_count_b", "quantization", "license")
_RUNTIME_KEYS = ("engine", "runtime_template")
_RESOURCE_KEYS = ("download_mb", "disk_mb", "peak_memory_mb", "gpu_role")
_LIFECYCLE_KEYS = ("mode",)
_NETWORK_KEYS = ("listen", "allowed_upstream")
_EXECUTION_KEYS = ("model_config_template", "healthcheck")
_SANDBOX_KEYS = ("policy", "degraded_install_allowed")


class ManifestError(ValueError):
    """Manifest 非法。field 为出错字段点路径，如 artifacts[0].sha256。"""

    def __init__(self, field: str, reason: str):
        self.field = field
        super().__init__(f"{field}: {reason}")


@dataclass(frozen=True)
class PackArtifact:
    id: str
    kind: str
    source: str
    sha256: str


@dataclass(frozen=True)
class PackManifest:
    pack_id: str
    pack_version: str
    capability: str
    model_id: str
    parameter_count_b: int
    quantization: str
    license: str
    engine: str
    runtime_template: str
    download_mb: int
    disk_mb: int
    peak_memory_mb: int
    gpu_role: str
    lifecycle_mode: str
    listen: str
    allowed_upstream: tuple[str, ...]
    artifacts: tuple[PackArtifact, ...]
    model_config_template: str
    healthcheck: str
    sandbox_policy: str
    degraded_install_allowed: bool

    def to_dict(self) -> dict:
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "capability": self.capability,
            "model": {"id": self.model_id,
                      "parameter_count_b": self.parameter_count_b,
                      "quantization": self.quantization,
                      "license": self.license},
            "runtime": {"engine": self.engine,
                        "runtime_template": self.runtime_template},
            "resources": {"download_mb": self.download_mb,
                          "disk_mb": self.disk_mb,
                          "peak_memory_mb": self.peak_memory_mb,
                          "gpu_role": self.gpu_role},
            "lifecycle": {"mode": self.lifecycle_mode},
            "network": {"listen": self.listen,
                        "allowed_upstream": list(self.allowed_upstream)},
            "artifacts": [{"id": a.id, "kind": a.kind, "source": a.source,
                           "sha256": a.sha256} for a in self.artifacts],
            "execution": {"model_config_template": self.model_config_template,
                          "healthcheck": self.healthcheck},
            "sandbox": {"policy": self.sandbox_policy,
                        "degraded_install_allowed":
                            self.degraded_install_allowed},
        }

    def to_dict_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False,
                              allow_unicode=True)


def _reject_unknown(section: str, data: dict, allowed: tuple | list) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        raise ManifestError(
            f"{section}.{'/'.join(sorted(unknown))}",
            f"unknown key(s) {sorted(unknown)} — manifest 字段严格白名单")


def _require(section: str, data: dict, keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in data]
    if not missing:
        return
    if len(missing) == 1:
        field = f"{section}.{missing[0]}" if section else missing[0]
    else:
        field = section or "<root>"
    raise ManifestError(field, f"missing required key(s) {missing}")


def parse_manifest(text: str | dict) -> PackManifest:
    """解析 YAML 文本或已加载 dict 为 PackManifest；任何违规抛 ManifestError。"""
    d = yaml.safe_load(text) if isinstance(text, str) else dict(text)
    if not isinstance(d, dict):
        raise ManifestError("", f"manifest must be a mapping, got {type(d).__name__}")
    _reject_unknown("", d, _TOP)
    _require("", d, tuple(_TOP))

    pack_id, capability = d["pack_id"], str(d["capability"])
    if not isinstance(pack_id, str) or not PACK_ID_RE.match(pack_id):
        raise ManifestError("pack_id", f"expected <capability>.<slug>, got {pack_id!r}")
    if not pack_id.startswith(capability + "."):
        raise ManifestError("pack_id",
                            f"prefix must match capability {capability!r}")
    version = str(d["pack_version"])
    if not PACK_VERSION_RE.match(version):
        raise ManifestError("pack_version",
                            f"expected semver X.Y.Z, got {version!r}")
    if capability not in CAPABILITIES:
        raise ManifestError("capability",
                            f"{capability!r} not in {sorted(CAPABILITIES)}")

    model = d["model"]
    _reject_unknown("model", model, _MODEL_KEYS)
    _require("model", model, _MODEL_KEYS)
    pcb = model["parameter_count_b"]
    if not isinstance(pcb, int) or pcb < 1:
        raise ManifestError("model.parameter_count_b",
                            f"expected positive int, got {pcb!r}")

    runtime = d["runtime"]
    _reject_unknown("runtime", runtime, _RUNTIME_KEYS)
    _require("runtime", runtime, _RUNTIME_KEYS)

    res = d["resources"]
    _reject_unknown("resources", res, _RESOURCE_KEYS)
    _require("resources", res, _RESOURCE_KEYS)
    for k in _RESOURCE_KEYS[:3]:
        if not isinstance(res[k], int) or res[k] < 1:
            raise ManifestError(f"resources.{k}",
                                f"expected positive int, got {res[k]!r}")
    if res["gpu_role"] not in GPU_ROLES:
        raise ManifestError("resources.gpu_role",
                            f"{res['gpu_role']!r} not in {sorted(GPU_ROLES)}")

    lc = d["lifecycle"]
    _reject_unknown("lifecycle", lc, _LIFECYCLE_KEYS)
    _require("lifecycle", lc, _LIFECYCLE_KEYS)
    if lc["mode"] not in LIFECYCLE_MODES:
        raise ManifestError("lifecycle.mode",
                            f"{lc['mode']!r} not in {sorted(LIFECYCLE_MODES)}")

    net = d["network"]
    _reject_unknown("network", net, _NETWORK_KEYS)
    _require("network", net, _NETWORK_KEYS)
    if net["listen"] != "loopback":
        raise ManifestError("network.listen",
                            f"only 'loopback' allowed, got {net['listen']!r}")
    upstream = net["allowed_upstream"]
    if not isinstance(upstream, list) or not all(isinstance(u, str) for u in upstream):
        raise ManifestError("network.allowed_upstream", "expected list of str")

    arts_raw = d["artifacts"]
    if not isinstance(arts_raw, list):
        raise ManifestError("artifacts", "expected list")
    # 空列表合法：系统内置能力（spec §3.5，如 macOS Vision）无下载 artifact
    artifacts = []
    for i, a in enumerate(arts_raw):
        path = f"artifacts[{i}]"
        if not isinstance(a, dict):
            raise ManifestError(path, "expected mapping")
        _reject_unknown(path, a, ("id", "kind", "source", "sha256"))
        _require(path, a, ("id", "kind", "source", "sha256"))
        if a["kind"] not in ARTIFACT_KINDS:
            raise ManifestError(f"{path}.kind",
                                f"{a['kind']!r} not in {sorted(ARTIFACT_KINDS)}")
        sha = str(a["sha256"]).lower()
        if not SHA256_RE.match(sha):
            raise ManifestError(f"{path}.sha256",
                                f"expected 64 hex chars, got {a['sha256']!r}")
        artifacts.append(PackArtifact(id=str(a["id"]), kind=a["kind"],
                                      source=str(a["source"]), sha256=sha))

    exe = d["execution"]
    _reject_unknown("execution", exe, _EXECUTION_KEYS)
    _require("execution", exe, _EXECUTION_KEYS)

    sb = d["sandbox"]
    _reject_unknown("sandbox", sb, _SANDBOX_KEYS)
    _require("sandbox", sb, _SANDBOX_KEYS)
    if not isinstance(sb["degraded_install_allowed"], bool):
        raise ManifestError("sandbox.degraded_install_allowed",
                            "expected bool")

    return PackManifest(
        pack_id=pack_id, pack_version=version, capability=capability,
        model_id=str(model["id"]), parameter_count_b=pcb,
        quantization=str(model["quantization"]), license=str(model["license"]),
        engine=str(runtime["engine"]),
        runtime_template=str(runtime["runtime_template"]),
        download_mb=res["download_mb"], disk_mb=res["disk_mb"],
        peak_memory_mb=res["peak_memory_mb"], gpu_role=res["gpu_role"],
        lifecycle_mode=lc["mode"], listen=net["listen"],
        allowed_upstream=tuple(upstream), artifacts=tuple(artifacts),
        model_config_template=str(exe["model_config_template"]),
        healthcheck=str(exe["healthcheck"]),
        sandbox_policy=str(sb["policy"]),
        degraded_install_allowed=sb["degraded_install_allowed"])
