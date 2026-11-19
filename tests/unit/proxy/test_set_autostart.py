"""
unit/proxy/test_set_autostart.py — F9/T3: autostart 开关（fork 移植）

覆盖:
  - handler._handle_set_autostart: POST /admin/models/set-autostart 响应契约
    （行级 YAML 编辑保持注释/键序、autostart 缺失时插到 name: 之后、404/500）
  - 路由 /admin/models/set-autostart 已注册于 _POST_ROUTES
  - ModelConfig.autostart 默认 False + loader 解析（models.d YAML 顶层键）
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.handler as handler_mod
from inferfabric.proxy.handler import ProxyHandler
from inferfabric.config import ModelConfig


class _FakeHandler:
    """_handle_set_autostart 需要的最小 handler 面。"""

    def __init__(self, body):
        self._body = body
        self.sent = None

    def _read_body(self):
        return self._body

    def _send_json(self, obj, code=200):
        self.sent = (code, obj)


def _fake_pm(model=None, reloader=None):
    pm = types.SimpleNamespace()
    pm.mgr = types.SimpleNamespace(
        _models={m.name: m for m in ([model] if model else [])})
    pm.config_reloader = reloader
    return pm


def _model(tmp_path, yaml_text="name: m1\ntype: ollama\n"):
    y = tmp_path / "m1.yaml"
    y.write_text(yaml_text)
    return types.SimpleNamespace(name="m1", yaml_path=str(y))


def test_set_autostart_inserts_after_name(tmp_path):
    m = _model(tmp_path)
    pm = _fake_pm(model=m)
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 200
    assert h.sent[1] == {"status": "ok", "model": "m1", "autostart": True}
    text = Path(m.yaml_path).read_text()
    assert text == "name: m1\nautostart: true\ntype: ollama\n"


def test_set_autostart_replaces_existing_line(tmp_path):
    m = _model(tmp_path, "name: m1\nautostart: true\ntype: ollama\n# keep\n")
    pm = _fake_pm(model=m)
    h = _FakeHandler({"name": "m1", "enabled": False})
    ProxyHandler._handle_set_autostart(h, pm)
    text = Path(m.yaml_path).read_text()
    # 行级替换：注释与键序原样保留
    assert text == "name: m1\nautostart: false\ntype: ollama\n# keep\n"


def test_set_autostart_triggers_reload(tmp_path):
    m = _model(tmp_path)
    reloads = []
    pm = _fake_pm(model=m, reloader=types.SimpleNamespace(
        reload_models=lambda: reloads.append(1)))
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert reloads == [1]


def test_set_autostart_unknown_model_404(tmp_path):
    pm = _fake_pm()
    h = _FakeHandler({"name": "nope", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 404


def test_set_autostart_missing_yaml_404(tmp_path):
    m = types.SimpleNamespace(name="m1", yaml_path=str(tmp_path / "gone.yaml"))
    pm = _fake_pm(model=m)
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 404


def test_route_registered():
    assert "/admin/models/set-autostart" in handler_mod._POST_ROUTES


def test_model_config_default_false_and_loader(tmp_path):
    """ModelConfig.autostart 缺省 False（安全缺省）；YAML 顶层键被解析。"""
    assert ModelConfig(name="x", description="d").autostart is False


def test_loader_parses_autostart_top_level_key(tmp_path):
    """models.d YAML 顶层 autostart: true 被 loader 解析；缺省为 False。"""
    from inferfabric.config import load_models

    d = tmp_path / "models.d"
    d.mkdir()
    # 文件名 stem 必须与 YAML name 一致（load_models 校验）
    (d / "m1.yaml").write_text("name: m1\ntype: ollama\nautostart: true\n")
    (d / "m2.yaml").write_text("name: m2\ntype: ollama\n")
    ms = load_models(d)
    assert ms["m1"].autostart is True
    assert ms["m2"].autostart is False
