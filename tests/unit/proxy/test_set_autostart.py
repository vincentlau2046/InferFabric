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
    assert h.sent[1] == {"status": "ok", "model": "m1", "autostart": True,
                         "reload": "skipped"}
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


# ═══════════════════════════════════════════════════════════════
# 评审 PR#19：B1 原子写 / N1 严格契约 / N3 reload 状态
# ═══════════════════════════════════════════════════════════════


def test_enabled_must_be_bool(tmp_path):
    """N1：enabled 缺 key / 非布尔（如 "true" 字符串 bool() 反向启用）→ 400。"""
    m = _model(tmp_path)
    pm = _fake_pm(model=m)
    for bad in ({}, {"name": "m1"}, {"name": "m1", "enabled": "true"},
                {"name": "m1", "enabled": 1}):
        h = _FakeHandler(bad)
        ProxyHandler._handle_set_autostart(h, pm)
        assert h.sent[0] == 400, bad
    # 原 YAML 未被改动
    assert Path(m.yaml_path).read_text() == "name: m1\ntype: ollama\n"


def test_atomic_write_survives_parse_failure(tmp_path, monkeypatch):
    """B1：写前 safe_load 验证失败 → 500 且原文件分毫未动（不砖网关）。"""
    import inferfabric.proxy.handler as H
    m = _model(tmp_path, "name: m1\ntype: ollama\n")
    pm = _fake_pm(model=m)
    orig_load = H.yaml.safe_load
    monkeypatch.setattr(H.yaml, "safe_load",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 500
    assert Path(m.yaml_path).read_text() == "name: m1\ntype: ollama\n"
    monkeypatch.setattr(H.yaml, "safe_load", orig_load)


def test_atomic_write_no_tmp_leftovers_on_replace_failure(tmp_path, monkeypatch):
    """B1：os.replace 失败 → 500、原文件不动、无 .tmp 残留。"""
    import inferfabric.proxy.handler as H
    m = _model(tmp_path)
    pm = _fake_pm(model=m)
    real_replace = H.os.replace
    def boom(a, b):
        real_replace("/nonexistent-src", "/nonexistent-dst")  # raise
    monkeypatch.setattr(H.os, "replace", boom)
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 500
    assert Path(m.yaml_path).read_text() == "name: m1\ntype: ollama\n"
    assert not list(tmp_path.parent.glob(".autostart-*")), "tmp 未清理"


def test_atomic_write_lands_and_verifies(tmp_path):
    """B1+N2：正常路径——flag 落位且可被 safe_load 验证；响应含 reload 状态。"""
    m = _model(tmp_path, "name: m1\n# comment\ntype: ollama\n")
    reloads = []
    pm = _fake_pm(model=m, reloader=types.SimpleNamespace(
        reload_models=lambda: reloads.append(1)))
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 200 and h.sent[1]["reload"] == "ok"
    import yaml as _yaml
    parsed = _yaml.safe_load(Path(m.yaml_path).read_text())
    assert parsed["autostart"] is True
    assert "# comment" in Path(m.yaml_path).read_text()
    assert not list(tmp_path.glob(".autostart-*"))


def test_no_name_line_is_error_not_silent_ok(tmp_path):
    """N2：无顶级 name: 行可插 → 验证失败 500，不再静默 no-op 报 ok。"""
    y = tmp_path / "weird.yaml"
    y.write_text("type: ollama\n")
    m = types.SimpleNamespace(name="m1", yaml_path=str(y))
    pm = _fake_pm(model=m)
    h = _FakeHandler({"name": "m1", "enabled": True})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 500
    assert y.read_text() == "type: ollama\n"


def test_reload_failure_surfaced(tmp_path):
    """N3：reload 抛异常 → 响应 reload=failed:...（不再静默 200 假成功）。"""
    m = _model(tmp_path)
    def boom():
        raise RuntimeError("reload broke")
    pm = _fake_pm(model=m, reloader=types.SimpleNamespace(reload_models=boom))
    h = _FakeHandler({"name": "m1", "enabled": False})
    ProxyHandler._handle_set_autostart(h, pm)
    assert h.sent[0] == 200
    assert h.sent[1]["reload"] == "failed: reload broke"
    # 盘上已落位 false
    assert "autostart: false" in Path(m.yaml_path).read_text()


# ═══════════════════════════════════════════════════════════════
# 评审 PR#19：B2 陈旧 DB 预检 / N4 exclusive 互斥 / N5 异常事件 / N6 线程体
# ═══════════════════════════════════════════════════════════════


def _fake_stack(models, switch_results=None, switch_raises=None):
    """_run_autostart 需要的最小 mgr 面（ProxyManager 形状）。"""
    import inferfabric.proxy.handler as H
    calls = []
    results = dict(switch_results or {})
    def switch(name):
        calls.append(name)
        if switch_raises and name in switch_raises:
            raise switch_raises[name]
        return results.get(name, {"status": "ok"})
    anomalies = []
    mgr = types.SimpleNamespace(
        mgr=types.SimpleNamespace(_models={m.name: m for m in models},
                                  switch=switch),
        anomalies=types.SimpleNamespace(record=anomalies.append))
    return mgr, calls, anomalies, H


def _am(name, autostart=True, exclusive=True):
    return types.SimpleNamespace(name=name, autostart=autostart,
                                 is_exclusive=exclusive)


def test_run_autostart_stale_db_does_not_skip(monkeypatch):
    """B2：不再读 active_services 预检——switch() 的现实校验说了算。"""
    mgr, calls, _, H = _fake_stack([_am("m1")])
    mgr.mgr.active_services = ["m1"]  # 陈旧 DB： Pretend active
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert calls == ["m1"], "陈旧 DB 不得吞掉拉起"


def test_run_autostart_sorted_deterministic_and_skips_non_autostart(monkeypatch):
    """N6：按 name 排序确定性顺序；非 autostart 模型跳过。"""
    mgr, calls, _, H = _fake_stack(
        [_am("b", exclusive=False), _am("a", exclusive=False),
         _am("z", autostart=False, exclusive=False)])
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert calls == ["a", "b"]


def test_run_autostart_exclusive_mutex(monkeypatch):
    """N4：两个 exclusive autostart——只启动先到者，第二个告警跳过 + 事件。"""
    mgr, calls, anomalies, H = _fake_stack(
        [_am("ex1"), _am("ex2"), _am("shared1", exclusive=False)])
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert calls == ["ex1", "shared1"], "ex2 被互斥跳过，shared 不受影响"
    assert len(anomalies) == 1
    assert "ex1" in anomalies[0].message


def test_run_autostart_exclusive_slot_only_on_success(monkeypatch):
    """N4：exclusive 首个启动失败（GPU 占用等）→ 槽未占，后续仍尝试。"""
    mgr, calls, _, H = _fake_stack(
        [_am("ex1"), _am("ex2")],
        switch_results={"ex1": {"status": "error", "message": "GPU occupied"}})
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert calls == ["ex1", "ex2"]


def test_run_autostart_failure_records_anomaly(monkeypatch):
    """N5：switch 异常 / 拒绝 → AnomalyEvent（category=model, error）。"""
    mgr, calls, anomalies, H = _fake_stack(
        [_am("m1", exclusive=False), _am("m2", exclusive=False)],
        switch_results={"m2": {"status": "error", "message": "OOM"}},
        switch_raises={"m1": RuntimeError("vllm boom")})
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert len(anomalies) == 2
    assert all(a.category == "model" and a.severity == "error" for a in anomalies)


def test_run_autostart_already_active_is_not_failure(monkeypatch):
    """B2：switch 裁决 already_active（现实校验通过）→ 记日志不算失败。"""
    mgr, calls, anomalies, H = _fake_stack(
        [_am("m1", exclusive=False)],
        switch_results={"m1": {"status": "already_active", "model": "m1"}})
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    H._run_autostart(mgr)
    assert calls == ["m1"]
    assert anomalies == []
