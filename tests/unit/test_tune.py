# tests/unit/test_tune.py
"""iff tune 场景预设调优 — 引擎无关编排器单元测试。

覆盖：preview diff / 白名单过滤 / 引擎上限钳制 / KV 池顶校验 / MTP 保护 /
baseline 快照 / default 回滚 / YAML 写回 / restart 走 GPU 状态机 / 引擎无感知。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric import tune
from inferfabric import config as cfgmod
from inferfabric.config import load_models, ModelConfig
from inferfabric.engine_adapter import get_adapter, register
from inferfabric.engine_adapter.base import EngineAdapter


# ── fixtures ─────────────────────────────────────────────────────

FIXTURE = """\
name: Qwen38-27B-TXT
description: 测试模型
type: ninfer
quantization: NVFP4
peak_vram_mb: 30500
ninfer:
  served_name: Qwen38-27B-TXT
  weight_path: ~/models/x.ninfer
  docker_image: ninfer:auto
  port: 8007
  container_name: iff-test-ninfer
  max_context: 262144
  kv_capacity: 600000
  max_concurrency: 6
  default_max_tokens: 32000
  prefill_chunk: 2048
  enable_mtp: true
  draft_tokens: 1
presets:
  # 内联场景（与侧车同名 → 侧车 small 覆盖本定义，仅验证覆盖语义）
  small:
    max_concurrency: 1
    max_context: 65536
  short-parallel:
    max_concurrency: 8
    max_context: 98304
    default_max_tokens: 16384
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
  big:
    max_concurrency: 5
    max_context: 204800
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 2
  wild:
    max_concurrency: 9
    weight_path: /evil
    prefill_chunk: 1000
    max_context: 98304
    default_max_tokens: 16384
    enable_mtp: true
    draft_tokens: 1
  # 池顶 < 固定 kv_capacity → 「满载余量」提示（非超卖）
  tiny:
    max_concurrency: 2
    max_context: 16384
    default_max_tokens: 8192
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
  # per-scenario kv：场景显式 kv_capacity → 校验/超卖按新值算
  kv-heavy:
    max_concurrency: 5
    max_context: 204800
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
    kv_capacity: 900000
  # kv 低于窗口 → 引擎启动必失败（❌ blocking）
  kv-under-window:
    max_concurrency: 2
    max_context: 200000
    default_max_tokens: 8192
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
    kv_capacity: 100000
  # kv=0 = auto 哨兵：引擎自取池顶——不按 min 钳制成 1、不触发 kv<窗口 ❌
  kv-auto:
    max_concurrency: 5
    max_context: 262144
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 3
    kv_capacity: 0
"""


# 侧车（手动配置单一真相源）：small 场景覆盖模型 YAML 内联同名场景。
# kv_capacity 现为可选场景字段：场景写它即覆盖模型 YAML 固定池，不写=沿用模型值。
SIDECAR = """\
Qwen38-27B-TXT:
  small:
    max_concurrency: 4
    max_context: 204800
    default_max_tokens: 32768
    prefill_chunk: 2048
    enable_mtp: true
    draft_tokens: 1
"""


@pytest.fixture
def model(tmp_path, monkeypatch):
    y = tmp_path / "Qwen38-27B-TXT.yaml"
    y.write_text(FIXTURE)
    (tmp_path / "scenarios.yaml").write_text(SIDECAR)
    applied = tmp_path / "active_scenarios.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    models = load_models(tmp_path)
    return models["Qwen38-27B-TXT"]


def _applied_yaml(tmp_path):
    """应用层文件当前内容（YAML dict；不存在为 {}）。"""
    import yaml as _y
    f = tmp_path / "active_scenarios.yaml"
    return _y.safe_load(f.read_text()) if f.exists() else {}


class FakeState:
    def __init__(self):
        self.switching = ""

    def clear_manual_stop(self, name):
        pass

    def record_manual_stop(self, name):
        pass

    def set(self, k, v):
        self.switching = v


class FakeMgr:
    """模拟 ModelManager：adapter 经 _proc.mgr 看到的就是自己。"""
    def __init__(self):
        self._proc = self
        self.mgr = self
        self.state = FakeState()
        self.calls = []

    def stop_service(self, name):
        self.calls.append(("stop", name))
        return {"status": "stopped", "model": name}

    def switch(self, name):
        self.calls.append(("switch", name))
        return {"status": "switched", "message": "ok"}


# ── preview ──────────────────────────────────────────────────────

def test_preview_short_parallel_diff(model):
    p = tune.preview(model, "short-parallel")
    assert p["preset"] == "short-parallel"
    assert p["before"]["max_concurrency"] == 6
    assert p["after"]["max_concurrency"] == 8
    assert p["after"]["max_context"] == 98304
    # 此场景未写 kv_capacity → 不进 diff（沿用模型 YAML 固定 600000）
    assert "kv_capacity" not in p["after"], "场景未写 kv → after 不应含 kv（沿用模型 YAML 值）"
    # 池顶 = 8×⌈98304/64⌉×64 = 786432；沿用固定 kv 600000 → 超卖 (786432−600000)/786432 = 23.7%
    assert not [i for i in p["issues"] if i.startswith("❌")]
    assert any("超卖 23.7%" in i for i in p["issues"])


def test_sidecar_scenario_overrides_inline(model):
    """侧车 scenarios.yaml 的同名场景覆盖模型 YAML 内联定义。"""
    assert model.presets["small"]["max_concurrency"] == 4  # 侧车值（非内联 1）
    p = tune.preview(model, "small")
    assert p["after"]["max_concurrency"] == 4
    assert p["after"]["max_context"] == 204800
    # 池顶 = 4×204800 = 819200；超卖 (819200−600000)/819200 = 26.8%
    assert any("超卖 26.8%" in i for i in p["issues"])


def test_preview_tiny_shows_surplus(model):
    """池顶 < 固定 kv_capacity → 展示满载余量（非超卖）。"""
    p = tune.preview(model, "tiny")
    # 池顶 = 2×16384 = 32768 < kv 600000 → 余量 (1−32768/600000) = 94.5%
    assert not [i for i in p["issues"] if i.startswith("❌")]
    assert any("满载余量 94.5%" in i for i in p["issues"])


def test_preview_scenario_kv_override(model):
    """场景显式 kv_capacity → 进入 diff，超卖按新 kv 计算（per-scenario KV 语义）。"""
    p = tune.preview(model, "kv-heavy")
    assert p["after"]["kv_capacity"] == 900000
    # 池顶 = 5×⌈204800/64⌉×64 = 1024000；kv 900000 → 超卖 (1024000−900000)/1024000 = 12.1%
    assert any("超卖 12.1%" in i for i in p["issues"])


def test_preview_kv_below_window_blocking(model):
    """kv_capacity < max_context → ❌blocking（引擎启动必失败，需拦住）。"""
    p = tune.preview(model, "kv-under-window")
    assert any(i.startswith("❌") and "kv_capacity" in i and "max_context" in i
               for i in p["issues"])


def test_preview_kv_zero_auto_sentinel(model):
    """kv_capacity=0 是 auto 哨兵：不按 min 钳制成 1，也不触发 kv<窗口 ❌。

    回归：engine_caps 曾给 kv_capacity 定 min=1，VL 场景 kv 一律 0 被抬成 1，
    再被 validate_scenario 误报 ❌ kv 1 < 窗口 → apply 被 TuneError 拦死。
    """
    p = tune.preview(model, "kv-auto")
    assert p["after"]["kv_capacity"] == 0                      # 未被抬成 1
    assert not any("kv_capacity" in n for n in p["clamp_notes"])
    assert not [i for i in p["issues"] if i.startswith("❌")]


def test_apply_kv_zero_sentinel_not_blocked(model):
    """apply(dry) 不因 kv=0 误报 ❌ 而 TuneError（场景可正常应用）。"""
    r = tune.apply(model, "kv-auto", dry=True)
    assert r["status"] == "preview"


def test_preview_wild_clamp_and_whitelist(model):
    """越界钳制 + 白名单外字段忽略（weight_path 不应污染）。"""
    p = tune.preview(model, "wild")
    assert p["after"]["max_concurrency"] == 8        # 9 → 8
    assert p["after"]["prefill_chunk"] == 896        # 1000 → 128 倍数
    assert "weight_path" not in p["after"]
    assert any("钳制" in n or "上限" in n for n in p["clamp_notes"])


def test_preview_mtp_warning(model):
    p = tune.preview(model, "big")
    assert any("draft" in i.lower() and "实测" in i for i in p["issues"])


def test_preview_unknown_preset(model):
    with pytest.raises(tune.TuneError):
        tune.preview(model, "nope")


def test_preview_unsupported_engine_model(tmp_path, monkeypatch):
    """适配器未声明 scenario_fields → 明确报"不支持"。"""
    class MinimalAdapter:
        engine_type = "minimal"
        scenario_fields = lambda self, m: []   # 继承思路：空白名单=不支持
        engine_caps = lambda self, m: {}
        validate_scenario = lambda self, m, v: []
        restart = lambda self, m: {"status": "ok"}

    register("minimal", MinimalAdapter)

    class Cfg:  # 引擎块
        gpu_mem = 4096
        max_batch = 2

    m = ModelConfig(name="mini", description="d", type="minimal")
    m.mycfg = Cfg()
    monkeypatch.setitem(ModelConfig._ENGINE_ATTR, "minimal", "mycfg")
    m.presets = {"p1": {"max_batch": 4}}
    m.yaml_path = str(tmp_path / "mini.yaml")
    with pytest.raises(tune.TuneError, match="不支持"):
        tune.preview(m, "p1")


# ── dry / apply / write-back ───────────────────────────────────

def test_apply_dry_no_write(model, tmp_path):
    path = Path(model.yaml_path)
    before_bytes = path.read_bytes()
    r = tune.apply(model, "short-parallel", dry=True)
    assert r["status"] == "preview"
    assert path.read_bytes() == before_bytes, "dry 不应写盘"
    assert model.active_preset == ""
    assert _applied_yaml(tmp_path) == {}, "dry 不应写应用层"


def test_apply_no_restart_writes_applied_layer(model, tmp_path):
    """应用 = 写应用层 + 内存；模型 YAML 逐字节不动（工具链只读）。"""
    yaml_bytes_before = Path(model.yaml_path).read_bytes()
    r = tune.apply(model, "short-parallel", restart=False)
    assert r["status"] == "applied_restart_pending"
    assert model.active_preset == "short-parallel"
    assert model.ninfer.max_concurrency == 8
    # 模型 YAML 原样未动
    assert Path(model.yaml_path).read_bytes() == yaml_bytes_before, "模型 YAML 必须保持只读"
    # 应用层条目 = 场景目标值（含 active_preset 标记）
    ap = _applied_yaml(tmp_path)
    assert ap["Qwen38-27B-TXT"]["active_preset"] == "short-parallel"
    assert ap["Qwen38-27B-TXT"]["overrides"]["max_concurrency"] == 8
    assert ap["Qwen38-27B-TXT"]["overrides"]["max_context"] == 98304


def test_apply_restart_goes_through_gpu_state_machine(model, tmp_path):
    """restart 走 stop_service→clear_manual_stop→switch (503 Switch Guard)。"""
    mgr = FakeMgr()
    r = tune.apply(model, "short-parallel", restart=True, mgr=mgr)
    assert r["status"] == "applied"
    assert r["restart"]["status"] == "switched"
    assert mgr.calls == [("stop", "Qwen38-27B-TXT"), ("switch", "Qwen38-27B-TXT")]
    assert mgr.state.switching == "", "switch 后应清空 switching_target"


def test_apply_default_rollback(model, tmp_path):
    """default = 清应用层条目 → 值落回纯模型 YAML（YAML 自始至终未被写过）。"""
    yaml_bytes_before = Path(model.yaml_path).read_bytes()
    tune.apply(model, "short-parallel", restart=False)
    assert model.ninfer.max_concurrency == 8
    r = tune.apply(model, "default", restart=False)
    assert r["status"] == "rolled_back_pending"
    assert r["preset"] == "default"
    assert model.active_preset == ""
    assert model.ninfer.max_concurrency == 6
    assert model.ninfer.kv_capacity == 600000
    # YAML 全程未动；应用层条目已清除
    assert Path(model.yaml_path).read_bytes() == yaml_bytes_before
    assert _applied_yaml(tmp_path) == {}


def test_apply_default_without_applied_entry(model, tmp_path):
    """无条目 + 模型未运行 → 纯 no-op（不写盘不重启，P0-2）：不报错。"""
    yaml_bytes_before = Path(model.yaml_path).read_bytes()
    r = tune.apply(model, "default", restart=False)
    assert r["status"] == "already_default"
    assert r["active_preset"] == ""
    assert "message" in r
    # 模型 YAML 原样；应用层无条目（no-op 不写盘）
    assert Path(model.yaml_path).read_bytes() == yaml_bytes_before
    assert _applied_yaml(tmp_path) == {}


def test_apply_default_drift_restarts_active_model(model, tmp_path):
    """无条目 + 模型运行中 → 重启使手改 YAML 进容器（P0-3）。"""
    class ActiveMgr(FakeMgr):
        active_services = ("Qwen38-27B-TXT",)

    mgr = ActiveMgr()
    r = tune.apply(model, "default", restart=True, mgr=mgr)
    assert r["status"] == "restarted"
    assert r["restart"]["status"] == "switched"
    assert _applied_yaml(tmp_path) == {}, "本来无条目，无需清除"
    assert ("switch", "Qwen38-27B-TXT") in mgr.calls


def test_applied_lock_excludes_concurrent_writer(model, tmp_path):
    """P0-4：写锁临界区内，另一 fd 的非阻塞加锁必须失败（flock 互斥）。"""
    import fcntl
    lock_path = Path(tune.APPLIED_FILE).parent / (tune.APPLIED_FILE.name + ".lock")
    with tune._applied_lock():
        f = lock_path.open("r+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise AssertionError("锁被持有时不应能再次非阻塞获取")
        except (BlockingIOError, PermissionError, OSError):
            pass  # 预期：第一个持有者独占
        finally:
            f.close()


def test_applied_lock_released_after_apply(model, tmp_path):
    """apply 结束（无论重启与否）后锁必须释放，否则后续 tune 永久卡死。"""
    import fcntl
    tune.apply(model, "short-parallel", restart=False)
    lock_path = Path(tune.APPLIED_FILE).parent / (tune.APPLIED_FILE.name + ".lock")
    f = lock_path.open("r+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # 应能立即拿到
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def test_apply_oversell_not_blocked(model, tmp_path):
    """超卖是预期行为（preempt 兜底）——⚠ 告警不阻塞 apply。"""
    r = tune.apply(model, "short-parallel", restart=False)
    assert r["status"] == "applied_restart_pending"
    assert any("超卖" in i for i in r["diff"]["issues"])


def test_apply_restart_rollback_on_failure(model, tmp_path):
    """启动失败 → 还原应用层（此前无条目）+ 内存旧值 + 重启旧配置。"""
    class FailMgr(FakeMgr):
        """第一次 switch 失败（场景配置起不来），回滚重启（第二次）成功。"""
        def switch(self, name):
            self.calls.append(("switch", name))
            if len([c for c in self.calls if c[0] == "switch"]) == 1:
                return {"status": "error", "message": "container crashed"}
            return {"status": "switched"}

    mgr = FailMgr()
    yaml_bytes_before = Path(model.yaml_path).read_bytes()
    r = tune.apply(model, "short-parallel", restart=True, mgr=mgr)
    assert r["status"] == "failed_rolled_back"
    # 模型 YAML 从未被写；应用层已还原（prev=无条目）；内存回到 YAML 值
    assert Path(model.yaml_path).read_bytes() == yaml_bytes_before
    assert _applied_yaml(tmp_path) == {}
    assert model.ninfer.max_concurrency == 6
    assert model.active_preset == ""
    # 还原后确实用旧配置重启过一次
    assert ("switch", "Qwen38-27B-TXT") in mgr.calls


def test_apply_restart_exception_rolls_back(model, tmp_path):
    """restart 抛非 TuneError 异常（状态机崩溃等）→ 统一回滚路径：
    文件层还原 + 内存还原 + 旧配置重启；apply() 不外抛异常（cross-review BUG-1）。"""
    class BoomMgr(FakeMgr):
        def switch(self, name):
            self.calls.append(("switch", name))
            if len([c for c in self.calls if c[0] == "switch"]) == 1:
                raise RuntimeError("GPU state machine exploded")
            return {"status": "switched"}

    mgr = BoomMgr()
    yaml_bytes_before = Path(model.yaml_path).read_bytes()
    r = tune.apply(model, "short-parallel", restart=True, mgr=mgr)  # 不得外抛
    assert r["status"] == "failed_rolled_back"
    assert "RuntimeError" in (r["error"] or "")
    # 文件层已还原（此前无条目）；内存回 YAML 值；模型 YAML 全程未动
    assert _applied_yaml(tmp_path) == {}
    assert model.ninfer.max_concurrency == 6
    assert model.active_preset == ""
    assert Path(model.yaml_path).read_bytes() == yaml_bytes_before


def test_apply_emits_structured_event(model, tmp_path, caplog):
    """场景变更结构化事件：状态变更路径发一行 JSON（参数/池顶/超卖/from→to），
    供后续与指标/请求日志按时间戳做关联分析。"""
    import json
    import logging
    caplog.set_level(logging.INFO, logger="inferfabric.tune")
    tune.apply(model, "short-parallel", restart=False)
    evs = [json.loads(r.getMessage().split("[tune-event] ", 1)[1])
           for r in caplog.records if "[tune-event]" in r.getMessage()]
    assert len(evs) == 1
    ev = evs[0]
    assert ev["model"] == "Qwen38-27B-TXT" and ev["engine"] == "ninfer"
    assert ev["preset"] == "short-parallel"
    assert ev["from_preset"] == "default" and ev["to_preset"] == "short-parallel"
    assert ev["status"] == "applied_restart_pending" and ev["restart"] is None
    # 关键参数 = 最终 live 值
    assert ev["params"]["max_concurrency"] == 8
    assert ev["params"]["max_context"] == 98304
    # 池顶/超卖数值化（= 8×⌈98304/64⌉×64；(786432−600000)/786432 = 23.7%）
    assert ev["pool_top"] == 786432
    assert ev["kv_capacity"] == 600000
    assert ev["oversell_pct"] == 23.7


def test_apply_already_default_emits_no_event(model, tmp_path, caplog):
    """already_default no-op（不写盘不重启）不发事件——无状态变更，无关联分析价值。"""
    import logging
    caplog.set_level(logging.INFO, logger="inferfabric.tune")
    r = tune.apply(model, "default", restart=False)
    assert r["status"] == "already_default"
    assert not [rec for rec in caplog.records if "[tune-event]" in rec.getMessage()]


def test_apply_default_with_entry_restarts_inactive_model(model, tmp_path):
    """D3：有条目（漂移）+ 模型未运行 → 清条目 + 重启（部署 YAML 当前值）。"""
    tune.apply(model, "short-parallel", restart=False)
    assert _applied_yaml(tmp_path).get("Qwen38-27B-TXT", {}).get("active_preset") == "short-parallel"
    mgr = FakeMgr()
    r = tune.apply(model, "default", restart=True, mgr=mgr)
    assert r["status"] == "rolled_back"
    assert r["restart"]["status"] == "switched"
    assert _applied_yaml(tmp_path) == {}, "条目已清除"
    assert model.ninfer.max_concurrency == 6
    assert model.active_preset == ""


# ── 引擎无感知（stub 引擎全流程）────────────────────────────────
# 注意：注册 key 必须唯一（tune-stub），不要占用其他测试文件已有的 "stub" 注册项。

class StubAdapter(EngineAdapter):
    """只实现钩子、字段完全不同的"别家引擎"。"""
    engine_type = "tune-stub"

    def check_health(self, m):
        return "✅"

    def get_context_window(self, m):
        return None

    def validate_config(self, m):
        return []

    def start(self, m):
        return {"status": "ok"}

    def stop(self, m):
        return {"status": "ok"}

    def is_alive(self, m):
        return True

    def scenario_fields(self, m):
        return ["gpu_mem_mib", "max_batch"]

    def engine_caps(self, m):
        return {"max_batch": {"min": 1, "max": 4}}

    def validate_scenario(self, m, v):
        out = []
        if v.get("gpu_mem_mib") and v["gpu_mem_mib"] > 30000:
            out.append("⚠ 显存超 30GB")
        return out

    def restart(self, m):
        return {"status": "ok", "message": "stub restarted"}


register("tune-stub", StubAdapter)


def test_stub_engine_drives_tune_without_importing_ninfer(monkeypatch, tmp_path):
    """编排器对引擎无感知的核心证明：stub 字段驱动 preview + apply。"""
    import yaml as _y
    class StubCfg:
        gpu_mem_mib = 24576
        max_batch = 2

    y = tmp_path / "stub.yaml"
    y.write_text("name: stub\npresets:\n  p1:\n    max_batch: 9\n    gpu_mem_mib: 32768\n")
    m = ModelConfig(name="stub", description="d", type="tune-stub")
    m.mycfg = StubCfg()
    monkeypatch.setitem(ModelConfig._ENGINE_ATTR, "tune-stub", "mycfg")
    m.presets = _y.safe_load(y.read_text())["presets"]
    m.yaml_path = str(y)

    applied = tmp_path / "applied.yaml"
    monkeypatch.setattr(tune, "APPLIED_FILE", applied)
    p = tune.preview(m, "p1")
    assert p["after"]["max_batch"] == 4          # 引擎 caps 钳制
    assert p["after"]["gpu_mem_mib"] == 32768
    assert any("30GB" in i for i in p["issues"])

    r = tune.apply(m, "p1", restart=False)
    assert r["status"] == "applied_restart_pending"
    # 模型 YAML（stub.yaml）原样未动；状态全在应用层
    assert _y.safe_load(y.read_text())["presets"]["p1"]["max_batch"] == 9
    ap = _y.safe_load(applied.read_text())
    assert ap["stub"]["active_preset"] == "p1"
    assert ap["stub"]["overrides"]["max_batch"] == 4
    assert m.mycfg.max_batch == 4


# ── 应用层 overlay（load_models 启动路径）────────────────────────

def test_load_models_applied_overlay(tmp_path, monkeypatch):
    """load_models 默认合并应用层（启动 = YAML + overlay）；include_applied=False = 纯 YAML。"""
    y = tmp_path / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  kv_capacity: 600000\n  max_concurrency: 6\n")
    applied = tmp_path / "active_scenarios.yaml"
    applied.write_text("M:\n  active_preset: short\n  overrides:\n    max_concurrency: 8\n    max_context: 98304\n")
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)

    m = load_models(tmp_path)["M"]
    assert m.ninfer.max_concurrency == 8          # 被 overlay
    assert m.ninfer.max_context == 98304
    assert m.active_preset == "short"
    assert m.ninfer.kv_capacity == 600000          # 未在 overrides 的 YAML 字段原样

    m2 = load_models(tmp_path, include_applied=False)["M"]
    assert m2.ninfer.max_concurrency == 6          # 纯 YAML
    assert m2.active_preset == ""


def test_applied_layer_bad_entry_ignored(tmp_path, monkeypatch):
    """应用层坏条目不阻塞启动——静默回退纯 YAML 值。"""
    y = tmp_path / "M.yaml"
    y.write_text("name: M\ntype: ninfer\nninfer:\n  port: 8007\n  max_concurrency: 6\n")
    applied = tmp_path / "active_scenarios.yaml"
    applied.write_text("M: not-a-dict\n")
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    m = load_models(tmp_path)["M"]
    assert m.ninfer.max_concurrency == 6
    assert m.active_preset == ""