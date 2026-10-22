"""inferfabric/model_pool/config.py — 逻辑模型池配置数据模型与静态校验。

配置文件 IFF_DATA_DIR/model_pools.yaml：
  schema_version: 1
  pools:
    fast:
      enabled: true
      strategy: priority_failover
      allowed_locations: [cloud]        # 空或缺省 = 不启用任何位置
      accept_declared: false            # 是否接受管理员声明的协议证据
      allow_cross_location: false       # 跨本地/云回退（需 allowed_locations 同时含 local+cloud）
      max_attempts: 2
      deadline_ms: 60000
      candidates:
        - id: fast_primary
          priority: 10                  # 越小越优先，池内唯一
          kind: cloud                   # local | cloud
          provider: provider_a          # cloud 必填
          model_id: model_a             # cloud 必填
          # kind: local 时用 model_name（models.d 的 served_name）
          declared_protocols: [openai]  # 管理员声明（declared 证据）
          features: [tools, stream]     # 默认空 = 仅纯文本

纯解析 + 静态校验，不做任何 I/O 副作用或运行时查询（models.d / provider
存在性由 registry 阶段校验，见 registry.py）。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("inferfabric.model_pool.config")

VALID_KINDS = {"local", "cloud"}
VALID_FEATURES = {"tools", "stream", "vision", "structured"}
VALID_PROTOCOLS = {"openai", "anthropic"}
VALID_LOCATIONS = {"local", "cloud"}


def _str_seq(value, field_name: str, where: str, issues: list[str]) -> tuple[str, ...] | None:
    """list/tuple of str → tuple；类型不符记 issue 并返回 None（不抛异常）。

    YAML 可解析但类型畸形的值（`features: 4`、`declared_protocols: [{}]`）
    必须降级为校验问题，而不是 TypeError 逃逸导致整份配置无法加载。
    """
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        issues.append(f"{where}: {field_name} must be a list of strings")
        return None
    for item in value:
        if not isinstance(item, str):
            issues.append(f"{where}: {field_name} entries must be strings")
            return None
    return tuple(value)


@dataclass(frozen=True)
class CandidateConfig:
    id: str
    priority: int
    kind: str                                   # "local" | "cloud"
    model_name: str = ""                        # local: models.d served_name
    provider: str = ""                          # cloud: provider 名
    model_id: str = ""                          # cloud: 上游模型 ID
    declared_protocols: tuple[str, ...] = ()    # 管理员声明的协议
    features: tuple[str, ...] = ()              # 支持的功能（默认仅纯文本）


@dataclass(frozen=True)
class PoolConfig:
    name: str
    enabled: bool = False
    strategy: str = "priority_failover"
    allowed_locations: tuple[str, ...] = ()
    accept_declared: bool = False
    allow_cross_location: bool = False
    max_attempts: int = 2
    deadline_ms: int = 60000
    candidates: tuple[CandidateConfig, ...] = ()


@dataclass(frozen=True)
class PoolsFile:
    schema_version: int
    revision: int
    pools: dict[str, PoolConfig] = field(default_factory=dict)


def _parse_candidate(raw: dict, pool_name: str, issues: list[str]) -> CandidateConfig | None:
    cid = raw.get("id", "")
    if not isinstance(cid, str) or not cid:
        issues.append(f"pool '{pool_name}': candidate id must be a non-empty string")
        return None
    kind = raw.get("kind", "")
    if not isinstance(kind, str) or kind not in VALID_KINDS:
        issues.append(f"pool '{pool_name}' candidate '{cid}': kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
        return None

    model_name = raw.get("model_name", "")
    provider = raw.get("provider", "")
    model_id = raw.get("model_id", "")
    for label, value in (("model_name", model_name), ("provider", provider),
                         ("model_id", model_id)):
        if not isinstance(value, str):
            issues.append(f"pool '{pool_name}' candidate '{cid}': {label} must be a string")
            return None
    if kind == "local" and not model_name:
        issues.append(f"pool '{pool_name}' candidate '{cid}': local candidate requires model_name")
        return None
    if kind == "cloud" and not provider:
        issues.append(f"pool '{pool_name}' candidate '{cid}': cloud candidate requires provider")
        return None
    if kind == "cloud" and not model_id:
        issues.append(f"pool '{pool_name}' candidate '{cid}': cloud candidate requires model_id")

    # 禁池引用池：目标名不得落在 iff/ 逻辑命名空间
    target_names = [model_name, model_id] if kind == "cloud" else [model_name]
    for t in target_names:
        if t.startswith("iff/"):
            issues.append(
                f"pool '{pool_name}' candidate '{cid}': target '{t}' is in the iff/ "
                f"logical namespace — pools must not reference pools")
            return None

    where = f"pool '{pool_name}' candidate '{cid}'"
    declared = _str_seq(raw.get("declared_protocols"), "declared_protocols", where, issues)
    if declared is None:
        return None
    for p in declared:
        if p not in VALID_PROTOCOLS:
            issues.append(f"{where}: unknown protocol {p!r}")
            return None
    features = _str_seq(raw.get("features"), "features", where, issues)
    if features is None:
        return None
    for f in features:
        if f not in VALID_FEATURES:
            issues.append(f"{where}: unknown feature {f!r}")
            return None

    try:
        priority = int(raw.get("priority"))
    except (TypeError, ValueError):
        issues.append(f"pool '{pool_name}' candidate '{cid}': priority must be an integer")
        return None

    return CandidateConfig(
        id=cid, priority=priority, kind=kind,
        model_name=model_name, provider=provider, model_id=model_id,
        declared_protocols=declared, features=features,
    )


def _parse_pool(name: str, raw: dict, issues: list[str]) -> PoolConfig | None:
    if "/" in name:
        issues.append(f"pool name '{name}' must not contain '/'")
        return None

    cand_raw = raw.get("candidates") or []
    if not isinstance(cand_raw, list):
        issues.append(f"pool '{name}': candidates must be a list")
        return None

    candidates: list[CandidateConfig] = []
    for c in cand_raw:
        if not isinstance(c, dict):
            issues.append(f"pool '{name}': candidate entry must be a mapping")
            continue
        parsed = _parse_candidate(c, name, issues)
        if parsed is not None:
            candidates.append(parsed)

    seen_ids: set[str] = set()
    seen_priorities: set[int] = set()
    for c in candidates:
        if c.id in seen_ids:
            issues.append(f"pool '{name}': duplicate candidate id '{c.id}'")
        seen_ids.add(c.id)
        if c.priority in seen_priorities:
            issues.append(f"pool '{name}': duplicate priority {c.priority} (candidate '{c.id}')")
        seen_priorities.add(c.priority)

    allowed_locations = _str_seq(raw.get("allowed_locations"), "allowed_locations",
                                 f"pool '{name}'", issues)
    if allowed_locations is None:
        return None
    for loc in allowed_locations:
        if loc not in VALID_LOCATIONS:
            issues.append(f"pool '{name}': unknown location {loc!r} "
                          f"(must be one of {sorted(VALID_LOCATIONS)})")
            return None
    allow_cross = bool(raw.get("allow_cross_location", False))
    if allow_cross and not ({"local", "cloud"} <= set(allowed_locations)):
        issues.append(
            f"pool '{name}': allow_cross_location=true requires allowed_locations "
            f"to contain both 'local' and 'cloud'")

    # 候选为空 → 池强制 disabled（不假装可用）
    enabled = bool(raw.get("enabled", False))
    if not candidates:
        enabled = False

    try:
        max_attempts = int(raw.get("max_attempts", 2))
        deadline_ms = int(raw.get("deadline_ms", 60000))
    except (TypeError, ValueError):
        issues.append(f"pool '{name}': max_attempts/deadline_ms must be integers")
        return None

    return PoolConfig(
        name=name,
        enabled=enabled,
        strategy=str(raw.get("strategy", "priority_failover")),
        allowed_locations=allowed_locations,
        accept_declared=bool(raw.get("accept_declared", False)),
        allow_cross_location=allow_cross,
        max_attempts=max_attempts,
        deadline_ms=deadline_ms,
        candidates=tuple(sorted(candidates, key=lambda c: c.priority)),
    )


def load_pools_dict(raw: dict, current_revision: int) -> tuple[PoolsFile | None, list[str]]:
    """解析 + 静态校验。返回 (配置, 问题列表)；问题非空时配置为 None。"""
    issues: list[str] = []
    if not isinstance(raw, dict):
        return None, ["model_pools config root must be a mapping"]
    if raw.get("schema_version") != 1:
        return None, [f"unsupported schema_version: {raw.get('schema_version')!r} (expected 1)"]

    pools_raw = raw.get("pools") or {}
    if not isinstance(pools_raw, dict):
        return None, ["'pools' must be a mapping of pool name → pool config"]

    pools: dict[str, PoolConfig] = {}
    for name, praw in pools_raw.items():
        if not isinstance(praw, dict):
            issues.append(f"pool '{name}': config must be a mapping")
            continue
        pool = _parse_pool(str(name), praw, issues)
        if pool is not None:
            pools[pool.name] = pool

    if issues:
        return None, issues
    return PoolsFile(schema_version=1, revision=current_revision, pools=pools), []


def load_pools_file(path: Path) -> tuple[PoolsFile | None, list[str]]:
    """从磁盘加载。文件缺失 → (None, [])；损坏/非法 → (None, [问题])。"""
    if not path.exists():
        return None, []
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        return None, [f"failed to parse {path}: {e}"]
    if raw is None:
        return None, []
    return load_pools_dict(raw, current_revision=0)
