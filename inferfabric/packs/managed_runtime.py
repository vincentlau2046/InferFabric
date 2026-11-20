"""受管能力包运行时解析与共享启动决策 — 阶段 1 Task 3（spec §4）。

契约：
- resolve(model)：legacy 模型（无 managed_by_pack 标记）返回 None；
  坏引用 / 缺失记录 / 模板或产物身份不匹配抛 PackRuntimeError（带稳定
  reason_code），绝不默认降级 legacy。
- verify(instance)：正式启动/调度边界复核已解析身份（store 记录仍在、
  产物未漂移、模板仍登记）。
- check_start(model)：解析 + 复核 + GPU idle-only 准入（占用 →
  resource_busy），必须发生在任何 stop/switch 副作用之前；GPU 锁由
  调用方持有（锁内探测，不重入）。冲突只拒绝——绝不收养或停止外来
  实例（spec §4：只认属于自己的已验证身份）。

线程模型：ManagedRuntime 无内部可变状态；注册表/注入依赖由调用方管理。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from inferfabric.packs.schema import parse_manifest

__all__ = ["PackRuntimeError", "ResolvedRuntime", "ManagedRuntime",
           "set_managed_runtime", "get_managed_runtime", "MARKER_VERSION"]

MARKER_VERSION = 1

# 各引擎配置段的端口属性（冲突探测用；缺失段跳过）
_ENGINE_SECTIONS = ("ninfer", "llama_cpp", "vllm", "sglang", "ollama",
                    "asr_server", "tts_server")


class PackRuntimeError(Exception):
    """受管运行时拒绝。reason_code ∈ spec §3 wire 契约。"""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


@dataclass(frozen=True)
class ResolvedRuntime:
    """解析后的受管实例：审核模板、已验证身份、所有权与绑定。"""

    pack_id: str
    pack_version: str
    template_name: str
    artifact_identities: tuple[str, ...]
    role: str                 # "owned" | "external"（借用的既有产物）
    model_name: str
    marker: dict


def _model_port(model) -> int | None:
    for attr in _ENGINE_SECTIONS:
        cfg = getattr(model, attr, None)
        port = getattr(cfg, "port", None)
        if isinstance(port, int):
            return port
    return None


class ManagedRuntime:
    """受管包运行时解析器。依赖全部可注入（测试无运行时副作用）。"""

    def __init__(self, store, pack_root, registry_lookup=None,
                 probe_gpu_busy=None, models_lookup=None):
        self._store = store
        self._pack_root = Path(pack_root)
        from inferfabric.packs.admission import get_template
        self._registry_lookup = registry_lookup or get_template
        self._probe_gpu_busy = probe_gpu_busy
        self._models_lookup = models_lookup or {}

    # ── 解析 ────────────────────────────────────────────────────

    def resolve(self, model) -> ResolvedRuntime | None:
        marker = getattr(model, "managed_by_pack", None)
        if not marker:
            return None                      # legacy：走既有路径
        if not isinstance(marker, dict) or not marker.get("pack_id"):
            raise PackRuntimeError("runtime_identity_mismatch",
                                   "malformed managed_by_pack marker")
        pack_id = marker["pack_id"]
        rec = self._store.get(pack_id)
        if rec is None or rec.status != "installed":
            raise PackRuntimeError("ownership_conflict",
                                   f"no installed pack record for {pack_id}")
        version = marker.get("pack_version") or rec.pack_version
        manifest = self._load_manifest(pack_id, version)
        if manifest is None:
            raise PackRuntimeError("runtime_identity_mismatch",
                                   f"pack manifest missing for {pack_id}")
        template = self._registry_lookup(manifest.runtime_template)
        if template is None:
            raise PackRuntimeError(
                "runtime_identity_mismatch",
                f"runtime template {manifest.runtime_template!r} not registered")
        self._check_collision(model, pack_id)
        identities = tuple(sorted(a.sha256 for a in manifest.artifacts))
        return ResolvedRuntime(
            pack_id=pack_id, pack_version=manifest.pack_version,
            template_name=manifest.runtime_template,
            artifact_identities=identities,
            role=str(marker.get("role", "owned")),
            model_name=model.name, marker=dict(marker))

    def _load_manifest(self, pack_id: str, version: str):
        path = self._pack_root / pack_id / version / "manifest.yaml"
        try:
            with open(path, encoding="utf-8") as f:
                return parse_manifest(yaml.safe_load(f))
        except (OSError, yaml.YAMLError, ValueError):
            return None

    def _check_collision(self, model, pack_id: str) -> None:
        """端口/名字冲突：只拒绝，绝不收养或停止外来实例。"""
        my_port = _model_port(model)
        if my_port is None:
            return
        for name, other in self._models_lookup.items():
            if name == model.name or getattr(other, "managed_by_pack", None):
                continue                     # legacy 实例才构成外来占用
            if _model_port(other) == my_port:
                raise PackRuntimeError(
                    "ownership_conflict",
                    f"port {my_port} is bound to legacy model {name!r}; "
                    "managed pack never adopts or stops foreign instances")

    # ── 复核（调度边界）─────────────────────────────────────────

    def verify(self, instance: ResolvedRuntime) -> None:
        rec = self._store.get(instance.pack_id)
        if rec is None or rec.status != "installed":
            raise PackRuntimeError("ownership_conflict",
                                   f"pack record gone: {instance.pack_id}")
        manifest = self._load_manifest(instance.pack_id, instance.pack_version)
        if manifest is None:
            raise PackRuntimeError("runtime_identity_mismatch",
                                   "pack manifest missing at dispatch")
        identities = tuple(sorted(a.sha256 for a in manifest.artifacts))
        if identities != instance.artifact_identities:
            raise PackRuntimeError("runtime_identity_mismatch",
                                   "installed artifacts drifted since resolve")
        if self._registry_lookup(instance.template_name) is None:
            raise PackRuntimeError("runtime_identity_mismatch",
                                   "runtime template no longer registered")

    # ── 启动准入（任何副作用之前）────────────────────────────────

    def check_start(self, model, *, proc=None, on_conflict_stop=None) \
            -> ResolvedRuntime | None:
        """解析 + 复核 + idle-only GPU 准入。

        on_conflict_stop 仅存在用于断言「本路径从不收养/停止外来实例」；
        受管试点永不调用它（spec §4）。
        """
        inst = self.resolve(model)
        if inst is None:
            return None
        self.verify(inst)
        if getattr(model, "gpu_role", "none") != "none" \
                and self._probe_gpu_busy is not None and self._probe_gpu_busy():
            # GPU 包 idle-only：不挤占现役、不执行 GPU reset（spec §4）
            raise PackRuntimeError(
                "resource_busy",
                "GPU busy — managed GPU pack admission is idle-only")
        return inst


# ── 进程级单例（网关装配注入；测试可重置）────────────────────────

_managed_runtime: ManagedRuntime | None = None


def set_managed_runtime(rt: ManagedRuntime | None) -> None:
    global _managed_runtime
    _managed_runtime = rt


def get_managed_runtime() -> ManagedRuntime | None:
    return _managed_runtime


def _managed_marker(model) -> dict | None:
    """受管标记（仅结构化 dict 计数）。

    getattr 兜底 legacy ModelConfig（无该字段 → None）；非 dict 真值
    （如测试 mock 自动属性）不得激活受管路径 —— 受管准入只在真实
    marker 存在时生效，防误拒 legacy 启动。
    """
    marker = getattr(model, "managed_by_pack", None)
    return marker if isinstance(marker, dict) and marker else None
