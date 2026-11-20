"""PackStore 状态持久化与磁盘对账 — PK1（Task 6）。

包状态唯一记录：status ∈ {available, preflight, ..., installed, failed,
degraded, removed}（阶段名见 installer.INSTALL_STAGES；failed/degraded 为
终态标记）。对账（reconcile）兜底状态-磁盘不一致：安装进程任意阶段被杀后，
下一次状态读取可解释并修正（计划 Review Focus 1）。
"""

import json
import os
from dataclasses import dataclass, asdict, replace


@dataclass(frozen=True)
class PackRecord:
    pack_id: str
    pack_version: str
    status: str          # INSTALL_STAGES 之一或 failed/degraded/removed
    stage: str
    error: str | None
    sandbox_degraded: bool
    installed_at: int | None
    # 受管所有权（阶段 1 Task 3）：None = 旧记录（无所有权段）；
    # {"marker_version": 1, "role": "owned"|"external", ...}
    ownership: dict | None = None


class PackStore:
    """JSON 持久化；路径注入（生产 ~/.inferfabric/packs/state.json）。"""

    def __init__(self, path):
        self._path = path

    def _read(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        os.makedirs(os.path.dirname(str(self._path)) or ".", exist_ok=True)
        part = f"{self._path}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(part, self._path)

    def set(self, rec: PackRecord) -> None:
        data = self._read()
        data[rec.pack_id] = asdict(rec)
        self._write(data)

    def get(self, pack_id: str) -> PackRecord | None:
        d = self._read().get(pack_id)
        return PackRecord(**d) if d else None

    def all(self) -> list[PackRecord]:
        return [PackRecord(**d) for d in self._read().values()]

    def remove(self, pack_id: str) -> None:
        data = self._read()
        if pack_id in data:
            del data[pack_id]
            self._write(data)

    def update(self, pack_id: str, **fields) -> None:
        cur = self.get(pack_id)
        if cur is None:
            raise KeyError(f"pack {pack_id!r} not in store")
        self.set(replace(cur, **fields))


def reconcile(store: PackStore, root) -> list[str]:
    """状态-磁盘对账；返回被修正的 pack_id 列表。

    - 记录 installed 但目录缺失 → failed（stale）
    - 目录存在（非 _staging）但无记录 → failed（unknown_origin）
    """
    root_exists = root.exists()
    fixed: list[str] = []
    on_disk: set[str] = set()
    if root_exists:
        for child in root.iterdir():
            if child.name == "_staging" or not child.is_dir():
                continue
            on_disk.add(child.name)
    for rec in store.all():
        if rec.status == "installed" and rec.pack_id not in on_disk:
            store.update(rec.pack_id, status="failed",
                         error="stale: installed record without pack directory")
            fixed.append(rec.pack_id)
    recorded = {r.pack_id for r in store.all()}
    for name in sorted(on_disk - recorded):
        store.set(PackRecord(pack_id=name, pack_version="0.0.0",
                             status="failed", stage="reconcile",
                             error="unknown_origin: pack directory without "
                                   "record (kept for diagnosis per spec "
                                   "§7.3; remove via uninstall)",
                             sandbox_degraded=False, installed_at=None))
        fixed.append(name)
    return fixed
