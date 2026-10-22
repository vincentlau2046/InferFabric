"""inferfabric/model_pool/evidence.py — 协议/功能兼容性证据存储。

证据五态（spec §5.1）：
  UNKNOWN  无任何证据 → 恒不参与选择
  DECLARED 管理员明确声明 → 仅当池 accept_declared=True 时参与
  PROBED   对应行为用例实测通过 → 恒可参与
  FAILED   验证失败 → 恒不参与；需显式清除并记录，新声明不能悄悄覆盖
  STALE    依赖指纹变化后失效 → 恒不参与，需重新验证/声明

证据按「目标 + 协议端点指纹 + 上游模型 ID + 功能」记录，与池配置解耦：
候选健康变化不改写证据；Provider base URL / 认证 / 模型 ID 变化通过
指纹失配使证据转 STALE（invalidate_by_fingerprint）。

持久化：JSON 文件，临时文件 + os.replace 原子替换；损坏文件不崩溃。
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path

log = logging.getLogger("inferfabric.model_pool.evidence")


class EvidenceState(str, Enum):
    UNKNOWN = "unknown"
    DECLARED = "declared"
    PROBED = "probed"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True)
class Evidence:
    state: EvidenceState
    protocol: str                       # "openai" | "anthropic"
    features: tuple[str, ...]           # 该证据覆盖的功能
    fingerprint: str                    # 依赖指纹（endpoint_fingerprint）
    recorded_at: str                    # ISO8601
    source: str                         # "admin-probe" | "admin-declare"


def endpoint_fingerprint(kind: str, provider_or_service: str,
                         base_url: str, model_id: str) -> str:
    """依赖指纹：kind/provider(base)/model_id 任一变化即失效。"""
    return "|".join((kind, provider_or_service, (base_url or "").rstrip("/"), model_id))


def _evidence_from_json(raw: dict) -> Evidence:
    return Evidence(
        state=EvidenceState(raw.get("state", "unknown")),
        protocol=str(raw.get("protocol", "")),
        features=tuple(raw.get("features") or ()),
        fingerprint=str(raw.get("fingerprint", "")),
        recorded_at=str(raw.get("recorded_at", "")),
        source=str(raw.get("source", "")),
    )


def _evidence_to_json(ev: Evidence) -> dict:
    d = asdict(ev)
    d["state"] = ev.state.value
    d["features"] = list(ev.features)
    return d


class EvidenceStore:
    """内存 dict + JSON 原子持久化。load() 失败保留内存旧态。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._data: dict[str, Evidence] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("Evidence store %s unreadable (%s) — starting empty", self._path, e)
            return
        if not isinstance(raw, dict):
            log.error("Evidence store %s has invalid root — starting empty", self._path)
            return
        loaded: dict[str, Evidence] = {}
        for key, val in raw.items():
            try:
                loaded[str(key)] = _evidence_from_json(val)
            except Exception:
                log.warning("Skipping invalid evidence entry %r", key)
        self._data = loaded

    def save(self) -> None:
        payload = {k: _evidence_to_json(v) for k, v in self._data.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".model_pool_evidence.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str) -> Evidence | None:
        return self._data.get(key)

    def record(self, key: str, evidence: Evidence) -> None:
        self._data[key] = evidence

    def invalidate_by_fingerprint(self, old_fingerprint: str) -> int:
        """把匹配指纹的 PROBED 证据转 STALE。返回命中数。

        DECLARED 不动（声明是管理员意图，不因端点技术细节失效；
        FAILED 不动（已是排除态）。
        """
        n = 0
        for key, ev in list(self._data.items()):
            if ev.fingerprint == old_fingerprint and ev.state == EvidenceState.PROBED:
                self._data[key] = Evidence(
                    state=EvidenceState.STALE, protocol=ev.protocol,
                    features=ev.features, fingerprint=ev.fingerprint,
                    recorded_at=ev.recorded_at, source=ev.source)
                n += 1
        if n:
            log.info("Evidence: %d entries marked STALE for fingerprint %s", n, old_fingerprint)
        return n
