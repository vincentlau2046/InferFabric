"""
unit/infra/test_manager_scenario.py — D1（启动即读）+ D5（展示读磁盘）manager 层测试

测试手法（与 test_manager_force_reset.py 一致）：MagicMock 管理器 + 非绑定类方法调用，
避免构造真实 ModelManager（会触碰 StateDB / ProcessManager / 生产 models.d）。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


def _tmp_models_dir(tmp_path, monkeypatch):
    """tmp models.d + 应用层文件（APPLIED_SCENARIOS_FILE monkeypatch 到 tmp）。"""
    from inferfabric import config as cfgmod
    models_dir = tmp_path / "models.d"
    models_dir.mkdir()
    (models_dir / "M.yaml").write_text(
        "name: M\ntype: ninfer\ngpu_role: exclusive\n"
        "ninfer:\n  port: 8007\n  kv_capacity: 600000\n  max_concurrency: 6\n")
    applied = tmp_path / "active_scenarios.yaml"
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", applied)
    return models_dir, applied


def test_switch_d1_rereads_registry_for_inactive_model(tmp_path, monkeypatch):
    """D1：部署非活跃模型前重读「YAML + 应用层」（长驻代理内存过期场景）。

    即使后续 transition 校验失败、部署未发生，注册表已同步到磁盘值——
    内存 ≠ 磁盘 永远不会再导致「启动的是旧场景」。
    """
    from inferfabric.manager import ModelManager
    from inferfabric.config import load_models
    models_dir, applied = _tmp_models_dir(tmp_path, monkeypatch)

    mgr = MagicMock()
    mgr.models_dir = models_dir
    mgr.active_services = ["s1"]            # M 非活跃 → 触发 D1 重读
    mgr.gpu_mode = "shared"
    mgr._models = load_models(models_dir)   # 内存旧状态（max_concurrency=6）
    assert mgr._models["M"].ninfer.max_concurrency == 6

    # 另一入口（CLI/dashboard）写入应用层
    applied.write_text("M:\n  active_preset: short\n  overrides:\n    max_concurrency: 8\n")

    # shared → exclusive 非法 transition → 部署不发生，但 D1 重读已完成
    r = ModelManager.switch(mgr, "M")
    assert r["status"] == "error"
    # D1 副作用：注册表已同步磁盘（YAML + 应用层）
    assert mgr._models["M"].ninfer.max_concurrency == 8
    assert mgr._models["M"].active_preset == "short"


def test_switch_no_reread_for_active_model(tmp_path, monkeypatch):
    """活跃模型不重读（其漂移由 vLLM drift 检查负责）——注册表对象保持原样。"""
    from inferfabric.manager import ModelManager
    from inferfabric.config import load_models
    models_dir, applied = _tmp_models_dir(tmp_path, monkeypatch)

    mgr = MagicMock()
    mgr.models_dir = models_dir
    mgr.active_services = ["M"]
    mgr.gpu_mode = "idle"
    mgr._models = load_models(models_dir)
    stale_obj = mgr._models["M"]

    r = ModelManager.switch(mgr, "M")
    # 活跃 + 非 vLLM → already_active（未触发重读）
    assert r["status"] == "already_active"
    assert mgr._models["M"] is stale_obj, "活跃模型不得触发注册表重读"


def test_manager_backrefs_facade_for_state_machine_restart(tmp_path, monkeypatch):
    """ModelManager 必须回填 proc.mgr（状态机感知重启钩子，防静默退回基类 stop+start）。

    NInferAdapter.restart 经 getattr(proc, "mgr") 走 stop_service→clear_manual_stop→switch
    （503 Switch Guard / 历史 / 状态同步）；若 facade 上没有回引用，重启会**静默**退化成
    基类 stop()+start()，state.db 与状态机漂移。回归锁死该接线。
    """
    import inferfabric.config as cfgmod
    from inferfabric.manager import ModelManager
    from inferfabric.process_manager import ProcessManager
    # 隔离生产应用层文件（load_models include_applied=True 会读它）
    monkeypatch.setattr(cfgmod, "APPLIED_SCENARIOS_FILE", tmp_path / "active_scenarios.yaml")

    (tmp_path / "M.yaml").write_text(
        "name: M\ntype: ninfer\ngpu_role: exclusive\n"
        "ninfer:\n  port: 8007\n  max_concurrency: 6\n")
    state = MagicMock()
    proc = ProcessManager(state, tmp_path / "logs")
    mgr = ModelManager(models_dir=str(tmp_path), state=state, proc=proc)
    assert proc.mgr is mgr, "facade 必须持有 ModelManager 回引用（restart 钩子依赖）"


def test_switch_d1_reread_failure_keeps_memory_registry(tmp_path, monkeypatch):
    """D1 降级：重读失败（磁盘异常）→ 沿用内存注册表，switch 不因此阻塞（error 日志可见）。"""
    import inferfabric.manager as mgrmod
    from inferfabric.manager import ModelManager
    from inferfabric.config import load_models
    models_dir, applied = _tmp_models_dir(tmp_path, monkeypatch)

    mgr = MagicMock()
    mgr.models_dir = models_dir
    mgr.active_services = ["s1"]
    mgr.gpu_mode = "shared"
    stale = load_models(models_dir)
    mgr._models = stale

    def _boom(d, include_applied=True):
        raise OSError("disk read error")
    monkeypatch.setattr(mgrmod, "load_models", _boom)

    # shared → exclusive 非法 transition → 部署不发生，但内存注册表必须原样保留
    r = ModelManager.switch(mgr, "M")
    assert r["status"] == "error"
    assert mgr._models is stale, "重读失败 → 沿用内存注册表（不得被空 dict 覆盖）"


def test_status_scenario_active_reads_applied_layer(tmp_path, monkeypatch):
    """D5：status()['scenario_active'] 直读应用层文件（≡ 容器实际值，所有模型卡可用）。"""
    import inferfabric.manager as mgrmod
    from inferfabric.manager import ModelManager
    from inferfabric.config import load_models
    models_dir, applied = _tmp_models_dir(tmp_path, monkeypatch)
    # DeviceTelemetry T5: status() 的 gpu_* 字段经设备遥测投影（不再用模块级
    # gpu_*_mb）；本用例只关心 scenario_active → NullTelemetry 空态即可。
    from inferfabric.platform.telemetry import NullTelemetry

    mgr = MagicMock()
    mgr._models = load_models(models_dir)
    mgr.active_services = []
    mgr.state.get_all_sleep_states.return_value = {}
    mgr._device_telemetry = NullTelemetry()

    s = ModelManager.status(mgr)
    assert s["scenario_active"] == {"M": "default"}, "无条目 = default（= 模型 YAML 当前值）"

    # CLI/其它入口写应用层 → 下次 /status 立即反映（不依赖代理内存）
    applied.write_text("M:\n  active_preset: short\n  overrides:\n    max_concurrency: 8\n")
    s2 = ModelManager.status(mgr)
    assert s2["scenario_active"] == {"M": "short"}
