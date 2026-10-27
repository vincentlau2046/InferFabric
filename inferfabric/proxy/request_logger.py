"""单用户请求日志 — JSONL 按日轮转 + SQLite 持久化

每个请求完成时写一行 JSONL 到 logs/access-YYYY-MM-DD.jsonl，
同时附加到内存缓冲区，后台线程批量写入 request_log.db。
iff.yaml 新增 access_log_jsonl: true（默认 true），false 时不写 JSONL。
线程安全（threading.Lock 保护 fd/buffer 读写）。
"""

import json
import logging
import os
import queue
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from inferfabric.request_log_db import RequestLogDB

log = logging.getLogger("inferfabric.request_logger")


@dataclass
class RequestLog:
    """单个请求的结构化日志记录。"""
    req_id: str
    key_name: str
    model: str
    status: int
    ttft_ms: float | None = None       # 首 token 延迟
    tokens_in: int = 0
    tokens_in_cached: int = 0          # tokens_in 中命中前缀缓存的部分（缓存命中率 = cached/in）
    tokens_out: int = 0
    agent: str = ""      # 客户端 Agent id（agents.d 归一化，unknown 兜底；v6.5）
    ua: str = ""         # 原始 User-Agent（认领学习回路原料）
    x_app: str = ""      # 事实层：原始 x-app header（reclassify 原料；v6.6）
    duration_ms: float = 0.0
    route: str = "local"               # "local" | "cloud"
    cloud_provider: str | None = None   # "baidu-codingplan" | None
    error: str | None = None
    timestamp: float = 0.0   # time.time() at log time (G-2: sliding window)
    ts: str = ""


class RequestLogger:
    """JSONL 按日轮转请求日志 + SQLite 持久化。线程安全。

    - enabled=False → 所有 log() 调用为空操作
    - 按日自动轮转，实时 flush
    - 内存缓冲区 + 后台线程批量写入 SQLite (RequestLogDB)
    """

    def __init__(self, log_dir: str | Path = "logs", enabled: bool = True,
                 on_log_queue: queue.Queue | None = None,
                 db: "RequestLogDB | None" = None,
                 jsonl_enabled: bool = True,
                 batch_size: int = 50,
                 flush_interval: float = 2.0,
                 retention_days: int = 90):
        self._log_dir = Path(log_dir)
        self._enabled = enabled
        self._jsonl_enabled = jsonl_enabled
        self._current_date: str = ""
        self._fd: IO | None = None
        self._lock = threading.Lock()
        self._on_log_queue = on_log_queue

        # SQLite 持久化
        self._db = db
        self._buffer: list[dict] = []
        self._buf_lock = threading.Lock()
        # PR-20 评审修复 5：串行化所有 flush 执行（flush 线程/终刷/close），
        # 使 flush_blocking 能等出在途 DB 写并确认缓冲已清。RLock：close()
        # 与 flush_blocking 各自嵌套调用 _do_flush。
        self._flush_op_lock = threading.RLock()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._flush_event = threading.Event()
        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._retention_days = retention_days
        self._retention_seconds = retention_days * 86400

        if self._enabled and self._db:
            self._flush_thread = threading.Thread(
                target=self._flush_loop, daemon=True, name="reqlog-flush"
            )
            self._flush_thread.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def log(self, entry: RequestLog):
        """写一行 JSONL（如启用）+ 提交到 SQLite 缓冲区。线程安全。"""
        if not self._enabled:
            return
        # G-2: auto-fill timestamp if not set
        if entry.timestamp == 0:
            entry.timestamp = time.time()
        if not entry.ts:
            entry.ts = datetime.now(tz=timezone.utc).isoformat()

        # 1. JSONL (if enabled, unchanged)
        if self._jsonl_enabled:
            self._write_jsonl(entry)

        # 2. SQLite buffer — 持锁 append
        if self._db:
            d = {
                "req_id": entry.req_id,
                "key_name": entry.key_name,
                "model": entry.model,
                "status": entry.status,
                "ttft_ms": entry.ttft_ms,
                "tokens_in": entry.tokens_in,
                "tokens_in_cached": entry.tokens_in_cached,
                "tokens_out": entry.tokens_out,
                "agent": entry.agent,
                "ua": entry.ua,
                "x_app": entry.x_app,
                "duration_ms": entry.duration_ms,
                "route": entry.route,
                "cloud_provider": entry.cloud_provider,
                "error": entry.error,
                "timestamp": entry.timestamp,
                "ts": entry.ts,
            }
            with self._buf_lock:
                self._buffer.append(d)
                should_flush = len(self._buffer) >= self._batch_size
            if should_flush:
                self._flush_event.set()

        # 3. Queue (unchanged)
        if self._on_log_queue is not None:
            try:
                self._on_log_queue.put_nowait(entry)
            except Exception:
                log.warning("Failed to enqueue for aggregator", exc_info=True)

    def _write_jsonl(self, entry: RequestLog):
        """写一行 JSONL（内部方法，不验证 enabled/jsonl_enabled）。"""
        with self._lock:
            try:
                today = date.today().isoformat()
                if today != self._current_date:
                    self._rotate(today)
                if self._fd:
                    line = json.dumps(asdict(entry), ensure_ascii=False)
                    self._fd.write(line + "\n")
                    self._fd.flush()
            except Exception as e:
                log.warning("Failed to write request log: %s", e)

    def close(self):
        """关闭所有资源：flush 线程、DB、JSONL 文件句柄。

        无 flush 线程（db=None）时不关 _enabled，使 close() 后的 log() 可重新打开
        JSONL（恢复 P2 前行为）；有 flush 线程时 close() 后永久禁用。
        """
        if self._flush_thread:
            self._stop_event.set()
            self._flush_event.set()
            self._flush_thread.join(timeout=10)
            if self._flush_thread.is_alive():
                log.error("Flush thread did not terminate in 10s, "
                          "in-memory buffer may be lost")
            self._enabled = False
        self._do_flush()  # Final flush — flush thread 已退出，安全
        if self._db:
            try:
                self._db.checkpoint()
                self._db.close()
            except Exception:
                log.debug("DB checkpoint/close failed (ignored)", exc_info=True)
        # JSONL close (unchanged)
        with self._lock:
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
                self._current_date = ""

    def flush_blocking(self, timeout: float = 5.0) -> int:
        """评审修复 5：关停前的有界终刷 —— 只刷不关。

        os._exit(0) 会跳过后台 flush 线程与 close() 路径，缓冲区里未落库
        的请求日志会丢。本方法在排空完成后、硬退出前调用：
          1. 等出 flush 线程的在途 DB 写（已 swap-out 的批次不得被截断）；
          2. 清空缓冲区落库；
          3. 全程有界 —— DB 写卡死时按时返回，不拖垮关停预算。
        不停线程、不禁用、不关 DB/JSONL —— EDGE_HARD_EXIT=0 继续运行时
        logger 仍完全可用（与 close() 的关键差异）。
        返回本次落库条数；-1 = 未在 timeout 内完成（在途/卡死写）。
        """
        if not (self._enabled and self._db):
            return 0
        result = {"flushed": -1}

        def _final_flush():
            with self._flush_op_lock:  # 等 flush 线程的在途写完成
                with self._buf_lock:
                    pending = len(self._buffer)
                if pending:
                    self._do_flush()
                with self._buf_lock:
                    remaining = len(self._buffer)
                result["flushed"] = pending - remaining

        t = threading.Thread(target=_final_flush, daemon=True,
                             name="reqlog-final-flush")
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            log.warning("RequestLogger final flush unfinished within %.1fs "
                        "— buffered entries may be lost", timeout)
            return -1
        return result["flushed"]

    def _flush_loop(self):
        """后台 flush 线程。每 flush_interval 秒或收到 event 时 flush。"""
        while not self._stop_event.is_set():
            self._flush_event.wait(timeout=self._flush_interval)
            self._flush_event.clear()
            self._do_flush()
            # Lost-wakeup guard: flush 后 buffer 又满了则立即再 flush
            with self._buf_lock:
                if len(self._buffer) >= self._batch_size:
                    self._flush_event.set()

    def _do_flush(self):
        """将缓冲区刷入 SQLite。swap out buffer → lock-free DB write。

        PR-20 评审修复 5：_flush_op_lock 串行化所有 flush 执行（flush 线程/
        终刷/close），使 flush_blocking 能等出在途写并确认缓冲已清。
        """
        with self._flush_op_lock:
            # 持锁 swap out buffer
            with self._buf_lock:
                if not self._buffer:
                    return
                batch = self._buffer
                self._buffer = []  # 原子替换为空列表

            # 锁外执行 DB 写入（不阻塞 log()）
            try:
                self._db.insert_request_log(batch)
                log.debug("Flushed %d request_log entries to SQLite", len(batch))
            except Exception as e:
                log.error("Failed to flush request_log: %s", e)
                # 回插失败批次到 buffer 前端
                # 防止 buffer 无限膨胀（磁盘满场景）
                with self._buf_lock:
                    if len(self._buffer) + len(batch) > 10000:
                        overflow = len(self._buffer) + len(batch) - 10000
                        log.error("Buffer overflow on flush failure: discarding %d oldest entries",
                                 overflow)
                        batch = batch[overflow:]
                    self._buffer = batch + self._buffer

            # 惰性清理（1% 概率）
            if random.random() < 0.01:
                self._maybe_prune()

    def _maybe_prune(self):
        """惰性清理过期日志记录（1% 概率触发）。"""
        try:
            cutoff = time.time() - self._retention_seconds
            deleted = self._db.prune_request_log(cutoff)
            if deleted > 0:
                log.info("Pruned %d request_log entries older than %d days",
                         deleted, self._retention_days)
        except Exception as e:
            log.warning("Prune failed: %s", e)

    def _rotate(self, new_date: str):
        """按日轮转到新文件（调用者需持锁）。"""
        # Close old fd
        if self._fd:
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._current_date = ""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f"access-{new_date}.jsonl"
            self._fd = open(path, "a", encoding="utf-8")
            self._current_date = new_date
            log.debug("Rotated access log to %s", path)
        except Exception as e:
            log.error("Failed to rotate access log: %s", e)
            self._fd = None
