"""inferfabric/model_pool/registry.py — 池配置 registry：不可变快照 / revision / CAS 热更新。

- 快照编译：把池配置解析为 ResolvedCandidate（含端点指纹与证据），
  悬空目标（models.d / provider 不存在）进 pool.issues、不进 candidates。
- 热更新：静态校验 → expected_revision CAS → 临时文件 + os.replace 原子写盘
  → 整体替换内存快照引用。非法新配置保留最后有效快照（fail-closed）。
- revision 持久化在配置文件的 `revision` 键；外部编辑（无该键）按
  当前 revision+1 拾取，保证热加载不回退。
"""

import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Union

import yaml

from inferfabric.model_pool.config import (
    CandidateConfig,
    PoolConfig,
    PoolsFile,
    load_pools_dict,
)
from inferfabric.model_pool.evidence import (
    Evidence,
    EvidenceState,
    EvidenceStore,
    endpoint_fingerprint,
)

log = logging.getLogger("inferfabric.model_pool.registry")

_KnownRef = Union[Iterable[str], Callable[[], Iterable[str]]]


class RevisionConflict(Exception):
    """CAS 更新时 expected_revision 与当前 revision 不符。"""


@dataclass(frozen=True)
class ResolvedCandidate:
    candidate: CandidateConfig
    endpoint_fingerprint: str
    evidence: dict[str, Evidence] = field(default_factory=dict)  # protocol → evidence

    @property
    def evidence_key_prefix(self) -> str:
        return _evidence_key_prefix(self.candidate)


@dataclass(frozen=True)
class PoolSnapshot:
    revision: int
    pool: PoolConfig
    candidates: tuple[ResolvedCandidate, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistrySnapshot:
    revision: int
    pools: dict[str, PoolSnapshot] = field(default_factory=dict)


def _evidence_key_prefix(cand: CandidateConfig) -> str:
    if cand.kind == "cloud":
        return f"cloud:{cand.provider}:{cand.model_id}"
    return f"local:{cand.model_name}:{cand.model_name}"


def _as_getter(ref) -> Callable[[], set]:
    if callable(ref):
        return lambda: set(ref())
    items = set(ref or ())
    return lambda: items


_EMPTY_SNAPSHOT = RegistrySnapshot(revision=0, pools={})


class PoolRegistry:
    """线程安全：内部 lock 只保护整体替换，snapshot() 返回不可变对象。"""

    def __init__(self, config_path: Path, evidence_path: Path,
                 known_local: _KnownRef = (),
                 known_providers: _KnownRef = (),
                 provider_base: Callable[[str], str] | None = None):
        self._config_path = Path(config_path)
        self._lock = threading.Lock()
        self._snapshot: RegistrySnapshot = _EMPTY_SNAPSHOT
        self._known_local = _as_getter(known_local)
        self._known_providers = _as_getter(known_providers)
        self._provider_base = provider_base or (lambda provider: "")
        self.evidence = EvidenceStore(evidence_path)

    # ── 快照 ──

    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    # ── 加载 ──

    def load(self) -> None:
        """启动加载。失败保留空快照（revision 0），不抛出。"""
        self.evidence.load()
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> bool:
        import yaml as _yaml
        if not self._config_path.exists():
            return False
        try:
            raw = _yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("model_pools.yaml unreadable (%s) — pools disabled", e)
            return False
        if not isinstance(raw, dict) or not raw:
            return False
        file_revision = raw.get("revision")
        revision = int(file_revision) if isinstance(file_revision, int) else self._snapshot.revision + 1
        parsed, issues = load_pools_dict(raw, current_revision=revision)
        if parsed is None:
            log.error("model_pools.yaml invalid — keeping last good snapshot: %s",
                      "; ".join(issues))
            return False
        self._publish_locked(parsed)
        return True

    def reload_from_disk(self) -> bool:
        """热加载（SIGHUP / /reload-config 复用）。返回是否拾取到新配置。"""
        self.evidence.load()
        with self._lock:
            return self._load_locked()

    # ── CAS 更新 ──

    def update(self, raw_dict: dict, expected_revision: int) -> RegistrySnapshot:
        """静态校验 + CAS + 原子写盘 + 发布新快照。

        Raises:
            ValueError: 静态校验失败（问题列表 join 在 message）。
            RevisionConflict: expected_revision 过期。
        """
        with self._lock:
            current = self._snapshot.revision
            if expected_revision != current:
                raise RevisionConflict(
                    f"expected_revision {expected_revision} != current {current}")
            new_revision = current + 1
            raw = dict(raw_dict or {})
            raw.pop("revision", None)  # revision 由 registry 管理
            parsed, issues = load_pools_dict(raw, current_revision=new_revision)
            if parsed is None:
                raise ValueError("; ".join(issues))
            raw["revision"] = new_revision
            self._write_atomic(raw)
            self._publish_locked(parsed)
            return self._snapshot

    # ── 内部 ──

    def _write_atomic(self, payload: dict) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._config_path.parent),
                                   prefix=".model_pools.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._config_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _publish_locked(self, parsed: PoolsFile) -> None:
        self._snapshot = self._compile(parsed)

    def _compile(self, parsed: PoolsFile) -> RegistrySnapshot:
        known_local = self._known_local()
        known_providers = self._known_providers()
        pools: dict[str, PoolSnapshot] = {}
        for name, pool in parsed.pools.items():
            resolved: list[ResolvedCandidate] = []
            issues: list[str] = []
            for cand in pool.candidates:
                if cand.kind == "local":
                    if cand.model_name not in known_local:
                        issues.append(
                            f"candidate '{cand.id}': local model '{cand.model_name}' "
                            f"not found in models.d — excluded")
                        continue
                    fp = endpoint_fingerprint("local", cand.model_name, "", cand.model_name)
                else:
                    if cand.provider not in known_providers:
                        issues.append(
                            f"candidate '{cand.id}': provider '{cand.provider}' "
                            f"not configured — excluded")
                        continue
                    fp = endpoint_fingerprint(
                        "cloud", cand.provider, self._provider_base(cand.provider),
                        cand.model_id)
                resolved.append(ResolvedCandidate(
                    candidate=cand, endpoint_fingerprint=fp,
                    evidence=self._evidence_for(cand, fp),
                ))
            pools[name] = PoolSnapshot(
                revision=parsed.revision, pool=pool,
                candidates=tuple(resolved), issues=tuple(issues),
            )
        return RegistrySnapshot(revision=parsed.revision, pools=pools)

    def _evidence_for(self, cand: CandidateConfig, fp: str) -> dict[str, Evidence]:
        """证据注入：store 记录优先（含 FAILED/STALE 覆盖声明），否则用声明推导。"""
        prefix = _evidence_key_prefix(cand)
        out: dict[str, Evidence] = {}
        for protocol in cand.declared_protocols:
            out[protocol] = Evidence(
                state=EvidenceState.DECLARED, protocol=protocol,
                features=cand.features, fingerprint=fp,
                recorded_at="", source="admin-declare")
        # store 条目覆盖同协议声明
        for key, ev in self.evidence._data.items():
            if not key.startswith(prefix + ":"):
                continue
            protocol = key.rsplit(":", 1)[-1]
            out[protocol] = ev
        return out
