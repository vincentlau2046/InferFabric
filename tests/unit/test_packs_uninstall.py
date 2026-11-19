"""卸载状态机/池确认/503/零残留 — PK2（Task 10）。

契约（计划 Interfaces）：
- 状态机：installed → disabling_route → draining → stopping_process_group
  → removing_generated_config → purging_pool_candidates →
  deleting_pack_directory → gateway_reload → removed（spec §7.2）。
- preview_uninstall(pack_id) → {"affected_pools": [...]}（kind=local 且
  model_name 属该包的池候选）。
- 卸载语义（spec §7.2 裁决）：受影响池非空且未确认 → 409 带候选列表；
  disabling_route 先于一切删除（新请求 503）；drain 上限 10s 后强杀；
  删包自有目录，不碰用户模型/其他包。
- HTTP：GET /admin/packs/uninstall-preview?pack_id=；
  POST /admin/packs/uninstall {pack_id, confirmed}（旧 {name} 流程保留，
  body 分流同 Task 8 install）。
"""

import threading
import time
from pathlib import Path

import pytest
import yaml

from inferfabric.packs.store import PackRecord, PackStore
from inferfabric.packs.supervisor import PackSupervisor
from inferfabric.packs.uninstaller import PackUninstaller

PID = "llm.qwen3-4b-instruct-2507"
MODEL_ID = "qwen3-4b-instruct-2507"


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "packs"
    pack_dir = root / PID / "1.0.0"
    (pack_dir / "models").mkdir(parents=True)
    (pack_dir / "models" / "model").write_bytes(b"M")
    (pack_dir / "runtime").mkdir()
    (pack_dir / "runtime" / "engine").write_bytes(b"E")
    # 兄弟包 + 用户模型（不受影响断言）
    (root / "llm.sibling" / "1.0.0").mkdir(parents=True)
    (root / "llm.sibling" / "1.0.0" / "keep").write_text("x")
    models_d = tmp_path / "models.d"
    models_d.mkdir()
    y = models_d / "pack-qwen3-4b-instruct-2507-1-0-0.yaml"
    y.write_text(yaml.safe_dump({
        "name": "pack-qwen3-4b-instruct-2507-1-0-0",
        "type": "llama_cpp", "model_type": "llm",
        "managed_by_pack": {"pack_id": PID, "pack_version": "1.0.0"},
        "llama_cpp": {"model_path": str(pack_dir / "models" / "model")},
    }, sort_keys=False))
    user_y = models_d / "user-imported.yaml"
    user_y.write_text("name: user-imported\ntype: llama_cpp\n")
    store = PackStore(root / "state.json")
    store.set(PackRecord(pack_id=PID, pack_version="1.0.0",
                         status="installed", stage="installed", error=None,
                         sandbox_degraded=False, installed_at=1))

    class FakeRegistry:
        # 真实 PoolRegistry 契约：raw_config()（PK4 生产冒烟回归）
        def __init__(self):
            self.raw = {"pools": {"fast": {"candidates": [{
                "id": "c1", "kind": "local", "model_name": MODEL_ID}]}}}

        def raw_config(self):
            return self.raw

        def purge_candidates(self, predicate):
            hit = removed = 0
            for p in (self.raw.get("pools") or {}).values():
                cands = p.get("candidates") or []
                kept = [c for c in cands if not predicate(c)]
                removed += len(cands) - len(kept)
                p["candidates"] = kept
            return removed, 2

    events = []

    def manifest_for(pack_id):
        class M:
            model_id = MODEL_ID
        return M() if pack_id == PID else None

    live = {"alive": True}
    uni = PackUninstaller(
        root=root, store=store, models_d=models_d,
        supervisor=PackSupervisor(drain_timeout_s=0.3, poll_interval_s=0.02),
        manifest_for=manifest_for,
        registry=FakeRegistry(),
        disable_route_fn=lambda name: events.append(("disable", name)),
        stop_pack_fn=lambda name: events.append(("stop", name)) or
        live.__setitem__("alive", False) or None,
        is_alive_fn=lambda names: live["alive"],
        pgid_fn=lambda: 424242,
        reload_fn=lambda: events.append(("reload", None)))
    return uni, events, root, models_d, store


def test_preview_lists_affected_pool_candidates(env):
    uni, *_ = env
    prev = uni.preview_uninstall(PID)
    assert prev["affected_pools"] == [
        {"pool": "fast", "candidate_id": "c1", "model_name": MODEL_ID}]


def test_uninstall_confirmed_full_flow(env):
    uni, events, root, models_d, store = env
    code, payload = uni.uninstall(PID, confirmed=True)
    assert code == 200 and payload["status"] == "removed"
    # 顺序：disable → stop → reload，且先于删除
    assert events[0] == ("disable", MODEL_ID)
    assert ("stop", MODEL_ID) in events
    assert events[-1] == ("reload", None)
    assert not (root / PID).exists()                    # 整目录删除
    assert (root / "llm.sibling" / "1.0.0" / "keep").exists()  # 兄弟包不动
    assert not list(models_d.glob("pack-qwen3-4b*"))    # 受控 YAML 删除
    assert (models_d / "user-imported.yaml").exists()   # 用户模型不动
    assert store.get(PID) is None                       # 状态移除


def test_uninstall_without_confirm_409_with_pools(env):
    uni, *_ = env
    code, payload = uni.uninstall(PID)
    assert code == 409
    assert payload["affected_pools"] == [
        {"pool": "fast", "candidate_id": "c1", "model_name": MODEL_ID}]
    # 未确认不产生任何删除
    assert (uni.root / PID).exists()


def test_uninstall_without_pools_needs_no_confirm(env, tmp_path=None):
    uni, events, root, models_d, store = env
    empty = type(uni._registry)()
    empty.raw = {"pools": {"fast": {"candidates": []}}}   # 空池：无池引用 → 无需确认
    uni._registry = empty
    code, payload = uni.uninstall(PID)
    assert code == 200 and payload["status"] == "removed"


def test_uninstall_drain_new_requests_get_503_window(env):
    # Review Focus 3：disabling_route 后、目录删除前，路由已禁用（新请求 503
    # 的判定依据），而文件还在 — 禁用先于删除的顺序由本测试钉住
    uni, events, root, models_d, store = env
    uni._stop_pack_fn = lambda name: time.sleep(0.2)   # 拉长 drain 窗口
    result = {}

    def run():
        result["res"] = uni.uninstall(PID, confirmed=True)

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 2
    while time.time() < deadline and not events:
        time.sleep(0.01)
    assert ("disable", MODEL_ID) in events             # 已禁用
    assert uni.is_route_disabled(MODEL_ID) is True     # 新请求 503 依据
    assert (root / PID).exists()                       # 文件尚未删除
    t.join(timeout=5)
    assert result["res"][0] == 200


def test_uninstall_unknown_pack_404(env):
    uni, *_ = env
    code, _ = uni.uninstall("llm.nope")
    assert code == 404


def test_uninstall_drain_timeout_force_kills(env, monkeypatch):
    uni, events, root, models_d, store = env
    # stop 后仍存活 → drain 超时强杀（supervisor 持 pgid_fn 直接 killpg）
    killed = []
    monkeypatch.setattr("inferfabric.packs.supervisor.os.killpg",
                        lambda pg, sig: killed.append(pg))
    uni._stop_pack_fn = lambda name: None
    uni._is_alive_fn = lambda names: True
    code, payload = uni.uninstall(PID, confirmed=True)
    assert killed == [424242]
    assert code == 200


def test_route_registration_uninstall_preview():
    from inferfabric.proxy.handler import _GET_ROUTES
    assert "/admin/packs/uninstall-preview" in _GET_ROUTES


def test_uninstall_fallback_kill_tolerates_pgid_none(env):
    # 终审 deferred #2a：pgid_fn 返回 None 时兜底强杀不得 TypeError 中断卸载
    uni, events, root, models_d, store = env
    uni._pgid_fn = lambda: None
    uni._is_alive_fn = lambda names: True   # 永不停止 → drain 超时走兜底分支
    code, payload = uni.uninstall(PID, confirmed=True)
    assert code == 200 and payload["status"] == "removed"
    assert not (root / PID).exists()


def test_disabled_cleared_after_uninstall(env):
    # 终审 deferred #2b：禁用集生命周期归一 — 卸载完成即解除
    uni, events, root, models_d, store = env
    code, _ = uni.uninstall(PID, confirmed=True)
    assert code == 200
    assert uni.is_route_disabled(MODEL_ID) is False
