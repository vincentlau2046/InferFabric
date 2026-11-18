"""manager._admit_memory 直测（评审 PR#2：此前零直测）。

契约：
- 非 macOS（IS_MACOS=False）→ 返回 None，不做任何账本检查；
- 时序：switch() 对已驻留目标先返回 already_active，不触发准入
  （Blocking-2：账本不得把已加载模型当新负载误拒）。
"""
from types import SimpleNamespace

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
