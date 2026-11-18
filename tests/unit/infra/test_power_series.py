"""Power series: v007 migration + IFFDB gpu_power_samples + PowerSampler + 分桶聚合。

覆盖（TDD 先红后绿）：
- v007 迁移建表；IFFDB insert/query/prune 往返
- 分桶聚合：hour 24 桶对齐小时 / day 30 桶对齐本地零点 / month 自然月
- 能量梯形积分 + 单样本兜底 + 累计电量/电费（¥1/度）
- PowerSampler：线程化成帧插入（mock probe，含 None 跳过）

口径：功耗 = GPU 板卡实时功耗（nvidia-smi power.draw），60s 采样一次，¥1/度换算电费。
"""

import sys
import time
import itertools
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.power_stats import (  # noqa: E402
    PRICE_YUAN_PER_KWH,
    PowerSampler,
    bucket_stats,
    day_slots,
    hour_slots,
    month_slots,
    week_slots,
    query_power_series,
)
from inferfabric.db import REQUEST_LOG_DB  # noqa: E402


@pytest.fixture
def tmp_iffdb(tmp_path):
    """本地 IFFDB 替身：conftest 的 tmp_iffdb（tmp_path/"iff.db"）把 data_dir 当文件
    路径用（iff.db/state.db），建库即炸。data_dir 应指目录。"""
    from inferfabric.db import IFFDB
    return IFFDB(tmp_path)


def _mktime(y, mo, d, h=0, mi=0, s=0):
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))


# ── 1. 迁移 + IFFDB 往返 ────────────────────────────────────────

def test_migration_v007_creates_table(tmp_iffdb):
    with tmp_iffdb.connect(REQUEST_LOG_DB) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(gpu_power_samples)").fetchall()]
    # 表必须存在且含 ts(主键)/watts
    assert "ts" in cols and "watts" in cols


def test_iffdb_insert_query_prune_roundtrip(tmp_iffdb):
    tmp_iffdb.insert_gpu_power_samples([
        {"ts": 1000, "watts": 500.5},
        {"ts": 1060, "watts": 510.2},
        {"ts": 1120, "watts": 480.1},
    ])
    rows = tmp_iffdb.query_gpu_power_samples(since=0)
    assert [r["watts"] for r in sorted(rows, key=lambda r: r["ts"])] == [500.5, 510.2, 480.1]

    # since/until 过滤
    mid = tmp_iffdb.query_gpu_power_samples(since=1050, until=1100)
    assert len(mid) == 1 and abs(mid[0]["watts"] - 510.2) < 1e-9

    # prune
    deleted = tmp_iffdb.prune_gpu_power_samples(before=1100)
    assert deleted == 2
    assert len(tmp_iffdb.query_gpu_power_samples(since=0)) == 1

    # 主键冲突 INSERT OR IGNORE 幂等
    tmp_iffdb.insert_gpu_power_samples([{"ts": 1120, "watts": 999.0}])
    rows = tmp_iffdb.query_gpu_power_samples(since=0)
    assert len(rows) == 1 and abs(rows[0]["watts"] - 480.1) < 1e-9


# ── 2. 分桶聚合数学 ─────────────────────────────────────────────

def test_hour_slots_aligned_24():
    now = _mktime(2026, 9, 23, 14, 37, 0)
    slots = hour_slots(now)
    assert len(slots) == 24
    # 第一个桶起点 = 23 小时前的整点；每桶 3600s
    assert slots[0][0] == (now - (now % 3600)) - 23 * 3600
    assert all(end - start == 3600 for start, end in slots)
    # 最后一个桶 = 当前小时（部分）
    assert slots[-1][0] == now - (now % 3600)


def test_day_slots_aligned_local_midnight():
    now = _mktime(2026, 9, 23, 14, 37, 0)
    slots = day_slots(now)
    assert len(slots) == 30
    # 本地零点对齐（非 UTC：epoch % 86400 在 UTC+8 下恒为 28800，不能用余数断言）
    for start, _ in slots:
        lt = time.localtime(start)
        assert (lt.tm_hour, lt.tm_min, lt.tm_sec) == (0, 0, 0)
    assert slots[-1][0] == _mktime(2026, 9, 23)             # 今天零点
    assert slots[0][0] == _mktime(2026, 9, 23) - 29 * 86400
    assert all(end - start == 86400 for start, end in slots)


def test_week_slots_aligned_local_monday():
    now = _mktime(2026, 9, 23, 14, 0, 0)
    slots = week_slots(now)
    assert len(slots) == 13
    # 每桶恰 7 天；桶起点 = 本地周一 00:00（对齐自然周，非滑动窗口）
    assert all(end - start == 7 * 86400 for start, end in slots)
    for start, _ in slots:
        lt = time.localtime(start)
        assert (lt.tm_hour, lt.tm_min, lt.tm_sec) == (0, 0, 0)
        assert lt.tm_wday == 0, "周桶必须本地周一 00:00 对齐"
    # 末桶 = 本周（部分）：起点 ≤ now < 终点；首桶 = 12 周前周一（13×7d ≈ 91 天，覆盖近 90 天）
    assert slots[-1][0] <= now < slots[-1][1]
    assert slots[0][0] == slots[-1][0] - 12 * 7 * 86400


def test_month_slots_natural_months():
    now = _mktime(2026, 9, 23)
    first = _mktime(2026, 8, 5)
    slots = month_slots(now, first)
    starts = [s for s, _ in slots]
    assert _mktime(2026, 8, 1) in starts and _mktime(2026, 9, 1) in starts
    assert len(slots) == 2
    # 8 月桶 = 8月1日 → 9月1日；9 月桶 = 9月1日 → 10月1日
    assert slots[0] == (_mktime(2026, 8, 1), _mktime(2026, 9, 1))


def test_bucket_energy_trapezoid():
    # 60s 间距两样本：500W & 540W → 平均 520W × 60s = 0.0086667 kWh
    slots = [(0, 3600)]
    buckets = bucket_stats([(0, 500.0), (60, 540.0)], slots)
    assert abs(buckets[0]["kwh"] - (520.0 * 60 / 3.6e6)) < 1e-6
    assert abs(buckets[0]["avg_w"] - 520.0) < 1e-6


def test_bucket_energy_single_sample_falls_back_interval():
    slots = [(0, 3600)]
    buckets = bucket_stats([(0, 500.0)], slots)
    # 单样本无法梯形积分 → 按采样间隔 60s 折算
    assert abs(buckets[0]["kwh"] - (500.0 * 60 / 3.6e6)) < 1e-6
    assert abs(buckets[0]["avg_w"] - 500.0) < 1e-6


def test_bucket_cumulative_and_yuan():
    # 两个桶各 1 样本：桶A 500W*60s，桶B 600W*60s（不同 ts）
    slots = [(0, 3600), (3600, 7200)]
    buckets = bucket_stats([(0, 500.0), (3600, 600.0)], slots)
    a, b = buckets
    assert a["cum_kwh"] > 0 and b["cum_kwh"] > a["cum_kwh"]       # 只升不降
    assert abs(b["cum_yuan"] - b["cum_kwh"] * PRICE_YUAN_PER_KWH) < 1e-6
    assert abs(a["kwh"] - (500.0 * 60 / 3.6e6)) < 1e-6


def test_empty_bucket_none_avg_zero_energy():
    slots = [(0, 3600), (3600, 7200)]
    buckets = bucket_stats([(3600, 600.0)], slots)
    assert buckets[0]["avg_w"] is None and buckets[0]["kwh"] == 0
    assert buckets[1]["avg_w"] is not None


# ── 3. query_power_series（DB 驱动）────────────────────────────

def test_query_power_series_hour(tmp_iffdb, monkeypatch):
    now = _mktime(2026, 9, 23, 14, 37, 0)
    monkeypatch.setattr("inferfabric.power_stats.time.time", lambda: now)
    samples = []
    for i in range(24 * 60):                       # 24h 连续每分钟一个样本
        t = now - (24 * 60 - 1 - i) * 60           # 覆盖 23 小时前至今
        w = 500.0 + (i % 10)
        samples.append({"ts": t, "watts": w})
    tmp_iffdb.insert_gpu_power_samples(samples)

    res = query_power_series(tmp_iffdb, "hour")
    assert res["gran"] == "hour" and res["available"]
    assert len(res["buckets"]) == 24
    assert res["price_yuan_per_kwh"] == 1.0
    # 首桶 t = 23 小时前整点；avg_w = 该桶样本均值
    # 样本 i=59..118（500 + i%10）→ 6 个满周期 (500..509)，均值 504.5
    b0 = res["buckets"][0]
    assert b0["t"] == (now - (now % 3600)) - 23 * 3600
    assert abs(b0["avg_w"] - 504.5) < 0.01
    # 累计只增不减、末桶总电量 = totals
    cums = [b["cum_kwh"] for b in res["buckets"]]
    assert cums == sorted(cums) and cums[0] > 0
    assert abs(res["totals"]["kwh"] - cums[-1]) < 0.01
    assert abs(res["totals"]["yuan"] - cums[-1]) < 0.01         # ¥1/度
    assert res["totals"]["avg_w"] is not None


def test_query_power_series_day_buckets(tmp_iffdb, monkeypatch):
    now = _mktime(2026, 9, 23, 8, 0, 0)
    monkeypatch.setattr("inferfabric.power_stats.time.time", lambda: now)
    # 今天零点起每小时一个样本（仅今天 + 昨天部分）→ 30 桶、今天桶有值
    today = _mktime(2026, 9, 23)
    for i in range(8):
        tmp_iffdb.insert_gpu_power_samples([{"ts": today + i * 3600, "watts": 450.0}])
    res = query_power_series(tmp_iffdb, "day")
    assert len(res["buckets"]) == 30
    assert res["buckets"][-1]["t"] == today
    assert res["buckets"][-1]["avg_w"] is not None
    assert res["buckets"][0]["avg_w"] is None                    # 29 天前无数据
    assert res["totals"]["count"] == 8


def test_query_power_series_week(tmp_iffdb, monkeypatch):
    now = _mktime(2026, 9, 21, 8, 0, 0)          # 周一
    monkeypatch.setattr("inferfabric.power_stats.time.time", lambda: now)
    week_start = _mktime(2026, 9, 21)
    # 本周 / 上周 / 上上周 各一个样本（样本时刻 = 各自周一起点 + 1h，向过去回退）
    for i in range(3):
        tmp_iffdb.insert_gpu_power_samples(
            [{"ts": week_start - i * 7 * 86400 + 3600, "watts": 500.0 / (i + 1)}])
    res = query_power_series(tmp_iffdb, "week")
    assert res["gran"] == "week"
    assert len(res["buckets"]) == 13
    assert res["buckets"][-1]["t"] == week_start          # 末桶 = 本周
    nonempty = [b for b in res["buckets"] if b["avg_w"] is not None]
    assert len(nonempty) == 3
    assert res["totals"]["count"] == 3
    # 累计只增不减、末桶 total 对齐
    cums = [b["cum_kwh"] for b in res["buckets"]]
    assert cums == sorted(cums)


def test_query_power_series_month(tmp_iffdb, monkeypatch):
    now = _mktime(2026, 9, 23)
    monkeypatch.setattr("inferfabric.power_stats.time.time", lambda: now)
    for ts, w in [(_mktime(2026, 8, 15), 400.0), (_mktime(2026, 8, 20), 420.0),
                  (_mktime(2026, 9, 1), 500.0)]:
        tmp_iffdb.insert_gpu_power_samples([{"ts": ts, "watts": w}])
    res = query_power_series(tmp_iffdb, "month")
    starts = [b["t"] for b in res["buckets"]]
    assert _mktime(2026, 8, 1) in starts and _mktime(2026, 9, 1) in starts
    aug = res["buckets"][starts.index(_mktime(2026, 8, 1))]
    sep = res["buckets"][starts.index(_mktime(2026, 9, 1))]
    assert aug["avg_w"] is not None and sep["avg_w"] is not None
    assert sep["cum_kwh"] > aug["cum_kwh"]                       # 累计跨月递增
    assert aug["kwh"] > 0


def test_query_power_series_empty(tmp_iffdb, monkeypatch):
    now = _mktime(2026, 9, 23, 14, 0, 0)
    monkeypatch.setattr("inferfabric.power_stats.time.time", lambda: now)
    res = query_power_series(tmp_iffdb, "hour")
    assert not res["available"]
    assert len(res["buckets"]) == 24
    assert all(b["avg_w"] is None and b["kwh"] == 0 for b in res["buckets"])
    assert res["totals"] == {"kwh": 0.0, "yuan": 0.0, "avg_w": None, "count": 0}


# ── 4. PowerSampler ────────────────────────────────────────────

def _fake_telemetry(readings, start_ts=1000, step=60):
    """DeviceTelemetry T4: PowerSampler 消费统一遥测（不再用 probe 回调）。

    伪后端按序吐 snapshot：power_w 取自 readings（None=本拍不可用），
    timestamp 自 start_ts 步进 step（采样落盘的 ts 源）。
    """
    from inferfabric.platform.telemetry import DeviceSnapshot
    seq = iter(readings)
    ts_seq = itertools.count(start_ts, step)

    class _Tel:
        def snapshot(self):
            return DeviceSnapshot(
                platform="linux", accelerator="nvidia", memory_kind="vram",
                memory_used_mb=None, memory_total_mb=None,
                memory_budget_mb=None, utilization_pct=None,
                power_w=next(seq, None), temperature_c=None,
                clock_mhz=None, pressure_level=None,
                timestamp=float(next(ts_seq)), sources={})

        def capabilities(self):
            from inferfabric.platform.telemetry import DeviceCapabilities
            return DeviceCapabilities(power=True)

    return _Tel()


def test_sampler_thread_inserts_non_none(tmp_iffdb, monkeypatch):
    s = PowerSampler(tmp_iffdb, interval=0.02,
                     telemetry=_fake_telemetry([500.5, None, 510.2, 480.1, None]))
    # _now 只驱动清理检查（time.time 会触发首拍 prune 清掉小 ts 样本）；
    # 钉 0 → 永不 prune（本用例只验证采样落盘）。
    monkeypatch.setattr(s, "_now", lambda: 0)
    s.start()
    time.sleep(0.15)
    s.stop()
    rows = tmp_iffdb.query_device_power_samples(since=0, device_id="aggregate")
    # None 读数跳过 → 只落 3 行非 None 采样；ts 单调递增
    assert len(rows) == 3
    rows_sorted = sorted(rows, key=lambda r: r["ts"])
    assert [r["watts"] for r in rows_sorted] == [500.5, 510.2, 480.1]
    assert [r["ts"] for r in rows_sorted] == sorted(r["ts"] for r in rows_sorted)


def test_sampler_direct_sample_once(tmp_iffdb):
    s = PowerSampler(tmp_iffdb, interval=999, telemetry=_fake_telemetry([520.0]))
    assert s.sample_once() == 520.0
    s2 = PowerSampler(tmp_iffdb, interval=999, telemetry=_fake_telemetry([None]))
    assert s2.sample_once() is None                              # 不可用 → 不落盘
    rows = tmp_iffdb.query_device_power_samples(since=0, device_id="aggregate")
    assert len(rows) == 1 and abs(rows[0]["watts"] - 520.0) < 1e-9