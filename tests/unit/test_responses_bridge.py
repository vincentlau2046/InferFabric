"""Responses API 桥接单测（Issue #1，2026-11-17）。

覆盖：
- 路由注册：/v1/responses 在 _POST_ROUTES
- 兜底翻译：Responses → chat（instructions/input/tools/max_output_tokens）
- 响应翻译：chat → Responses（output_text/usage/tool_calls）
- 原生优先契约：2xx 原生透传不触发翻译；400/404/422/501 才兜底
- 池别名：fabric-fast 旧名入 /v1/responses 解析到 flash 池
"""

import json
import pytest

from inferfabric.proxy.responses_bridge import (
    RESPONSES_UNSUPPORTED_STATUSES,
    chat_to_responses,
    responses_to_chat,
)


def test_responses_route_registered():
    from inferfabric.proxy.handler import _POST_ROUTES
    assert "/v1/responses" in _POST_ROUTES


def test_unsupported_status_set_matches_ruling():
    # 用户裁决：只有明确返回失败不支持才兜底（400/404/422/501）；
    # 401/403/429/5xx 交既有动作表
    assert RESPONSES_UNSUPPORTED_STATUSES == frozenset({400, 404, 422, 501})


def test_responses_to_chat_string_input():
    chat = responses_to_chat({
        "model": "fabric-flash",
        "instructions": "be brief",
        "input": "hello",
        "max_output_tokens": 256,
        "temperature": 0.3,
    })
    assert chat["model"] == "fabric-flash"
    assert chat["messages"][0] == {"role": "system", "content": "be brief"}
    assert chat["messages"][1] == {"role": "user", "content": "hello"}
    assert chat["max_tokens"] == 256
    assert chat["temperature"] == 0.3
    assert chat["stream"] is False


def test_responses_to_chat_items_and_tools():
    chat = responses_to_chat({
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "list files"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell",
             "arguments": "{\"cmd\": \"ls\"}"},
            {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
            {"type": "reasoning", "summary": []},   # 兜底路径跳过
        ],
        "tools": [{"type": "function", "name": "shell",
                   "description": "run", "parameters": {"type": "object"}}],
        "tool_choice": "auto",
    })
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert chat["messages"][1]["tool_calls"][0]["id"] == "c1"
    assert chat["messages"][2]["tool_call_id"] == "c1"
    assert chat["tools"][0]["function"]["name"] == "shell"
    assert chat["tool_choice"] == "auto"


def test_chat_to_responses_basic():
    resp, usage = chat_to_responses({
        "choices": [{"message": {"role": "assistant", "content": "hi there"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }, "fabric-flash")
    assert resp["object"] == "response"
    assert resp["model"] == "fabric-flash"
    assert resp["output_text"] == "hi there"
    assert resp["output"][0]["type"] == "message"
    assert resp["usage"]["input_tokens"] == 11
    assert resp["usage"]["output_tokens"] == 4
    assert usage["prompt_tokens"] == 11


def test_chat_to_responses_tool_calls():
    resp, _ = chat_to_responses({
        "choices": [{"message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "call_9", "type": "function",
                                                 "function": {"name": "shell",
                                                              "arguments": "{\"x\":1}"}}]},
                     "finish_reason": "tool_calls"}],
        "usage": {},
    }, "m")
    fc = resp["output"][0]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "call_9"
    assert fc["name"] == "shell"
    assert fc["arguments"] == "{\"x\":1}"


# ── M3 Task 4：原生优先合同（真实本机 stub；不改生产代码） ──────────
#
# 合同（计划 Task 4 / Review Focus 4）：
#   N1 native 2xx：请求路径/模型正确，JSON/SSE 字节直通交付，零 Chat 请求
#   N2 未提交且状态 ∈ {400,404,422,501}：同候选、同一 transport 调用内
#      恰一次 Chat 兜底，交付正确 Responses 封装
#   N3 已提交（哪怕状态码在集合内）/ 429/500/503 / 无状态传输错误：
#      永不兜底，零 Chat 调用
#   N4 负对照：临时副本颠倒 native 优先 / 放开 fallback gate → 对应新合同
#      必须失败（证明合同能抓住回归），还原后原样 GREEN。
import http.client
import http.server
import importlib.util
import threading
import types
from pathlib import Path

import yaml

from inferfabric.proxy import responses_bridge as _RB

GOLDEN_SSE = Path(__file__).parent.parent / "golden" / "responses_native_stream.sse"


class _Stub(http.server.BaseHTTPRequestHandler):
    """仅回环 stub：/v1/responses 行为由 cfg 控制；/v1/chat/completions 恒 200。"""

    def _cfg(self):
        return self.server.cfg

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        cfg = self._cfg()
        if self.path.startswith("/v1/responses"):
            cfg["counts"]["native"] += 1
            cfg["native_bodies"].append(body)
            if cfg.get("native_reset"):
                self.wfile.close()          # 无状态传输错误：连接被掐断
                return
            code = cfg.get("native_status", 200)
            if cfg.get("native_mode") == "sse" and code == 200:
                payload = GOLDEN_SSE.read_bytes()
                ctype = "text/event-stream"
            else:
                payload = json.dumps({"id": "resp_native", "object": "response",
                                      "model": "srv-x", "output": []}).encode()
                ctype = "application/json"
            data = payload if code == 200 else json.dumps(
                {"error": {"message": "no responses support"}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype if code == 200
                             else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/v1/chat/completions"):
            cfg["counts"]["chat"] += 1
            cfg["chat_bodies"].append(body)
            data = json.dumps({
                "choices": [{"message": {"role": "assistant",
                                         "content": "ok from chat"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class _StubPM:
    """_probe_native / _resolve_chat_target 所需的最小 pm 面。"""

    def __init__(self, port):
        self.port = port
        self.mgr = types.SimpleNamespace(
            find_model_by_served_name=lambda name: types.SimpleNamespace(
                name="m1", served_name=name),
            active_services=["m1"])
        self.dual_gate = types.SimpleNamespace(
            acquire_target=lambda name, timeout=None: types.SimpleNamespace(
                ok=True, release=lambda: None))

    def get_target_port(self, name):
        return self.port

    def make_conn(self, port, timeout=None):
        return http.client.HTTPConnection("127.0.0.1", port,
                                          timeout=timeout or 10)


class _FakeHandler:
    def __init__(self, fail_writes_at=None):
        self.status = None
        self.headers_out = {}
        self.chunks = []
        self.json_errors = []
        self._writes = 0
        self._fail_writes_at = fail_writes_at
        self._usage = {}

    def send_response(self, s):
        self.status = s

    def send_header(self, k, v):
        self.headers_out[k] = v

    def end_headers(self):
        pass

    def _safe_write(self, b):
        self._writes += 1
        if self._fail_writes_at is not None and self._writes >= self._fail_writes_at:
            raise OSError("broken pipe")
        self.chunks.append(b)

    def _send_json(self, obj, code=200, extra_headers=None):
        self.json_errors.append((obj, code))

    def raw(self):
        return b"".join(self.chunks)


def _cand():
    return types.SimpleNamespace(
        candidate=types.SimpleNamespace(kind="local", model_name="srv-x",
                                        provider=None, model_id=None),
        route=None)


def _make_stub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    srv.cfg = {"counts": {"native": 0, "chat": 0},
               "native_bodies": [], "chat_bodies": []}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _transport(rb, handler, srv, *, client_stream=False):
    pm = _StubPM(srv.server_address[1])
    data = {"model": "fabric-flash", "input": "hi"}
    tr = rb._transport_factory(handler, pm, data, requested_model="fabric-flash",
                               pool=None, client_stream=client_stream)
    return tr(_cand(), 5.0)


def _scenario_native_nonstream(rb):
    srv = _make_stub()
    try:
        h = _FakeHandler()
        res = _transport(rb, h, srv)
        assert res.ok and res.committed
        assert h.status == 200
        assert h.headers_out.get("Content-Type") == "application/json"
        env = json.loads(h.raw())
        assert env["id"] == "resp_native" and env["object"] == "response"
        # 请求路径/模型：native 打到 /v1/responses，模型换成本地 served-name
        assert srv.cfg["counts"]["native"] == 1
        assert json.loads(srv.cfg["native_bodies"][0])["model"] == "srv-x"
        assert srv.cfg["counts"]["chat"] == 0          # 零 Chat 请求
        assert not h.json_errors
    finally:
        srv.shutdown()


def _scenario_native_sse(rb):
    srv = _make_stub()
    try:
        srv.cfg["native_mode"] = "sse"
        h = _FakeHandler()
        res = _transport(rb, h, srv, client_stream=True)
        assert res.ok and res.committed
        assert h.headers_out.get("Transfer-Encoding") == "chunked"
        # SSE 事件字节级直通（去 chunked 帧）
        golden = GOLDEN_SSE.read_bytes()
        body = h.raw().replace(b"0\r\n\r\n", b"")
        parts = body.split(b"\r\n")
        delivered = b"".join(parts[i] for i in range(1, len(parts), 2))
        assert delivered == golden
        assert b"response.output_text.delta" in delivered
        assert srv.cfg["counts"]["chat"] == 0
    finally:
        srv.shutdown()


def _scenario_fallback_once(rb, status):
    srv = _make_stub()
    try:
        srv.cfg["native_status"] = status
        h = _FakeHandler()
        res = _transport(rb, h, srv)
        assert res.ok and res.committed, res.error
        assert srv.cfg["counts"]["native"] == 1        # 同候选一次探测
        assert srv.cfg["counts"]["chat"] == 1          # 恰一次 Chat 兜底
        assert json.loads(srv.cfg["chat_bodies"][0])["model"] == "srv-x"
        assert len(h.json_errors) == 1                 # 封装经 _send_json 交付
        env, code = h.json_errors[0]
        assert code == 200
        assert env["object"] == "response"
        assert env["output_text"] == "ok from chat"
        assert env["usage"]["input_tokens"] == 3
    finally:
        srv.shutdown()


def _scenario_never_fallback(rb, status):
    srv = _make_stub()
    try:
        srv.cfg["native_status"] = status
        h = _FakeHandler()
        res = _transport(rb, h, srv)
        assert not res.ok and not res.committed
        assert res.status == status
        assert srv.cfg["counts"]["chat"] == 0          # 零 Chat 调用
        assert not h.json_errors                       # 未直发声封（交动作表）
    finally:
        srv.shutdown()


def test_responses_native_nonstream_preserves_envelope():
    _scenario_native_nonstream(_RB)


def test_responses_native_sse_preserves_events():
    _scenario_native_sse(_RB)


@pytest.mark.parametrize("status", [400, 404, 422, 501])
def test_unsupported_uncommitted_native_falls_back_once(status):
    _scenario_fallback_once(_RB, status)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_other_native_failure_never_falls_back(status):
    _scenario_never_fallback(_RB, status)


def test_stateless_transport_error_never_falls_back():
    srv = _make_stub()
    try:
        srv.cfg["native_reset"] = True
        h = _FakeHandler()
        res = _transport(_RB, h, srv)
        assert not res.ok and res.status is None       # 无状态：交 run_attempts
        assert srv.cfg["counts"]["chat"] == 0
    finally:
        srv.shutdown()


def test_committed_native_failure_never_falls_back(monkeypatch):
    # 已提交（哪怕状态码 404 在集合内）：delivery error → transport 原样返回
    srv = _make_stub()
    committed_err = _RB.AttemptResult(ok=False, committed=True, status=404,
                                      route="r", error="native delivery error")
    monkeypatch.setattr(_RB, "_probe_native",
                        lambda *a, **k: committed_err)
    try:
        h = _FakeHandler()
        res = _transport(_RB, h, srv, client_stream=True)
        assert res.committed and res.status == 404
        assert srv.cfg["counts"]["chat"] == 0
    finally:
        srv.shutdown()


def _mutated_rb(tmp_path, old, new):
    """临时副本 + 文本突变 → 独立模块（负对照用，不改原文件）。"""
    src = Path(_RB.__file__).read_text(encoding="utf-8")
    assert src.count(old) == 1, "mutation anchor not unique"
    p = tmp_path / ("rb_mut_" + str(abs(hash(old)) % 10**8) + ".py")
    p.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_negative_control_flip_native_priority(tmp_path):
    """颠倒 native 优先（native 结果恒报 unsupported → 必兜底）：
    N3『永不兜底』合同必须失败。"""
    rb_m = _mutated_rb(
        tmp_path,
        "        res = _probe_native(handler, pm, payload, cand, pool=pool,\n"
        "                            remaining=remaining, client_stream=client_stream)",
        "        _probe_native(handler, pm, payload, cand, pool=pool,\n"
        "                      remaining=remaining, client_stream=client_stream)\n"
        "        res = AttemptResult(ok=False, committed=False, status=501,\n"
        "                                route=\"mut\", error=\"responses_unsupported\",\n"
        "                                incompatible=True)")
    with pytest.raises(AssertionError):
        _scenario_never_fallback(rb_m, 429)


def test_negative_control_open_fallback_gate(tmp_path):
    """放开 fallback gate（任何未提交状态都兜底）：
    N3『429/500/503 永不兜底』合同必须失败。"""
    rb_m = _mutated_rb(
        tmp_path,
        "        if res.status not in RESPONSES_UNSUPPORTED_STATUSES:\n"
        "            return res",
        "        if False:\n"
        "            return res")
    with pytest.raises(AssertionError):
        _scenario_never_fallback(rb_m, 429)
    with pytest.raises(AssertionError):
        _scenario_never_fallback(rb_m, 500)

# ── 评审 PR#17 review fixes：B1/B2/B3/H1 ──

class _FakeGate:
    def __init__(self):
        self.ok = True
        self.reason = ""
        self.released = 0

    def release(self):
        self.released += 1


class _FakeDualGate:
    def __init__(self):
        self.target_gates = []
        self.model_calls = []

    def acquire_target(self, key, timeout=None):
        g = _FakeGate()
        self.target_gates.append((key, g))
        return g

    def acquire(self, model, timeout=None):
        g = _FakeGate()
        self.model_calls.append((model, g))
        return g


def _fake_pm_for_probe(kind="local"):
    """_probe_native 直连单测的最小 pm 面（本地活跃目标）。"""
    model_obj = types.SimpleNamespace(name="m1", served_name="srv-x")
    pm = types.SimpleNamespace()
    pm.mgr = types.SimpleNamespace(
        find_model_by_served_name=lambda n: model_obj,
        active_services={"m1"})
    pm.dual_gate = _FakeDualGate()
    pm.get_target_port = lambda n: 8123
    _reg = {"prov/mdl-x": types.SimpleNamespace(model_id="mdl-x")}
    pm.cloud = types.SimpleNamespace(
        get_provider_config=lambda name: types.SimpleNamespace(
            openai_base="https://api.x.test/v1", api_key="sk-t", timeout=5)
        if name == "prov" else None,
        cloud_models=_reg,
        # PR2: _probe_native 查表走 resolve_cloud_model（键 ∪ model_id 双匹配）
        resolve_cloud_model=lambda p, m, _reg=_reg: _reg.get(f"{p}/{m}"))
    return pm, model_obj


class _FakeResp:
    def __init__(self, body=b"{}", status=200, ctype="application/json"):
        self._body = body
        self.status = status
        self._ctype = ctype

    def getheader(self, k):
        return self._ctype if k == "Content-Type" else None

    def read(self, n=-1):
        b, self._body = self._body, b""
        return b

    def close(self):
        pass


def test_probe_native_cloud_provider_bare_name_resolves(monkeypatch):
    """B1：native 候选 provider 必须用裸名——带 cloud: 前缀时
    get_provider_config 查不到，status=None 使 fallback 永不触发。"""
    pm, _ = _fake_pm_for_probe()
    seen = []
    real_cfg = pm.cloud.get_provider_config
    pm.cloud.get_provider_config = lambda n: (seen.append(n), real_cfg(n))[1]
    cand = _RB._DirectCandidate(provider="prov", model_id="mdl-x", kind="cloud")

    class _Boom(Exception):
        pass

    def _fail_urlopen(req, timeout=None):
        raise OSError("conn refused")

    monkeypatch.setattr(_RB, "urlopen", _fail_urlopen)
    h = _FakeHandler()
    res = _RB._probe_native(h, pm, {"model": "mdl-x"}, cand, pool=None,
                            remaining=None, client_stream=False)
    assert seen == ["prov"], "config must be looked up by bare provider name"
    assert res.status is None and not res.ok  # 连接失败语义，非 target unavailable


def test_probe_native_gate_held_through_delivery(monkeypatch):
    """B2a：gate 持有到交付完毕——urlopen 前/交付中不得 release。"""
    pm, _ = _fake_pm_for_probe()
    body = json.dumps({"id": "r1", "usage": {"input_tokens": 7,
                                             "output_tokens": 3}}).encode()
    monkeypatch.setattr(_RB, "urlopen",
                        lambda req, timeout=None: _FakeResp(body=body))
    cand = _RB._DirectCandidate(provider="prov", model_id="mdl-x", kind="cloud")
    h = _FakeHandler()
    res = _RB._probe_native(h, pm, {"model": "mdl-x"}, cand, pool=None,
                            remaining=None, client_stream=False)
    gate = pm.dual_gate.target_gates[0][1]
    assert res.ok and res.committed
    assert gate.released == 1, "gate must release exactly once, after delivery"


def test_probe_native_nonstream_usage_parsed(monkeypatch):
    """H1：非流式 native 响应解析 Responses usage（此前恒 {}）。"""
    pm, _ = _fake_pm_for_probe()
    body = json.dumps({
        "id": "r1", "status": "completed",
        "usage": {"input_tokens": 12, "output_tokens": 34,
                  "input_tokens_details": {"cached_tokens": 5}},
    }).encode()
    monkeypatch.setattr(_RB, "urlopen",
                        lambda req, timeout=None: _FakeResp(body=body))
    cand = _RB._DirectCandidate(provider="prov", model_id="mdl-x", kind="cloud")
    res = _RB._probe_native(_FakeHandler(), pm, {"model": "mdl-x"}, cand,
                            pool=None, remaining=None, client_stream=False)
    assert res.usage == {"prompt_tokens": 12, "prompt_tokens_cached": 5,
                         "completion_tokens": 34}


class _FakeDirectHandler(_FakeHandler):
    """handle_responses_direct 需要的极简 headers 面。"""

    def __init__(self):
        super().__init__()
        self.headers = {}
        self._agent_hit = types.SimpleNamespace(agent="", ua="", x_app="")


def _fake_pm_for_direct():
    pm = types.SimpleNamespace()
    pm.new_request_id = lambda: "req-1"
    pm.auth = types.SimpleNamespace(enabled=False, key_name=lambda a: "anon")
    pm.dual_gate = _FakeDualGate()
    pm.ensure_cloud_discovered = lambda: None
    pm.cloud = types.SimpleNamespace(
        resolve_route=lambda m, local: "cloud:prov",
        get_provider_config=lambda n: types.SimpleNamespace(
            openai_base="https://api.x.test/v1", api_key="sk-t", timeout=5),
        resolve_cloud_model=lambda p, m: object(),
        alias_target=lambda m: None)  # PR2: 直连分支别名解析面（非别名基线）
    logs = []
    pm.logger = types.SimpleNamespace(log=logs.append)
    pm.model_to_service = lambda m: None
    pm.mgr = types.SimpleNamespace(_models={})
    return pm, logs


def test_direct_cloud_transport_failure_sends_envelope(monkeypatch):
    """B3：直连未交付失败（status=None）→ 502 信封，不再静默 return。"""
    pm, logs = _fake_pm_for_direct()
    monkeypatch.setattr(_RB, "_probe_native", lambda *a, **k: _RB.AttemptResult(
        ok=False, committed=False, status=None, route="cloud:prov",
        error="conn refused"))
    h = _FakeDirectHandler()
    handled = _RB.handle_responses_direct(h, pm, {"model": "mdl-x"}, "mdl-x")
    assert handled is True
    assert h.json_errors and h.json_errors[0][1] == 502
    assert h.json_errors[0][0]["status"] == "upstream_unavailable"
    assert len(logs) == 1, "RequestLog 仍要记"
    # B2b：逻辑级 gate 必须被 acquire 且释放
    assert pm.dual_gate.model_calls and pm.dual_gate.model_calls[0][0] == "mdl-x"
    assert pm.dual_gate.model_calls[0][1].released == 1


def test_direct_cloud_upstream_status_envelope(monkeypatch):
    """B3：非回退类上游错误（如 5xx 重试耗尽）→ 按上游状态发声封。"""
    pm, _ = _fake_pm_for_direct()
    monkeypatch.setattr(_RB, "_probe_native", lambda *a, **k: _RB.AttemptResult(
        ok=False, committed=False, status=502, route="cloud:prov",
        error="upstream_http_502"))
    h = _FakeDirectHandler()
    _RB.handle_responses_direct(h, pm, {"model": "mdl-x"}, "mdl-x")
    assert h.json_errors and h.json_errors[0][1] == 502
    assert h.json_errors[0][0]["status"] == "upstream_error"


def test_direct_logic_gate_429(monkeypatch):
    """B2b：逻辑级限流失败 → 429 信封（此前 /v1/responses 直连零限流）。"""
    pm, logs = _fake_pm_for_direct()

    class _Full:
        ok = False
        reason = "rpm exceeded"

    pm.dual_gate.acquire = lambda m, timeout=None: _Full()
    h = _FakeDirectHandler()
    _RB.handle_responses_direct(h, pm, {"model": "mdl-x"}, "mdl-x")
    assert h.json_errors and h.json_errors[0][1] == 429
    assert h.json_errors[0][0]["status"] == "rate_limit"
    assert logs and logs[0].status == 429

def test_direct_pack_gate_503(monkeypatch):
    """N1：responses 直连补 pack gate（auth 之后）——chat 503 的模型
    不得从 responses 侧门进。"""
    pm, _ = _fake_pm_for_direct()
    pm.pack_manager = types.SimpleNamespace(is_model_disabled=lambda m: True)
    h = _FakeDirectHandler()
    handled = _RB.handle_responses_direct(h, pm, {"model": "mdl-x"}, "mdl-x")
    assert handled is True
    assert h.json_errors == [({"error": "pack_uninstalling",
                               "status": "pack_uninstalling"}, 503)]


def test_direct_pack_gate_503_releases_logic_gate(monkeypatch):
    """B4：pack gate 在逻辑级 acquire 之后，503 返回必须 release——否则
    per-model Semaphore 每次泄漏 1 槽，卸载窗口期反复探测耗尽并发。"""
    pm, _ = _fake_pm_for_direct()
    pm.pack_manager = types.SimpleNamespace(is_model_disabled=lambda m: True)
    h = _FakeDirectHandler()
    handled = _RB.handle_responses_direct(h, pm, {"model": "mdl-x"}, "mdl-x")
    assert handled is True
    assert h.json_errors and h.json_errors[0][1] == 503
    # acquire 过（B2b 语义保留）且本次 503 路径已释放，无槽泄漏
    assert pm.dual_gate.model_calls[0][0] == "mdl-x"
    assert pm.dual_gate.model_calls[0][1].released == 1


# ── #66：兜底探测三形态解析（别名上游 id / spec 键 / provider 前缀键）──────
# 契约（specs/alias-fix-round-66-73 §#66）：
# - 查表一律 resolve_cloud_model（c.model_id 三形态未必等于注册表键）；
# - 上游永远收注册表条目的原始 model_id（对齐 forwarder chat 路径）——
#   spec 键直呼时 c.model_id 是注册表键而非上游 id，透传必 404。

def _mk_cd_spec_key(tmp_path):
    """prov：spec 键 k3（注册表键 k3 / prov/k3，上游 model_id = kimi/k3）
    + 别名 my-k3 → kimi/k3。构造时 spec-only 注册即生效。"""
    from inferfabric.cloud_discovery import CloudDiscovery
    p = tmp_path / "cloud_provider.yaml"
    p.write_text(yaml.dump({"providers": {"prov": {
        "api_key": "sk-t", "openai_base": "https://up.test/v1",
        "models": {"k3": {"model_id": "kimi/k3", "name": "K3"}},
        "aliases": {"my-k3": "kimi/k3"},
    }}}), encoding="utf-8")
    return CloudDiscovery(p)


def test_chat_fallback_three_forms_send_upstream_model_id(tmp_path, monkeypatch):
    cd = _mk_cd_spec_key(tmp_path)
    pm = types.SimpleNamespace(cloud=cd, dual_gate=_FakeDualGate())
    seen = {}

    def _fake_probe(provider_cfg, chat_data, remaining):
        seen["model"] = chat_data.get("model")
        return 200, b"{}"

    monkeypatch.setattr(_RB, "_probe_cloud_chat", _fake_probe)
    h = _FakeHandler()
    for form, mid in (("alias-upstream-id", "kimi/k3"),
                      ("spec-key", "k3"),
                      ("provider-prefixed-key", "prov/k3")):
        seen.clear()
        cand = _RB._DirectCandidate(provider="prov", model_id=mid, kind="cloud")
        status, raw, route = _RB._resolve_chat_target(
            h, pm, {"model": mid}, cand, pool=None, remaining=None)
        assert status == 200, f"{form}: 探测必须可达（三形态都可解析）"
        assert seen["model"] == "kimi/k3", \
            f"{form}: 上游必须收到注册表原始 model_id"


def test_probe_native_spec_key_sends_upstream_model_id(tmp_path, monkeypatch):
    cd = _mk_cd_spec_key(tmp_path)
    pm = types.SimpleNamespace(cloud=cd, dual_gate=_FakeDualGate())
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(body=json.dumps(
            {"id": "resp-1", "status": "completed", "output": []}).encode())

    monkeypatch.setattr(_RB, "urlopen", _fake_urlopen)
    cand = _RB._DirectCandidate(provider="prov", model_id="k3", kind="cloud")
    res = _RB._probe_native(_FakeHandler(), pm,
                            {"model": "k3", "input": "hi"}, cand,
                            pool=None, remaining=None, client_stream=False)
    assert res.ok, "spec 键直呼 native 探测必须可达"
    assert captured["body"]["model"] == "kimi/k3", \
        "native 上游必须收到注册表原始 model_id，而非 spec 键"
