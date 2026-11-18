"""
inferfabric/power_stats.py — GPU 板卡功耗时间序列 (v6.2 监控 TAB「功耗 / 电费」卡).

口径：功耗 = GPU 板卡实时功耗（nvidia-smi power.draw，含 idle 底耗）；
每 60s 后台采样一次落 SQLite（gpu_power_samples，v007 迁移），90 天留存。
电费 = 累计耗电度数 × ¥1/度（PRICE_YUAN_PER_KWH，常量，后续可配置化）。

视图语义（已与用户定稿）：
  小时 = 近 24h，每小时一桶（对齐整点，末桶为当前小时的部分桶）
  天   = 近 30 天，每天一桶（对齐本地零点）
  周   = 近 ~90 天，每周一桶（对齐本地周一，末桶为本周的部分桶；月整体弃用）
  月   = 按自然月（YYYY-MM），从最早有数据的月份到当前月（只读端点保留，UI 不再提供）

每桶输出：
  avg_w     桶内样本均值（无样本 → None，前端断开）
  kwh       桶内耗电（梯形积分；单样本按采样间隔 60s 折算）
  cum_kwh   窗口起点累计到该桶末的总耗电（只增不减）
  cum_yuan  累计电费 = cum_kwh × price（¥1/度 下数值相等）

边界说明：梯形积分只取桶内相邻样本对，跨桶边界的一对样本断桥
（一小时 ~60 个样本，桥接误差 < 2%，可接受）。
"""

from __future__ import annotations

import logging
import threading
import time

# 墙钟 slot 单一事实源在 bucketing.py；此处 re-export 保向后兼容
# （test_power_series 仍 `from power_stats import hour_slots …`）。
from inferfabric.bucketing import (  # noqa: F401
    HOUR_WIN_BUCKETS, DAY_WIN_BUCKETS, WEEK_WIN_BUCKETS, WEEK_BUCKET_SEC,
    hour_slots, day_slots, week_slots,
)

log = logging.getLogger("inferfabric.power_stats")

PRICE_YUAN_PER_KWH = 1.0          # 电价常量（¥/度；1 度 = 1 kWh）
SAMPLE_INTERVAL_SEC = 60.0        # PowerSampler 采样间隔（单样本能量折算用）


# ── 探针 ───────────────────────────────────────────────────────

def probe_gpu_power_w() -> float | None:
    """DEPRECATED shim — 委托 DeviceTelemetry snapshot().power_w。

    供采样器之外的旧调用方使用；不可用 → None（不伪造 0）。"""
    try:
        from .platform import detect
        from .platform.telemetry import telemetry_for_profile
        return telemetry_for_profile(detect()).snapshot().power_w
    except Exception:
        return None


# ── 采样器 ─────────────────────────────────────────────────────

class PowerSampler:
    """60s 后台线程采样设备功耗并落 SQLite（device_power_samples，v011）。

    消费 DeviceTelemetry：每拍成功 snapshot 落一个聚合样本
    (device_id='aggregate', source='device_telemetry')；power_w 为 None
    （不可用）跳过本拍、下一拍重试 —— 不伪造 0（真实 0W 保留）。
    每天清理一次超期样本（RETENTION_DAYS，幂等）。
    """

    RETENTION_DAYS = 90

    def __init__(self, db, interval: float = 60.0, probe=None, telemetry=None):
        self._db = db
        self._interval = interval
        # DeviceTelemetry T4: probe 参数仅为向后兼容保留（不再使用）；
        # telemetry 未注入时用工厂默认（按平台装配）。
        self._probe = probe  # deprecated, unused
        if telemetry is None:
            from .platform import detect
            from .platform.telemetry import telemetry_for_profile
            telemetry = telemetry_for_profile(detect())
        self._telemetry = telemetry
        self._stop = threading.Event()
        self._thread = None
        self._last_prune = 0.0
        self.sample_count = 0

    def _now(self):
        return time.time()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="gpu-power")
        self._thread.start()

    def stop(self):
        self._stop.set()
        th = self._thread
        if th:
            th.join(timeout=2.0)

    def sample_once(self):
        """拍一拍：snapshot.power_w 成功落一行，None 跳过不落盘。"""
        snap = self._telemetry.snapshot()
        w = snap.power_w
        if w is None:
            return None
        self._db.insert_device_power_samples([{
            "ts": int(snap.timestamp),
            "platform": snap.platform,
            "accelerator": snap.accelerator,
            "device_id": "aggregate",
            "watts": round(float(w), 1),
            "source": "device_telemetry",
        }])
        self.sample_count += 1
        return w

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as e:
                log.debug("power sample failed: %s", e)
            try:
                now = self._now()
                if now - self._last_prune >= 86400:
                    self._last_prune = now
                    self._db.prune_device_power_samples(now - self.RETENTION_DAYS * 86400)
            except Exception as e:
                log.debug("power prune failed: %s", e)
            self._stop.wait(self._interval)


# ── 分桶 ───────────────────────────────────────────────────────
# hour/day/week_slots 已移至 inferfabric.bucketing（墙钟对齐单一事实源），
# 顶部 re-export；month_slots 为功耗卡专属（变长、需 first_ts），保留于此。

def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _month_start(y: int, m: int) -> int:
    return int(time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1)))


def month_slots(now_ts: int, first_ts: int) -> list[tuple[int, int]]:
    """自然月桶：从 first_ts 所在月到当前月（首个无数据月给空桶，与 hour/day 同构）。"""
    lt0 = time.localtime(first_ts)
    lt1 = time.localtime(now_ts)
    y, m = lt0.tm_year, lt0.tm_mon
    y1, m1 = lt1.tm_year, lt1.tm_mon
    slots: list[tuple[int, int]] = []
    while (y, m) <= (y1, m1):
        start = _month_start(y, m)
        ny, nm = _next_month(y, m)
        end = _month_start(ny, nm)
        slots.append((start, end))
        y, m = ny, nm
    return slots


def bucket_stats(samples: list[tuple[int, float]],
                 slots: list[tuple[int, int]]) -> list[dict]:
    """samples[(ts,watts)] 升序 × 升序桶 → 每桶 {t, avg_w, kwh, cum_kwh, cum_yuan}。

    能量 = 桶内相邻样本对梯形积分；单样本按 SAMPLE_INTERVAL_SEC 折算。
    cum 从窗口起点起算，跨桶累计（只增不减）。
    """
    buckets: list[dict] = []
    cum = 0.0
    si = 0
    n = len(samples)
    for start, end in slots:
        pts: list[tuple[int, float]] = []
        while si < n and samples[si][0] < start:
            si += 1
        j = si
        while j < n and samples[j][0] < end:
            pts.append(samples[j])
            j += 1
        if pts:
            avg_w = round(sum(w for _, w in pts) / len(pts), 1)
            if len(pts) == 1:
                en = pts[0][1] * SAMPLE_INTERVAL_SEC / 3.6e6
            else:
                en = sum((pts[i][1] + pts[i + 1][1]) / 2.0 * (pts[i + 1][0] - pts[i][0])
                         for i in range(len(pts) - 1)) / 3.6e6
        else:
            avg_w = None
            en = 0.0
        cum += en
        buckets.append({
            "t": int(start),
            "avg_w": avg_w,
            "kwh": en,
            "cum_kwh": cum,
            "cum_yuan": cum * PRICE_YUAN_PER_KWH,
        })
    return buckets


# ── 查询入口（handler /api/power、/api/device_power 调用）────────

def _power_series_from_stream(db, gran: str, price: float,
                              device_id: str) -> dict:
    """单流分桶序列（legacy 或 aggregate），流之间从不跨流积分。"""
    now = int(time.time())
    if gran == "day":
        slots = day_slots(now)
    elif gran == "week":
        slots = week_slots(now)
    elif gran == "month":
        oldest = db.query_device_power_samples(since=0, limit=1,
                                               device_id=device_id)
        slots = month_slots(now, oldest[0]["ts"] if oldest else now)
    else:
        slots = hour_slots(now)
    since = slots[0][0]
    rows = db.query_device_power_samples(since, until=now + 60,
                                         device_id=device_id)
    samples = [(int(r["ts"]), float(r["watts"])) for r in rows]
    buckets = bucket_stats(samples, slots)
    avg_ws = [b["avg_w"] for b in buckets if b["avg_w"] is not None]
    kwh_total = sum(b["kwh"] for b in buckets)
    return {
        "gran": gran,
        "price_yuan_per_kwh": price,
        "buckets": buckets,
        "totals": {
            "kwh": round(kwh_total, 4),
            "yuan": round(kwh_total * price, 4),
            "avg_w": round(sum(avg_ws) / len(avg_ws), 1) if avg_ws else None,
            "count": len(rows),
        },
        "available": len(rows) > 0,
    }


def query_power_series(db, gran: str = "hour", price: float = PRICE_YUAN_PER_KWH) -> dict:
    """GET /api/power — legacy 流（unknown/unknown，v007 历史）分桶序列。

    只读 legacy 流：v007 首卡样本永不与 v011 全卡样本跨流插值。
    """
    return _power_series_from_stream(db, gran, price, device_id="unknown")


def query_device_power_series(db, gran: str = "hour", price: float = PRICE_YUAN_PER_KWH) -> dict:
    """GET /api/device_power — 当前聚合流（accelerator/aggregate）分桶序列。

    与 /api/power 同 shape {gran, buckets, totals, available}。"""
    return _power_series_from_stream(db, gran, price, device_id="aggregate")