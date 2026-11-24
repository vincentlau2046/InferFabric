"""PackUninstaller — 卸载状态机 — PK2（Task 10）。

状态机（spec §7.2，2026-11-15 裁决；purge 前移 2026-11-24 评审裁决）：
  installed → disabling_route
  → purging_pool_candidates（校验闸：整份 strict 校验拒绝 → 解除禁用
    + 400 原样返回，零删除副作用）
  → draining → stopping_process_group
  → removing_generated_config
  → deleting_pack_directory → gateway_reload → removed

- 被模型池引用：确认框列候选（preview/409 带列表），确认后自动清除。
- disabling_route 先于一切删除：路由禁用即新请求 503 的判定依据（handler
  经 is_route_disabled 查询）；drain 上限 10s（supervisor），超时强杀进程组。
- 只删包自有 runtime/模型/受控 YAML/状态；不碰用户导入模型与其他包。
"""

import shutil
import threading
from pathlib import Path

import yaml

from inferfabric.packs.supervisor import PackSupervisor

DRAIN_TIMEOUT_S = 10.0


def _noop(*a, **k) -> None:
    return None


class PackUninstaller:
    def __init__(self, root, store, models_d, supervisor=None,
                 manifest_for=None, registry=None,
                 disable_route_fn=None, stop_pack_fn=None, is_alive_fn=None,
                 pgid_fn=None, reload_fn=None, force_kill=None):
        self.root = Path(root)
        self._store = store
        self._models_d = Path(models_d)
        self._supervisor = supervisor or PackSupervisor(
            drain_timeout_s=DRAIN_TIMEOUT_S)
        self._manifest_for = manifest_for or (lambda pid: None)
        self._registry = registry    # 需 raw_config/purge_candidates（真实契约）
        self._disable_route_fn = disable_route_fn or _noop
        self._stop_pack_fn = stop_pack_fn or _noop
        self._is_alive_fn = is_alive_fn or (lambda: False)
        self._pgid_fn = pgid_fn
        self._reload_fn = reload_fn or _noop
        self._force_kill = force_kill
        self._disabled: set[str] = set()
        self._lock = threading.Lock()

    # ── 路由禁用（503 判定依据） ──

    def is_route_disabled(self, model_name: str) -> bool:
        with self._lock:
            return model_name in self._disabled

    # ── 池影响查询 ──

    def _iter_candidates(self):
        """枚举 (pool, candidate_raw)。真实 PoolRegistry 契约：raw_config()
        返回 pools YAML dict（pools→candidates 列表）— 生产冒烟回归（PK4）：
        registry 并无 iter_candidates，测试 fake 曾假设了不存在的接口。"""
        try:
            raw = self._registry.raw_config()
        except AttributeError:
            return
        if isinstance(raw, dict):
            for pool, p in (raw.get("pools") or {}).items():
                if not isinstance(p, dict):
                    continue
                for c in (p.get("candidates") or []):
                    if isinstance(c, dict):
                        yield pool, c

    def _affected_pools(self, model_names: set[str]) -> list[dict]:
        if self._registry is None:
            return []
        rows = []
        for pool, raw in self._iter_candidates():
            if raw.get("kind") == "local" and \
                    raw.get("model_name") in model_names:
                rows.append({"pool": pool,
                             "candidate_id": raw.get("id"),
                             "model_name": raw.get("model_name")})
        return rows

    def preview_uninstall(self, pack_id: str) -> dict:
        manifest = self._manifest_for(pack_id)
        names = {manifest.model_id} if manifest else set()
        return {"affected_pools": self._affected_pools(names)}

    # ── 主流程 ──

    def uninstall(self, pack_id: str, confirmed: bool = False) -> tuple[int, dict]:
        manifest = self._manifest_for(pack_id)
        if manifest is None or not (self.root / pack_id).exists():
            return 404, {"error": f"unknown pack {pack_id!r}"}
        model_names = {manifest.model_id}
        affected = self._affected_pools(model_names)
        if affected and not confirmed:
            return 409, {"affected_pools": affected}

        # disabling_route：先于一切删除（新请求 503 依据）
        with self._lock:
            self._disabled |= model_names
        for name in model_names:
            self._disable_route_fn(name)

        # purging_pool_candidates —— 校验闸前移到一切删除之前（7.11.3）：
        # purge 走 registry 整份 strict 校验，存量非法池（如 max_attempts <
        # 候选数）会 ValueError。此刻仅路由禁用（可逆）——校验拒绝 → 解除
        # 禁用 + 可操作 400，零删除副作用；校验通过才进入 drain/删除。
        if self._registry is not None:
            try:
                self._registry.purge_candidates(
                    lambda raw: raw.get("kind") == "local" and
                    raw.get("model_name") in model_names)
            except ValueError as e:
                with self._lock:
                    self._disabled -= model_names
                return 400, {"error": f"pool purge rejected: {e}",
                             "affected_pools": affected}

        # draining + stopping_process_group（stop/is_alive 均以模型名为对象 —
        # 生产侧由注入回调映射到受控 ModelConfig；与 disable 同一命名空间）
        alive_fn = self._is_alive_fn
        if alive_fn(model_names):
            res = self._supervisor.drain_and_stop(
                pack_id,
                stop_fn=lambda: [self._stop_pack_fn(n) for n in model_names],
                is_alive_fn=lambda: alive_fn(model_names),
                pgid_fn=self._pgid_fn)
            if res.forced_kill is False and not res.stopped and self._pgid_fn:
                # supervisor 无 PGID 不盲杀；本层持有组 ID 时兜底强杀
                pgid = self._pgid_fn()
                if pgid is not None:   # pgid_fn 可能动态返回 None（deferred #2a）
                    if self._force_kill is not None:
                        self._force_kill(pgid)
                    else:
                        import os
                        import signal
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except OSError:
                            pass

        # removing_generated_config（按 managed_by_pack 定位受控 YAML）
        self._remove_managed_yamls(pack_id)

        # deleting_pack_directory（包自有全部版本；零残留连父目录）
        shutil.rmtree(self.root / pack_id, ignore_errors=True)

        # 状态移除 + gateway_reload；卸载完成解除路由禁用（禁用只覆盖
        # 卸载在途窗口 — 禁用集单一事实源在本层，deferred #2b）
        self._store.remove(pack_id)
        with self._lock:
            self._disabled -= model_names
        self._reload_fn()
        return 200, {"pack_id": pack_id, "status": "removed",
                     "affected_pools": affected}

    def _remove_managed_yamls(self, pack_id: str) -> None:
        if not self._models_d.exists():
            return
        for y in self._models_d.glob("*.yaml"):
            try:
                d = yaml.safe_load(y.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and \
                    isinstance(d.get("managed_by_pack"), dict) and \
                    d["managed_by_pack"].get("pack_id") == pack_id:
                y.unlink()
