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
    # cloud 候选编译时的双协议路由快照（openai_base / anthropic_base），
    # mid-flight 请求持旧快照引用不受 provider 配置变化影响（终审 #5）
    route: dict[str, str] = field(default_factory=dict)

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
                 provider_base: Callable[[str], str] | None = None,
                 provider_route: Callable[[str], dict[str, str]] | None = None):
        self._config_path = Path(config_path)
        self._lock = threading.Lock()
        self._snapshot: RegistrySnapshot = _EMPTY_SNAPSHOT
        # 最后一次成功加载/写入的原始配置 dict（toggle 等字段级修改的基底，
        # 保留悬空候选/未回传字段，避免经有损视图重建丢数据）
        self._raw: dict | None = None
        self._known_local = _as_getter(known_local)
        self._known_providers = _as_getter(known_providers)
        self._provider_base = provider_base or (lambda provider: "")
        self._provider_route = provider_route
        self.evidence = EvidenceStore(evidence_path)
        # 运行状态（v7.1.0 三态，供 GET /admin/model-pools）：
        # configured=True 曾成功加载/写入过有效配置（快照/raw 可用）
        # configured=False + last_load_error=None → 从未配置（前端出初始化向导）
        # last_load_error≠None → 最近一次加载失败（前端错误横幅；registry
        #   保留最后有效快照继续服务）
        self._configured = False
        self._load_error: str | None = None

    # ── 运行状态（只读属性） ──

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def last_load_error(self) -> str | None:
        return self._load_error

    def raw_config(self) -> dict | None:
        """最后成功加载/写入的原始配置深拷贝（编辑器读-改-写基底）。

        GET 视图是有损的（悬空候选被剔除、部分字段不回传）——全量 CAS
        更新必须以 raw 为基底，否则一次编辑即丢数据。
        """
        import copy
        with self._lock:
            return copy.deepcopy(self._raw) if self._raw is not None else None

    # ── 快照 ──

    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    # ── 加载 ──

    def load(self) -> None:
        """启动加载。失败保留空快照（revision 0），不抛出。"""
        self.evidence.load()
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> bool | None:
        """三态：True=拾取新配置；None=无文件或内容未变；False=非法/损坏
        （保留最后有效快照）。调用方必须区分 False 与 None — 前者要在
        reload_all 里上报失败，后者是正常空转（终审 #19）。"""
        import yaml as _yaml
        if not self._config_path.exists():
            # 文件消失 ≠ 文件损坏：清除陈旧 load_error，前端回到
            # 「从未配置」向导态而非卡在错误横幅（终审 M7，升级 Important）
            self._load_error = None
            return None
        try:
            raw = _yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("model_pools.yaml unreadable (%s) — pools disabled", e)
            self._load_error = f"model_pools.yaml unreadable: {e}"
            return False
        if not isinstance(raw, dict) or not raw:
            self._load_error = "model_pools.yaml is empty or not a mapping"
            return False
        unchanged = raw == self._raw
        file_revision = raw.get("revision")
        current = self._snapshot.revision
        if isinstance(file_revision, int):
            revision = file_revision
        else:
            revision = current + 1
        if unchanged:
            # 内容未变绝不回退 revision（二次重载磁盘 revision 键仍为旧值，
            # 回退会让陈旧 CAS 编辑者重新获得写窗口 — 终审 #4）
            revision = max(revision, current)
        elif revision <= current:
            # 外部编辑常原地保留旧 revision 键：内容变化必须推进 revision，
            # 否则持有旧 revision 的编辑者可 CAS 覆盖掉他人的修改（评审 #2）
            revision = current + 1
        try:
            parsed, issues = load_pools_dict(raw, current_revision=revision)
        except Exception as e:
            # 解析崩溃（畸形类型逃逸静态校验等）→ 保留最后有效快照，不抛出
            log.error("model_pools.yaml parse crashed (%s) — keeping last good snapshot",
                      type(e).__name__)
            self._load_error = f"model_pools.yaml parse crashed: {type(e).__name__}: {e}"
            return False
        if parsed is None:
            log.error("model_pools.yaml invalid — keeping last good snapshot: %s",
                      "; ".join(issues))
            self._load_error = "model_pools.yaml invalid: " + "; ".join(issues)
            return False
        self._raw = raw
        self._publish_locked(parsed)
        self._configured = True
        self._load_error = None
        return None if unchanged else True

    def reload_from_disk(self) -> bool | None:
        """热加载（SIGHUP / /reload-config 复用）。三态见 _load_locked。"""
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
            self._raw = raw
            self._publish_locked(parsed)
            self._configured = True
            self._load_error = None
            return self._snapshot

    def compile_pool(self, config: dict, pool_name: str) -> PoolSnapshot:
        """dry-run 编辑态预览：把传入配置编译为单池快照（只读，不发布）。

        Raises:
            ValueError: 配置非法（静态校验问题 join 在 message）。
            KeyError: 池不存在。
        """
        parsed, issues = load_pools_dict(config or {},
                                         current_revision=self._snapshot.revision)
        if parsed is None:
            raise ValueError("; ".join(issues))
        with self._lock:
            snap = self._compile(parsed)
        if pool_name not in snap.pools:
            raise KeyError(pool_name)
        return snap.pools[pool_name]

    def flip_pool_enabled(self, pool_name: str, enabled: bool,
                          expected_revision: int) -> RegistrySnapshot:
        """服务端原子翻转单池 enabled（保留其余原始结构）。

        与 update() 同一 CAS/写盘/发布路径，但不要求调用方持有完整配置 —
        基于 self._raw 做字段级修改，悬空候选与未回传字段不丢失。

        Raises:
            KeyError: 池不存在（含从未成功加载任何配置）。
            ValueError: 翻转后校验失败（不应发生 — 仅改 enabled）。
            RevisionConflict: expected_revision 过期。
        """
        import copy
        with self._lock:
            # CAS 先于存在性检查：池被外部删除时，过期 revision 必须报冲突
            # （409）而非 404 — 否则调用方会基于陈旧状态误判「池不存在」
            current = self._snapshot.revision
            if expected_revision != current:
                raise RevisionConflict(
                    f"expected_revision {expected_revision} != current {current}")
            raw = copy.deepcopy(self._raw) if self._raw else None
            pools = (raw or {}).get("pools") or {}
            if not isinstance(pools, dict) or pool_name not in pools:
                raise KeyError(pool_name)
            pools[pool_name]["enabled"] = bool(enabled)
            new_revision = current + 1
            raw.pop("revision", None)
            parsed, issues = load_pools_dict(raw, current_revision=new_revision)
            if parsed is None:
                raise ValueError("; ".join(issues))
            raw["revision"] = new_revision
            self._write_atomic(raw)
            self._raw = raw
            self._publish_locked(parsed)
            self._configured = True
            self._load_error = None
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

    def _route_for(self, provider: str) -> dict[str, str]:
        """provider 的双协议路由（openai_base / anthropic_base）。

        provider_route 回调优先（per-protocol）；仅提供 provider_base 时
        双协议共用同一 base（向后兼容）。"""
        if self._provider_route is not None:
            route = self._provider_route(provider) or {}
            return {"openai_base": str(route.get("openai_base", "")),
                    "anthropic_base": str(route.get("anthropic_base", ""))}
        base = self._provider_base(provider)
        return {"openai_base": base, "anthropic_base": base}

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
                    route: dict[str, str] = {}
                else:
                    if cand.provider not in known_providers:
                        issues.append(
                            f"candidate '{cand.id}': provider '{cand.provider}' "
                            f"not configured — excluded")
                        continue
                    # 指纹覆盖双协议 base：任一协议端点变化即失效全部旧证据
                    # （终审 #6 — 只看 openai_base 会漏 anthropic 端点变更）
                    route = self._route_for(cand.provider)
                    fp = endpoint_fingerprint(
                        "cloud", cand.provider,
                        f"openai={route['openai_base']}|anthropic={route['anthropic_base']}",
                        cand.model_id)
                resolved.append(ResolvedCandidate(
                    candidate=cand, endpoint_fingerprint=fp,
                    evidence=self._evidence_for(cand, fp),
                    route=route,
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
        # store 条目覆盖同协议声明；指纹不符（provider base/认证/模型变化）
        # 的旧证据不注入 — 避免为「现在的端点」背书「过去的探测」（评审 #3）
        for key, ev in self.evidence._data.items():
            if not key.startswith(prefix + ":"):
                continue
            if ev.fingerprint != fp:
                continue
            protocol = key.rsplit(":", 1)[-1]
            out[protocol] = ev
        return out
