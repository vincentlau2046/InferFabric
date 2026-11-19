"""网关卸载生产接线 + 请求路径 503 — PK4（Task 1）。

契约（计划 Interfaces）：
- attach_pack_uninstaller(pm)：网关启动装配 — 注入真实回调
  （registry=pm.pool_registry、stop=先 stop_independent 后 stop_service、
  is_alive=status().active_services 交集、pgid_fn=None 绝不盲杀、
  reload=pm.mgr.reload_models），并挂 pm.pack_manager 供请求路径查询。
- is_alive_fn 新契约：uninstaller 传 model_names 集合。
- 503：pack 模型卸载在途（is_model_disabled）→ chat/messages 路径 503
  pack_uninstalling，先于池/本地分发。
"""

import pytest

from inferfabric.packs.store import PackRecord, PackStore
from inferfabric.packs.supervisor import PackSupervisor
from inferfabric.proxy.pack_handlers import (
    PackManager,
    attach_pack_uninstaller,
    reset_pack_manager,
)

PID = "llm.qwen3-4b-instruct-2507"
MODEL_ID = "qwen3-4b-instruct-2507"


class FakeMgr:
    """pm.mgr：记录生命周期调用，status 返回运行态。"""

    def __init__(self, active=()):
        self.active = list(active)
        self.stopped_independent = []
        self.stopped_service = []
        self.reloaded = 0

    def status(self):
        return {"active_services": list(self.active)}

    def stop_independent(self, name):
        self.stopped_independent.append(name)
        return {}

    def stop_service(self, name):
        self.stopped_service.append(name)
        return {}

    def reload_models(self):
        self.reloaded += 1


class FakeRegistry:
    # 真实 PoolRegistry 契约：raw_config()（PK4 生产冒烟回归）
    def __init__(self):
        self.raw = {"pools": {"fast": {"candidates": [{
            "id": "c1", "kind": "local", "model_name": MODEL_ID}]}}}

    def raw_config(self):
        return self.raw

    def purge_candidates(self, predicate):
        removed = 0
        for p in (self.raw.get("pools") or {}).values():
            cands = p.get("candidates") or []
            kept = [c for c in cands if not predicate(c)]
            removed += len(cands) - len(kept)
            p["candidates"] = kept
        return removed, 2


class FakePM:
    """生产 GatewayState 的最小替身：mgr + pool_registry。"""

    def __init__(self):
        self.mgr = FakeMgr(active=[MODEL_ID])
        self.pool_registry = FakeRegistry()


@pytest.fixture()
def pack_manager(tmp_path, monkeypatch):
    """隔离的 PackManager 单例（不触真实 ~/.inferfabric）。"""
    from inferfabric.proxy import pack_handlers
    monkeypatch.setenv("MACFABRIC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MACFABRIC_MODELS_DIR", str(tmp_path / "models.d"))
    reset_pack_manager()
    mgr = PackManager(root=tmp_path / "packs",
                      models_d=tmp_path / "models.d",
                      catalog_path=_catalog(tmp_path))
    # 生产单例经 get_pack_manager() 创建时无 reload_gateway（未知网关回调）；
    # 安装路径的热加载只能靠 attach_pack_uninstaller 装配注入 — 不在此预置。
    monkeypatch.setattr(pack_handlers, "_manager", mgr)   # 单例指向测试实例
    yield mgr
    reset_pack_manager()


def _catalog(tmp_path):
    import hashlib
    import yaml
    from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML
    from inferfabric.packs.schema import parse_manifest
    m = parse_manifest(MINIMAL_MANIFEST_YAML).to_dict()
    m["resources"]["download_mb"] = 1
    m["resources"]["disk_mb"] = 1
    files = {"model": b"M", "engine": b"E"}
    m["artifacts"] = [
        {"id": aid, "kind": "model" if aid == "model" else "runtime",
         "source": f"https://x/{aid}",
         "sha256": hashlib.sha256(data).hexdigest()}
        for aid, data in files.items()]
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump({"packs": [{
        "pack_id": m["pack_id"], "display_name": "T", "summary": "s",
        "tags": [], "risk_level": "verified", "builtin": False,
        "manifest": m}]}, sort_keys=False))
    return p


def test_attach_wires_real_callbacks_and_mounts_pm(pack_manager):
    pm = FakePM()
    attach_pack_uninstaller(pm)
    assert pm.pack_manager is pack_manager
    assert pack_manager._uninstaller is not None


def test_attach_wires_install_path_reload_gateway(pack_manager):
    # 回归钉住 2026-11-16 沙箱 e2e 缺口：get_pack_manager 单例从未接线
    # reload_gateway → 装包后 _reload() 静默空转 → /models 无新模型
    # （Unknown model）。装配必须把 pm.mgr.reload_models 注入安装路径。
    pm = FakePM()
    attach_pack_uninstaller(pm)
    assert pm.pack_manager._reload_gateway is not None
    before = pm.mgr.reloaded
    pm.pack_manager._reload()
    assert pm.mgr.reloaded == before + 1


def test_uninstall_full_flow_hits_production_callbacks(pack_manager, tmp_path):
    # 包目录 + installed 记录就位 → 卸载走真实回调链
    pm = FakePM()
    attach_pack_uninstaller(pm)
    mgr = pm.pack_manager
    pack_dir = tmp_path / "packs" / PID / "1.0.0"
    pack_dir.mkdir(parents=True)
    mgr.store.set(PackRecord(pack_id=PID, pack_version="1.0.0",
                             status="installed", stage="installed",
                             error=None, sandbox_degraded=False,
                             installed_at=1))
    code, payload = mgr.uninstall(PID, confirmed=True)
    assert code == 200 and payload["status"] == "removed"
    # stop 以模型名（served name）为对象 → lifecycle stop_independent 命中
    assert pm.mgr.stopped_independent == [MODEL_ID]
    assert pm.mgr.reloaded >= 1
    assert pm.pool_registry.raw == {"pools": {"fast": {"candidates": []}}}
    assert not pack_dir.exists()                      # 零残留


def test_is_alive_reads_active_services(pack_manager):
    # 运行中（active_services 交集非空）→ drain 分支触发 stop
    pm = FakePM()
    pm.mgr.active = [MODEL_ID]
    attach_pack_uninstaller(pm)
    assert pm.pack_manager._uninstaller._is_alive_fn({MODEL_ID}) is True
    assert pm.pack_manager._uninstaller._is_alive_fn({"other"}) is False


def test_is_alive_swallows_status_error(pack_manager):
    pm = FakePM()
    pm.mgr.status = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    attach_pack_uninstaller(pm)
    assert pm.pack_manager._uninstaller._is_alive_fn({MODEL_ID}) is False


def test_pgid_fn_is_none_never_blind_kill(pack_manager):
    pm = FakePM()
    attach_pack_uninstaller(pm)
    assert pm.pack_manager._uninstaller._pgid_fn is None


def test_chat_503_when_pack_model_disabled(pack_manager, monkeypatch):
    # 请求路径：is_model_disabled → 503 pack_uninstalling，先于本地分发
    from inferfabric.proxy import chat_handlers

    class FakeHandler:
        def __init__(self):
            self.sent = None

        def _send_json(self, payload, code=200):
            self.sent = (code, payload)

    pm = FakePM()
    attach_pack_uninstaller(pm)
    monkeypatch.setattr(chat_handlers.time, "monotonic", lambda: 0.0)
    h = FakeHandler()
    data = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}
    # 卸载在途：禁用集合里
    pm.pack_manager._uninstaller._disabled.add(MODEL_ID)
    chat_handlers.handle_chat(h, pm, data)
    assert h.sent is not None and h.sent[0] == 503
    assert h.sent[1].get("error") == "pack_uninstalling"
    # 未禁用 → 不拦截（会走到后续分发；此处只断言不是 503 pack_uninstalling）
    pm.pack_manager._uninstaller._disabled.discard(MODEL_ID)
    h2 = FakeHandler()
    try:
        chat_handlers.handle_chat(h2, pm, data)
    except Exception:
        pass   # 后续分发在 fake pm 上可能抛错 — 只要不 503 即可
    if h2.sent is not None:
        assert not (h2.sent[0] == 503 and
                    h2.sent[1].get("error") == "pack_uninstalling")


def test_install_stage_progress_wired(pack_manager):
    # installer 进度回调 → _in_progress 实时推进（App 阶段文案数据源）
    mgr = pack_manager
    with mgr._progress_lock:
        mgr._in_progress[PID] = "preflight"
    mgr.installer._report(PID, "downloading")
    with mgr._progress_lock:
        assert mgr._in_progress[PID] == "downloading"
    # 不在进行中的包不得复活条目（卸载/完成后回调迟到）
    mgr.installer._report("llm.other", "downloading")
    with mgr._progress_lock:
        assert "llm.other" not in mgr._in_progress


def test_status_payload_clears_stale_error_while_in_progress(pack_manager):
    # PK4 生产验收发现：失败后重装，旧记录的 error/stage 泄漏到进行中状态
    # （下载中却显示上次的 download_error:URLError）
    from inferfabric.packs.store import PackRecord
    mgr = pack_manager
    mgr.store.set(PackRecord(pack_id=PID, pack_version="1.0.0",
                             status="failed", stage="downloading",
                             error="download_error:URLError",
                             sandbox_degraded=False, installed_at=None))
    with mgr._progress_lock:
        mgr._in_progress[PID] = "verifying"
    p = {x["pack_id"]: x for x in mgr.status_payload()["packs"]}[PID]
    assert p["status"] == "verifying"
    assert p["stage"] == "verifying"
    assert p["error"] is None
