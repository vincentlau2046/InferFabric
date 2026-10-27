"""
inferfabric/proxy/async_server.py — aiohttp 生产级 HTTP 边缘 (PR-19)

启动方式：
  python -m inferfabric.proxy.handler --async

混合执行模型：
  * 现有同步 route fn（fn(h, pm) 契约）与同步 forwarder 全部在
    ThreadPoolExecutor 中执行 —— 转发核心不变（PR-14 排除条款继续有效）。
  * 7 条转发路由经 StreamResponse 原生流式回传：worker 线程把 forwarder
    写出的手工 chunked 帧喂入线程安全的 wfile sink，sink 解帧（unframe）
    后经 asyncio.Queue 泵到 StreamResponse —— 真增量 SSE，无整包缓冲。
  * 其余 ~30 条管理/查询路由整包缓冲为单个 web.Response，完整头传播
    （ETag / Retry-After / CSP / CORS…）；content-length、
    transfer-encoding、connection 由 aiohttp 自管（strip 集）。

相对实验版（R7 Step 1）修复的缺陷：
  1. 流式响应整包缓冲 → 原生增量流式
  2. 响应头只传 content-type → 完整头传播（除 strip 集）
  3. 默认 client_max_size=1MB 会截断 100MB payload → 显式 100MB
  4. h.path 丢查询串 → path_qs
  5. _MockServer 缺 watchdog（/watchdog_status 503）
  6. 缺 health_loop / TokenStatsCollector / 启动 reconcile /
     WATCHDOG=1 / STOPPING=1 / pycache 清除 / 双 excepthook
"""

import asyncio
import errno
import io
import json
import logging
import os
import shutil
import signal as signal_mod
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from inferfabric import forwarder  # noqa: F401  (保持 _deps 注入顺序与线程模式一致)
from inferfabric.proxy.handler import (
    ProxyHandler,
    _validate_admin_token_safety,
    PROXY_HOST, PROXY_PORT,
    HEALTH_CHECK_INTERVAL, WATCHDOG_INTERVAL,
)
from inferfabric.proxy import handler as handler_module
from inferfabric.proxy_manager import ProxyManager
from inferfabric.watchdog import ModelWatchdog

log = logging.getLogger("inferfabric.async_server")

# 请求体上限 —— 与 forwarder.read_body 的 100MB 限制一致
MAX_BODY_BYTES = 100 * 1024 * 1024

# aiohttp 自管分帧/连接：这些头不得透传（避免双重分帧/冲突）
_STRIP_HEADERS = {"content-length", "transfer-encoding", "connection"}

# 7 条转发路由（可能流式 / 大 body）→ 走 StreamResponse 泵送路径
_FORWARD_POST_PATHS = {
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/messages",
    "/api/chat",
    "/api/generate",
    "/v1/embeddings",
    "/v1/rerank",
}

_SENTINEL = object()


# ═══════════════════════════════════════════════════════════════
# wfile 替身
# ═══════════════════════════════════════════════════════════════

class _WfileSink:
    """worker 线程使用的线程安全 wfile 替身（流式路由）。

    同步 forwarder 对流式响应写手工 chunked 帧（hex 尺寸 + CRLF）；
    aiohttp 原生分帧，故 sink 必须解帧只出 payload，否则会双重分帧。
    两种模式（在 end_headers 时按响应头判定）：
      * unframe：解析手工帧，发 payload；终止帧 0 尺寸 → _SENTINEL
      * passthrough：原样字节透传（JSON 整包 / ollama 原生 SSE）
    客户端断连：mark_client_gone() 后 write 抛 BrokenPipeError，
    由 forwarder 既有 except (BrokenPipeError, ConnectionResetError) 捕获。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._buf = b""
        self._unframe = False
        self._gone = False
        self._enqueue = None  # 由 build 侧注入：loop.call_soon_threadsafe 包装

    # ── worker 侧 API（在 executor 线程中被调用）──
    def write(self, data: bytes) -> int:
        if self._gone:
            raise BrokenPipeError("client disconnected")
        if not data:
            return 0
        with self._lock:
            if not self._unframe:
                items = [data]
            else:
                self._buf += data
                items = self._drain_frames()
        for item in items:
            if self._enqueue is not None:
                self._enqueue(item)
        return len(data)

    def flush(self):
        pass

    def set_unframe(self):
        """end_headers 捕获到 Transfer-Encoding: chunked 时调用。"""
        with self._lock:
            self._unframe = True

    def mark_client_gone(self):
        self._gone = True

    def _drain_frames(self):
        """从 self._buf 解析完整帧，返回 payload item 列表（持有锁调用）。"""
        out = []
        while True:
            buf = self._buf
            nl = buf.find(b"\r\n")
            if nl < 0:
                break  # 尺寸行未收全
            size_tok = buf[:nl]
            if not size_tok:
                self._buf = buf[nl + 2:]  # 防御：容忍空行
                continue
            try:
                size = int(size_tok.split(b";")[0], 16)
            except ValueError:
                # 畸形帧 —— 放弃解帧，剩余字节原样透传
                out.append(buf)
                self._buf = b""
                self._unframe = False
                return out
            if size == 0:
                self._buf = b""
                out.append(_SENTINEL)
                return out
            end = nl + 2 + size + 2  # payload + 尾部 CRLF
            if len(buf) < end:
                break  # 帧未收全，等下次 write
            out.append(buf[nl + 2: nl + 2 + size])
            self._buf = buf[end:]
        return out


class _PlainSink:
    """非流式（整包缓冲）路由的 wfile 替身。"""

    def __init__(self):
        self._buf = io.BytesIO()

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        self._buf.write(data)
        return len(data)

    def flush(self):
        pass

    def set_unframe(self):
        pass

    def mark_client_gone(self):
        pass

    def getvalue(self) -> bytes:
        return self._buf.getvalue()


# ═══════════════════════════════════════════════════════════════
# ProxyHandler 内存 shim
# ═══════════════════════════════════════════════════════════════

class _MockServer:
    """线程版 HTTPServer 的最小替身 — 持有 proxy_mgr + watchdog。"""

    def __init__(self, pm, watchdog):
        self.proxy_mgr = pm
        self.watchdog = watchdog
        self.server_close = lambda: None


def _build_shim(request, body, pm, watchdog, wfile, loop):
    """创建 ProxyHandler 内存实例：socket I/O 全部替换为内存版本。

    属性对照 stdlib self.*：
      path = request.path_qs   （stdlib self.path 含查询串，chat_handlers
                                ._forward_request 会把它原样转发给上游）
      headers = request.headers（CIMultiDictProxy，大小写不敏感 .get()，
                                与 email.message.Message 用法一致）
    """
    h = ProxyHandler.__new__(ProxyHandler)
    h.server = _MockServer(pm, watchdog)
    h.path = request.path_qs
    h.command = request.method
    h.headers = request.headers
    h.rfile = io.BytesIO(body)
    h.wfile = wfile
    h._req_id = ""
    h._req_start = time.monotonic()
    h._key_name = "anonymous"
    h._usage = {"prompt_tokens": 0, "completion_tokens": 0}
    h._ttft_ms = None

    # 响应捕获
    h._resp_status = 200
    h._resp_headers = {}
    h._headers_locked = False
    h._headers_evt = asyncio.Event()

    def _send_response(status, message=None):
        # 头锁：流已开始后二次 send_response（forwarder 重试路径）忽略，
        # 比 stdlib 的双状态行更安全
        if h._headers_locked:
            return
        h._resp_status = status

    def _send_header(key, value):
        if h._headers_locked or value is None:
            return
        h._resp_headers[key] = str(value)

    def _end_headers():
        if h._headers_locked:
            return
        h._headers_locked = True
        te = h._resp_headers.get("Transfer-Encoding", "")
        if te.lower() == "chunked":
            wfile.set_unframe()
        try:
            loop.call_soon_threadsafe(h._headers_evt.set)
        except RuntimeError:
            pass  # loop 已关闭（关停中）

    h.send_response = _send_response
    h.send_header = _send_header
    h.end_headers = _end_headers
    return h


# ═══════════════════════════════════════════════════════════════
# 响应工具
# ═══════════════════════════════════════════════════════════════

def _json_response(payload, status=200):
    """JSON 响应体与 stdlib forwarder.send_json 逐字节一致（ensure_ascii=False）。"""
    body = json.dumps(payload, ensure_ascii=False).encode()
    return web.Response(status=status, body=body,
                       content_type="application/json", charset="utf-8")


def _filtered_headers(captured: dict) -> dict:
    """strip 掉 aiohttp 自管的头；None 值（extra_headers 跳过标记）一并丢弃。"""
    return {k: v for k, v in captured.items()
            if v is not None and k.lower() not in _STRIP_HEADERS}


async def _read_body(request):
    """预读请求体（aiohttp 原生解码 chunked 请求体）。

    超限 → 与 forwarder.read_body 一致的 413 JSON 体。
    返回 bytes 或 web.Response（413 时）。
    """
    cl = request.headers.get("Content-Length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return _json_response({"error": "payload too large (max 100MB)"}, 413)
        except ValueError:
            pass
    try:
        return await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _json_response({"error": "payload too large (max 100MB)"}, 413)


def _log_worker_error(fut):
    exc = fut.exception()
    if exc is not None:
        log.exception("worker error (detached): %s", exc)


# ═══════════════════════════════════════════════════════════════
# 路由派发
# ═══════════════════════════════════════════════════════════════

async def _handle_forward(request):
    """7 条转发路由：worker 线程跑同步 route fn，流增量泵到客户端。"""
    pm = request.app[_KEY_PM]
    executor = request.app[_KEY_EXECUTOR]
    watchdog = request.app[_KEY_WATCHDOG]

    body = await _read_body(request)
    if isinstance(body, web.Response):
        return body

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=1024)  # ~8MB 背压上限
    sink = _WfileSink()

    def _enqueue(item):
        def _put():
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # 背压超限（客户端持续慢 8MB+）→ 停止推送（截断，已文档化）
                sink.mark_client_gone()
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # loop 已关闭

    sink._enqueue = _enqueue
    h = _build_shim(request, body, pm, watchdog, sink, loop)

    fn = handler_module._POST_ROUTES.get(request.path)
    if fn is None:
        return _json_response({"error": "not found"}, 404)

    worker = loop.run_in_executor(executor, fn, h, pm)

    # passthrough 模式（非流式整包）没有 0 尺寸终止帧 —— worker 结束即流结束。
    # unframe 模式的多余 _SENTINEL 无害（泵在第一个即退出，队列随请求销毁）。
    def _on_worker_done(_f):
        try:
            loop.call_soon_threadsafe(_enqueue, _SENTINEL)
        except RuntimeError:
            pass  # loop 已关闭
    worker.add_done_callback(_on_worker_done)

    # 等待"头就绪"或"worker 结束"（worker 提前失败时头不会来）
    headers_task = loop.create_task(h._headers_evt.wait())
    done, pending = await asyncio.wait(
        (headers_task, worker),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if headers_task in pending:
        headers_task.cancel()  # 未触发则取消等待任务（避免 never-awaited 告警）
    if not h._headers_locked:
        try:
            await worker
        except Exception:
            log.exception("forward route %s failed before headers", request.path)
        return _json_response({"error": "internal error"}, 500)

    resp = web.StreamResponse(status=h._resp_status)
    for k, v in _filtered_headers(h._resp_headers).items():
        resp.headers[k] = v
    await resp.prepare(request)

    detached = False
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            try:
                await resp.write(item)
            except (ConnectionResetError, BrokenPipeError, OSError):
                # 含 aiohttp ClientConnectionResetError（ConnectionResetError 子类）
                log.info("client disconnected during %s", request.path)
                sink.mark_client_gone()
                detached = True
                break
    except asyncio.CancelledError:
        # handler_cancellation：连接丢失 → 任务被取消 → 停推（worker 的
        # 后续 write 抛 BrokenPipeError，forwarder 既有 except 捕获）
        log.info("client connection lost during %s", request.path)
        sink.mark_client_gone()
        detached = True
        raise
    try:
        await resp.write_eof()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass

    if detached:
        # worker 仍在跑（上游读有 300s 超时兜底）—— 不再阻塞本请求协程
        worker.add_done_callback(_log_worker_error)
    else:
        try:
            await worker
        except Exception:
            log.exception("worker error after response started: %s", request.path)
    return resp


def _make_buffered_handler(method):
    """管理/查询路由：整包缓冲 + 头传播（行为与线程模式一致，除 strip 集）。"""
    tables = {
        "GET": lambda: handler_module._GET_ROUTES,
        "POST": lambda: handler_module._POST_ROUTES,
        "DELETE": lambda: handler_module._DELETE_ROUTES,
    }[method]

    async def _handle(request):
        pm = request.app[_KEY_PM]
        executor = request.app[_KEY_EXECUTOR]
        watchdog = request.app[_KEY_WATCHDOG]

        body = await _read_body(request)
        if isinstance(body, web.Response):
            return body

        loop = asyncio.get_running_loop()
        wfile = _PlainSink()
        h = _build_shim(request, body, pm, watchdog, wfile, loop)

        fn = tables().get(request.path)
        if fn is None:
            return _json_response({"error": "not found"}, 404)

        try:
            await loop.run_in_executor(executor, fn, h, pm)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("route %s %s failed", method, request.path)
            if h._headers_locked:
                # worker 已发出部分响应后异常 —— 尽力返回已捕获内容
                return web.Response(status=h._resp_status,
                                    headers=_filtered_headers(h._resp_headers),
                                    body=wfile.getvalue())
            return _json_response({"error": "internal error"}, 500)

        if not h._headers_locked:
            # route fn 未产生任何响应（对齐 stdlib 的"连接直接关闭"语义）
            return web.Response(status=h._resp_status)
        return web.Response(status=h._resp_status,
                            headers=_filtered_headers(h._resp_headers),
                            body=wfile.getvalue())

    return _handle


async def _handle_options(request):
    """对齐 do_OPTIONS：204 + CORS。"""
    return web.Response(status=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })


async def _not_found(request):
    """对齐 do_GET/do_POST 的未知路径响应体。"""
    return _json_response({"error": "not found"}, 404)


# 类型化 AppKey（避免 NotAppKeyWarning）
_KEY_PM = web.AppKey("pm", object)
_KEY_EXECUTOR = web.AppKey("executor", object)
_KEY_WATCHDOG = web.AppKey("watchdog", object)


def build_app(pm, executor, watchdog, client_max_size=MAX_BODY_BYTES):
    """构建 aiohttp Application（路由表逐条注册 + catch-all）。

    注意：handler_cancellation=True 在 Server 层（经 AppRunner/TestServer 的
    kwargs 传入，见 _run 与测试）—— 连接丢失（RST）时取消 handler 任务 →
    转发路由的 pump 协程走 CancelledError 分支停推（见 _handle_forward）。
    """
    app = web.Application(client_max_size=client_max_size)
    app[_KEY_PM] = pm
    app[_KEY_EXECUTOR] = executor
    app[_KEY_WATCHDOG] = watchdog

    for path in handler_module._GET_ROUTES:
        app.router.add_get(path, _make_buffered_handler("GET"))
    for path in handler_module._POST_ROUTES:
        if path in _FORWARD_POST_PATHS:
            app.router.add_post(path, _handle_forward)
        else:
            app.router.add_post(path, _make_buffered_handler("POST"))
    for path in handler_module._DELETE_ROUTES:
        app.router.add_delete(path, _make_buffered_handler("DELETE"))

    # catch-all（精确路由优先于模式路由）
    app.router.add_route("OPTIONS", "/{tail:.*}", _handle_options)
    app.router.add_get("/{tail:.*}", _not_found)
    app.router.add_post("/{tail:.*}", _not_found)
    app.router.add_delete("/{tail:.*}", _not_found)
    return app


# ═══════════════════════════════════════════════════════════════
# 服务启动 / 生命周期（与线程版 main() 对齐）
# ═══════════════════════════════════════════════════════════════

async def _start_site_once(site):
    """patch 缝：测试可 monkeypatch 此函数模拟 EADDRINUSE。"""
    await site.start()


async def start_site_with_retry(runner, host=None, port=None,
                                retries=5, retry_delay=2.0):
    """绑定端口，EADDRINUSE 有界重试（复刻 handler._create_server 语义）。"""
    host = PROXY_HOST if host is None else host
    port = PROXY_PORT if port is None else port
    site = web.TCPSite(runner, host, port)
    for attempt in range(retries):
        try:
            await _start_site_once(site)
            return site
        except OSError as e:
            if e.errno == errno.EADDRINUSE and attempt < retries - 1:
                log.warning(
                    "Port %d in use (stale proxy still bound?) — retry %d/%d in %.0fs",
                    port, attempt + 1, retries, retry_delay,
                )
                await asyncio.sleep(retry_delay)
            else:
                log.error("Cannot bind %s:%d: %s", host, port, e)
                raise
    raise RuntimeError("unreachable")


def _build_runner(app) -> web.AppRunner:
    """PR-20 ①：有界排空 — shutdown_timeout 默认 30s（原 60s 默认）。

    排空上限 = SIGTERM 后在途请求最长等待，超限由 aiohttp 取消剩余
    handler；env EDGE_SHUTDOWN_DRAIN 可调。P0 实测：60s 排空上限 +
    非守护线程 join = 91.7s 拒连窗口的两大组成部分。
    """
    drain = float(os.environ.get("EDGE_SHUTDOWN_DRAIN", "30"))
    return web.AppRunner(app, keepalive_timeout=75, access_log=None,
                         handler_cancellation=True, shutdown_timeout=drain)


async def _shutdown_gracefully(mgr, watchdog, shutdown_event, site, runner,
                               executor, sd_notify, exit_fn=None):
    """PR-20 ①③：有序关停序列 — 封印 → 停后台 → 有界排空 → 硬退出。

    - begin_shutdown：封印 StateDB（垂死进程禁写状态，③）+ 停
      health_monitor/telemetry 后台写入者；watchdog 也在此停（关停期
      不得再自动重启模型）。
    - site.stop → runner.cleanup：端口先收，在途请求有界排空
      （≤ shutdown_timeout，由 _build_runner 设定）。
    - 硬退出 os._exit(0)：绕过非守护 executor 转发线程的 join
      （P0 实测 60–91.7s 挂死根因；executor.shutdown(wait=False) 不够，
      Python 退出时仍 join）。EDGE_HARD_EXIT=0 关闭（调试走
      start_async finally 兜底）。systemd TimeoutStopSec=45 最终兜底。
    """
    if exit_fn is None:
        exit_fn = os._exit
    log.info("Shutting down async edge...")
    try:
        mgr.begin_shutdown("proxy shutdown")
    except Exception as e:
        log.exception("begin_shutdown failed — continuing shutdown: %s", e)
    shutdown_event.set()  # health_loop / sd-watchdog 退出
    try:
        watchdog.stop()
    except Exception as e:
        log.warning("watchdog stop failed: %s", e)
    async def drain():
        await site.stop()
        await runner.cleanup()

    # aiohttp may spend shutdown_timeout twice (handler completion and
    # cancellation). Bound the entire cleanup, not merely each internal phase.
    cleanup = asyncio.create_task(drain())
    try:
        deadline = float(os.environ.get("EDGE_SHUTDOWN_DRAIN", "30"))
        done, _ = await asyncio.wait({cleanup}, timeout=deadline)
        if done:
            cleanup.result()
        else:
            cleanup.cancel()
            log.warning("Total drain deadline reached (%.1fs)", deadline)
    except Exception:
        cleanup.cancel()
        log.exception("Graceful drain error — proceeding to hard exit")

    # PR-20 评审修复 5：排空后、硬退出前的有界终刷 —— os._exit 会跳过
    # 后台 flush 线程与 close() 路径，缓冲区里的请求日志必须在此落库。
    # 只刷不关：EDGE_HARD_EXIT=0 继续运行时 logger 仍完全可用；
    # 终刷失败不阻断关停（尽力而为，退出必须发生）。
    logger = getattr(getattr(mgr, "telemetry", None), "logger", None)
    if logger is not None and hasattr(logger, "flush_blocking"):
        try:
            flushed = logger.flush_blocking(timeout=5.0)
            if flushed:
                log.info("Final request-log flush: %d entries persisted", flushed)
        except Exception:
            log.warning("Final request-log flush failed — continuing shutdown",
                        exc_info=True)

    # 在跑 worker 有界（gate 30s / 上游 300s 超时兜底），不做无限阻塞 join；
    # systemd TimeoutStopSec 是最终兜底（与线程版 daemon_threads 语义等价）
    executor.shutdown(wait=False)
    sd_notify("STOPPING=1")
    if os.environ.get("EDGE_HARD_EXIT", "1") != "0":
        log.info("Shutdown complete — hard exit (bypass non-daemon thread joins)")
        exit_fn(0)
    else:
        log.info("Shutdown complete (EDGE_HARD_EXIT=0 — soft return)")


async def _run(mgr, watchdog, shutdown_event, sd_notify):
    """事件循环主体：executor + ClientSession 无关（PR-19 转发仍同步）+ 有序关停。"""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=int(os.environ.get("EDGE_ASYNC_WORKERS", "32")),
        thread_name_prefix="iff-async",
    )
    app = build_app(mgr, executor, watchdog)
    runner = _build_runner(app)
    await runner.setup()
    site = await start_site_with_retry(runner)

    log.info("InferFabric async edge: %s:%d (auto_switch=%s, v%s)",
             PROXY_HOST, PROXY_PORT, mgr.auto_switch,
             __import__("inferfabric").__version__)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)

    stopped = asyncio.Event()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except (ValueError, RuntimeError, NotImplementedError):
            pass

    sd_notify("READY=1")

    if os.environ.get("NOTIFY_SOCKET"):
        def _watchdog_notify_loop():
            while not shutdown_event.is_set():
                shutdown_event.wait(WATCHDOG_INTERVAL)
                if not shutdown_event.is_set():
                    sd_notify("WATCHDOG=1")
        threading.Thread(target=_watchdog_notify_loop, daemon=True,
                         name="sd-watchdog").start()

    await stopped.wait()
    await _shutdown_gracefully(mgr, watchdog, shutdown_event, site, runner,
                               executor, sd_notify)


def start_async():
    """入口：与线程版 main() 全生命周期对齐。"""
    import traceback

    def _global_excepthook(exc_type, exc_value, exc_tb):
        logging.critical('Unhandled exception (%s): %s', exc_type.__name__, exc_value)
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = _global_excepthook

    def _thread_excepthook(args):
        logging.critical('Unhandled thread exception: %s', args.exc_value)
    threading.excepthook = _thread_excepthook

    # Purge stale .pyc to prevent version-desync (INF-001649)
    pkg_dir = Path(__file__).resolve().parent.parent
    pycache = pkg_dir / "__pycache__"
    if pycache.is_dir():
        shutil.rmtree(pycache, ignore_errors=True)
        logging.getLogger("inferfabric").debug("Purged stale __pycache__: %s", pycache)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    log_dir = Path.home() / ".inferfabric" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_dir / "proxy.log", maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logging.getLogger("inferfabric").addHandler(fh)

    _validate_admin_token_safety()

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    # 运行时健康 watchdog
    watchdog = ModelWatchdog(mgr.mgr, check_interval=30, auto_restart=True)
    watchdog.start()

    # v5.2: 统一热加载（含 agents 域 registry；与线程版 main 共用同一装配）
    from inferfabric.config_reloader import build_config_reloader
    config_reloader = build_config_reloader(mgr, auth=mgr.auth, cloud=mgr.cloud)
    mgr.config_reloader = config_reloader
    config_reloader.setup()

    # 启动 reconcile（对齐线程版 main）
    try:
        rec = mgr.mgr.reconcile()
        if rec.get("actions"):
            log.info("Startup reconcile: %s", rec["actions"])
    except Exception as e:
        log.warning("Startup reconcile failed: %s", e)

    def health_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if not shutdown_event.is_set():
                mgr.health_check()
    threading.Thread(target=health_loop, daemon=True, name="health").start()

    # token 统计采集器（5 分钟间隔）。用 TelemetryHub 里带 db 的 collector：
    # DB 驱动、引擎无关（含 ninfer/ollama），按 cloud_provider 拆 local/cloud；
    # 后台线程每周期从 request_log.db 重聚合并写回 token-stats.json。
    mgr.telemetry.start_token_collector(lambda: mgr.mgr)
    # GPU 板卡功耗采样器（60s，独立于前端轮询——页面关着历史也连续）。
    # 数据源：监控 TAB「功耗 / 电费」卡（/api/power，5min TTL）。
    mgr.telemetry.start_power_sampler()

    _notify_socket = os.environ.get('NOTIFY_SOCKET')

    def sd_notify(message: str):
        if not _notify_socket:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(_notify_socket)
            sock.sendall(message.encode())
            sock.close()
        except Exception:
            pass

    try:
        asyncio.run(_run(mgr, watchdog, shutdown_event, sd_notify))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        watchdog.stop()
        mgr.telemetry.token_collector.stop()
        mgr.telemetry.power_sampler.stop()
        mgr.health_monitor.stop()
        sd_notify("STOPPING=1")
        log.info("InferFabric async server stopped.")
