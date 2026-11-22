"""inferfabric/monitor_report.py — 统一统计口径（PLAN-2026-11-15 v2 第 3 节契约）。

P0 纯函数：窗口解析、分桶边界、模型身份。不做 I/O、不依赖时钟——
until 一律显式传入，保证同一报告内所有分组共享同一区间。

窗口契约（滚动窗口，非自然日历）：
  1h  → 3600s  × 12 桶（5min）
  24h → 86400s × 24 桶（1h）
  7d  → 604800s × 28 桶（6h）
  30d → 2592000s × 30 桶（24h）
区间 [since, until)：since 纳入、until 排除，与 SQL timestamp >= since
AND timestamp < until 严格对应。

身份契约：(location, provider, actual_model_id)。location ∈ local/cloud/
unknown；local 的 provider 为空串；route 缺失或无法定位时 location=unknown，
不用当前配置回填猜测历史行。
"""

import threading
import time

# window → (跨度秒, 桶数)
_WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 12),
    "24h": (86400, 24),
    "7d": (604800, 28),
    "30d": (2592000, 30),
}

DEFAULT_WINDOW = "24h"
VALID_WINDOWS = frozenset(_WINDOWS)
VALID_SCOPES = frozenset({"all", "local", "cloud"})

# 报告契约版本（Task 7）：结构变更必须推进
REPORT_CONTRACT = "monitor-report/1"

# 分页扫描页大小：query_request_log 默认 limit 会截断 10 万行窗口，
# 这里按键分页全量扫；样本（ttft/duration 浮点）是唯一驻留内存的大对象
_PAGE_SIZE = 20_000
# 键集分页安全上限：2000 页 × 2 万行 = 4000 万行。触顶即 truncated=True
#（如实标注不完整），绝不静默谎报完整（终审 I1）
_MAX_PAGES = 2_000

# 错误 Top 条数（脱敏后仅 class + 计数）
_ERROR_TOP_N = 10

_UNKNOWN = "unknown"


def resolve_window(window: str | None, until: float) -> tuple[float, float, int]:
    """window → (since, until, buckets)。区间 [since, until)，until 排除。

    非法窗口抛 ValueError；window 为 None/空串时取默认 24h。
    """
    win = window or DEFAULT_WINDOW
    if win not in _WINDOWS:
        raise ValueError(
            f"unknown window {window!r} (expected one of {sorted(_WINDOWS)})")
    span, buckets = _WINDOWS[win]
    return until - span, until, buckets


def bucket_edges(since: float, until: float, buckets: int) -> tuple[float, ...]:
    """[since, until) 均分为 buckets 桶，返回 buckets+1 个边界。

    edges[i] <= t < edges[i+1] 归第 i 桶；edges[0]=since（纳入）、
    edges[-1]=until（排除）。按实际秒长均分，不冒称本地自然日。
    """
    if buckets <= 0:
        raise ValueError(f"buckets must be positive, got {buckets}")
    if until <= since:
        raise ValueError(f"until must be greater than since ({since=} {until=})")
    width = (until - since) / buckets
    return tuple(since + i * width for i in range(buckets + 1))


def model_identity(row: dict) -> tuple[str, str, str]:
    """请求日志行 → (location, provider, actual_model_id) 聚合身份。

    - route 含 `cloud:<provider>/…`（含 pool 路由 `pool:<p>/cloud:…`）→
      cloud，provider 取 route 解析值，缺失时退回 cloud_provider 列。
    - route 含 `local:<…>` → local，provider 为空串。
    - 其余（route 缺失/无法识别）→ unknown，不冒认；model 缺失同样
      记 unknown。显示名称不参与聚合主键。
    """
    model = row.get("model") or _UNKNOWN
    route = row.get("route") or ""
    cloud_provider = row.get("cloud_provider") or ""

    location, provider = _UNKNOWN, _UNKNOWN
    idx = route.find("cloud:")
    if idx != -1:
        rest = route[idx + len("cloud:"):]
        provider = rest.split("/", 1)[0] if rest else ""
        if not provider:
            provider = cloud_provider
        location = "cloud"
    elif "local:" in route:
        location, provider = "local", ""
    else:
        provider = _UNKNOWN

    if not provider:
        provider = _UNKNOWN if location == _UNKNOWN else provider
    return location, provider, model


# ═══════════════════════════════════════════════════════════════════
# 同快照监控报告（PLAN-2026-11-15 v2 Task 7 / P3）
# ═══════════════════════════════════════════════════════════════════


def _percentile(samples: list[float], q: float) -> float | None:
    """线性插值分位（全窗 p95 按原样本求，不平均桶分位）。空样本 → None。"""
    if not samples:
        return None
    s = sorted(samples)
    idx = (len(s) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _error_class(row: dict) -> str:
    """失败行 → 脱敏错误类别。只出类别名，原文（可能含凭证）不进摘要。"""
    status = row.get("status")
    error = str(row.get("error") or "")
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if isinstance(status, int) and status >= 500:
        return "upstream_5xx"
    lowered = error.lower()
    if any(k in lowered for k in ("no_upstream", "unknown_pool", "pool_unavailable",
                                  "no_available_candidate", "no_ready_target",
                                  "no_compatible_target")):
        return "no_upstream"
    if isinstance(status, int) and status >= 400:
        return "upstream_4xx"
    if error:
        return "other"
    return "other"


def _pool_name(row: dict) -> str | None:
    """行归属池名：摘要优先，其次 route 的 `pool:<name>` 标记；否则 None。"""
    ps = row.get("metadata") or {}
    summary = ps.get("pool_summary") if isinstance(ps, dict) else None
    if isinstance(summary, dict) and summary.get("pool"):
        return str(summary["pool"])
    route = row.get("route") or ""
    if route.startswith("pool:"):
        rest = route[len("pool:"):]
        return rest.split("/", 1)[0] or None
    return None


def build_monitor_report(db, *, window: str = DEFAULT_WINDOW, until: float,
                         scope: str = "all", prices: dict | None = None) -> dict:
    """同快照监控报告：totals/models/series/pools/errors 共享区间与读快照。

    db 需提供 query_request_log(since, until=None, model=None, limit=N)
    （TelemetryHub 直通 db.py 即可）。扫描按键分页全量进行，不截断；
    聚合流式进行，仅性能样本（浮点列表）驻留内存。

    - 身份与分桶遵守第 3 节契约（model_identity / bucket_edges）；池行带
      final_target.actual_model_id 时按实际模型聚合（行级 model 保留请求名）。
    - scope 过滤：local / cloud 按 identity location；unknown 只进 all。
    - 缺 TTFT → 样本不计入（分位为 None，不是 0ms）；摘要 usage_known
      False 的行、以及无摘要证据的成功零 token 行，计入
      totals.usage_unknown_rows；prices 缺省 → 成本为 null（unpriced），
      不冒充 0。prices 形状 = metrics_aggregator.price_config
      （model_id → CloudModelPrice，¥/1M tokens），计价走 cost_of_row
      唯一入口（口径防漂移）。
    - 池统计：早期失败（无摘要）入分母；attempt/retry/fallback 只汇总
      有摘要的行；summary_coverage = 有摘要行 / 分母。
    - 扫描按 (timestamp, id) 真键集分页（终审 I1）：同秒海量行也能全量
      计数；仅触及安全页数上限时 truncated=True（无法证明完整）。
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r} (expected one of {sorted(VALID_SCOPES)})")
    since, until, buckets = resolve_window(window, until)
    edges = bucket_edges(since, until, buckets)

    totals = {"requests": 0, "success": 0, "success_rate": 0.0,
              "tokens_in": 0, "tokens_out": 0,
              "usage_unknown_rows": 0, "cost_estimate": None,
              "cost_coverage": "unpriced"}
    models: dict[tuple, dict] = {}
    series = [{"bucket_start": edges[i], "bucket_end": edges[i + 1],
               "requests": 0, "success": 0, "tokens_in": 0, "tokens_out": 0}
              for i in range(buckets)]
    pools: dict[str, dict] = {}
    error_counts: dict[str, int] = {}
    pool_summary_rows = 0
    cost = 0.0
    priced_tokens = False

    import bisect

    from inferfabric.metrics_aggregator import cost_of_row

    # (timestamp, id) 真键集分页（终审 I1）：键严格递减，无重叠无死循环；
    # nextafter 方案在海量同秒行会触发守卫提前停扫且谎报完整，已废弃
    cursor_ts: float | None = None
    cursor_id: int | None = None
    truncated = False
    pages = 0
    while True:
        rows = db.query_request_log(since, until, before_ts=cursor_ts,
                                    before_id=cursor_id, limit=_PAGE_SIZE)
        if not rows:
            break
        pages += 1
        last = rows[-1]
        cursor_ts = last.get("timestamp")
        cursor_id = last.get("id")
        for row in rows:
            meta = row.get("metadata") or {}
            summary = meta.get("pool_summary") if isinstance(meta, dict) else None
            loc, provider, base_model = model_identity(row)
            # 池行身份按实际承接模型（终审 I4）：final_target.actual_model_id
            # 有据可用时取代请求别名；行级 model 不改写（历史行为可复现）
            model = base_model
            if isinstance(summary, dict):
                ft = summary.get("final_target")
                actual = ft.get("actual_model_id") if isinstance(ft, dict) else None
                if actual:
                    model = str(actual)
            if scope == "local" and loc != "local":
                continue
            if scope == "cloud" and loc != "cloud":
                continue

            status = row.get("status")
            # 2xx 才是成功（终审 I2）：302/304 与传输契约一致，不计成功
            is_success = isinstance(status, int) and 200 <= status < 300
            tin = int(row.get("tokens_in") or 0)
            tout = int(row.get("tokens_out") or 0)

            totals["requests"] += 1
            if is_success:
                totals["success"] += 1
            totals["tokens_in"] += tin
            totals["tokens_out"] += tout

            # series 桶（全量行都归桶，保证图桶合计 == 总量）
            ts = row.get("timestamp")
            if isinstance(ts, (int, float)) and since <= ts < until:
                b = series[bisect.bisect_right(edges, ts) - 1]
                b["requests"] += 1
                if is_success:
                    b["success"] += 1
                b["tokens_in"] += tin
                b["tokens_out"] += tout

            # models 聚合（跨 provider 同名不混合：键含 location+provider）
            mkey = (loc, provider, model)
            m = models.get(mkey)
            if m is None:
                m = models[mkey] = {
                    "location": loc, "provider": provider, "model": model,
                    "requests": 0, "success": 0, "tokens_in": 0, "tokens_out": 0,
                    "ttft_samples": [], "duration_samples": [],
                }
            m["requests"] += 1
            if is_success:
                m["success"] += 1
            m["tokens_in"] += tin
            m["tokens_out"] += tout
            ttft = row.get("ttft_ms")
            if isinstance(ttft, (int, float)) and ttft > 0:
                m["ttft_samples"].append(float(ttft))
            dur = row.get("duration_ms")
            if isinstance(dur, (int, float)) and dur > 0:
                m["duration_samples"].append(float(dur))

            # 错误类别（脱敏）
            if not is_success and (row.get("error") or (isinstance(status, int) and status >= 400)):
                cls = _error_class(row)
                error_counts[cls] = error_counts.get(cls, 0) + 1

            # usage 缺失证据（终审 I3）：有摘要用摘要的 usage_known；
            # 无摘要的成功零 token 行按缺失计（tokens 列 NOT NULL，缺失
            # 落库即 0，读取侧无法区分 NULL；真实 0/0 计 unknown 与
            # Task 3「宁 unknown 不冒认 0」裁决同向）
            summary_counts_unknown = False
            if isinstance(summary, dict):
                for at in summary.get("attempts") or []:
                    if isinstance(at, dict) and at.get("usage_known") is False:
                        totals["usage_unknown_rows"] += 1
                        summary_counts_unknown = True
                        break
            elif is_success and tin == 0 and tout == 0:
                totals["usage_unknown_rows"] += 1

            # 池统计
            pname = _pool_name(row)
            if pname is not None:
                p = pools.get(pname)
                if p is None:
                    p = pools[pname] = {
                        "pool": pname, "requests": 0, "success": 0,
                        "final_targets": {}, "attempt_count": 0,
                        "retry_count": 0, "fallback_count": 0,
                        "summary_rows": 0,
                    }
                p["requests"] += 1
                if is_success:
                    p["success"] += 1
                if isinstance(summary, dict):
                    pool_summary_rows += 1
                    p["summary_rows"] += 1
                    ft = summary.get("final_target") or {}
                    cid = ft.get("candidate_id") if isinstance(ft, dict) else None
                    if cid:
                        p["final_targets"][cid] = p["final_targets"].get(cid, 0) + 1
                    p["attempt_count"] += int(summary.get("attempt_count") or 0)
                    p["retry_count"] += int(summary.get("retry_count") or 0)
                    p["fallback_count"] += int(summary.get("fallback_count") or 0)

            # 成本估算（终审 I5）：复用 cost_of_row 唯一计价入口
            # （¥/1M tokens，仅云端行）；无价格配置 = unpriced（null 而非 0）
            if prices:
                c = cost_of_row(prices, row)
                if c > 0:
                    cost += c
                    priced_tokens = True

        if len(rows) < _PAGE_SIZE:
            break
        if pages >= _MAX_PAGES:
            truncated = True   # 触及安全上限，无法证明扫描完整
            break

    totals["success_rate"] = round(totals["success"] / totals["requests"], 4) \
        if totals["requests"] else 0.0
    if priced_tokens:
        totals["cost_estimate"] = round(cost, 6)
        totals["cost_coverage"] = "priced"

    model_rows = []
    for m in models.values():
        ttft = m.pop("ttft_samples")
        dur = m.pop("duration_samples")
        # 分位不做 1 位舍入：round(95.05,1) 因浮点表示回退 95.0，破坏
        # 线性插值契约；展示层自行格式化
        m["p50_ttft_ms"] = _percentile(ttft, 0.5)
        m["p95_ttft_ms"] = _percentile(ttft, 0.95)
        m["ttft_samples"] = len(ttft)
        m["p50_duration_ms"] = _percentile(dur, 0.5)
        m["p95_duration_ms"] = _percentile(dur, 0.95)
        m["duration_samples"] = len(dur)
        m["success_rate"] = round(m["success"] / m["requests"], 4) if m["requests"] else 0.0
        model_rows.append(m)
    model_rows.sort(key=lambda m: (-m["requests"], m["model"]))

    pool_rows = []
    for p in pools.values():
        cov = p.pop("summary_rows") / p["requests"] if p["requests"] else 0.0
        p["summary_coverage"] = round(cov, 4)
        pool_rows.append(p)
    pool_rows.sort(key=lambda p: -p["requests"])

    errors = [{"class": c, "count": n}
              for c, n in sorted(error_counts.items(), key=lambda kv: -kv[1])[:_ERROR_TOP_N]]

    return {
        "contract": REPORT_CONTRACT,
        "window": window or DEFAULT_WINDOW,
        "scope": scope,
        "since": since,
        "until": until,
        "collected_at": time.time(),
        "coverage": {
            "rows_scanned": totals["requests"],
            "pool_summary_rows": pool_summary_rows,
            # (timestamp,id) 键集全量扫描；True 仅在触及安全页数上限时
            "truncated": truncated,
        },
        "totals": totals,
        "models": model_rows,
        "series": series,
        "pools": pool_rows,
        "errors": errors,
    }


class MonitorReportCache:
    """按 (window, scope) 的短 TTL 单飞缓存。

    TTL 内同查询复用同一报告对象（数字/表/图天然一致）；miss 时在锁内
    构建做单飞（并发请求等待首个构建，不重复扫库）。大扫描只发生在
    报告端点，绝不接入 3s 全局轮询路径。
    """

    def __init__(self, ttl_s: float = 15.0):
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def get_or_build(self, db, *, window: str, until: float,
                     scope: str = "all", prices: dict | None = None) -> dict:
        key = (window or DEFAULT_WINDOW, scope)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
            report = build_monitor_report(db, window=window, until=until,
                                          scope=scope, prices=prices)
            self._cache[key] = (now, report)
            # 防 key 膨胀（窗口×scope 组合有限，仍兜底清理）
            if len(self._cache) > 32:
                cutoff = now - self._ttl
                self._cache = {k: v for k, v in self._cache.items()
                               if v[0] >= cutoff} or {key: (now, report)}
            return report
