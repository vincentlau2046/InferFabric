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
