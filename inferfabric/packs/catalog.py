"""Catalog 加载与校验 — PK0（Task 4）。

Catalog 是后端可读取的能力目录（spec §6.1）：列出经过审核的包，不直接表示
已安装状态；第一阶段随应用发布。安装入口收口裁决：能力包安装只接受 Catalog
pack_id，URL/hash 由后端提供（App 不得提交任意 URL）。

条目格式：display 元数据 + 内嵌 manifest: 块（走 parse_manifest 严格校验）。
"""

from dataclasses import dataclass

import yaml

from inferfabric.packs.schema import PackManifest, parse_manifest


class CatalogError(ValueError):
    def __init__(self, pack_id: str, reason: str):
        self.pack_id = pack_id
        super().__init__(f"catalog[{pack_id}]: {reason}")


_ENTRY_TOP = ("pack_id", "display_name", "summary", "tags", "risk_level",
              "builtin", "source_page", "manifest")
_RISK_LEVELS = frozenset({"verified", "experimental"})


@dataclass(frozen=True)
class CatalogEntry:
    manifest: PackManifest
    display_name: str
    summary: str
    tags: tuple[str, ...]
    risk_level: str
    builtin: bool
    source_page: str = ""


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]

    def find(self, pack_id: str) -> CatalogEntry | None:
        for e in self.entries:
            if e.manifest.pack_id == pack_id:
                return e
        return None

    def list_capability(self, cap: str) -> list[CatalogEntry]:
        return [e for e in self.entries if e.manifest.capability == cap]


def load_catalog(path) -> list[CatalogEntry]:
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict) or not isinstance(d.get("packs"), list):
        raise CatalogError("<root>", "expected top-level 'packs' list")
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(d["packs"]):
        pack_id = item.get("pack_id", f"<entry {i}>") if isinstance(item, dict) \
            else f"<entry {i}>"
        if not isinstance(item, dict):
            raise CatalogError(pack_id, "expected mapping")
        unknown = set(item) - set(_ENTRY_TOP)
        if unknown:
            raise CatalogError(pack_id, f"unknown key(s) {sorted(unknown)}")
        missing = [k for k in _ENTRY_TOP if k not in item
                   and k != "source_page"]   # source_page 可选（捆绑目录必带）
        if missing:
            raise CatalogError(pack_id, f"missing key(s) {missing}")
        if pack_id in seen:
            raise CatalogError(pack_id, "duplicate pack_id in catalog")
        seen.add(pack_id)
        if item["risk_level"] not in _RISK_LEVELS:
            raise CatalogError(pack_id,
                               f"risk_level {item['risk_level']!r} not in "
                               f"{sorted(_RISK_LEVELS)}")
        try:
            manifest = parse_manifest(item["manifest"])
        except Exception as exc:
            raise CatalogError(pack_id, f"invalid manifest: {exc}") from exc
        if manifest.pack_id != pack_id:
            raise CatalogError(
                pack_id, f"manifest.pack_id mismatch: {manifest.pack_id!r}")
        entries.append(CatalogEntry(
            manifest=manifest,
            display_name=str(item["display_name"]),
            summary=str(item["summary"]),
            tags=tuple(str(t) for t in item["tags"]),
            risk_level=item["risk_level"],
            builtin=bool(item["builtin"]),
            source_page=str(item.get("source_page", ""))))
    return entries
