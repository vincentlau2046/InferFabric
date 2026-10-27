"""
unit/proxy/test_request_logger.py — 请求日志记录器测试

测试对象: inferfabric.proxy.request_logger.RequestLog, RequestLogger
覆盖范围:
  - RequestLog dataclass 默认值 & 序列化
  - enabled=False 时 no-op
  - enabled=True 时 JSONL 写入、自动时间戳、cloud route、error 记录
  - 按日轮转、close 后 reopen
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from inferfabric.proxy.request_logger import RequestLog, RequestLogger


class TestRequestLog:
    """RequestLog dataclass"""

    def test_defaults(self):
        entry = RequestLog(req_id="iff-1", key_name="primary", model="test", status=200)
        assert entry.route == "local"
        assert entry.cloud_provider is None
        assert entry.ttft_ms is None
        assert entry.tokens_in == 0
        assert entry.tokens_in_cached == 0
        assert entry.error is None

    def test_tokens_in_cached_field(self):
        """缓存命中 token 计数（v6.1 缓存命中率统计）。"""
        entry = RequestLog(req_id="iff-2", key_name="primary", model="qwen38",
                           status=200, tokens_in=145514,
                           tokens_in_cached=129428, tokens_out=2112)
        assert entry.tokens_in_cached == 129428

    def test_asdict(self):
        entry = RequestLog(
            req_id="iff-1", key_name="primary", model="qwen36-35b-vl",
            status=200, ttft_ms=487.5, duration_ms=2100, route="local",
        )
        d = entry.__dict__.copy()
        d["ts"] = "2026-07-30T12:00:00+08:00"
        line = json.dumps(d, ensure_ascii=False)
        assert "iff-1" in line
        assert "487.5" in line


class TestRequestLoggerDisabled:
    """enabled=False → 不写日志"""

    def test_disabled_no_file(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=False)
        entry = RequestLog(req_id="iff-1", key_name="primary", model="test", status=200)
        logger.log(entry)
        # No files created
        assert list(tmp_path.iterdir()) == []

    def test_disabled_log_is_noop(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=False)
        assert not logger.enabled
        # Should not raise
        logger.log(RequestLog(req_id="iff-1", key_name="p", model="m", status=200))


class TestRequestLoggerEnabled:
    """enabled=True → 写 JSONL"""

    def test_write_single_entry(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        entry = RequestLog(
            req_id="iff-7f3a", key_name="primary", model="qwen36-35b-vl",
            status=200, ttft_ms=487, duration_ms=2100, route="local",
        )
        logger.log(entry)
        logger.close()

        today = date.today().isoformat()
        log_file = tmp_path / f"access-{today}.jsonl"
        assert log_file.exists()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["req_id"] == "iff-7f3a"
        assert data["model"] == "qwen36-35b-vl"
        assert data["ttft_ms"] == 487
        assert data["route"] == "local"

    def test_write_multiple_entries(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        for i in range(3):
            logger.log(RequestLog(
                req_id=f"iff-{i}", key_name="primary", model="test", status=200,
            ))
        logger.close()

        today = date.today().isoformat()
        lines = (tmp_path / f"access-{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3

    def test_auto_timestamp(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        entry = RequestLog(req_id="iff-1", key_name="p", model="m", status=200)
        logger.log(entry)
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["ts"]  # non-empty
        assert "2026" in data["ts"]  # reasonable year

    def test_cloud_route(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(
            req_id="iff-1", key_name="primary", model="deepseek-v4-flash",
            status=200, route="cloud", cloud_provider="baidu-codingplan",
        ))
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["route"] == "cloud"
        assert data["cloud_provider"] == "baidu-codingplan"

    def test_error_entry(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(
            req_id="iff-1", key_name="primary", model="test",
            status=429, error="rate_limit",
        ))
        logger.close()

        today = date.today().isoformat()
        data = json.loads((tmp_path / f"access-{today}.jsonl").read_text().strip())
        assert data["status"] == 429
        assert data["error"] == "rate_limit"


class TestRequestLoggerRotation:
    """按日轮转"""

    def test_close_and_reopen(self, tmp_path):
        logger = RequestLogger(log_dir=tmp_path, enabled=True)
        logger.log(RequestLog(req_id="iff-1", key_name="p", model="m", status=200))
        logger.close()
        assert logger._fd is None

        # Re-log should reopen
        logger.log(RequestLog(req_id="iff-2", key_name="p", model="m", status=200))
        logger.close()

        today = date.today().isoformat()
        lines = (tmp_path / f"access-{today}.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2


class TestFlushBlocking:
    """PR-20 评审修复 5：关停前的有界终刷。

    os._exit(0) 跳过后台 flush 线程与 close() 路径 —— 缓冲区里未落库的
    请求日志会丢。flush_blocking：等出 flush 线程的在途写 + 清空缓冲，
    全程有界；只刷不关（EDGE_HARD_EXIT=0 继续运行时 logger 仍可用）。
    """

    def _entry(self, i):
        return RequestLog(req_id=f"r{i}", key_name="primary", model="m", status=200)

    def test_flushes_buffered_entries_without_closing(self, tmp_path):
        """缓冲条目落库；logger 不被关闭/禁用（与 close() 的关键差异）。"""
        import threading
        from unittest.mock import MagicMock

        db = MagicMock()
        lg = RequestLogger(log_dir=tmp_path, db=db, jsonl_enabled=False,
                           batch_size=1000, flush_interval=999)
        try:
            for i in range(3):
                lg.log(self._entry(i))
            with lg._buf_lock:
                assert len(lg._buffer) == 3  # 未到批量阈值，仍在缓冲

            flushed = lg.flush_blocking(timeout=2)

            assert flushed == 3
            db.insert_request_log.assert_called_once()
            assert lg.enabled                      # 只刷不关
            assert lg._flush_thread.is_alive()     # flush 线程未停
            lg.log(self._entry(9))                 # 仍可继续记录
            with lg._buf_lock:
                assert len(lg._buffer) == 1
        finally:
            lg.close()

    def test_waits_for_inflight_flush_thread_write(self, tmp_path):
        """在途写（已 swap-out、正在 insert）必须等完成 —— 否则 os._exit 截断丢失。"""
        import threading
        from unittest.mock import MagicMock

        db = MagicMock()
        in_write, release = threading.Event(), threading.Event()

        def slow_insert(batch):
            in_write.set()
            release.wait(5)

        db.insert_request_log.side_effect = slow_insert
        lg = RequestLogger(log_dir=tmp_path, db=db, jsonl_enabled=False,
                           batch_size=2, flush_interval=999)
        try:
            lg.log(self._entry(0))
            lg.log(self._entry(1))  # 到批量阈值 → flush 线程取走，卡在 insert
            assert in_write.wait(2)

            done = threading.Event()
            t = threading.Thread(
                target=lambda: (lg.flush_blocking(timeout=5), done.set()),
                daemon=True)
            t.start()
            # 在途写未完成 → 终刷不得提前返回
            assert not done.wait(0.3), "flush_blocking 在在途 DB 写未完成时提前返回"
            release.set()
            assert done.wait(3), "在途写完成后 flush_blocking 应返回"
        finally:
            release.set()
            lg.close()

    def test_bounded_when_db_write_stuck(self, tmp_path):
        """DB 写卡死 → flush_blocking 必须在 timeout 内有界返回。"""
        import time as _t
        import threading
        from unittest.mock import MagicMock

        db = MagicMock()
        release = threading.Event()

        def stuck_insert(batch):
            release.wait(10)

        db.insert_request_log.side_effect = stuck_insert
        lg = RequestLogger(log_dir=tmp_path, db=db, jsonl_enabled=False,
                           batch_size=1000, flush_interval=999)
        try:
            lg.log(self._entry(0))
            t0 = _t.monotonic()
            flushed = lg.flush_blocking(timeout=0.2)
            elapsed = _t.monotonic() - t0
            assert elapsed < 2.0, f"flush_blocking 应有界（0.2s 预算），实际 {elapsed:.1f}s"
            assert flushed == -1  # 未在预算内完成
        finally:
            release.set()
            lg.close()

    def test_no_db_or_disabled_is_noop(self, tmp_path):
        """无 db / 禁用时 flush_blocking 是无害 no-op（返回 0）。"""
        from unittest.mock import MagicMock

        lg = RequestLogger(log_dir=tmp_path, db=None, jsonl_enabled=False)
        assert lg.flush_blocking(timeout=1) == 0
        lg.close()

        lg2 = RequestLogger(log_dir=tmp_path, enabled=False, db=MagicMock(),
                            jsonl_enabled=False)
        assert lg2.flush_blocking(timeout=1) == 0
        lg2.close()
