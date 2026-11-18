"""DeviceTelemetry T4 — device power persistence migration + sampler wiring.

Covers: v011 migration (v007 legacy copy + compat view + idempotence +
rollback), device-scoped DB facade methods, PowerSampler consuming
DeviceTelemetry, and stream separation between legacy and aggregate series.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from inferfabric.db import IFFDB
from inferfabric.migrations import v007_gpu_power_series, v011_device_power_samples
from inferfabric.platform.telemetry import DeviceSnapshot
from inferfabric.power_stats import (
    PowerSampler,
    probe_gpu_power_w,
    query_device_power_series,
    query_power_series,
)


# ── helpers ──────────────────────────────────────────────────────

def _legacy_db() -> sqlite3.Connection:
    """In-memory DB with a v007 gpu_power_samples table + rows."""
    conn = sqlite3.connect(":memory:")
    v007_gpu_power_series.upgrade(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO gpu_power_samples (ts, watts) VALUES (?, ?)",
        [(1_000, 41.5), (2_000, 42.5), (3_000, 43.5)],
    )
    conn.commit()
    return conn


class FakeTelemetry:
    def __init__(self, power_w, *, accelerator="nvidia", platform="linux",
                 timestamp=None):
        self._power_w = power_w
        self._accelerator = accelerator
        self._platform = platform
        self._timestamp = timestamp if timestamp is not None else 1_700_000_000.0
        self.snapshot_calls = 0

    def snapshot(self) -> DeviceSnapshot:
        self.snapshot_calls += 1
        return DeviceSnapshot(
            platform=self._platform, accelerator=self._accelerator,
            memory_kind="vram", memory_used_mb=100, memory_total_mb=1000,
            memory_budget_mb=None, utilization_pct=10.0,
            power_w=self._power_w, temperature_c=50.0, clock_mhz=1500,
            pressure_level=None, timestamp=self._timestamp, sources={},
        )


class FakePowerDB:
    """Records insert_device_power_samples calls without a real DB."""

    def __init__(self):
        self.inserted = []

    def insert_device_power_samples(self, samples):
        self.inserted.extend(samples)

    def prune_device_power_samples(self, before):
        return 0


# ── v011 migration ───────────────────────────────────────────────

def test_v011_migrates_v007_rows_preserving_history():
    conn = _legacy_db()
    v011_device_power_samples.upgrade(conn)

    rows = conn.execute(
        "SELECT ts, platform, accelerator, device_id, watts, source "
        "FROM device_power_samples ORDER BY ts ASC").fetchall()
    assert rows == [
        (1_000, "unknown", "unknown", "unknown", 41.5, "legacy_gpu_power_samples"),
        (2_000, "unknown", "unknown", "unknown", 42.5, "legacy_gpu_power_samples"),
        (3_000, "unknown", "unknown", "unknown", 43.5, "legacy_gpu_power_samples"),
    ]
    # legacy table replaced; no fabricated attribution
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='gpu_power_samples'").fetchone() is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 11


def test_v011_compat_view_shape_and_collision_prefers_legacy():
    conn = _legacy_db()
    v011_device_power_samples.upgrade(conn)
    # new aggregate sample colliding with a legacy timestamp
    conn.execute(
        "INSERT INTO device_power_samples VALUES "
        "(2_000, 'linux', 'nvidia', 'aggregate', 200.0, 'device_telemetry')")
    conn.commit()

    # view shape is exactly (ts, watts)
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM gpu_power_samples LIMIT 0").description]
    assert cols == ["ts", "watts"]
    rows = conn.execute(
        "SELECT ts, watts FROM gpu_power_samples ORDER BY ts").fetchall()
    # historical rows + non-colliding aggregate; collision prefers legacy
    assert rows == [(1_000, 41.5), (2_000, 42.5), (3_000, 43.5)]


def test_v011_migration_is_idempotent():
    conn = _legacy_db()
    v011_device_power_samples.upgrade(conn)
    v011_device_power_samples.upgrade(conn)  # second run must not raise/duplicate
    n = conn.execute("SELECT COUNT(*) FROM device_power_samples").fetchone()[0]
    assert n == 3


def test_v011_failed_copy_rolls_back():
    conn = _legacy_db()
    # sabotage: a view already occupies the replacement table's name so the
    # copy INSERT fails inside the transaction
    conn.execute("CREATE VIEW device_power_samples AS SELECT 1 AS ts")
    conn.commit()
    with pytest.raises(Exception):
        v011_device_power_samples.upgrade(conn)
    # legacy table + rows intact, user_version untouched
    n = conn.execute("SELECT COUNT(*) FROM gpu_power_samples").fetchone()[0]
    assert n == 3
    assert conn.execute("PRAGMA user_version").fetchone()[0] < 11


def test_fresh_iffdb_gets_v011_schema(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    with db.connect("request_log") as conn:
        cols = [d[1] for d in conn.execute(
            "PRAGMA table_info(device_power_samples)").fetchall()]
        assert cols == ["ts", "platform", "accelerator", "device_id",
                        "watts", "source"]
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 11


# ── device-scoped DB facade ──────────────────────────────────────

def test_insert_query_prune_device_power_samples(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    db.insert_device_power_samples([
        {"ts": 5_000, "platform": "linux", "accelerator": "nvidia",
         "device_id": "aggregate", "watts": 300.0, "source": "device_telemetry"},
        {"ts": 6_000, "platform": "linux", "accelerator": "nvidia",
         "device_id": "aggregate", "watts": 310.0, "source": "device_telemetry"},
    ])
    rows = db.query_device_power_samples(since=5_000, until=6_000)
    # [since, until) contract
    assert [r["ts"] for r in rows] == [5_000]
    assert rows[0]["watts"] == 300.0 and rows[0]["accelerator"] == "nvidia"
    # ascending order contract
    rows = db.query_device_power_samples(since=0)
    assert [r["ts"] for r in rows] == [5_000, 6_000]
    # same-second idempotence (PK ts+accelerator+device_id)
    db.insert_device_power_samples([
        {"ts": 5_000, "platform": "linux", "accelerator": "nvidia",
         "device_id": "aggregate", "watts": 999.0, "source": "device_telemetry"}])
    rows = db.query_device_power_samples(since=0)
    assert [r["watts"] for r in rows] == [300.0, 310.0]
    # prune
    assert db.prune_device_power_samples(before=5_500) == 1
    assert [r["ts"] for r in db.query_device_power_samples(since=0)] == [6_000]


def test_legacy_write_redirects_to_new_table(tmp_path):
    """Legacy insert/prune must land in device_power_samples, not the view."""
    db = IFFDB(data_dir=tmp_path)
    db.insert_gpu_power_samples([{"ts": 7_000, "watts": 55.0}])
    rows = db.query_device_power_samples(since=0)
    assert rows == [{"ts": 7_000, "platform": "unknown", "accelerator": "unknown",
                     "device_id": "unknown", "watts": 55.0,
                     "source": "legacy_gpu_power_samples"}]
    # legacy read shape preserved (ts, watts) via the compat view
    legacy = db.query_gpu_power_samples(since=0)
    assert legacy == [{"ts": 7_000, "watts": 55.0}]
    assert db.prune_gpu_power_samples(before=7_500) == 1
    assert db.query_device_power_samples(since=0) == []


# ── PowerSampler ─────────────────────────────────────────────────

def test_sampler_records_snapshot_power_with_metadata():
    fake_db = FakePowerDB()
    tel = FakeTelemetry(12.5, timestamp=1_700_000_123.0)
    sampler = PowerSampler(fake_db, interval=60.0, telemetry=tel)
    w = sampler.sample_once()
    assert w == 12.5 and sampler.sample_count == 1
    assert fake_db.inserted == [{
        "ts": 1_700_000_123, "platform": "linux", "accelerator": "nvidia",
        "device_id": "aggregate", "watts": 12.5, "source": "device_telemetry",
    }]


def test_sampler_keeps_real_zero_and_skips_none():
    fake_db = FakePowerDB()
    sampler = PowerSampler(fake_db, telemetry=FakeTelemetry(0.0))
    assert sampler.sample_once() == 0.0  # real zero is kept
    assert fake_db.inserted[0]["watts"] == 0.0

    sampler2 = PowerSampler(fake_db, telemetry=FakeTelemetry(None))
    assert sampler2.sample_once() is None  # None skipped, no fake zero
    assert len(fake_db.inserted) == 1
    assert sampler2.sample_count == 0


def test_sampler_defaults_interval_and_retention():
    sampler = PowerSampler(FakePowerDB())
    assert sampler._interval == 60.0
    assert PowerSampler.RETENTION_DAYS == 90


def test_probe_gpu_power_w_delegates_to_snapshot():
    # on this darwin host the Apple backend yields power_w=None; the shim
    # must not raise and must return None (or a real reading if present)
    val = probe_gpu_power_w()
    assert val is None or isinstance(val, float)


# ── series: streams never interpolated across ────────────────────

def test_power_series_does_not_interpolate_across_streams(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    now = int(time.time())
    legacy = [
        {"ts": now - 120, "platform": "unknown", "accelerator": "unknown",
         "device_id": "unknown", "watts": 40.0, "source": "legacy_gpu_power_samples"},
        {"ts": now - 60, "platform": "unknown", "accelerator": "unknown",
         "device_id": "unknown", "watts": 60.0, "source": "legacy_gpu_power_samples"},
    ]
    agg = [
        {"ts": now - 120, "platform": "linux", "accelerator": "nvidia",
         "device_id": "aggregate", "watts": 300.0, "source": "device_telemetry"},
        {"ts": now - 60, "platform": "linux", "accelerator": "nvidia",
         "device_id": "aggregate", "watts": 400.0, "source": "device_telemetry"},
    ]
    db.insert_device_power_samples(legacy + agg)

    legacy_series = query_power_series(db, gran="hour")
    device_series = query_device_power_series(db, gran="hour")

    # each stream integrated on its own: totals differ, counts split
    assert legacy_series["totals"]["count"] == 2
    assert device_series["totals"]["count"] == 2
    assert legacy_series["totals"]["avg_w"] == 50.0
    assert device_series["totals"]["avg_w"] == 350.0
    # same response shape on both endpoints
    assert set(legacy_series.keys()) == set(device_series.keys())
    assert {"gran", "buckets", "totals", "available"} <= set(device_series.keys())
    assert legacy_series["available"] and device_series["available"]


def test_v011_tolerates_view_from_concurrent_migration():
    """Race hardening: the legacy-table check must happen inside the write
    transaction. If another process already migrated (gpu_power_samples is
    now the compat view), this run must skip the copy/drop, not crash."""
    conn = _legacy_db()
    v011_device_power_samples.upgrade(conn)   # first process finishes
    conn.execute("PRAGMA user_version = 0")   # second process started earlier
    v011_device_power_samples.upgrade(conn)   # must not raise / duplicate
    n = conn.execute("SELECT COUNT(*) FROM device_power_samples").fetchone()[0]
    assert n == 3


def test_v011_compat_view_excludes_per_device_rows():
    """The (ts, watts) view carries historical + aggregate rows only —
    a per-device sample (device_id != unknown/aggregate) must never leak
    into the legacy one-row-per-ts projection."""
    conn = _legacy_db()
    v011_device_power_samples.upgrade(conn)
    conn.execute(
        "INSERT INTO device_power_samples VALUES "
        "(4_000, 'linux', 'nvidia', 'card0', 77.0, 'device_telemetry')")
    conn.commit()
    rows = conn.execute(
        "SELECT ts, watts FROM gpu_power_samples ORDER BY ts").fetchall()
    assert rows == [(1_000, 41.5), (2_000, 42.5), (3_000, 43.5)]
