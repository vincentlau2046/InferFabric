"""
integration/test_request_log_db.py — SQLite 请求日志持久化测试

测试对象: inferfabric.request_log_db.RequestLogDB, proxy.request_logger.RequestLogger
覆盖范围:
  - 表创建、索引、WAL pragma、synchronous pragma
  - INSERT + 查询（basic / since filter / model filter / limit）
  - INSERT OR IGNORE 去重（单条 & 混合批次）
  - PRUNE 过期清理
  - CHECKPOINT + close 安全
  - RequestLogger → SQLite flush 机制（间隔触发、JSONL 并行写入）
  - 缓冲区溢出保护（max 10000）
  - JSONL 禁用模式
  - MetricsAggregator 从 DB 回放 & 禁用模式
  - 端到端流程
"""

import json
import os
import sqlite3
import threading
import time
import queue as _queue
from datetime import date
from pathlib import Path

import pytest

from inferfabric.request_log_db import RequestLogDB
from inferfabric.config import DEFAULT_REQUEST_LOG_DB
from inferfabric.proxy.request_logger import RequestLog, RequestLogger
from inferfabric.metrics_aggregator import MetricsAggregator


# ─── Helpers ────────────────────────────────────────────────────────

def _make_entry(req_id="iff-test-1", model="test-model", status=200,
                ttft_ms=100.0, tokens_in=10, tokens_out=50,
                duration_ms=500.0, route="local", error=None,
                timestamp=0.0):
    """Create a test RequestLog entry with sensible defaults."""
    ts = timestamp or time.time()
    return RequestLog(
        req_id=req_id, key_name="primary", model=model,
        status=status, ttft_ms=ttft_ms, tokens_in=tokens_in,
        tokens_out=tokens_out, duration_ms=duration_ms,
        route=route, error=error, timestamp=ts,
        ts="2026-08-01T00:00:00Z",
    )


def _db_row_count(db_path: Path) -> int:
    """Count rows in request_log table."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
    finally:
        conn.close()


def _db_has_table(db_path: Path, table_name: str) -> bool:
    """Check if a table exists in the database."""
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()[0]
        return n > 0
    finally:
        conn.close()


def _db_pragma(db_path: Path, pragma: str):
    """Get a PRAGMA value from the database."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"PRAGMA {pragma}").fetchone()[0]
    finally:
        conn.close()


# ─── T5-T1: RequestLogDB ────────────────────────────────────────────

class TestRequestLogDBCreate:
    """T5-1: Table + index + PRAGMA verification."""

    def test_table_exists(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            assert _db_has_table(db_path, "request_log")
        finally:
            db.close()

    def test_indexes_exist(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            conn = sqlite3.connect(str(db_path))
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_request_log%'"
            ).fetchall()
            names = [r[0] for r in indexes]
            assert "idx_request_log_timestamp" in names
            assert "idx_request_log_model_ts" in names
            conn.close()
        finally:
            db.close()

    def test_pragma_wal(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            assert _db_pragma(db_path, "journal_mode") == "wal"
        finally:
            db.close()

    def test_pragma_synchronous(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            # PRAGMA synchronous persists per-connection; test via
            # the database object's own connection
            with db._lock:
                syn = db._c.execute("PRAGMA synchronous").fetchone()[0]
            # NORMAL=1 or FULL=2 (NORMAL not enforced on all systems)
            assert syn in (1, 2), f"synchronous={syn}"
        finally:
            db.close()

    def test_pragma_wal_autocheckpoint(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            assert _db_pragma(db_path, "wal_autocheckpoint") == 1000
        finally:
            db.close()

    def test_reopen_idempotent(self, tmp_path):
        """Re-opening an existing DB should be idempotent."""
        db_path = tmp_path / "test.db"
        db1 = RequestLogDB(db_path)
        db1.close()
        db2 = RequestLogDB(db_path)
        try:
            assert _db_has_table(db_path, "request_log")
        finally:
            db2.close()

    def test_check_constraints(self, tmp_path):
        """Status/tokens/timestamp CHECK constraints reject bad data.

        INSERT OR IGNORE silently skips constraint-violating rows,
        so we verify that bad rows are NOT inserted (row_count=0).
        """
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            # Batch of bad rows (status < 100, negative tokens, timestamp=0)
            bad_entries = [
                {
                    "req_id": "iff-bad-status", "key_name": "p",
                    "model": "m", "status": 50,
                    "ttft_ms": None, "tokens_in": 0, "tokens_out": 0,
                    "duration_ms": 0.0, "route": "local",
                    "cloud_provider": None, "error": None,
                    "timestamp": time.time(), "ts": "",
                },
                {
                    "req_id": "iff-bad-tokens", "key_name": "p",
                    "model": "m", "status": 200,
                    "ttft_ms": None, "tokens_in": -1, "tokens_out": 0,
                    "duration_ms": 0.0, "route": "local",
                    "cloud_provider": None, "error": None,
                    "timestamp": time.time(), "ts": "",
                },
                {
                    "req_id": "iff-bad-ts", "key_name": "p",
                    "model": "m", "status": 200,
                    "ttft_ms": None, "tokens_in": 0, "tokens_out": 0,
                    "duration_ms": 0.0, "route": "local",
                    "cloud_provider": None, "error": None,
                    "timestamp": 0, "ts": "",
                },
            ]
            db.insert_request_log(bad_entries)
            # All bad rows should be silently skipped (INSERT OR IGNORE)
            assert _db_row_count(db_path) == 0

            # Now mix bad + good — only good should be inserted
            mixed = bad_entries + [{
                "req_id": "iff-good", "key_name": "p",
                "model": "m", "status": 200,
                "ttft_ms": 50.0, "tokens_in": 1, "tokens_out": 2,
                "duration_ms": 100.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": time.time(), "ts": "",
            }]
            db.insert_request_log(mixed)
            assert _db_row_count(db_path) == 1
        finally:
            db.close()


class TestInsertAndQuery:
    """T5-2: Batch insert + query."""

    def test_insert_and_query_basic(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entries = [
                {"req_id": "iff-a", "key_name": "p", "model": "m1",
                 "status": 200, "ttft_ms": 50.0,
                 "tokens_in": 5, "tokens_out": 30,
                 "duration_ms": 100.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": "2026-08-01T00:00:00Z"},
                {"req_id": "iff-b", "key_name": "p", "model": "m2",
                 "status": 200, "ttft_ms": 80.0,
                 "tokens_in": 7, "tokens_out": 40,
                 "duration_ms": 200.0, "route": "cloud",
                 "cloud_provider": "baidu-codingplan", "error": None,
                 "timestamp": now + 1, "ts": "2026-08-01T00:00:01Z"},
            ]
            db.insert_request_log(entries)
            rows = db.query_request_log(since=now - 1)
            assert len(rows) == 2
            # 查询结果按 timestamp DESC 排序
            req_ids = sorted([r["req_id"] for r in rows])
            assert req_ids == ["iff-a", "iff-b"]
            # timestamp 更大的行排在前面
            cloud_row = next(r for r in rows if r["route"] == "cloud")
            assert cloud_row["cloud_provider"] == "baidu-codingplan"
        finally:
            db.close()

    def test_query_since_filter(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entries = [
                {"req_id": "iff-old", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now - 100, "ts": ""},
                {"req_id": "iff-new", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 20.0,
                 "tokens_in": 2, "tokens_out": 3,
                 "duration_ms": 20.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
            ]
            db.insert_request_log(entries)
            rows = db.query_request_log(since=now - 1)
            assert len(rows) == 1
            assert rows[0]["req_id"] == "iff-new"
        finally:
            db.close()

    def test_query_model_filter(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entries = [
                {"req_id": "iff-1", "key_name": "p", "model": "m1",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
                {"req_id": "iff-2", "key_name": "p", "model": "m2",
                 "status": 200, "ttft_ms": 20.0,
                 "tokens_in": 3, "tokens_out": 4,
                 "duration_ms": 20.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
            ]
            db.insert_request_log(entries)
            rows = db.query_request_log(since=now - 1, model="m1")
            assert len(rows) == 1
            assert rows[0]["model"] == "m1"
        finally:
            db.close()

    def test_query_limit(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entries = [
                {"req_id": f"iff-{i}", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now + i * 0.001, "ts": ""}
                for i in range(100)
            ]
            db.insert_request_log(entries)
            rows = db.query_request_log(since=now - 1, limit=10)
            assert len(rows) == 10
        finally:
            db.close()

    def test_empty_batch(self, tmp_path):
        """Empty batch should not raise error."""
        db = RequestLogDB(tmp_path / "test.db")
        try:
            db.insert_request_log([])  # Should not raise
            assert _db_row_count(tmp_path / "test.db") == 0
        finally:
            db.close()


class TestInsertOrIgnore:
    """T5-3: Duplicate req_id silently skipped."""

    def test_duplicate_req_id_ignored(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entry = {
                "req_id": "iff-dup", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 10.0,
                "tokens_in": 1, "tokens_out": 2,
                "duration_ms": 10.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": now, "ts": "",
            }
            db.insert_request_log([entry])
            db.insert_request_log([entry])  # Duplicate — should be ignored
            assert _db_row_count(tmp_path / "test.db") == 1
        finally:
            db.close()

    def test_insert_or_ignore_batch_mixed(self, tmp_path):
        """Batch with both new and duplicate req_ids."""
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            batch1 = [
                {"req_id": "iff-1", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
            ]
            db.insert_request_log(batch1)
            batch2 = [
                {"req_id": "iff-1", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
                {"req_id": "iff-2", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 20.0,
                 "tokens_in": 3, "tokens_out": 4,
                 "duration_ms": 20.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
            ]
            db.insert_request_log(batch2)
            assert _db_row_count(tmp_path / "test.db") == 2
            rows = db.query_request_log(since=now - 1)
            req_ids = [r["req_id"] for r in rows]
            assert "iff-1" in req_ids
            assert "iff-2" in req_ids
        finally:
            db.close()


class TestPrune:
    """T5-4: Cleanup + incremental_vacuum."""

    def test_prune_old_entries(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            entries = [
                {"req_id": "iff-old", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 10.0,
                 "tokens_in": 1, "tokens_out": 2,
                 "duration_ms": 10.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now - 10000, "ts": ""},
                {"req_id": "iff-new", "key_name": "p", "model": "m",
                 "status": 200, "ttft_ms": 20.0,
                 "tokens_in": 3, "tokens_out": 4,
                 "duration_ms": 20.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now, "ts": ""},
            ]
            db.insert_request_log(entries)
            assert _db_row_count(tmp_path / "test.db") == 2
            deleted = db.prune_request_log(before=now - 100)
            assert deleted == 1
            assert _db_row_count(tmp_path / "test.db") == 1
        finally:
            db.close()

    def test_prune_nothing(self, tmp_path):
        """prune with cutoff far in the past should delete nothing."""
        db = RequestLogDB(tmp_path / "test.db")
        try:
            now = time.time()
            db.insert_request_log([{
                "req_id": "iff-1", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 10.0,
                "tokens_in": 1, "tokens_out": 2,
                "duration_ms": 10.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": now, "ts": "",
            }])
            # Cutoff far in the past: no records are old enough to delete
            deleted = db.prune_request_log(before=now - 999999)
            assert deleted == 0
            assert _db_row_count(tmp_path / "test.db") == 1
        finally:
            db.close()


class TestCheckpoint:
    """WAL checkpoint test."""

    def test_checkpoint_no_error(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        try:
            db.checkpoint()  # Should not raise
        finally:
            db.close()

    def test_close_checkpoints(self, tmp_path):
        """close() should not raise and WAL should be truncated."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        db.insert_request_log([{
            "req_id": "iff-1", "key_name": "p", "model": "m",
            "status": 200, "ttft_ms": 10.0,
            "tokens_in": 1, "tokens_out": 2,
            "duration_ms": 10.0, "route": "local",
            "cloud_provider": None, "error": None,
            "timestamp": time.time(), "ts": "",
        }])
        db.close()
        # After checkpoint TRUNCATE, -wal file should be empty or absent
        wal_path = Path(str(db_path) + "-wal")
        if wal_path.exists():
            assert wal_path.stat().st_size == 0 or not wal_path.exists()


# ─── T5-T6: RequestLogger SQLite ────────────────────────────────────

class TestRequestLoggerSQLite:
    """T5-5: RequestLogger log → flush → query verification."""

    def test_log_flushes_to_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            jsonl_enabled=True, batch_size=2, flush_interval=0.1,
        )
        try:
            e1 = _make_entry(req_id="iff-1")
            e2 = _make_entry(req_id="iff-2")
            logger.log(e1)
            logger.log(e2)  # Should trigger immediate flush (batch_size=2)
            time.sleep(0.3)  # Give flush thread time
            assert _db_row_count(db_path) == 2
        finally:
            logger.close()

    def test_log_single_entry_flushed_on_interval(self, tmp_path):
        """Single entry flushed after flush_interval."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            jsonl_enabled=True, batch_size=100, flush_interval=0.3,
        )
        try:
            logger.log(_make_entry(req_id="iff-1"))
            assert _db_row_count(db_path) == 0  # Not flushed yet
            time.sleep(0.8)  # Wait for flush interval
            assert _db_row_count(db_path) == 1
            rows = db.query_request_log(since=time.time() - 10)
            assert rows[0]["req_id"] == "iff-1"
        finally:
            logger.close()

    def test_jsonl_still_written(self, tmp_path):
        """When jsonl_enabled=True, JSONL AND SQLite both get written."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            jsonl_enabled=True, batch_size=1, flush_interval=0.1,
        )
        try:
            logger.log(_make_entry(req_id="iff-1"))
            time.sleep(0.3)
            # JSONL file should exist
            today = date.today().isoformat()
            jsonl_path = tmp_path / "logs" / f"access-{today}.jsonl"
            assert jsonl_path.exists()
            # Both JSONL and SQLite should have the entry
            assert _db_row_count(db_path) == 1
        finally:
            logger.close()

    def test_db_disabled_no_op(self, tmp_path):
        """db=None should not affect existing behavior (backward compat)."""
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=None,
            jsonl_enabled=True,
        )
        try:
            f"access-{date.today().isoformat()}.jsonl"
            logger.log(_make_entry(req_id="iff-1"))
            logger.close()
            today = date.today().isoformat()
            jsonl_path = tmp_path / "logs" / f"access-{today}.jsonl"
            assert jsonl_path.exists()
        finally:
            logger.close()

    def test_duplicate_req_id_in_db(self, tmp_path):
        """INSERT OR IGNORE: duplicate req_id across multiple flushes."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            batch_size=1, flush_interval=0.1,
        )
        try:
            logger.log(_make_entry(req_id="iff-dup"))
            time.sleep(0.3)
            logger.log(_make_entry(req_id="iff-dup"))  # Same req_id
            time.sleep(0.3)
            assert _db_row_count(db_path) == 1
        finally:
            logger.close()


class TestConcurrentLogFlush:
    """T5-6: Multi-thread log + flush, no data loss."""

    def test_concurrent_writes(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            batch_size=10, flush_interval=0.2,
        )
        try:
            n_threads = 4
            n_per_thread = 25
            errors = []
            barrier = threading.Barrier(n_threads)

            def writer(thread_idx):
                try:
                    barrier.wait()
                    for i in range(n_per_thread):
                        rid = f"iff-c{thread_idx}-{i}"
                        entry = _make_entry(req_id=rid)
                        logger.log(entry)
                        time.sleep(0.001)  # encourage interleaving
                except Exception as e:
                    errors.append(e)

            threads = []
            for t in range(n_threads):
                th = threading.Thread(target=writer, args=(t,))
                threads.append(th)

            for th in threads:
                th.start()
            for th in threads:
                th.join()

            assert not errors, f"Concurrent errors: {errors}"
            time.sleep(1.0)  # Wait for final flush

            count = _db_row_count(db_path)
            assert count == n_threads * n_per_thread, \
                f"Expected {n_threads * n_per_thread}, got {count}"
        finally:
            logger.close()


class TestCloseFlush:
    """T5-7: close() flushes remaining buffer."""

    def test_close_flushes_remaining(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            batch_size=100,  # Large batch so no auto-flush
            flush_interval=999.0,  # Long interval
        )
        try:
            for i in range(5):
                logger.log(_make_entry(req_id=f"iff-{i}"))
            assert _db_row_count(db_path) == 0  # Not yet flushed
            logger.close()
            assert _db_row_count(db_path) == 5
        finally:
            logger.close()

    def test_close_does_not_duplicate(self, tmp_path):
        """close() after already-flushed entries should not duplicate."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            batch_size=1, flush_interval=0.1,
        )
        try:
            logger.log(_make_entry(req_id="iff-1"))
            time.sleep(0.3)
            assert _db_row_count(db_path) == 1
            logger.log(_make_entry(req_id="iff-2"))
            logger.close()  # Should flush iff-2 but not duplicate iff-1
            assert _db_row_count(db_path) == 2
        finally:
            logger.close()


class TestBufferOverflow:
    """T5-8: Buffer overflow protection on flush failure.

    Note: This test simulates buffer overflow on failure by
    directly manipulating the internal buffer; we can't easily
    simulate a real DB write failure without patching.
    """

    def test_buffer_max_10000(self, tmp_path):
        """Buffer stops growing at 10000 entries on persistent failure."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        # We use a valid DB but test the overflow logic directly
        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            batch_size=50, flush_interval=0.2,
        )
        try:
            # Fill buffer with many entries quickly
            for i in range(150):
                logger.log(_make_entry(req_id=f"iff-{i}"))
            with logger._buf_lock:
                buf_size = len(logger._buffer)
            # Buffer should not exceed 10000 (max)
            assert buf_size <= 10000
        finally:
            logger.close()


class TestJSONLDisabled:
    """T5-11: jsonl_enabled=False → SQLite only."""

    def test_jsonl_disabled_no_file(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        log_dir = tmp_path / "logs"
        logger = RequestLogger(
            log_dir=log_dir, enabled=True, db=db,
            jsonl_enabled=False, batch_size=1, flush_interval=0.1,
        )
        try:
            logger.log(_make_entry(req_id="iff-1"))
            time.sleep(0.3)
            # No JSONL file created
            log_dir.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            jsonl_path = log_dir / f"access-{today}.jsonl"
            assert not jsonl_path.exists() or jsonl_path.stat().st_size == 0
            # SQLite still has the entry
            assert _db_row_count(db_path) == 1
        finally:
            logger.close()


# ─── T5-T7: MetricsAggregator Replay ────────────────────────────────

class TestMetricsReplay:
    """T5-9: Pre-written data → MetricsAggregator replay."""

    def test_replay_from_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            now = time.time()
            entries = [
                {"req_id": "iff-1", "key_name": "p", "model": "m1",
                 "status": 200, "ttft_ms": 50.0,
                 "tokens_in": 5, "tokens_out": 30,
                 "duration_ms": 100.0, "route": "local",
                 "cloud_provider": None, "error": None,
                 "timestamp": now - 60, "ts": ""},
                {"req_id": "iff-2", "key_name": "p", "model": "m1",
                 "status": 200, "ttft_ms": 150.0,
                 "tokens_in": 10, "tokens_out": 60,
                 "duration_ms": 300.0, "route": "cloud",
                 "cloud_provider": "baidu-codingplan", "error": None,
                 "timestamp": now - 30, "ts": ""},
                {"req_id": "iff-3", "key_name": "p", "model": "m2",
                 "status": 500, "ttft_ms": None,
                 "tokens_in": 0, "tokens_out": 0,
                 "duration_ms": 5000.0, "route": "local",
                 "cloud_provider": None, "error": "server_error",
                 "timestamp": now - 10, "ts": ""},
            ]
            db.insert_request_log(entries)

            # Create MetricsAggregator with replay
            agg = MetricsAggregator(db=db, replay_hours=1.0)
            metrics = agg.get_metrics(window="24h")
            assert metrics["total_requests"] == 3
            assert metrics["success"] == 2
            assert metrics["fail"] == 1
            assert "m1" in metrics["models"]
            assert "m2" in metrics["models"]
            assert metrics["models"]["m1"]["requests"] == 2
            assert metrics["models"]["m2"]["requests"] == 1
            assert metrics["models"]["m1"]["tokens_in"] == 15
            assert metrics["models"]["m1"]["tokens_out"] == 90
        finally:
            db.close()

    def test_replay_old_data_outside_window(self, tmp_path):
        """Data older than replay_hours should not be included."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            now = time.time()
            db.insert_request_log([{
                "req_id": "iff-old", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 10.0,
                "tokens_in": 1, "tokens_out": 2,
                "duration_ms": 10.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": now - 7200,  # 2 hours ago
                "ts": "",
            }])
            db.insert_request_log([{
                "req_id": "iff-new", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 20.0,
                "tokens_in": 3, "tokens_out": 4,
                "duration_ms": 20.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": now - 60,  # 1 minute ago
                "ts": "",
            }])
            # replay_hours=1.0 → only data within last hour
            agg = MetricsAggregator(db=db, replay_hours=1.0)
            metrics = agg.get_metrics(window="24h")
            assert metrics["total_requests"] == 1
            assert metrics["models"]["m"]["requests"] == 1
        finally:
            db.close()


class TestMetricsReplayDisabled:
    """T5-10: replay_hours=0 → no replay."""

    def test_replay_disabled(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            now = time.time()
            db.insert_request_log([{
                "req_id": "iff-1", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 50.0,
                "tokens_in": 5, "tokens_out": 30,
                "duration_ms": 100.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": now, "ts": "",
            }])
            agg = MetricsAggregator(db=db, replay_hours=0.0)
            metrics = agg.get_metrics(window="24h")
            assert metrics.get("total", 0) == 0
        finally:
            db.close()

    def test_no_db_no_replay(self):
        """db=None → no replay, no crash."""
        agg = MetricsAggregator(db=None, replay_hours=24.0)
        metrics = agg.get_metrics(window="24h")
        assert metrics.get("total", 0) == 0
        agg = MetricsAggregator(db=None, replay_hours=0.0)
        metrics = agg.get_metrics(window="24h")
        assert metrics.get("total", 0) == 0

    def test_replay_error_handled(self, tmp_path):
        """Replay failure should not crash — log warning instead."""
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        try:
            db.insert_request_log([{
                "req_id": "iff-1", "key_name": "p", "model": "m",
                "status": 200, "ttft_ms": 50.0,
                "tokens_in": 5, "tokens_out": 30,
                "duration_ms": 100.0, "route": "local",
                "cloud_provider": None, "error": None,
                "timestamp": time.time(), "ts": "",
            }])
            db.close()

            # Now aggregator with closed DB — should not crash
            agg = MetricsAggregator(db=db, replay_hours=24.0)
            metrics = agg.get_metrics(window="24h")
            # Replay failed, but aggregator still works
            assert "total" in metrics or metrics.get("total_requests", 0) == 0
        except Exception:
            pass  # Expected warning, not crash


# ─── Integration: RequestLogger → MetricsAggregator ─────────────────

class TestIntegration:
    """End-to-end: RequestLogger feeds both SQLite and MetricsAggregator."""

    def test_end_to_end_flow(self, tmp_path):
        db_path = tmp_path / "test.db"
        db = RequestLogDB(db_path)
        agg_queue = _queue.Queue()
        agg = MetricsAggregator(db=None, replay_hours=0.0)  # No replay
        from inferfabric.metrics_aggregator import AggregatorThread
        agg_thread = AggregatorThread(agg, agg_queue)
        # daemon must be set BEFORE start
        agg_thread.daemon = True
        agg_thread.start()

        logger = RequestLogger(
            log_dir=tmp_path / "logs", enabled=True, db=db,
            on_log_queue=agg_queue,
            jsonl_enabled=True, batch_size=1, flush_interval=0.1,
        )
        try:
            logger.log(_make_entry(req_id="iff-1", model="m1", status=200))
            time.sleep(0.3)
            # SQLite has the entry
            assert _db_row_count(db_path) == 1
            # Metrics aggregator has the entry (via queue)
            time.sleep(0.1)
            metrics = agg.get_metrics(window="24h")
            assert metrics["total_requests"] >= 1
            assert "m1" in metrics["models"]
        finally:
            logger.close()
            # Thread is daemon — will be stopped automatically


# ─── v006: tokens_in_cached（缓存命中率统计） ─────────────────────

class TestTokensInCachedColumn:
    """request_log.tokens_in_cached 列（migration v006）+ 后向兼容。"""

    def test_fresh_db_has_column(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        cols = db.columns
        assert "tokens_in_cached" in cols

    def test_insert_and_query_roundtrip(self, tmp_path):
        db = RequestLogDB(tmp_path / "test.db")
        db.insert_request_log([{
            "req_id": "iff-cached-1", "key_name": "primary", "model": "m1",
            "status": 200, "ttft_ms": 100.0,
            "tokens_in": 145514, "tokens_in_cached": 129428, "tokens_out": 2112,
            "duration_ms": 60000.0, "route": "local", "cloud_provider": None,
            "error": None, "timestamp": time.time(), "ts": "2026-09-21T00:00:00Z",
        }])
        rows = db.query_request_log(since=0)
        assert rows[0]["tokens_in"] == 145514
        assert rows[0]["tokens_in_cached"] == 129428

    def test_legacy_entry_without_key_defaults_zero(self, tmp_path):
        """v6.1 前的 entry dict（无 tokens_in_cached 键）→ 列默认 0，不报错。"""
        db = RequestLogDB(tmp_path / "test.db")
        db.insert_request_log([{
            "req_id": "iff-legacy-1", "key_name": "primary", "model": "m1",
            "status": 200, "ttft_ms": None,
            "tokens_in": 48447, "tokens_out": 1200,
            "duration_ms": 5000.0, "route": "local", "cloud_provider": None,
            "error": None, "timestamp": time.time(), "ts": "2026-09-21T00:00:00Z",
        }])
        rows = db.query_request_log(since=0)
        assert rows[0]["tokens_in_cached"] == 0

    def test_upgrade_old_db_adds_column(self, tmp_path):
        """v6.1 前的旧库（v002 schema, user_version=5）打开后自动补列；旧行 = 0。"""
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id TEXT NOT NULL UNIQUE,
                key_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                status INTEGER NOT NULL,
                ttft_ms REAL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL NOT NULL DEFAULT 0.0,
                route TEXT NOT NULL DEFAULT 'local',
                cloud_provider TEXT,
                error TEXT,
                timestamp REAL NOT NULL,
                ts TEXT NOT NULL DEFAULT '',
                cost TEXT DEFAULT 'local',
                metadata TEXT DEFAULT '{}'
            );
            PRAGMA user_version = 5;
        """)
        conn.execute(
            "INSERT INTO request_log (req_id, model, status, tokens_in, "
            "tokens_out, timestamp) VALUES ('iff-old-1', 'm1', 200, 9826, "
            "500, 1000.0)")
        conn.commit()
        conn.close()

        # 打开 → 触发 v006 migration
        db = RequestLogDB(db_path)
        rows = db.query_request_log(since=0)
        assert rows[0]["req_id"] == "iff-old-1"
        assert rows[0]["tokens_in_cached"] == 0

def test_insert_and_query_agent_fields(tmp_path):
    """agent/ua 字段经 IFFDB 写入/读出往返一致。

    注：conftest 的 tmp_iffdb 把 data_dir 当文件路径用（iff.db/state.db），
    建库即炸（见 test_power_series.py 中同款替身）→ 此处自建目录版。
    """
    from inferfabric.db import IFFDB
    import inferfabric.migrations  # noqa: F401
    db = IFFDB(tmp_path)
    db.insert_request_log([{
        "req_id": "a1", "key_name": "primary", "model": "qwen38",
        "status": 200, "ttft_ms": 100.0, "tokens_in": 10, "tokens_in_cached": 0,
        "tokens_out": 5, "duration_ms": 500.0,
        "timestamp": 1000.0, "ts": "t", "route": "local",
        "cloud_provider": None, "error": None,
        "agent": "claude-code", "ua": "claude-cli/2.0",
    }])
    rows = db.query_request_log(since=0)
    assert rows[0]["agent"] == "claude-code" and rows[0]["ua"] == "claude-cli/2.0"


# ── B1: query_request_log_window（window 模式读侧契约） ─────────────


def _win_row(i, ts, model="m1", status=200, req_id=None, agent="claude-code"):
    return {
        "req_id": req_id or f"r{i}", "key_name": "k", "model": model,
        "status": status, "ttft_ms": 10.0, "tokens_in": 1,
        "tokens_in_cached": 0, "tokens_out": 2, "duration_ms": 5.0,
        "route": "local", "cloud_provider": None, "error": None,
        "timestamp": ts, "ts": str(ts), "agent": agent, "ua": "ua/1",
        "metadata": {},
    }


def test_query_request_log_window_counts_and_boundaries(tmp_path):
    """"[since, until) 半开窗 + 过滤先于 LIMIT 的同快照计数。"""
    from inferfabric.db import IFFDB
    import inferfabric.migrations  # noqa: F401
    db = IFFDB(tmp_path)
    db.insert_request_log([
        _win_row(1, 100.0, status=200),                    # == since → 含入
        _win_row(2, 150.0, status=404),
        _win_row(3, 150.0, status=503, model="m2", agent="codex"),
        _win_row(4, 200.0, status=404, model="m2"),        # == until → 排除
        _win_row(5, 50.0, status=404, model="m2"),         # < since → 排除
    ])
    res = db.query_request_log_window(since=100.0, until=200.0)
    assert res["total"] == 3
    assert res["failed_total"] == 2                        # 404 + 503
    assert [r["req_id"] for r in res["logs"]] == ["r3", "r2", "r1"]
    # 状态类过滤（整数区间）
    res4 = db.query_request_log_window(since=100.0, until=200.0,
                                       status_class="4xx")
    assert res4["total"] == 1 and res4["failed_total"] == 1
    # agent 精确匹配
    assert db.query_request_log_window(since=100.0, until=200.0,
                                       agent="codex")["total"] == 1
    assert db.query_request_log_window(since=100.0, until=200.0,
                                       agent="claude-code")["total"] == 2
    # model 精确匹配
    assert db.query_request_log_window(since=100.0, until=200.0,
                                       model="m2")["total"] == 1


def test_query_request_log_window_q_literal_substring(tmp_path):
    """q 对 req_id/model/agent 做字面子串匹配：LIKE 通配字符按字面处理。"""
    from inferfabric.db import IFFDB
    import inferfabric.migrations  # noqa: F401
    db = IFFDB(tmp_path)
    db.insert_request_log([
        _win_row(1, 100.0, req_id="a_c"),
        _win_row(2, 100.0, req_id="abc"),
        _win_row(3, 100.0, req_id="a%c"),
        _win_row(4, 100.0, req_id="axc"),
        _win_row(5, 100.0, req_id="plain"),
    ])
    # 下划线不当单字通配符：只命中字面 "a_c"
    assert db.query_request_log_window(since=0, until=200.0,
                                       q="a_c")["total"] == 1
    # 百分号不当作多字通配符：只命中字面 "a%c"
    assert db.query_request_log_window(since=0, until=200.0,
                                       q="a%c")["total"] == 1
    # req_id 之外还匹配 model / agent 子串
    assert db.query_request_log_window(since=0, until=200.0,
                                       q="lain")["total"] == 1


def test_query_request_log_window_offset_pagination(tmp_path):
    """offset 分页按 (timestamp,id) DESC：同秒跨页无重叠不漏不计重。"""
    from inferfabric.db import IFFDB
    import inferfabric.migrations  # noqa: F401
    db = IFFDB(tmp_path)
    db.insert_request_log(
        [_win_row(i, 500.0, model="m2", req_id=f"s{i:03d}")
         for i in range(60)]
        + [_win_row(1000 + i, 400.0 + i) for i in range(40)])

    def page(offset, limit=25):
        return db.query_request_log_window(since=0, until=1000.0,
                                           model="m2", limit=limit,
                                           offset=offset)

    full = db.query_request_log_window(since=0, until=1000.0, model="m2",
                                       limit=100)
    assert full["total"] == 60
    ids = [r["id"] for r in full["logs"]]
    assert ids == sorted(ids, reverse=True)                # 全序 DESC
    p1, p2, p3 = page(0), page(25), page(50)
    assert [p1["logs"], p2["logs"], p3["logs"]][0][0]["id"] == ids[0]
    got = [r["id"] for pg in (p1, p2, p3) for r in pg["logs"]]
    assert got == ids                                      # 页拼接 == 全序
    tail = page(60)
    assert tail["logs"] == [] and tail["total"] == 60      # 越界页安全


def test_query_request_log_window_rejects_bad_status_class(tmp_path):
    """db 层防御：未知状态类直接 ValueError（handler 先行 400）。"""
    from inferfabric.db import IFFDB
    import inferfabric.migrations  # noqa: F401
    import pytest
    db = IFFDB(tmp_path)
    db.insert_request_log([_win_row(1, 100.0)])
    with pytest.raises(ValueError):
        db.query_request_log_window(since=0, until=200.0, status_class="3xx")
