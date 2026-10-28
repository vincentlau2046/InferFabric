"""
tests/unit/proxy/test_async_server.py — PR-19 aiohttp 边缘层测试

用 aiohttp TestClient 打 build_app()，route fn 以 stub 替身（monkeypatch 路由表）
验证边缘层机制：流式增量泵送、头传播 + strip 集、413/404/405、admin guard、
chunked 请求体、客户端断连 → worker 停推。
管线逻辑（auth/gate/cache…）由 test_handler.py 等既有测试覆盖，这里只测边缘。
"""

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

# 对齐生产运行序：proxy_manager 在 import 时把 _deps 注入 sys.path[0]，
# 保证 vendored aiohttp 栈优先于 conda 环境中的同名包（类型身份一致）。
_DEPS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "_deps"))
if os.path.isdir(_DEPS) and _DEPS not in sys.path:
    sys.path.insert(0, _DEPS)

from aiohttp.test_utils import TestClient, TestServer

import inferfabric.proxy.handler as handler_module
from inferfabric.proxy import async_server as aserver


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════

def _run(coro):
    return asyncio.run(coro)


def _executor():
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="iff-test")


def _fake_pm():
    return MagicMock()


def _start(route_path, stub, monkeypatch, table="POST"):
    """把路由表条目换成 stub 并构建 app。"""
    tables = {
        "GET": handler_module._GET_ROUTES,
        "POST": handler_module._POST_ROUTES,
        "DELETE": handler_module._DELETE_ROUTES,
    }
    monkeypatch.setitem(tables[table], route_path, stub)


# ═══════════════════════════════════════════════════════════════
# 基础路由行为
# ═══════════════════════════════════════════════════════════════

def test_health_200_json(monkeypatch):
    def stub_health(h, pm):
        h._send_json({"status": "ok", "gpu_mode": "idle"})

    _start("/health", stub_health, monkeypatch, "GET")
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            # strip 集：aiohttp 自管连接/分帧
            assert "Connection" not in resp.headers
            assert "Transfer-Encoding" not in resp.headers

    _run(scenario())
    ex.shutdown(wait=True)


def test_unknown_route_404_json():
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/definitely-not-a-route")
            assert resp.status == 404
            assert (await resp.json()) == {"error": "not found"}

    _run(scenario())
    ex.shutdown(wait=True)


def test_wrong_method_404_like_threaded():
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        # /health 是 GET 路由 → POST 落入 catch-all → 404 JSON
        # （与线程版 do_POST 的"未知路径 404"行为一致，优于 aiohttp 原生 405）
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/health", json={})
            assert resp.status == 404
            assert (await resp.json()) == {"error": "not found"}

    _run(scenario())
    ex.shutdown(wait=True)


def test_options_204_with_cors():
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.options("/anything/at/all")
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
            assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")

    _run(scenario())
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# 流式（核心回归：增量到达，非整包缓冲）
# ═══════════════════════════════════════════════════════════════

def _stub_stream(h, pm, payloads, interval=0.05):
    """模拟 forwarder.pipe_stream_response 的写模式（手工 chunked 帧）。"""
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()
    for p in payloads:
        time.sleep(interval)
        h.wfile.write(b"%x\r\n" % len(p))
        h.wfile.write(p)
        h.wfile.write(b"\r\n")
        h.wfile.flush()
    h.wfile.write(b"0\r\n\r\n")
    h.wfile.flush()


def test_stream_chunks_arrive_incrementally(monkeypatch):
    """SSE 增量到达回归测试 —— 整包缓冲实现无法通过（总时长 >= 3×interval）。"""
    payloads = [b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"]

    def stub(h, pm):
        _stub_stream(h, pm, payloads)

    _start("/v1/messages", stub, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/messages", json={"model": "x"})
            assert resp.status == 200
            ts = []
            data = b""
            async for chunk in resp.content.iter_any():
                ts.append(time.monotonic())
                data += chunk
            assert data == b"".join(payloads)
            assert len(ts) >= 3
            # 增量到达：首末 chunk 间隔必须覆盖 3 段 sleep 的总时长
            assert ts[-1] - ts[0] >= 0.08, f"chunks arrived in one burst: {ts}"

    _run(scenario())
    ex.shutdown(wait=True)


def test_forward_non_stream_full_body(monkeypatch):
    def stub(h, pm):
        h._send_json({"choices": [{"message": {"content": "hi"}}]})

    _start("/v1/chat/completions", stub, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions",
                                     json={"model": "x", "stream": False})
            assert resp.status == 200
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "hi"
            # 转发路由走 StreamResponse：aiohttp 自管分帧（未知长度 body 用
            # 原生 TE: chunked，合法且客户端透明）；不得出现 stdlib 的
            # Connection: close（keep-alive 保留）
            te = resp.headers.get("Transfer-Encoding")
            assert te is None or te == "chunked"
            assert "close" not in resp.headers.get("Connection", "").lower()
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    _run(scenario())
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# 头传播
# ═══════════════════════════════════════════════════════════════

def test_retry_after_header_propagates(monkeypatch):
    def stub(h, pm):
        h._send_json({"error": "rate limited"}, 429,
                     extra_headers={"Retry-After": "10"})

    _start("/v1/messages", stub, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/messages", json={"model": "x"})
            assert resp.status == 429
            assert resp.headers.get("Retry-After") == "10"
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    _run(scenario())
    ex.shutdown(wait=True)


def test_snapshot_304_etag(monkeypatch):
    """ETag/If-None-Match 头必须传播（实验版丢头的回归测试）。"""
    ETAG = '"v1"'

    def stub(h, pm):
        if h.headers.get("If-None-Match") == ETAG:
            h.send_response(304)
            h.send_header("ETag", ETAG)
            h.end_headers()
            return
        body = b'{"snapshot": 1}'
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("ETag", ETAG)
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)
        h.wfile.flush()

    _start("/api/snapshot", stub, monkeypatch, "GET")
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            r1 = await client.get("/api/snapshot")
            assert r1.status == 200
            etag = r1.headers.get("ETag")
            assert etag == ETAG
            r2 = await client.get("/api/snapshot", headers={"If-None-Match": ETAG})
            assert r2.status == 304
            assert r2.headers.get("ETag") == ETAG

    _run(scenario())
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# 请求体
# ═══════════════════════════════════════════════════════════════

def test_oversized_body_413(monkeypatch):
    def stub(h, pm):
        h._send_json({"echo": True})

    _start("/v1/messages", stub, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock(), client_max_size=1024)
        big = {"model": "x", "blob": "a" * 4096}
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/messages", json=big)
            assert resp.status == 413
            assert (await resp.json())["error"] == "payload too large (max 100MB)"

    _run(scenario())
    ex.shutdown(wait=True)


def test_chunked_request_body_supported(monkeypatch):
    """chunked 请求体（无 Content-Length）→ 原生解码（缺陷 1 端到端）。"""
    seen = {}

    def stub(h, pm):
        raw = h.rfile.read()
        d = json.loads(raw) if raw else {}
        seen["model"] = d.get("model")
        seen["cl"] = h.headers.get("Content-Length")
        h._send_json({"echo_model": d.get("model")})

    _start("/v1/messages", stub, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())

        async def _agen():
            yield b'{"model": "chunky", '
            yield b'"stream": false}'

        async with TestClient(TestServer(app)) as client:
            # 异步生成器 body → 客户端以 chunked TE 发送（无 Content-Length）
            resp = await client.post("/v1/messages", data=_agen())
            assert resp.status == 200
            assert (await resp.json())["echo_model"] == "chunky"

    _run(scenario())
    assert seen.get("model") == "chunky"
    # 服务端确认：chunked 请求体无 Content-Length（缺陷 1 的端到端证据）
    assert seen.get("cl") is None
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# admin guard
# ═══════════════════════════════════════════════════════════════

def test_admin_guard_401_and_pass(monkeypatch):
    monkeypatch.setattr(handler_module, "_ADMIN_TOKEN", "secret")

    def stub_switch(h, pm):
        if not h._check_admin():  # 真实 _check_admin：401 由其自身发出
            return
        h._send_json({"status": "switched"})

    _start("/switch", stub_switch, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            r1 = await client.post("/switch", json={"model": "idle"})
            assert r1.status == 401
            r2 = await client.post("/switch", json={"model": "idle"},
                                   headers={"X-Admin-Token": "secret"})
            assert r2.status == 200
            assert (await r2.json())["status"] == "switched"

    _run(scenario())
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# 异常路径
# ═══════════════════════════════════════════════════════════════

def test_worker_exception_500_json(monkeypatch):
    def stub_boom(h, pm):
        raise RuntimeError("boom before headers")

    _start("/health", stub_boom, monkeypatch, "GET")
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal error"

    _run(scenario())
    ex.shutdown(wait=True)


def test_sink_mechanics():
    """_WfileSink 机制单测：passthrough / unframe 解帧 / 终止帧 / 断连标记。"""
    items = []
    sink = aserver._WfileSink()
    sink._enqueue = items.append

    # passthrough 模式：原样字节
    sink.write(b'{"a":1}')
    assert items == [b'{"a":1}']

    # unframe 模式：手工 chunked 帧解帧（跨 write 的帧也能重组）
    sink.set_unframe()
    sink.write(b"3\r\nxyz")          # 帧未收全（缺尾部 CRLF）
    assert items == [b'{"a":1}']
    sink.write(b"\r\n")              # 补上尾部 CRLF → 出 payload
    assert items == [b'{"a":1}', b"xyz"]
    sink.write(b"0\r\n\r\n")         # 终止帧 → _SENTINEL
    assert items[-1] is aserver._SENTINEL

    # 断连标记：gone 后 write 抛 BrokenPipe
    sink.mark_client_gone()
    with pytest.raises(BrokenPipeError):
        sink.write(b"zzz")


def test_client_rst_stops_worker(monkeypatch):
    """客户端 RST 断开（SO_LINGER 0）→ 连接丢失 → pump 取消 → worker 停推。

    用裸 socket 保证 RST 必达（普通 close 发 FIN，服务端视为 keep-alive
    不会触发 connection_lost；小块数据还会被内核缓冲吸收）。
    """
    import struct
    import socket as _socket

    events = []

    def stub_stream(h, pm):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        payload = b"x" * 65536
        for i in range(200):  # 4s 长流：保证断开时 worker 仍在写
            time.sleep(0.02)
            try:
                h.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                events.append("broken")
                return
        events.append("done")
        h.wfile.write(b"0\r\n\r\n")

    _start("/v1/messages", stub_stream, monkeypatch)
    ex = _executor()

    async def scenario():
        app = aserver.build_app(_fake_pm(), ex, MagicMock())
        server = TestServer(app, handler_cancellation=True)
        await server.start_server()
        try:
            s = _socket.create_connection(("127.0.0.1", server.port))
            s.sendall(
                b"POST /v1/messages HTTP/1.1\r\n"
                b"Host: test\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"\r\n"
                b"{}"
            )
            await asyncio.sleep(0.5)  # 让流开始
            # linger=0 → close() 发 RST 而非 FIN
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
            s.close()
            # 等 worker 观察到 BrokenPipe（有界等待）
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and "broken" not in events:
                await asyncio.sleep(0.1)
            assert "broken" in events, \
                f"worker did not observe client RST (events={events})"
        finally:
            await server.close()

    _run(scenario())
    ex.shutdown(wait=True)


# ═══════════════════════════════════════════════════════════════
# PR-20 ① 有界排空 + 硬退出（重启零中断）
# ═══════════════════════════════════════════════════════════════

class TestBuildRunnerBoundedDrain:
    def test_default_drain_30s(self, monkeypatch):
        """AppRunner 必须带 shutdown_timeout=30（P0 实测 60s 排空上限是
        91.7s 拒连窗口的组成部分；有界排空是硬前提）。"""
        recorded = {}

        class FakeAppRunner:
            def __init__(self, app, **kwargs):
                recorded.update(kwargs)

        monkeypatch.setattr(aserver.web, "AppRunner", FakeAppRunner)
        aserver._build_runner(_fake_pm())
        assert recorded["shutdown_timeout"] == 30.0
        # 既有语义不回退
        assert recorded["keepalive_timeout"] == 75
        assert recorded["handler_cancellation"] is True

    def test_env_override_drain(self, monkeypatch):
        recorded = {}

        class FakeAppRunner:
            def __init__(self, app, **kwargs):
                recorded.update(kwargs)

        monkeypatch.setattr(aserver.web, "AppRunner", FakeAppRunner)
        monkeypatch.setenv("EDGE_SHUTDOWN_DRAIN", "5")
        aserver._build_runner(_fake_pm())
        assert recorded["shutdown_timeout"] == 5.0


class TestShutdownGracefully:
    """关停序列：封印 → 停后台 → 端口收 → 有界排空 → 硬退出。

    硬退出绕过非守护 executor 线程 join（P0 实测 60–91.7s 挂死根因）。
    """

    def _fakes(self):
        calls = []

        class FakeSite:
            async def stop(self):
                calls.append("site.stop")

        class FakeRunner:
            async def cleanup(self):
                calls.append("runner.cleanup")

        mgr = MagicMock()
        mgr.begin_shutdown = lambda reason: calls.append("begin_shutdown")
        mgr.telemetry.logger.flush_blocking = lambda timeout=5: calls.append("reqlog.flush")
        watchdog = MagicMock()
        watchdog.stop = lambda: calls.append("watchdog.stop")
        ev = MagicMock()
        ev.set = lambda: calls.append("event.set")
        executor = MagicMock()
        executor.shutdown = lambda **kw: calls.append("executor.shutdown")
        notify = lambda msg: calls.append(f"sd:{msg}")
        exit_fn = lambda code: calls.append(f"exit({code})")
        return calls, mgr, watchdog, ev, FakeSite(), FakeRunner(), executor, notify, exit_fn

    def test_sequence_order_and_hard_exit(self, monkeypatch):
        monkeypatch.delenv("EDGE_HARD_EXIT", raising=False)
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()
        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, runner, ex, notify, exit_fn))
        assert calls == [
            "begin_shutdown", "event.set", "watchdog.stop",
            "site.stop", "runner.cleanup", "reqlog.flush", "executor.shutdown",
            "sd:STOPPING=1", "exit(0)",
        ]

    def test_final_flush_failure_does_not_block_exit(self, monkeypatch):
        """评审修复 5：终刷失败不得阻断关停（日志尽力而为，退出必须发生）。"""
        monkeypatch.delenv("EDGE_HARD_EXIT", raising=False)
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()
        mgr.telemetry.logger.flush_blocking = MagicMock(
            side_effect=RuntimeError("db busy"))
        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, runner, ex, notify, exit_fn))
        assert calls[-1] == "exit(0)"

    def test_seal_failure_does_not_block_shutdown(self, monkeypatch):
        monkeypatch.delenv("EDGE_HARD_EXIT", raising=False)
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()
        mgr.begin_shutdown = MagicMock(side_effect=RuntimeError("seal boom"))
        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, runner, ex, notify, exit_fn))
        assert "site.stop" in calls
        assert calls[-1] == "exit(0)"

    def test_drain_error_still_hard_exits(self, monkeypatch):
        monkeypatch.delenv("EDGE_HARD_EXIT", raising=False)
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()

        class BrokenRunner:
            async def cleanup(self):
                raise RuntimeError("drain boom")

        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, BrokenRunner(), ex, notify, exit_fn))
        assert calls[-1] == "exit(0)"

    def test_total_drain_deadline_bounds_cleanup(self, monkeypatch):
        monkeypatch.setenv("EDGE_SHUTDOWN_DRAIN", "0.02")
        monkeypatch.setenv("EDGE_HARD_EXIT", "1")
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()

        class SlowRunner:
            async def cleanup(self):
                await asyncio.sleep(0.25)

        started = time.monotonic()
        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, SlowRunner(), ex, notify, exit_fn))
        assert time.monotonic() - started < 0.15
        assert calls[-1] == "exit(0)"

    def test_hard_exit_env_off(self, monkeypatch):
        monkeypatch.setenv("EDGE_HARD_EXIT", "0")
        calls, mgr, wd, ev, site, runner, ex, notify, exit_fn = self._fakes()
        asyncio.run(aserver._shutdown_gracefully(
            mgr, wd, ev, site, runner, ex, notify, exit_fn))
        assert "exit(0)" not in calls
        assert calls[-1] == "sd:STOPPING=1"
