# 模型延迟趋势（时间轴 × 模型分色）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把监控页「延迟分布 P50/P95」双卡（x 轴 = 模型名，用户反馈看不懂）改造成**模型延迟趋势**：两张小倍数卡（左 TTFT / 右 TPOT），**横轴 = 时间**、**每条线 = 一个模型（不同颜色）**、**图例 = 模型→颜色**、**值 = 时间桶内的统计百分位（历史状态，非 live）**。方便横向对比各模型、定位要优化的模型。

**Architecture:** 后端新增 `MetricsAggregator.get_latency_series()`（时间分桶 × 逐模型 TTFT/TPOT 分位，零 schema 变更，TPOT 仍逐请求推导）+ 只读端点 `GET /api/latency?window=`（服务端 `_ExpensiveCache` 单飞缓存）；前端把双卡从「模型条形」换成「时间轴 × 逐模型分色折线」，加共享**模型 chip 选择器**（默认前 4、上限 5）+ **P50 / P50+P95 切换**（P95 同色虚线，默认关）+ **1h/24h/7d 窗口**（桶宽 5min/1h/6h）。

**Tech Stack:** Python 3.10+（MetricsAggregator 纯内存 + 现有 `quantile`/`median`），ECharts（vendored，`window.IFCharts` 封装），dashboard 零构建。

**Spec:** 本会话用户确认的重做方向（2026-09-22）：x 轴走时间、模型用颜色区分、TTFT/TPOT 是状态统计值而非 live 值。三个已定决策：①默认请求数前 4、chip 增减、同屏上限 5（CVD 校验上限，见 Global Constraints）；②P50 实线 + P95 同色虚线（默认关）；③1h/24h/7d 三档，桶宽 5min/1h/6h（12/24/28 点）。

**取代：** 上一版 plan `2026-09-22-latency-ttft-tpot-dual-cards.md`（已上线 b524bbc 的双卡条形方案）。本 plan 的 2 张卡**替换**该方案的渲染逻辑与卡片内 seg/表格；后端 `get_metrics`/`_metrics_axis` 保留不动。

## Global Constraints

- **只读**：监控页禁止 `POST`、`<form`、`method=`（新增的 chip/seg 一律 `type="button"`）。测试 `test_monitor_readonly` 会断言。
- **单 y 轴**：两指标量纲差 ~2 数量级（TTFT ~1000–3000 ms，TPOT ~2–16 ms/token），**保持两张小倍数卡、各卡单 y 轴**，禁双 y 轴（`IFCharts` `_applyRules` 会截断 dual-axis）。
- **转发核心不动**：`handler.py` 只加 metrics/latency 接线（新端点 + 缓存 + 路由），不改转发/路由/认证/重试/缓存核心。
- **零 schema 变更**：TTFT/TPOT 从现有 `request_log` 列（`ttft_ms`/`duration_ms`/`tokens_out`/`status`/`timestamp`/`model`）推导，不加 DB 列。
- **CVD 安全配色**：折线颜色用独立 **5 色**分类调色板（主题各自校验通过：dark `#3a86e0,#b57a14,#12a594,#8b5cf6,#e0574a`，light `#1f6fd6,#b45309,#0e8f7f,#7c3aed,#cf4636`），**固定顺序、绝不循环**；同屏 6 色会触发绿↔红 deutan ΔE≈5.2（低于合法下限 8），故同屏上限定 5，API `top_n` 默认 5。改动配色必须重跑 `validate_palette.js`（dataviz skill）保持 ALL PASS。
- **TPOT 推导条件**（沿用 `get_metrics`）：仅 `status<400 且 ttft_ms>0 且 duration_ms>ttft_ms 且 tokens_out>=2` 的样本。
- **空窗口**：窗口内零样本 → `series: {}`（无模型），但 `buckets` 始终是完整时间轴（无样本 → 各 series 值全 None，长度不变）；前端在模型集为空时走 empty state（不注入占位）。
- **开发位置**：所有代码在 `sandbox/`（gitignored 工作副本）开发 → pytest → smoke → diff review → 合并到生产 main。`sandbox/` 在 `.gitignore`。
- commit 末尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`；生产合并用**显式 pathspec**（绝不 `git add -A`，工作树有 `.state.db-shm/wal` 删除 + 未跟踪项须排除）。

---

### Task 0: 沙盒基线同步

**Files:**
- Sandbox: 全仓

**Interfaces:**
- Produces: 沙盒 dev 分支基线 = 生产 main（内容逐字节一致），后续 diff 干净。

- [ ] **Step 1: 确认生产/沙盒内容已一致（上一 feature 回同步后应零漂移）**

Run: `cd /home/vince/inferfabric && diff -rq inferfabric sandbox/inferfabric 2>/dev/null | grep -v __pycache__ | grep -v "只存在\|only in"`
Expected: 无输出（零漂移）。若有输出，`cp` 生产 → 沙盒对应文件后重跑至零漂移。

- [ ] **Step 2: 打基线 commit（记录本 plan 起点）**

```bash
cd /home/vince/inferfabric/sandbox
git add -A 2>/dev/null || true   # 仅沙盒内（沙盒 dev 分支），.superpowers 不入
git commit -m "chore(baseline): v5.4 延迟趋势 plan 起点（内容=生产 main b524bbc）" --allow-empty
git rev-parse HEAD   # 记为 BASE，review package 用
```

记录 BASE = `git rev-parse HEAD`（Task 1 前）。

---

### Task 1: 后端 `get_latency_series`

**Files:**
- Modify: `inferfabric/metrics_aggregator.py`（加 `import math`、`import datetime`；加方法 `get_latency_series`）
- Test: `tests/unit/infra/test_metrics_series.py`（新建）

**Interfaces:**
- Consumes: `self._samples`（deque）、`self._lock`、`self._name_map`、模块级 `quantile`/`median`/`defaultdict`。
- Produces:
  ```python
  def get_latency_series(self, window: str = "24h", bucket_ms: int = 3600000,
                         top_n: int = 5,
                         percentiles: tuple[float, ...] = (0.50, 0.95),
                         source_of: dict | None = None) -> dict:
      # 返回 { window, bucket_ms, buckets: [str..], percentiles: ["p50","p95"],
      #        series: { model: { source, requests,
      #                           ttft_p50:[..], ttft_p95:[..], ttft_n:[..],
      #                           tpot_p50:[..], tpot_p95:[..], tpot_n:[..] } },
      #        total_models: int }
      # buckets 与 series[*][...] 所有数组等长；桶内无样本 → 该位置 None。
      # series 键序 = 窗口内请求数降序（前 top_n 个）。
  ```

- [ ] **Step 1: 写失败测试** `tests/unit/infra/test_metrics_series.py`

```python
import sys, time
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.metrics_aggregator import MetricsAggregator


def _mk():
    return MetricsAggregator(model_name_map={"qwen38": "Qwen38-27B-TXT"},
                             price_config={})


def _sample(model, ts, ttft, dur, out, status=200):
    return {"model": model, "timestamp": ts, "ttft_ms": ttft,
            "duration_ms": dur, "tokens_out": out, "tokens_in": 1,
            "status": status, "error": ""}


def test_series_buckets_and_p50():
    agg = _mk()
    t0 = time.time() - 7200          # 2h 内
    # 两个 1h 桶各塞样本：桶0 ttft=[100,200] → p50≈150；桶1 ttft=[300,400] → p50≈350
    agg._samples.append(_sample("qwen38", t0 + 1800, 100.0, 500.0, 10))   # 桶0
    agg._samples.append(_sample("qwen38", t0 + 1800, 200.0, 600.0, 12))   # 桶0
    agg._samples.append(_sample("qwen38", t0 + 5400, 300.0, 700.0, 8))    # 桶1
    agg._samples.append(_sample("qwen38", t0 + 5400, 400.0, 800.0, 9))    # 桶1
    r = agg.get_latency_series("24h", bucket_ms=3600000, top_n=5)
    assert r["bucket_ms"] == 3600000
    n_buckets = len(r["buckets"])
    s = r["series"]["Qwen38-27B-TXT"]
    assert len(s["ttft_p50"]) == n_buckets
    # 找非 None 的两个桶，验证 p50 计算
    vals = [v for v in s["ttft_p50"] if v is not None]
    assert len(vals) == 2
    assert min(vals) == 150.0 and max(vals) == 350.0
    assert s["source"] == "observed"    # 未给 source_of → 默认 observed


def test_series_tpot_and_exclusions():
    agg = _mk()
    t0 = time.time() - 3600
    # 成功流式：ttft=100 dur=400 out=101 → tpot=(400-100)/100=3.0
    agg._samples.append(_sample("m1", t0, 100.0, 400.0, 101))
    # 失败样本不进 ttft/tpot
    agg._samples.append(_sample("m1", t0, 50.0, 60.0, 5, status=500))
    r = agg.get_latency_series("1h", bucket_ms=3600000, top_n=5)
    s = r["series"]["m1"]
    tp = [v for v in s["tpot_p50"] if v is not None]
    assert tp == [3.0]
    # 失败样本被排除 → ttft_n 里对应桶只有 1 个成功样本
    tn = [v for v in s["ttft_n"] if v]
    assert sum(s["ttft_n"]) == 1


def test_series_top_n_and_order():
    agg = _mk()
    t0 = time.time() - 3600
    for i in range(5):
        agg._samples.append(_sample("hot", t0, 100.0, 400.0, 10))
    for i in range(3):
        agg._samples.append(_sample("mid", t0, 100.0, 400.0, 10))
    for i in range(1):
        agg._samples.append(_sample("cold", t0, 100.0, 400.0, 10))
    r = agg.get_latency_series("1h", bucket_ms=3600000, top_n=2)
    keys = list(r["series"].keys())
    assert keys == ["hot", "mid"]      # 请求数降序，前 2
    assert r["total_models"] == 2
    assert r["series"]["hot"]["requests"] == 5


def test_series_empty_window():
    agg = _mk()
    r = agg.get_latency_series("1h", bucket_ms=3600000, top_n=5)
    assert r["series"] == {}
    assert r["total_models"] == 0
    assert len(r["buckets"]) >= 1   # buckets 始终是完整时间轴（全 None 值）


def test_series_source_of():
    agg = _mk()
    t0 = time.time() - 3600
    agg._samples.append(_sample("m1", t0, 100.0, 400.0, 10))
    r = agg.get_latency_series("1h", bucket_ms=3600000, top_n=5,
                               source_of={"m1": "local"})
    assert r["series"]["m1"]["source"] == "local"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/infra/test_metrics_series.py -v --tb=short`
Expected: FAIL（`get_latency_series` 未定义）。

- [ ] **Step 3: 实现 `get_latency_series`**（`metrics_aggregator.py` 顶部 import 区加 `import math`、`import datetime`；方法加在 `get_metrics` 之后）

```python
    def get_latency_series(self, window: str = "24h", bucket_ms: int = 3600000,
                          top_n: int = 5,
                          percentiles: tuple[float, ...] = (0.50, 0.95),
                          source_of: dict | None = None) -> dict:
        """时间分桶 × 逐模型 TTFT/TPOT 分位序列（模型延迟趋势图数据源）。

        - 桶起点 = 窗口起点向下取整到桶边界（墙钟对齐），保证不同客户端/刷新
          拿到同一套桶；桶内无样本 → 该位置 None（前端 connectNulls:false 断线）。
        - 仅保留窗口内请求数前 top_n 个模型（键序 = 请求数降序 → 名称升序）。
        - 零 schema 变更：TPOT 逐请求推导（同 get_metrics 过滤条件）。
        - source_of: {模型友好名: "local"/"cloud"}，缺省/未命中 → "observed"。
        """
        import math
        import datetime

        now = time.time()
        window_s = {"1h": 3600, "24h": 86400, "7d": 604800, "all": 86400}
        ws = window_s.get(window, 86400)
        cutoff = now - ws
        bucket_s = max(1.0, bucket_ms / 1000.0)
        start_bucket = math.floor(cutoff / bucket_s) * bucket_s
        n_buckets = int((now - start_bucket) / bucket_s) + 1

        with self._lock:
            samples = [s for s in self._samples if s.get("timestamp", 0) >= cutoff]

        friendly_of = (lambda raw: self._name_map.get(raw, raw))
        req_count: "defaultdict" = defaultdict(int)
        for s in samples:
            req_count[friendly_of(s["model"])] += 1
        top_models = [m for m, _ in sorted(req_count.items(),
                                           key=lambda kv: (-kv[1], kv[0]))[:top_n]]
        top_set = set(top_models)

        cells: "defaultdict" = defaultdict(lambda: {"ttft": [], "tpot": []})

        def _tpot_of(s):
            if (s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0
                    and s.get("duration_ms") and s["duration_ms"] > s["ttft_ms"]
                    and (s.get("tokens_out") or 0) >= 2):
                return (s["duration_ms"] - s["ttft_ms"]) / ((s.get("tokens_out") or 0) - 1)
            return None

        for s in samples:
            m = friendly_of(s["model"])
            if m not in top_set:
                continue
            ts = s.get("timestamp", 0)
            bi = int((ts - start_bucket) / bucket_s)
            if bi < 0 or bi >= n_buckets:
                continue
            cell = cells[(m, bi)]
            if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0:
                cell["ttft"].append(s["ttft_ms"])
            tp = _tpot_of(s)
            if tp is not None:
                cell["tpot"].append(tp)

        q_labels = ["p%.0f" % int(q * 100) for q in percentiles]
        series = {}
        for m in top_models:
            entry: dict = {"source": (source_of or {}).get(m, "observed"),
                           "requests": req_count[m]}
            for kind in ("ttft", "tpot"):
                for q, ql in zip(percentiles, q_labels):
                    rnd = 2 if kind == "tpot" else 1
                    arr = []
                    for bi in range(n_buckets):
                        vals = cells.get((m, bi), {}).get(kind, [])
                        arr.append(round(quantile(vals, q), rnd) if vals else None)
                    entry["%s_%s" % (kind, ql)] = arr
                entry["%s_n" % kind] = [
                    len(cells.get((m, bi), {}).get(kind, [])) for bi in range(n_buckets)]
            series[m] = entry

        labels = []
        for bi in range(n_buckets):
            t = start_bucket + bi * bucket_s
            fmt = "%m-%d %H:%M" if ws >= 86400 else "%H:%M"
            labels.append(datetime.datetime.fromtimestamp(t).strftime(fmt))

        return {
            "window": window,
            "bucket_ms": bucket_ms,
            "buckets": labels,
            "percentiles": q_labels,
            "series": series,
            "total_models": len(top_models),
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/infra/test_metrics_series.py -v --tb=short`
Expected: PASS（5 用例）。

- [ ] **Step 5: 提交**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/metrics_aggregator.py tests/unit/infra/test_metrics_series.py
git commit -m "feat(metrics/v5.4): get_latency_series 时间分桶×逐模型 TTFT/TPOT 分位（零 schema 变更）"
```

---

### Task 2: 后端接线 `/api/latency` + 缓存 + telemetry 透传

**Files:**
- Modify: `inferfabric/proxy/handler.py`（模块级 `_LAT_BUCKET_MS`/`_LAT_CACHE_TTL`/`_lat_series_cache`；方法 `_handle_api_latency`；`_GET_ROUTES` 加 `/api/latency`）
- Modify: `inferfabric/telemetry.py`（`TelemetryHub.get_latency_series` 透传）
- Test: `tests/unit/proxy/test_latency_endpoint.py`（新建）

**Interfaces:**
- Consumes: `pm.metrics.get_latency_series(...)`（Task 1）、`_metrics_axis(pm)`、`_ExpensiveCache`、`_send_json`。
- Produces: `GET /api/latency?window=1h|24h|7d` → Task 1 的 dict（非法 window 归 24h；异常 500 `{"error": "latency series unavailable"}`）。

- [ ] **Step 1: 写失败测试** `tests/unit/proxy/test_latency_endpoint.py`

```python
import sys, json
from pathlib import Path
from types import SimpleNamespace
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))
import inferfabric.proxy.handler as handler_module
from inferfabric.proxy.handler import ProxyHandler


def _fake_series(window, bucket_ms=3600000, top_n=5, percentiles=(0.5, 0.95),
                 source_of=None):
    return {"window": window, "bucket_ms": bucket_ms, "buckets": ["a", "b"],
            "percentiles": ["p50", "p95"],
            "series": {"m1": {"source": "local", "requests": 3,
                               "ttft_p50": [1.0, 2.0], "ttft_p95": [1.0, 2.0],
                               "ttft_n": [2, 2], "tpot_p50": [3.0, 4.0],
                               "tpot_p95": [3.0, 4.0], "tpot_n": [2, 2]}},
            "total_models": 1}


def _mk():
    pm = SimpleNamespace()
    pm.metrics = SimpleNamespace(get_latency_series=_fake_series)
    pm.mgr = SimpleNamespace(_models={"m1": SimpleNamespace(name="m1")})
    pm.cloud = SimpleNamespace(cloud_models={})
    calls = []
    pm.metrics.get_latency_series = lambda *a, **k: (calls.append((a, k)), _fake_series(*a, **k))[1]
    pm._calls = calls
    return pm


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_latency_default_24h():
    pm = _mk()
    h = _handler("/api/latency")
    h._handle_api_latency(pm)
    code, data = h._sent[-1]
    assert code == 200
    assert data["window"] == "24h"
    assert data["series"]["m1"]["source"] == "local"   # 来自 _metrics_axis


def test_latency_bad_window_falls_back_24h():
    pm = _mk()
    h = _handler("/api/latency?window=bogus")
    h._handle_api_latency(pm)
    assert h._sent[-1][1]["window"] == "24h"


def test_latency_window_passthrough():
    pm = _mk()
    h = _handler("/api/latency?window=7d")
    h._handle_api_latency(pm)
    assert h._sent[-1][1]["window"] == "7d"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/proxy/test_latency_endpoint.py -v --tb=short`
Expected: FAIL（`_handle_api_latency` 未定义）。

- [ ] **Step 3: 实现**（`handler.py` 模块级，放在 `_metrics_axis` 之后）

```python
_LAT_BUCKET_MS = {"1h": 5 * 60 * 1000, "24h": 3600 * 1000, "7d": 6 * 3600 * 1000}
_LAT_CACHE_TTL = {"1h": 30.0, "24h": 60.0, "7d": 60.0}
# 单飞 TTL 缓存：重扫描（deque + 分位）不随 3s 轮询重算；按 window 各一个实例。
_lat_series_cache = {w: _ExpensiveCache(ttl=_LAT_CACHE_TTL[w]) for w in _LAT_CACHE_TTL}
```

`ProxyHandler` 加方法（放在 `_handle_api_metrics` 之后）：

```python
    def _handle_api_latency(self, pm):
        """GET /api/latency?window=1h|24h|7d — 模型延迟趋势（时间分桶 × 逐模型分位）。"""
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query)
            window = qs.get("window", ["24h"])[0]
            if window not in ("1h", "24h", "7d"):
                window = "24h"
            source_of = {name: src for name, src in _metrics_axis(pm)}
            data = _lat_series_cache[window].get_or_refresh(
                lambda: pm.metrics.get_latency_series(
                    window, bucket_ms=_LAT_BUCKET_MS[window], top_n=5,
                    percentiles=(0.50, 0.95), source_of=source_of))
            self._send_json(data, 200)
        except Exception as e:
            log.error("/api/latency failed: %s", e)
            self._send_json({"error": "latency series unavailable"}, 500)
```

`_GET_ROUTES` 加一行（`/api/metrics` 附近）：

```python
        "/api/latency":           lambda h, pm: h._handle_api_latency(pm),
```

`telemetry.py` `TelemetryHub` 加透传（`get_metrics` 附近）：

```python
    def get_latency_series(self, window: str = "24h", bucket_ms: int = 3600000,
                           top_n: int = 5,
                           percentiles: tuple = (0.50, 0.95),
                           source_of: dict | None = None):
        return self.metrics.get_latency_series(
            window, bucket_ms=bucket_ms, top_n=top_n,
            percentiles=percentiles, source_of=source_of)
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/proxy/test_latency_endpoint.py tests/unit/proxy/test_snapshot_etag.py -v --tb=short`
Expected: PASS（新端点 3 + snapshot 14 全过）。

- [ ] **Step 5: 提交**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/proxy/handler.py inferfabric/telemetry.py tests/unit/proxy/test_latency_endpoint.py
git commit -m "feat(proxy/v5.4): GET /api/latency 只读端点（单飞 TTL 缓存 + 配置驱动 source_of）"
```

---

### Task 3: 前端趋势卡（时间轴 × 逐模型分色）+ chip + P50/P95

**Files:**
- Modify: `inferfabric/dashboard/fragments/monitor.html`（Row 3 控制条 + 双卡去 seg/表格）
- Modify: `inferfabric/dashboard/js/monitor.js`（趋势渲染 + chip + 委托）
- Modify: `inferfabric/dashboard/fragments/style.css`（chip 样式）
- Modify: `tests/unit/dashboard/test_dashboard_v2.py`（id 断言更新 + 只读断言 + 趋势结构测试）

**Interfaces:**
- Consumes: `GET /api/latency?window=`（Task 2）、`IFCharts.create/update`、`store`/`isMonitorActive`、`shortName`/`escHtml`。
- Produces（DOM 契约）：`#monLatChips`（chip 容器）、`data-seg="latwin"`（窗口，单条不再镜像）、`data-seg="latq"`（P50 / P50+P95）、`#monTtftChart`/`#monTpotChart`（保留）、`#monTtftSub`/`#monTpotSub`（保留）。移除 `#monTtftTable`/`#monTpotTable` 与 `data-seg="lattf"`/`"latpt"`。

- [ ] **Step 1: 定义 5 色模型调色板并校验**（`monitor.js` 顶部常量）

```js
  /* v5.4: 模型折线 5 色分类调色板（CVD 安全，固定顺序、绝不循环；主题各自校验通过）。
   * 同屏上限 = 本数组长度（5，CVD 驱动）；改动须重跑 dataviz validate_palette.js 保持 ALL PASS。
   * 模型 1..5 按请求数降序分色，与 chip 同序 → 同色；dark/light 各自一组（house 惯例）。 */
  var _MODEL_COLORS = {
    dark:  ['#3a86e0', '#b57a14', '#12a594', '#8b5cf6', '#e0574a'],
    light: ['#1f6fd6', '#b45309', '#0e8f7f', '#7c3aed', '#cf4636'],
  };
```

> 校验（Task 4 执行，**已在本会话预跑通过**）：dark `#3a86e0,#b57a14,#12a594,#8b5cf6,#e0574a`（surface #161c24）与 light `#1f6fd6,#b45309,#0e8f7f,#7c3aed,#cf4636`（surface #ffffff）均 `ALL CHECKS PASS`（CVD ΔE≥12、normal-vision≥19、对比度 3:1）。同屏 6 色会触发绿↔红 deutan ΔE≈5.2（低于合法下限），故上限定 5。

- [ ] **Step 2: 改 `monitor.html` Row 3**（替换现 107–160 行的双卡块）

在 Row 3 之前插入控制条（col-12），双卡去掉卡片内 seg 与表格：

```html
  <!-- Row 3: 模型延迟趋势 — 时间轴 × 逐模型分色折线（v5.4 取代模型条形双卡）。
       两张小倍数卡（左 TTFT / 右 TPOT）各单 y 轴（量纲差 ~2 数量级，禁双 y 轴）。
       共享控制条：窗口 1h/24h/7d（latwin）+ 分位 P50/P50+P95（latq）+ 模型 chip 选择器。 -->
  <div class="if-grid">
    <div class="col-12">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">模型延迟趋势</span>
          <span class="if-card-sub" id="monLatSub">时间分桶 · 桶内 P50</span>
          <div class="mon-seg" data-seg="latwin">
            <button type="button" class="mon-seg-btn" data-win="1h">1h</button>
            <button type="button" class="mon-seg-btn active" data-win="24h">24h</button>
            <button type="button" class="mon-seg-btn" data-win="7d">7d</button>
          </div>
          <div class="mon-seg" data-seg="latq">
            <button type="button" class="mon-seg-btn active" data-q="p50">P50</button>
            <button type="button" class="mon-seg-btn" data-q="p50p95">P50+P95</button>
          </div>
        </div>
        <div class="if-card-body">
          <div class="mon-lat-chips" id="monLatChips"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="if-grid">
    <div class="col-6">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">TTFT 趋势</span>
          <span class="if-card-sub" id="monTtftSub">ms · 24h · 每 1h</span>
        </div>
        <div class="if-card-body">
          <div class="mon-chart-wrap">
            <div id="monTtftChart" class="mon-chart"></div>
            <div id="monTtftEmpty" class="mon-chart-empty" style="display:none">暂无 TTFT 数据（窗口内无成功请求）</div>
          </div>
        </div>
      </div>
    </div>
    <div class="col-6">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">TPOT 趋势</span>
          <span class="if-card-sub" id="monTpotSub">ms/token · 24h · 每 1h</span>
        </div>
        <div class="if-card-body">
          <div class="mon-chart-wrap">
            <div id="monTpotChart" class="mon-chart"></div>
            <div id="monTpotEmpty" class="mon-chart-empty" style="display:none">暂无 TPOT 数据（需流式请求）</div>
          </div>
        </div>
      </div>
    </div>
  </div>
```

- [ ] **Step 3: 改 `monitor.js`** —— 删旧（`buildLatAxis`/`srcBadge`/`_latTooltip`/`renderLatTable`/旧 `renderLatCard`/旧 `getLatData`/`_latCtx`/`_latView`），换成趋势版。保留 `_charts.ttft/tpot` + `ensureCharts`。新增/替换：

```js
  /* ── 3. 模型延迟趋势：时间轴 × 逐模型分色折线（v5.4 取代模型条形双卡）── */
  var _latWin = '24h';
  var _latQ = 'p50';               // 'p50' | 'p50p95'
  var _latSel = null;              // 选中模型名数组（null=默认取前 4）
  var _latCache = {};              // window -> { data, at }
  var _latFetchInflight = {};
  var _LAT_TTL = { '1h': 30000, '24h': 60000, '7d': 60000 };   // 与后端 _LAT_CACHE_TTL 逐档对齐

  function _modelColors() {
    /* currentTheme 在 IFCharts 命名空间（charts.js 导出）；monitor.js 是 strict IIFE，
       裸调用会 ReferenceError。守卫模式同 L875 onThemeChange 用法。 */
    var th = (IFCharts && typeof IFCharts.currentTheme === 'function')
      ? IFCharts.currentTheme() : 'dark';
    return _MODEL_COLORS[th] || _MODEL_COLORS.dark;
  }

  function getLatSeries(win) {
    var c = _latCache[win];
    var now = Date.now();
    if (c && now - c.at < (_LAT_TTL[win] || 60000)) return c.data;
    if (!_latFetchInflight[win]) {
      _latFetchInflight[win] = true;
      fetch('/api/latency?window=' + win, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _latCache[win] = { data: data || {}, at: Date.now() };
          if (isMonitorActive()) renderLatencyCards();
        })
        .catch(function (e) { console.warn('[monitor] /api/latency fetch failed:', e); })
        .then(function () { _latFetchInflight[win] = false; });
    }
    return c ? c.data : {};
  }

  function _latAvailable(seriesObj) { return Object.keys(seriesObj || {}); }  // 请求数降序

  // chip 列表 = 请求数前 5（上限 = 调色板长度，CVD 驱动）；默认选中前 4
  function _latChipModels(seriesObj) {
    return _latAvailable(seriesObj).slice(0, _modelColors().length);
  }

  function _latSelected(seriesObj) {
    var chips = _latChipModels(seriesObj);
    if (!_latSel) _latSel = chips.slice(0, Math.min(4, chips.length));
    var sel = chips.filter(function (n) { return _latSel.indexOf(n) >= 0; });
    return sel.slice(0, _modelColors().length);   // 安全网：不会超调色板长度
  }

  function _latColorOf(name, seriesObj) {
    var i = _latAvailable(seriesObj).indexOf(name);
    return i >= 0 ? _modelColors()[i] : '#888';   // 按 rank 定色：chip 与图同色，绝不循环
  }

  function _latBucketLabel(win) { return win === '1h' ? '5min' : (win === '7d' ? '6h' : '1h'); }

  function _lowPt(v, n) {   // 低置信（0<n<30）半透明点；n=0/无值 → null（断线）
    if (v == null) return null;
    var low = n > 0 && n < 30;
    return low ? { value: v, itemStyle: { opacity: 0.45 } } : v;
  }

  function renderLatChips(seriesObj) {
    var el = $('monLatChips'); if (!el) return;
    var chips = _latChipModels(seriesObj);
    if (!chips.length) { el.innerHTML = '<span class="muted" style="font-size:11px">暂无模型</span>'; return; }
    var sel = _latSelected(seriesObj);
    var html = '';
    chips.forEach(function (name) {
      var on = sel.indexOf(name) >= 0;
      html += '<button type="button" class="mon-lat-chip' + (on ? ' on' : '') +
        '" data-model="' + escHtml(name) + '" style="--mc:' + _latColorOf(name, seriesObj) + '">' +
        '<span class="dot"></span>' + escHtml(shortName(name)) + '</button>';
    });
    el.innerHTML = html;
  }

  function _latTrendTooltip(prefix) {
    return function (params) {
      if (!params || !params.length) return '';
      var bucket = params[0].axisValue;
      var rows = '';
      params.forEach(function (p) {
        if (p.value == null || (typeof p.value === 'object' && p.value == null)) return;
        var v = (p.value && typeof p.value === 'object') ? p.value.value : p.value;
        rows += '<div>' + escHtml(p.seriesName) + '：' + v +
          (prefix === 'ttft' ? ' ms' : ' ms/token') + '</div>';
      });
      var unit = prefix === 'ttft' ? ' ms' : ' ms/token';
      return '<div><b>' + bucket + '</b></div>' + rows;
    };
  }

  function renderLatCard(metric) {
    var isTtft = metric === 'ttft';
    var prefix = isTtft ? 'ttft' : 'tpot';
    var unit = isTtft ? 'ms' : 'ms/token';
    if (!_charts[metric]) return;
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    var buckets = (data && data.buckets) || [];
    var sel = _latSelected(seriesObj);

    var series = [];
    var legendData = [];
    sel.forEach(function (name) {
      var s = seriesObj[name] || {};
      var color = _latColorOf(name, seriesObj);   // rank→色，与 chip 一致
      var n = s[prefix + '_n'] || [];
      var p50 = (s[prefix + '_p50'] || []).map(function (v, bi) { return _lowPt(v, n[bi]); });
      series.push({
        name: name, type: 'line', data: p50, connectNulls: false,
        lineStyle: { color: color, width: 2 },
        itemStyle: { color: color },
      });
      legendData.push(name);
      if (_latQ === 'p50p95') {
        var p95 = (s[prefix + '_p95'] || []).map(function (v, bi) { return _lowPt(v, n[bi]); });
        series.push({
          name: name + ' P95', type: 'line', data: p95, connectNulls: false,
          lineStyle: { color: color, width: 1, type: 'dashed' },
          itemStyle: { color: color },
        });
      }
    });

    var hasData = sel.length > 0 && buckets.length > 0;
    showEmpty(isTtft ? 'monTtftEmpty' : 'monTpotEmpty', !hasData);
    IFCharts.update(_charts[metric], {
      xAxis: { type: 'category', data: buckets, boundaryGap: false,
               axisLabel: { fontSize: 11, interval: 'auto' } },
      yAxis: { type: 'value', axisLabel: { formatter: '{value} ' + unit } },
      legend: { show: series.length >= 1, data: legendData, textStyle: { fontSize: 11 } },
      tooltip: { trigger: 'axis', formatter: _latTrendTooltip(prefix) },
      series: series,
    });
  }

  function renderLatencyCards() {
    ensureCharts();
    var data = getLatSeries(_latWin);
    var seriesObj = (data && data.series) || {};
    renderLatChips(seriesObj);
    var bl = _latBucketLabel(_latWin);
    var sub1 = $('monTtftSub'), sub2 = $('monTpotSub'), sub0 = $('monLatSub');
    if (sub1) sub1.textContent = 'ms · ' + _latWin + ' · 每 ' + bl;
    if (sub2) sub2.textContent = 'ms/token · ' + _latWin + ' · 每 ' + bl;
    if (sub0) sub0.textContent = '时间分桶 · 桶内 ' + (_latQ === 'p50p95' ? 'P50/P95' : 'P50');
    renderLatCard('ttft');
    renderLatCard('tpot');
  }
```

事件委托（monitor tab click handler）：删 `latwin` 的 ×2 镜像同步 + 删 `lattf`/`latpt` 分支；改 `latwin`（单条）+ 加 `latq` + 加 chip：

```js
      } else if (segType === 'latwin') {
        var latWin = btn.getAttribute('data-win');
        if (latWin && latWin !== _latWin) { _latWin = latWin; _latSel = null; renderLatencyCards(); }
      } else if (segType === 'latq') {
        var q = btn.getAttribute('data-q');
        if (q && q !== _latQ) { _latQ = q; renderLatencyCards(); }
      }
    // chip（不在 .mon-seg 内）
    var chip = ev.target.closest('.mon-lat-chip');
    if (chip && tabEl.contains(chip)) {
      var m = chip.getAttribute('data-model');
      var latData = getLatSeries(_latWin) || {};
      if (!_latSel) _latSel = _latSelected((latData.series || {}));
      var i = _latSel.indexOf(m);
      if (i >= 0) _latSel.splice(i, 1); else _latSel.push(m);
      // 无超限分支：chip 已封顶 top 5，选中 ≤ chip 数 = 调色板长度
      renderLatencyCards();
      return;
    }
```

> 注：chip 固定为请求数前 5（上限 = 调色板长度，CVD 驱动），颜色按 rank 定（chip 与图同色、绝不循环用色）；默认选中前 4，在 5 个内切换 → 超限不可达，无需裁尾。切窗口后 `_latSel=null` 重新默认前 4（按新窗口请求数序）。chip 的 `data-model` 用完整名，展示用 `shortName`。

- [ ] **Step 4: 改 `style.css`**（追加）

```css
/* v5.4 模型延迟趋势：模型 chip 选择器 */
.mon-lat-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.mon-lat-chip{--mc:#888;display:inline-flex;align-items:center;gap:6px;cursor:pointer;
  border:1px solid var(--border);background:var(--card);color:var(--text);
  border-radius:999px;padding:3px 10px;font-size:12px;line-height:1}
.mon-lat-chip .dot{width:8px;height:8px;border-radius:50%;background:var(--mc);
  opacity:.35;transition:opacity .15s}
.mon-lat-chip.on{border-color:var(--mc)}
.mon-lat-chip.on .dot{opacity:1}
```

- [ ] **Step 5: 改 `test_dashboard_v2.py`** —— 更新 id 断言（移除 `monTtftTable`/`monTpotTable`/`lattf`/`latpt`；加 `monLatChips`/`latq`/`monLatSub`）；`test_monitor_readonly` 保持（新 chip/seg 均 `type="button"`，无 `POST`/`<form`/`method=`）；新增 `test_monitor_latency_trend`：断言 `#monLatChips` 存在、`data-seg="latq"` 存在、`#monTtftChart`/`#monTpotChart` 仍在、无 `<form`。

- [ ] **Step 6: 跑前端契约测试**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/dashboard/test_dashboard_v2.py -v --tb=short`
Expected: PASS（含只读断言 + 趋势结构断言）。

- [ ] **Step 7: 提交**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/dashboard/fragments/monitor.html inferfabric/dashboard/js/monitor.js inferfabric/dashboard/fragments/style.css tests/unit/dashboard/test_dashboard_v2.py
git commit -m "feat(dashboard/v5.4): 延迟分布改时间轴×逐模型分色趋势 + 模型 chip + P50/P95"
```

---

### Task 4: 质量门（测试 + 调色板校验 + smoke）

**Files:** 只跑门，无代码改动（若调色板校验失败则回到 Task 3 调 `_MODEL_COLORS`）。

- [ ] **Step 1: 全量 pytest**

Run: `cd /home/vince/inferfabric/sandbox && python3 -m pytest tests/unit/ tests/integration/ -q --tb=short`
Expected: 全绿（上一版 1013 + 本 plan 新增 ≈ +9，0 failed）。

- [ ] **Step 2: import smoke**

Run: `cd /home/vince/inferfabric/sandbox && python3 -c "import inferfabric; print('import OK')"`
Expected: `import OK`。

- [ ] **Step 3: CVD 调色板校验**

Run: `node <dataviz-skill-base>/scripts/validate_palette.js` 两组（主题各自校验，surface 用对应图表底色 #161c24 / #ffffff）：
- `--mode dark`：`#3a86e0,#b57a14,#12a594,#8b5cf6,#e0574a`
- `--mode light`：`#1f6fd6,#b45309,#0e8f7f,#7c3aed,#cf4636`
Expected: ALL PASS（CVD Delta E ≥8）。与 `monitor.js` 的 `_MODEL_COLORS` 逐值一致（改色必须同步改此步）。任一 FAIL → 调 `_MODEL_COLORS`（同屏上限 5，CVD 驱动）后重跑本步。

- [ ] **Step 4: 运行时 smoke（:8998 避让生产 8999）**

```bash
cd /home/vince/inferfabric/sandbox
EDGE_PROXY_PORT=8998 nohup python3 -m inferfabric.proxy.handler > /tmp/iff-smoke-lat.log 2>&1 &
sleep 8
curl -s --max-time 30 "http://127.0.0.1:8998/api/latency?window=24h" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('window',d['window'],'| buckets',len(d['buckets']),'| models',d.get('total_models'))
for n,m in list(d['series'].items())[:4]:
    p50=[v for v in m['ttft_p50'] if v is not None]
    print('  %-24s src=%-8s ttft_p50_nonnull=%d sample=%s' % (n,m['source'],len(p50),p50[:3]))
"
curl -s --max-time 20 http://127.0.0.1:8998/ | grep -o 'monLatChips\|data-seg=\"latq\"\|monTtftChart\|monTpotChart' | sort | uniq -c
curl -s --max-time 30 -o /dev/null -w "snapshot HTTP %{http_code}\n" http://127.0.0.1:8998/api/snapshot
fuser -k 8998/tcp 2>/dev/null; sleep 1; echo "smoke stopped"
```
Expected: `/api/latency` 返回 buckets + 前 4 模型 ttft_p50 非空；HTML 含 `monLatChips`/`latq`/双卡 id；snapshot 200。

- [ ] **Step 5: 视觉验收（可选，playwright）**

`http://127.0.0.1:8998/` → Monitor tab → 截图两张趋势卡 + chip 条，肉眼核对：x 轴=时间、每模型一条不同色线、图例=模型名+色块、P95 虚线（开 P50+P95 时）、低置信点半透明、切 chip/窗口/分位/1h·24h·7d 正常。

---

### Task 5: 生产合并 + 服务重启

**Files:** 显式 pathspec 10 文件（含 Task 3 fix-round 的 `charts.js` replaceSeries 整替）：`inferfabric/metrics_aggregator.py`、`inferfabric/proxy/handler.py`、`inferfabric/telemetry.py`、`inferfabric/dashboard/fragments/monitor.html`、`inferfabric/dashboard/js/monitor.js`、`inferfabric/dashboard/fragments/style.css`、`tests/unit/infra/test_metrics_series.py`（新）、`tests/unit/proxy/test_latency_endpoint.py`（新）、`tests/unit/dashboard/test_dashboard_v2.py`。

- [ ] **Step 1: 沙盒 → 生产 cp（显式清单，不 bulk）**

```bash
cd /home/vince/inferfabric
for f in inferfabric/metrics_aggregator.py inferfabric/proxy/handler.py inferfabric/telemetry.py \
         inferfabric/dashboard/fragments/monitor.html inferfabric/dashboard/js/monitor.js \
         inferfabric/dashboard/js/charts.js \
         inferfabric/dashboard/fragments/style.css \
         tests/unit/infra/test_metrics_series.py tests/unit/proxy/test_latency_endpoint.py \
         tests/unit/dashboard/test_dashboard_v2.py; do cp "sandbox/$f" "$f" || exit 1; done
```

- [ ] **Step 2: 生产质量门**（pytest 全绿 + import smoke，从生产根）

- [ ] **Step 3: 生产 main commit（显式 pathspec，含 `Co-Authored-By`，不 push）**

```bash
git add <上述 10 文件>
git commit -m "feat(metrics/v5.4): 延迟分布改模型延迟趋势——时间轴×逐模型分色 + /api/latency

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 4: 回同步沙盒（字节级，保持零漂移）**

- [ ] **Step 5: 重启生产服务 + 校验 :8999**

```bash
systemctl --user restart inferfabric-proxy.service
sleep 6; systemctl --user is-active inferfabric-proxy.service
curl -s "http://127.0.0.1:8999/api/latency?window=24h" | python3 -c "import json,sys;d=json.load(sys.stdin);print('models',d.get('total_models'),'buckets',len(d['buckets']))"
curl -s http://127.0.0.1:8999/ | grep -o 'monLatChips' | head -1
```
Expected: 服务 active、`/api/latency` 有数据、HTML 含 `monLatChips`。

- [ ] **Step 6: push main + 收尾**（`git push origin main`；沙盒临时分支按 git-main-only-workflow 处理；删本 plan SDD 工作区）。
