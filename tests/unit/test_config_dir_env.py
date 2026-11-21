"""config 目录环境覆盖 — 生产迁移前置 PR（:8999 零风险迁移裁决）。

背景：MACFABRIC_MODELS_DIR/STATE_DIR 环境覆盖是 MacFabric gateway 的
安装版语义（launchd 用它指向 ~/.inferfabric），但从未上游到 InferFabric
config.py —— 只有 pack_handlers 认 env，造成 split-brain（packs 写数据
目录 models.d、核心读仓库 models.d）。

契约：
- IFF_MODELS_DIR > MACFABRIC_MODELS_DIR（旧名兼容）> BASE_DIR/models.d
- IFF_STATE_DIR > MACFABRIC_STATE_DIR（旧名兼容）> ~/.inferfabric
- 缺省（无 env）行为逐字节不变（Linux 现有部署形态保护）
- pack_handlers 收敛为单一事实源：直接读 config 常量，不再自读 env
"""
import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))


@pytest.fixture
def cfg(monkeypatch):
    """reload config（import 期常量按当次 env 计算），测试后恢复缺省。"""
    import inferfabric.config as _cfg

    def _reload():
        return importlib.reload(_cfg)
    yield _reload
    for var in ("IFF_MODELS_DIR", "MACFABRIC_MODELS_DIR",
                "IFF_STATE_DIR", "MACFABRIC_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(_cfg)


class TestDefaultsUnchanged:
    def test_no_env_repo_models_d(self, cfg):
        c = cfg()
        assert c.MODELS_DIR == c.BASE_DIR / "models.d"

    def test_no_env_data_dir_home(self, cfg, monkeypatch):
        for var in ("IFF_STATE_DIR", "MACFABRIC_STATE_DIR"):
            monkeypatch.delenv(var, raising=False)
        c = cfg()
        assert c.IFF_DATA_DIR == Path.home() / ".inferfabric"


class TestModelsDirOverride:
    def test_iff_models_dir(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("IFF_MODELS_DIR", str(tmp_path / "m"))
        assert cfg().MODELS_DIR == tmp_path / "m"

    def test_macfabric_legacy_alias(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("MACFABRIC_MODELS_DIR", str(tmp_path / "m"))
        assert cfg().MODELS_DIR == tmp_path / "m"

    def test_iff_precedence(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("IFF_MODELS_DIR", str(tmp_path / "iff"))
        monkeypatch.setenv("MACFABRIC_MODELS_DIR", str(tmp_path / "legacy"))
        assert cfg().MODELS_DIR == tmp_path / "iff"


class TestDataDirOverride:
    def test_iff_state_dir(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("IFF_STATE_DIR", str(tmp_path / "s"))
        assert cfg().IFF_DATA_DIR == tmp_path / "s"

    def test_macfabric_legacy_alias(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("MACFABRIC_STATE_DIR", str(tmp_path / "s"))
        assert cfg().IFF_DATA_DIR == tmp_path / "s"

    def test_iff_precedence(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("IFF_STATE_DIR", str(tmp_path / "iff"))
        monkeypatch.setenv("MACFABRIC_STATE_DIR", str(tmp_path / "legacy"))
        assert cfg().IFF_DATA_DIR == tmp_path / "iff"


class TestPackHandlersSingleSource:
    """pack_handlers 不再自读 env —— config 是唯一事实源（消灭 split-brain）。"""

    def test_pack_manager_uses_config_constants(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("IFF_STATE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("IFF_MODELS_DIR", str(tmp_path / "m"))
        c = cfg()
        from inferfabric.proxy import pack_handlers
        pack_handlers.reset_pack_manager()
        try:
            pmgr = pack_handlers.get_pack_manager()
            assert pmgr.root == c.IFF_DATA_DIR / "packs"
            assert pmgr.models_d == c.MODELS_DIR
        finally:
            pack_handlers.reset_pack_manager()
