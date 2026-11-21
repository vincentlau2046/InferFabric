"""ManagedRuntime 生产装配注入（阶段1遗留债：合并后接线）。

契约（spec §4 / STATUS 验证行）：
- ModelManager 构造即装配生产 ManagedRuntime（此前未注入 = 受管包
  fail-closed 拒绝「managed pack runtime not wired」）；
- 装配依赖：PackStore(IFF_DATA_DIR/packs/state.json) + pack_root
  IFF_DATA_DIR/packs + GPU busy 探针（_scan_actual_services 非空即 busy，
  探测异常按 busy——fail-closed）+ models_lookup=ModelManager._models；
- 显式 set_managed_runtime() 优先（测试/平台覆写），装配不覆盖；
- 装配自身无 I/O 副作用（store/manifest 均解析期才读盘）。
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fresh_runtime():
    """隔离模块级单例：进测试清空，出测试还原。"""
    from inferfabric.packs import managed_runtime as mr
    saved = mr._managed_runtime
    mr._managed_runtime = None
    yield mr
    mr._managed_runtime = saved


def _make_manager(tmp_path):
    from inferfabric.manager import ModelManager
    return ModelManager(models_dir=tmp_path / "models.d",
                        state_db_path=tmp_path / "s.db")


class TestProductionWiring:
    def test_model_manager_wires_managed_runtime(self, tmp_path, fresh_runtime, monkeypatch):
        monkeypatch.setattr("inferfabric.config.IFF_DATA_DIR",
                            Path(tmp_path / "iffdata"))
        mgr = _make_manager(tmp_path)
        rt = fresh_runtime.get_managed_runtime()
        assert rt is not None, "ModelManager 构造后受管运行时必须已装配"
        assert rt._pack_root == Path(tmp_path / "iffdata") / "packs"
        assert rt._store is not None
        assert rt._models_lookup is mgr._models

    def test_explicit_set_not_overwritten(self, tmp_path, fresh_runtime):
        sentinel = object()
        fresh_runtime.set_managed_runtime(sentinel)
        _make_manager(tmp_path)
        assert fresh_runtime.get_managed_runtime() is sentinel

    def test_probe_busy_follows_actual_services(self, tmp_path, fresh_runtime):
        mgr = _make_manager(tmp_path)
        rt = fresh_runtime.get_managed_runtime()
        mgr._gpu_state._scan_actual_services = MagicMock(return_value=["m1"])
        assert rt._probe_gpu_busy() is True
        mgr._gpu_state._scan_actual_services = MagicMock(return_value=[])
        assert rt._probe_gpu_busy() is False

    def test_probe_failure_is_busy_fail_closed(self, tmp_path, fresh_runtime):
        mgr = _make_manager(tmp_path)
        rt = fresh_runtime.get_managed_runtime()
        mgr._gpu_state._scan_actual_services = MagicMock(
            side_effect=RuntimeError("scan blew up"))
        assert rt._probe_gpu_busy() is True, "探测异常必须按 busy（fail-closed）"

    def test_wiring_failure_stays_none_fail_closed(self, tmp_path, fresh_runtime, monkeypatch):
        """装配路径炸掉不得带崩启动——保持 None（fail-closed 拒绝受管包）。"""
        from inferfabric.packs import managed_runtime as mr
        monkeypatch.setattr(
            mr, "ManagedRuntime",
            MagicMock(side_effect=RuntimeError("wiring blew up")))
        _make_manager(tmp_path)  # 构造必须成功
        assert fresh_runtime.get_managed_runtime() is None
