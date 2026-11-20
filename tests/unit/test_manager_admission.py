"""manager._admit_memory 直测（评审 PR#2：此前零直测）。

契约：
- 非 macOS（IS_MACOS=False）→ 返回 None，不做任何账本检查；
- 时序：switch() 对已驻留目标先返回 already_active，不触发准入
  （Blocking-2：账本不得把已加载模型当新负载误拒）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from inferfabric.manager import ModelManager


def _mgr(active=(), models=None):
    mgr = ModelManager.__new__(ModelManager)
    mgr.state = SimpleNamespace(get_active_services=lambda: list(active))
    mgr._models = models if models is not None else {}
    return mgr


def test_admit_memory_returns_none_off_macos(monkeypatch):
    from inferfabric.platform import probe
    monkeypatch.setattr(probe, "IS_MACOS", False)
    mgr = _mgr()
    model = SimpleNamespace(name="m", mem_baseline_mb=8192, peak_vram_mb=0)
    assert mgr._admit_memory(model) is None


def test_admit_memory_returns_none_when_no_baseline(monkeypatch):
    from inferfabric.platform import probe
    monkeypatch.setattr(probe, "IS_MACOS", True)
    mgr = _mgr()
    model = SimpleNamespace(name="m", mem_baseline_mb=0, peak_vram_mb=0)
    assert mgr._admit_memory(model) is None


def test_switch_already_active_skips_admission(monkeypatch):
    """Blocking-2 回归：已驻留 + 低预算 → already_active，而非
    memory_insufficient（准入闸在 already-running 检查之后）。"""
    from inferfabric.platform import probe

    monkeypatch.setattr(probe, "IS_MACOS", True)
    monkeypatch.setattr(probe, "budget_mb", lambda: 1)   # 极小预算
    model = SimpleNamespace(name="qwen", is_vllm=False, gpu_role="none",
                            mem_baseline_mb=8192, peak_vram_mb=0)
    mgr = _mgr(active=("qwen",), models={"qwen": model})

    res = mgr.switch("qwen")
    assert res["status"] == "already_active"


# ── 终审 F4：受管闸与真实状态/锁串行 ─────────────────────────────

def _managed_model():
    return SimpleNamespace(
        name="pilot", is_vllm=False, gpu_role="exclusive",
        mem_baseline_mb=8192, peak_vram_mb=24000,
        managed_by_pack={"marker_version": 1, "pack_id": "llm.pilot"})


class _FakeState:
    def __init__(self, gpu_mode="idle"):
        self.gpu_mode = gpu_mode
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v

    def set_multi(self, d):
        self._d.update(d)

    def add_history(self, *a, **k):
        pass

    def get_active_services(self):
        return []


def test_switch_active_managed_pack_returns_already_active(monkeypatch):
    """已驻留受管包再 switch：必须 already_active（真实状态），不得被
    受管闸拦截成 resource_busy / runtime_unavailable。"""
    from inferfabric.packs.managed_runtime import set_managed_runtime

    class _RT:
        def check_start(self, *a, **k):
            raise AssertionError("已驻留模型不得再过受管闸")

    set_managed_runtime(_RT())
    try:
        model = _managed_model()
        mgr = _mgr(active=("pilot",), models={"pilot": model})
        res = mgr.switch("pilot")
        assert res["status"] == "already_active", res
    finally:
        set_managed_runtime(None)


def test_managed_gpu_start_rechecked_serialized_under_gpu_lock(monkeypatch):
    """GPU 包受管启动：idle 检查必须与启动在同一把 GPU 锁内串行 ——
    预检 1 次 + 锁内复核 1 次，复核先于部署；锁内复核拒绝 → 绝不部署。"""
    from inferfabric.platform import probe
    from inferfabric.packs.managed_runtime import (
        PackRuntimeError, set_managed_runtime)
    monkeypatch.setattr(probe, "IS_MACOS", False)

    model = _managed_model()
    models = {"pilot": model}
    monkeypatch.setattr("inferfabric.manager.load_models",
                        lambda d: models)
    events = []

    class _RT:
        def __init__(self):
            self.n = 0

        def check_start(self, m, **k):
            self.n += 1
            events.append(f"check{self.n}")
            if self.n >= 2:
                raise PackRuntimeError("resource_busy", "gpu busy under lock")

    class _Lock:
        def acquire(self):
            events.append("lock")
            return True

        def release(self):
            events.append("release")

    mgr = ModelManager.__new__(ModelManager)
    mgr.state = _FakeState(gpu_mode="idle")
    mgr._models = models
    mgr._lock = _Lock()
    mgr._lifecycle = SimpleNamespace(
        _deploy_model=lambda m, mode: (events.append("deploy"),
                                       {"status": "switched"})[1])
    mgr._gpu_state = SimpleNamespace(
        _scan_actual_services=lambda: [],
        _scan_port_owners=lambda: {})
    mgr._device_telemetry = None
    mgr.models_dir = "/nonexistent"
    mgr._proc = MagicMock()

    rt = _RT()
    set_managed_runtime(rt)
    try:
        res = mgr.switch("pilot")
        # 预检 + 锁内复核；复核拒绝 → 绝不部署
        assert events == ["check1", "lock", "check2", "release"], events
        assert res["status"] == "error"
        assert res.get("reason_code") == "resource_busy"
        assert "deploy" not in events
    finally:
        set_managed_runtime(None)


def test_managed_gpu_start_passes_recheck_then_deploys(monkeypatch):
    """锁内复核通过 → 正常部署（串行不误伤空闲路径）。"""
    from inferfabric.platform import probe
    from inferfabric.packs.managed_runtime import set_managed_runtime
    monkeypatch.setattr(probe, "IS_MACOS", False)

    model = _managed_model()
    models = {"pilot": model}
    monkeypatch.setattr("inferfabric.manager.load_models",
                        lambda d: models)
    events = []

    class _RT:
        def check_start(self, m, **k):
            events.append("check")

    class _Lock:
        def acquire(self):
            return True

        def release(self):
            events.append("release")

    mgr = ModelManager.__new__(ModelManager)
    mgr.state = _FakeState(gpu_mode="idle")
    mgr._models = models
    mgr._lock = _Lock()
    mgr._lifecycle = SimpleNamespace(
        _deploy_model=lambda m, mode: (events.append("deploy"),
                                       {"status": "switched"})[1])
    mgr._gpu_state = SimpleNamespace(
        _scan_actual_services=lambda: [],
        _scan_port_owners=lambda: {})
    mgr._device_telemetry = None
    mgr.models_dir = "/nonexistent"
    mgr._proc = MagicMock()

    set_managed_runtime(_RT())
    try:
        res = mgr.switch("pilot")
        assert res["status"] == "switched", res
        assert events == ["check", "check", "deploy", "release"], events
    finally:
        set_managed_runtime(None)
