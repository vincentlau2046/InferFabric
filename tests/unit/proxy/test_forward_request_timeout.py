"""unit/proxy/test_forward_request_timeout.py — _forward_request 池预算超时管道

2026-10-31 浏览器门禁 test_08 实锤的潜在生产 bug：池路径以
`timeout=剩余预算` 调 _forward_request，原实现 `conn.settimeout(timeout)`
在 http.client.HTTPConnection 上不存在（只有 socket 有）→ AttributeError
→ 本地池候选转发必 "unreachable"（503 pool_unavailable）。非池调用方
不传 timeout 从不触发 → 单测全绿、线上只炸池本地候选。

契约：timeout 非 None 时必须经构造参数传给 pm.make_conn（HTTPConnection
连接期消费 self.timeout）；None 时维持既有缺省调用（默认 300，字节不变）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.chat_handlers as chat_handlers


class _FakeResp:
    status = 200

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self, n=None):
        return b'{"ok": true}'

    def close(self):
        pass


class _FakeConn:
    def __init__(self, resp):
        self._resp = resp

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self._resp

    def close(self):
        pass


class _FakePM:
    """记录 make_conn 调用参数 — timeout 契约的观察点。"""

    def __init__(self):
        self.calls = []

    def make_conn(self, port, timeout=300):
        self.calls.append((port, timeout))
        return _FakeConn(_FakeResp())

    def release_port(self, *a):
        pass


class _FakeHandler:
    def __init__(self):
        self.path = "/v1/chat/completions"

    def send_response(self, code):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def _safe_write(self, b):
        pass


def _run(pm, **kw):
    h = _FakeHandler()
    ok = chat_handlers._forward_request(h, pm, 8003, b"{}", False, **kw)
    return ok, pm.calls


def test_pool_timeout_passed_to_make_conn_at_construction():
    # timeout=剩余预算 → 必须以构造参数送达（settimeout 在 HTTPConnection
    # 上不存在；修复前这里收到缺省 300 → RED）
    ok, calls = _run(_FakePM(), model_name="m", timeout=5.5)
    assert ok is True
    assert calls == [(8003, 5.5)], calls


def test_no_timeout_keeps_default_call_shape():
    # 非池调用方（timeout=None）：make_conn 缺省调用形状字节不变（PR-14
    # 冻结面 — 不传 timeout 的行为不得有任何变化）
    ok, calls = _run(_FakePM(), model_name="m")
    assert ok is True
    assert calls == [(8003, 300)], calls
