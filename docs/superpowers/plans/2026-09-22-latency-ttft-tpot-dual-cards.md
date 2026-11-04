# Latency 双卡小倍数（TTFT + TPOT）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Monitor tab 的单卡"延迟 P50/P95 双线"（数据驱动 x 轴、line 图、仅 TTFT）改造为 **TTFT / TPOT 双卡小倍数**：两张卡共用同一模型 x 轴（配置驱动、稳定排序），每卡 P50/P95 成对条形 + 样本量 n + 低置信标记 + 表格视图 + 1h/24h/7d 窗口切换；后端 `get_metrics` 新增 TPOT 分位（从 request_log 现有列推导，零 schema 变更）。

**Architecture:** 后端两层：`MetricsAggregator.get_metrics()` 增加 `axis_models` 参数（配置驱动 x 轴 + `source` 标记 + 稳定排序 + TPOT 推导）；`proxy/handler.py` 新增 `_metrics_axis(pm)`（models.d 友好名 + 云预设模型）并接入 snapshot 与 `/api/metrics` 两处调用点。前端：monitor.html 第 3 行单卡 → 两个 col-6 卡（`monTtftChart`/`monTpotChart`），monitor.js 新增 `renderLatencyCards()`（24h 走 snapshot，1h/7d 走 GET `/api/metrics?window=`），复用 charts.js 已 CVD 验证的固定调色板（蓝→琥珀），不引入新颜色。

**Tech Stack:** Python 3.10（零新依赖）、ECharts（IFCharts 封装，现有 vendor）、pytest（unit + dashboard 契约测试）。

**Spec:** 本会话（2026-09-22）用户确认的方案：双卡小倍数（拒绝双 y 轴：两指标量纲差 ~2 个数量级，双轴比例任意、制造假相关）；TPOT 从现有列推导 `tpot_ms = (duration_ms − ttft_ms) / (tokens_out − 1)`；x 轴配置驱动（models.d 活跃 + 启用云预设），404 未知模型（claude-opus-4-7 等）不再漏成空槽而是显式 `source=observed` 灰显；n<30 低置信标记；表格视图；窗口切换。数据事实：24h 内 4 个模型有 TTFT（Qwen38-27B-TXT 3817 / glm-5.1 1482 / deepseek-v4-flash 625 / NI-Qwen38-27B-VL 7），claude-opus-4-7（222 全 404）与 claude-sonnet-4-6（172 全 404）是"未配置"的观测模型。

## Global Constraints

- **Sandbox-first**（steering/constitution.md）：所有代码改动在 `/home/vince/inferfabric/sandbox/`（独立 git 仓库，branch `dev`）完成 + pytest 通过，再按 Task 6 同步到生产根目录并 commit main。`sandbox/` 在 `.gitignore`。
- Python 3.10+；运行时依赖 vendored 于 `_deps/`（**禁止 pip install**）；vLLM 0.24 不动。
- **不修改 proxy 转发核心路径**（`proxy/forwarder.py` 转发主路径，PR-14 排除项）——本计划不触碰。
- **monitor.js 只读契约**（test_monitor_readonly）：js 与 fragment 中不得出现 `POST` 字样 / `<form` / `method=`；窗口切换是 GET-only display filter。
- **图表契约**（charts.js 头注释 spec §7 + test_monitor_chart_series_limit）：每图 series ≤4；单 y 轴（本计划因此用双卡而非双轴）；系列色固定取 charts.js `PALETTES` 顺序（蓝 #3b82f6 → 琥珀 #b45309），**不引入新颜色**（已 CVD 验证，无需重跑 validate_palette.js）；≥2 系列必有 legend（IFCharts._applyRules 自动处理）。
- `node --check monitor.js` 必须通过（pytest test_monitor_js_node_syntax 自动门）。
- dashboard 是启动时装配缓存（`inferfabric/dashboard/__init__.py`），**改动需重启 proxy 服务**（systemd user service `inferfabric-proxy.service`）才生效。
- 生产 commit 到本地 main（不推 feature 分支；是否 push 由用户决定），commit message 结尾：
  `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 质量门（CLAUDE.md）：全量 pytest（unit+integration）通过；`python3 -c "import inferfabric"` smoke；运行时 smoke（API 200）。

**涉及文件一览：**

| 文件 | 操作 |
|---|---|
| `inferfabric/metrics_aggregator.py` | 修改（Task 1） |
| `inferfabric/proxy/handler.py` + `inferfabric/telemetry.py` | 修改（Task 2） |
| `inferfabric/dashboard/fragments/monitor.html` | 修改（Task 4） |
| `inferfabric/dashboard/fragments/style.css` | 修改（Task 4） |
| `inferfabric/dashboard/js/monitor.js` | 修改（Task 4） |
| `tests/unit/infra/test_metrics_aggregator.py` | 新建（Task 1） |
| `tests/unit/proxy/test_metrics_axis.py` | 新建（Task 2） |
| `tests/unit/dashboard/test_dashboard_v2.py` | 修改（Task 3） |

---

### Task 0: 同步 sandbox 基线

**Files:**
- Modify: sandbox 中与生产不一致的被改文件（先 diff 后 cp）

**Interfaces:**
- Produces: sandbox 工作区与生产 HEAD（443e0d4）在 8 个目标文件上一致，后续任务在 sandbox 上安全迭代。

- [ ] **Step 1: diff 目标文件并同步**

```bash
cd /home/vince/inferfabric
for f in inferfabric/metrics_aggregator.py \
         inferfabric/proxy/handler.py \
         inferfabric/telemetry.py \
         inferfabric/dashboard/fragments/monitor.html \
         inferfabric/dashboard/fragments/style.css \
         inferfabric/dashboard/js/monitor.js \
         tests/unit/dashboard/test_dashboard_v2.py; do
  if ! diff -q "$f" "sandbox/$f" >/dev/null 2>&1; then
    echo "sync: $f"; cp "$f" "sandbox/$f"
  fi
done
```

- [ ] **Step 2: 记录基线 commit（仅同步文件，不动 sandbox 未跟踪杂物）**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/metrics_aggregator.py inferfabric/proxy/handler.py inferfabric/telemetry.py \
        inferfabric/dashboard/fragments/monitor.html inferfabric/dashboard/fragments/style.css \
        inferfabric/dashboard/js/monitor.js tests/unit/dashboard/test_dashboard_v2.py 2>/dev/null
git commit -m "chore: sync production baseline (pre latency dual-cards)" || echo "no changes to commit"
```

（`git add` 对无改动的文件是 no-op；若 Step 1 全部一致则跳过 commit。）

---

### Task 1: MetricsAggregator — TPOT 推导 + 配置驱动 x 轴

**Files:**
- Modify: `inferfabric/metrics_aggregator.py:146-215`（`get_metrics` 方法体）
- Test: `tests/unit/infra/test_metrics_aggregator.py`（新建）

**Interfaces:**
- Consumes: 现有 `quantile(data, q)`（`metrics_aggregator.py:27`）、`statistics.median`、`self._name_map`。
- Produces: `MetricsAggregator.get_metrics(window="24h", axis_models=None)`；`axis_models: list[tuple[str, str]] | None`（`(model_name, source)`，source ∈ `{"local","cloud"}`）。返回 dict 中 `models` 每个 entry 新增字段：`source`（`"local"|"cloud"|"observed"`）、`ttft_samples`（int，ttft 样本数，无则为 0）、`tpot_samples`（int）、`tpot_p50/tpot_p95/tpot_p99`（float，仅 tpot_samples>0 时存在）。`models` 键插入顺序 = **requests 降序 → 名称升序**（JSON 保持插入序，前端 `Object.keys` 即得稳定 x 轴）。

- [ ] **Step 1: 写失败测试** `sandbox/tests/unit/infra/test_metrics_aggregator.py`：

```python
"""unit/infra/test_metrics_aggregator.py — MetricsAggregator v5.4

覆盖：TPOT 推导（零 schema 变更，从 ttft_ms/duration_ms/tokens_out 计算）、
配置驱动 x 轴（axis_models 零请求占位 + source 标记 + 稳定排序）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.metrics_aggregator import MetricsAggregator


class _Entry:
    """RequestLog duck-type（record() 是 duck-typed）。"""
    def __init__(self, model, status=200, ttft=None, dur=0.0,
                 tin=0, tout=0, route="local", error=None, ts=None):
        self.model = model
        self.status = status
        self.ttft_ms = ttft
        self.duration_ms = dur
        self.tokens_in = tin
        self.tokens_out = tout
        self.route = route
        self.cloud_provider = None
        self.error = error
        self.timestamp = ts or time.time()


def _agg_with(entries):
    agg = MetricsAggregator()
    for e in entries:
        agg.record(e)
    return agg


def test_tpot_derivation():
    """tpot_ms = (duration_ms - ttft_ms) / (tokens_out - 1)，逐请求后取分位。"""
    agg = _agg_with([
        _Entry("m", ttft=1000.0, dur=5000.0, tout=5),    # (5000-1000)/4 = 1000
        _Entry("m", ttft=2000.0, dur=10000.0, tout=5),   # (10000-2000)/4 = 2000
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["tpot_p50"] == 1500.0
    assert m["tpot_p95"] == 1950.0      # quantile 线性插值：1000 + 0.95*1000
    assert m["tpot_p99"] == 1990.0
    assert m["tpot_samples"] == 2


def test_tpot_excludes_nonstream_short_and_failed():
    """非流式（duration<=ttft）、tokens_out<2、失败请求不进 TPOT 分位。"""
    agg = _agg_with([
        _Entry("m", ttft=5000.0, dur=4000.0, tout=5),    # duration < ttft → 排除
        _Entry("m", ttft=100.0, dur=200.0, tout=1),      # tokens_out<2 → 排除
        _Entry("m", status=500, ttft=100.0, dur=300.0, tout=3),  # 失败 → 排除
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert "tpot_p50" not in m
    assert m["tpot_samples"] == 0


def test_ttft_samples_counted():
    """ttft_samples = 有 ttft 的成功请求数；ttft 为 None/0 的不计。"""
    agg = _agg_with([
        _Entry("m", ttft=100.0, dur=500.0, tout=3),
        _Entry("m", ttft=None, dur=500.0, tout=3),
        _Entry("m", ttft=0.0, dur=500.0, tout=3),
        _Entry("m", status=502, ttft=100.0, dur=500.0),
    ])
    m = agg.get_metrics("24h")["models"]["m"]
    assert m["ttft_samples"] == 1


def test_axis_zero_entries_and_stable_order():
    """axis_models 中无请求的模型补零占位；键序 = requests 降序 → 名称升序。"""
    agg = _agg_with([
        _Entry("alpha", ttft=100.0, dur=500.0, tout=3),
        _Entry("alpha", ttft=200.0, dur=600.0, tout=3),
    ])
    res = agg.get_metrics("24h", axis_models=[("alpha", "local"), ("beta", "cloud")])
    assert list(res["models"].keys()) == ["alpha", "beta"]
    beta = res["models"]["beta"]
    assert beta["requests"] == 0 and beta["success"] == 0 and beta["fail"] == 0
    assert beta["source"] == "cloud"
    assert res["models"]["alpha"]["source"] == "local"


def test_observed_model_marked_observed():
    """窗口内出现但不在 axis 的模型 → source=observed（不丢、不冒充已配置）。"""
    agg = _agg_with([_Entry("ghost", ttft=100.0, dur=500.0, tout=3)])
    res = agg.get_metrics("24h", axis_models=[("alpha", "local")])
    assert res["models"]["ghost"]["source"] == "observed"
    # 无请求的 axis 模型也在
    assert res["models"]["alpha"]["requests"] == 0


def test_no_axis_keeps_all_sampled_models():
    """不传 axis_models 时行为向后兼容：models 只含窗口内出现过的模型，source=observed。"""
    agg = _agg_with([_Entry("solo", ttft=100.0, dur=500.0, tout=3)])
    res = agg.get_metrics("24h")
    assert list(res["models"].keys()) == ["solo"]
    assert res["models"]["solo"]["source"] == "observed"


def test_window_cutoff_unchanged():
    """1h 窗口过滤旧样本（回归守卫）。"""
    now = time.time()
    agg = _agg_with([
        _Entry("m", ttft=100.0, dur=500.0, tout=3, ts=now - 7200),   # 2h 前
        _Entry("m", ttft=200.0, dur=600.0, tout=3, ts=now - 60),     # 1min 前
    ])
    m1h = agg.get_metrics("1h")["models"]["m"]
    assert m1h["requests"] == 1
    assert m1h["ttft_p50"] == 200.0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/infra/test_metrics_aggregator.py -v --tb=short
```
Expected: FAIL（`get_metrics` 不接受 `axis_models` / 无 `tpot_*` 字段 / 无 `source`）。

- [ ] **Step 3: 实现** — 修改 `sandbox/inferfabric/metrics_aggregator.py` 的 `get_metrics`：

签名改为：
```python
def get_metrics(self, window: str = "24h",
                axis_models: list[tuple[str, str]] | None = None) -> dict:
```

`# 按 model 分组` 之后的循环整段替换为（`by_model` 分组保留不动，`result` 基础字段保留不动，cost/success_rate 收尾保留不动）：

```python
        # v5.4: 配置驱动 x 轴 — axis_models 补零占位 + source 标记 + 稳定排序
        friendly_of = (lambda raw: self._name_map.get(raw, raw))
        by_friendly = defaultdict(list)
        for raw, ss in by_model.items():
            by_friendly[friendly_of(raw)].extend(ss)
        source_of = {name: src for name, src in (axis_models or [])}
        for name in source_of:
            by_friendly.setdefault(name, [])
        all_names = sorted(by_friendly.keys(),
                           key=lambda n: (-len(by_friendly[n]), n))

        for model in all_names:
            msamples = by_friendly[model]
            ttfts = [s["ttft_ms"] for s in msamples if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0]
            durations = [s["duration_ms"] for s in msamples if s["status"] < 400 and s.get("duration_ms") and s["duration_ms"] > 0]
            tokens_in = sum(s.get("tokens_in", 0) for s in msamples)
            tokens_out = sum(s.get("tokens_out", 0) for s in msamples)

            # 费用估算
            price = self._price_config.get(model)
            cost = 0.0
            if price:
                cost = (tokens_in / 1_000_000) * price.price_input + \
                       (tokens_out / 1_000_000) * price.price_output

            m = {
                "requests": len(msamples),
                "success": sum(1 for s in msamples if s["status"] < 400 and not s["error"]),
                "fail": sum(1 for s in msamples if s["status"] >= 400 or s["error"]),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_yuan": round(cost, 4),
                "source": source_of.get(model, "observed"),
                "ttft_samples": len(ttfts),
            }

            if ttfts:
                m["ttft_p50"] = round(median(ttfts), 1)
                m["ttft_p95"] = round(quantile(ttfts, 0.95), 1)
                m["ttft_p99"] = round(quantile(ttfts, 0.99), 1)
            if durations:
                m["duration_p50"] = round(median(durations), 1)
                m["duration_p95"] = round(quantile(durations, 0.95), 1)
                m["duration_p99"] = round(quantile(durations, 0.99), 1)

            # v5.4: TPOT 逐请求推导 — 仅成功流式样本（tokens_out>=2 且 duration>ttft），
            # 零 schema 变更；非流式/短响应/失败样本不进分位
            tpots = [
                (s["duration_ms"] - s["ttft_ms"]) / ((s.get("tokens_out") or 0) - 1)
                for s in msamples
                if s["status"] < 400
                and s.get("ttft_ms") and s["ttft_ms"] > 0
                and s.get("duration_ms") and s["duration_ms"] > s["ttft_ms"]
                and (s.get("tokens_out") or 0) >= 2
            ]
            m["tpot_samples"] = len(tpots)
            if tpots:
                m["tpot_p50"] = round(median(tpots), 2)
                m["tpot_p95"] = round(quantile(tpots, 0.95), 2)
                m["tpot_p99"] = round(quantile(tpots, 0.99), 2)

            result["models"][model] = m
            result["cost_yuan"] += cost
```

注意：原代码 `m` 的 `result["models"][friendly] = m` 中 `friendly` 变量被 `by_friendly` 归并吸收（键直接用 `model`，它已是友好名）。`quantile`/`median`/`defaultdict` 已在文件内可用，无需新 import。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/infra/test_metrics_aggregator.py -v --tb=short
```
Expected: 7 passed。

- [ ] **Step 5: Commit（sandbox dev）**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/metrics_aggregator.py tests/unit/infra/test_metrics_aggregator.py
git commit -m "feat(metrics): get_metrics 支持 axis_models 配置驱动 x 轴 + TPOT 分位推导

- axis_models=(name, source) 列表：零请求占位、source=local/cloud/observed、
  键序 requests 降序→名称升序（前端 Object.keys 即稳定 x 轴）
- tpot_p50/p95/p99 从 request_log 现有列推导：
  (duration_ms - ttft_ms) / (tokens_out - 1)，仅成功流式样本
- 新增 ttft_samples / tpot_samples 计数（前端 n 显示与低置信判定）"
```

---

### Task 2: 后端接线 — `_metrics_axis(pm)` + snapshot/`/api/metrics` 两处接入

**Files:**
- Modify: `inferfabric/proxy/handler.py`（新增 `_metrics_axis` 模块函数，约放在 `_compute_expensive` 之前；改 `_compute_expensive` 内 `get_metrics("24h")` 调用点（`handler.py:119`）；改 `_handle_api_metrics` 内调用点（`handler.py:704` 附近））
- Modify: `inferfabric/telemetry.py:121`（`TelemetryHub.get_metrics` 透传 `axis_models`，保持 API 一致）
- Test: `tests/unit/proxy/test_metrics_axis.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `get_metrics(window, axis_models=...)`；`pm.mgr._models`（`dict[str, ModelConfig]`，`m.name` 为友好名）；`pm.cloud.cloud_models`（`dict[str, CloudModel]`，key 为云模型短名，如 `glm-5.1`）。
- Produces: 模块级函数 `inferfabric.proxy.handler._metrics_axis(pm) -> list[tuple[str, str]]`：local 在前、cloud 在后、去重保序（同名 local 优先）、构建失败返回 `[]`（降级为数据驱动轴，不拖垮 metrics）。

- [ ] **Step 1: 写失败测试** `sandbox/tests/unit/proxy/test_metrics_axis.py`：

```python
"""unit/proxy/test_metrics_axis.py — _metrics_axis(pm) 配置驱动 x 轴构建

覆盖：local+cloud 组装、同名去重（local 优先）、pm.cloud 缺失降级、
_handle_api_metrics 确实把 axis 传给 get_metrics。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.proxy.handler import _metrics_axis, ProxyHandler


def _pm(model_names=("Qwen38-27B-TXT", "NI-Qwen38-27B-VL"),
        cloud_ids=("glm-5.1", "deepseek-v4-flash")):
    pm = MagicMock()
    pm.mgr._models = {n: SimpleNamespace(name=n) for n in model_names}
    pm.cloud.cloud_models = {c: SimpleNamespace() for c in cloud_ids}
    return pm


def test_axis_local_first_cloud_second():
    axis = _metrics_axis(_pm())
    assert ("Qwen38-27B-TXT", "local") in axis
    assert ("glm-5.1", "cloud") in axis
    assert axis[0][1] == "local"
    assert axis[-1][1] == "cloud"
    names = [n for n, _ in axis]
    assert len(names) == len(set(names))


def test_axis_dedup_local_wins():
    pm = _pm(model_names=("glm-5.1",), cloud_ids=("glm-5.1", "deepseek-v4-flash"))
    axis = _metrics_axis(pm)
    assert ("glm-5.1", "local") in axis
    assert ("glm-5.1", "cloud") not in axis
    assert len([a for a in axis if a[0] == "glm-5.1"]) == 1


def test_axis_cloud_none_degrades():
    pm = _pm()
    pm.cloud = None                      # 云端发现不可用 → 只剩 local，不抛
    axis = _metrics_axis(pm)
    assert all(src == "local" for _, src in axis)


def test_api_metrics_passes_axis():
    """回归守卫：/api/metrics 必须把 axis_models 传进 get_metrics。"""
    pm = _pm()
    captured = {}

    def fake_get_metrics(window="24h", axis_models=None):
        captured["window"] = window
        captured["axis"] = axis_models
        return {}

    pm.metrics.get_metrics.side_effect = fake_get_metrics
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = "/api/metrics?window=1h"
    h._send_json = MagicMock()
    h._handle_api_metrics(pm)
    assert captured["window"] == "1h"
    assert ("Qwen38-27B-TXT", "local") in (captured["axis"] or [])
```

（若 `ProxyHandler.__new__` 实例化方式与 `tests/unit/proxy/test_handler.py` 中现有 `_handle_*` 测试惯例不同，以该文件惯例为准调整构造方式——先读 `tests/unit/proxy/test_handler.py` 前 60 行。）

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/proxy/test_metrics_axis.py -v --tb=short
```
Expected: FAIL（`ImportError: cannot import name '_metrics_axis'`）。

- [ ] **Step 3: 实现** — `sandbox/inferfabric/proxy/handler.py`，在 `_compute_expensive` 定义之前新增：

```python
def _metrics_axis(pm) -> list[tuple[str, str]]:
    """v5.4: 指标图 x 轴（配置驱动）— (model_name, source) 列表。

    local = models.d 友好名（ModelConfig.name）；cloud = 启用云预设的模型短名
    （CloudDiscovery.cloud_models 键）。同名 local 优先、去重保序。
    构建失败返回 []（降级为数据驱动轴，不拖垮指标采集）。"""
    axis: list[tuple[str, str]] = []
    try:
        for m in pm.mgr._models.values():
            axis.append((m.name, "local"))
    except Exception:
        pass
    try:
        for model_id in pm.cloud.cloud_models:
            axis.append((model_id, "cloud"))
    except Exception:
        pass
    seen = set()
    out: list[tuple[str, str]] = []
    for name, src in axis:
        if name and name not in seen:
            seen.add(name)
            out.append((name, src))
    return out
```

改 `_compute_expensive`（`handler.py:119`）：
```python
        exp["metrics_24h"] = pm.metrics.get_metrics(
            "24h", axis_models=_metrics_axis(pm))
```

改 `_handle_api_metrics`（`handler.py:704` 附近）：
```python
            data = pm.metrics.get_metrics(window, axis_models=_metrics_axis(pm))
```

改 `sandbox/inferfabric/telemetry.py:121` 的 wrapper（透传，保持 API 一致）：
```python
    def get_metrics(self, window: str = "24h",
                    axis_models: list[tuple[str, str]] | None = None) -> dict:
        return self.metrics.get_metrics(window, axis_models=axis_models)
```
（实现时以 telemetry.py:121 现有函数体为准，仅扩签名 + 透传。）

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/proxy/test_metrics_axis.py tests/unit/proxy/ -v --tb=short
```
Expected: 新测试 4 passed + 既有 proxy 测试全绿（无回归）。

- [ ] **Step 5: Commit（sandbox dev）**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/proxy/handler.py inferfabric/telemetry.py tests/unit/proxy/test_metrics_axis.py
git commit -m "feat(metrics): _metrics_axis(pm) 配置驱动 x 轴接入 snapshot 与 /api/metrics

local(models.d 友好名) + cloud(启用云预设模型) 去重保序；构建失败降级 []；
TelemetryHub.get_metrics 透传 axis_models"
```

---

### Task 3: 更新 dashboard 契约测试（红灯先行）

**Files:**
- Modify: `tests/unit/dashboard/test_dashboard_v2.py`（`test_monitor_structure`、`test_monitor_js_present` 两处 + 新增 1 个测试）

**Interfaces:**
- Produces: 新 DOM/JS 契约（Task 4 实现后转绿）：`monTtftChart`/`monTpotChart` 取代 `monLatencyChart`；`data-seg="latwin"`/`"lattf"`/`"latpt"`；`monTtftEmpty`/`monTpotEmpty`/`monTtftTable`/`monTpotTable`；js 含 `/api/metrics?window=` 与 `data-view`。

- [ ] **Step 1: 修改 `test_monitor_structure`（`test_dashboard_v2.py:416` 附近）**

panel id 元组中：
```python
        'id="monLatencyChart"', # 延迟 P50/P95 双线
```
替换为：
```python
        'id="monTtftChart"',    # 延迟小倍数 · TTFT P50/P95（v5.4 双卡）
        'id="monTpotChart"',    # 延迟小倍数 · TPOT P50/P95（v5.4 双卡）
```
并在函数末尾（`data-gran` 断言之后）追加：
```python
    # v5.4: 延迟双卡 — 窗口切换（latwin ×2 镜像）+ 图表/表格视图切换
    assert 'data-seg="latwin"' in html
    assert 'data-seg="lattf"' in html
    assert 'data-seg="latpt"' in html
    assert 'id="monTtftEmpty"' in html and 'id="monTpotEmpty"' in html
    assert 'id="monTtftTable"' in html and 'id="monTpotTable"' in html
```

- [ ] **Step 2: 修改 `test_monitor_js_present`（`test_dashboard_v2.py:439` 附近）**

```python
    assert "IFCharts.create('monLatencyChart')" in js
```
替换为：
```python
    assert "IFCharts.create('monTtftChart')" in js
    assert "IFCharts.create('monTpotChart')" in js
```

- [ ] **Step 3: 新增测试（放在 `test_monitor_js_present` 之后）**

```python
def test_monitor_latency_cards():
    """v5.4 延迟双卡小倍数契约：TTFT/TPOT 共用模型 x 轴；窗口数据走 GET /api/metrics。

    双卡而非双 y 轴：两指标量纲差 ~2 个数量级，单卡双轴比例任意、制造假相关
    （spec 反模式 #1）；小倍数 = 同 x 轴 + 各自满刻度 y 轴。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    assert "monTtftChart" in js and "monTpotChart" in js
    assert "/api/metrics?window=" in js      # 1h/7d 窗口取数（GET-only）
    assert "data-view" in js                 # 图表/表格视图切换
    html = _html()
    assert 'id="monTtftChart"' in html and 'id="monTpotChart"' in html
```

- [ ] **Step 4: 运行确认红灯**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/dashboard/test_dashboard_v2.py -v --tb=short
```
Expected: `test_monitor_structure` / `test_monitor_js_present` / `test_monitor_latency_cards` FAIL（HTML/JS 尚未改），其余测试保持通过。

（此任务红灯不单独 commit——与 Task 4 实现一起转绿后统一提交。）

---

### Task 4: 前端实现 — 双卡 HTML + CSS + monitor.js

**Files:**
- Modify: `inferfabric/dashboard/fragments/monitor.html`（Row 3 整段替换；文件头注释更新）
- Modify: `inferfabric/dashboard/fragments/style.css`（末尾追加少量样式）
- Modify: `inferfabric/dashboard/js/monitor.js`（头注释、`_charts` 声明、`ensureCharts`、`renderLatencyChart` → `renderLatencyCards` 族、`renderMonitor`、主题回调、segment 委托新增分支）

**Interfaces:**
- Consumes: Task 2 的 API 形状（`metrics_24h.models[name].{ttft_p50,p95,p99, tpot_*, ttft_samples, tpot_samples, requests, source}`；键序 = x 轴序）；charts.js `PALETTES`（series 1 蓝 = P50、series 2 琥珀 = P95，自动按序着色）；现有 `.mon-seg`/`.mon-seg-btn`/`.if-table.mon-tbl`/`.cp-table-wrap` 样式类。
- Produces: DOM 契约（Task 3 断言）：`monTtftChart`/`monTpotChart`/`monTtftEmpty`/`monTpotEmpty`/`monTtftTable`/`monTpotTable`；卡片头 `data-seg="latwin"`（1h/24h/7d，两卡镜像同步）与 `data-seg="lattf"`/`"latpt"`（`data-view="chart"|"table"`）；`monTtftSub`/`monTpotSub`（JS 随窗口更新副标题）。

- [ ] **Step 1: 替换 `monitor.html` Row 3**（`inferfabric/dashboard/fragments/monitor.html:107-119` 的单卡整段）：

```html
  <!-- Row 3: 延迟分布小倍数 — TTFT / TPOT 双卡 (col 6+6)，共用同一模型 x 轴
       v5.4: 取代原单卡 monLatencyChart 双线。双卡而非双 y 轴：两指标量纲差 ~2 个数量级，
       双轴比例任意且制造假相关；小倍数 = 同 x 轴 + 各自满刻度 y 轴。
       x 轴配置驱动（后端 axis_models：local + 云预设），404 观测模型 source=observed 灰显。 -->
  <div class="if-grid">
    <div class="col-6">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">TTFT P50 / P95</span>
          <span class="if-card-sub" id="monTtftSub">ms · 24h 滚动窗口</span>
          <div class="mon-seg" data-seg="latwin">
            <button type="button" class="mon-seg-btn" data-win="1h">1h</button>
            <button type="button" class="mon-seg-btn active" data-win="24h">24h</button>
            <button type="button" class="mon-seg-btn" data-win="7d">7d</button>
          </div>
          <div class="mon-seg" data-seg="lattf">
            <button type="button" class="mon-seg-btn active" data-view="chart">图表</button>
            <button type="button" class="mon-seg-btn" data-view="table">表格</button>
          </div>
        </div>
        <div class="if-card-body">
          <div class="mon-chart-wrap">
            <div id="monTtftChart" class="mon-chart"></div>
            <div id="monTtftEmpty" class="mon-chart-empty" style="display:none">暂无 TTFT 数据（窗口内无成功请求）</div>
          </div>
          <div id="monTtftTable" class="mon-lat-table" style="display:none"></div>
        </div>
      </div>
    </div>
    <div class="col-6">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">TPOT P50 / P95</span>
          <span class="if-card-sub" id="monTpotSub">ms/token · 24h 滚动窗口</span>
          <div class="mon-seg" data-seg="latwin">
            <button type="button" class="mon-seg-btn" data-win="1h">1h</button>
            <button type="button" class="mon-seg-btn active" data-win="24h">24h</button>
            <button type="button" class="mon-seg-btn" data-win="7d">7d</button>
          </div>
          <div class="mon-seg" data-seg="latpt">
            <button type="button" class="mon-seg-btn active" data-view="chart">图表</button>
            <button type="button" class="mon-seg-btn" data-view="table">表格</button>
          </div>
        </div>
        <div class="if-card-body">
          <div class="mon-chart-wrap">
            <div id="monTpotChart" class="mon-chart"></div>
            <div id="monTpotEmpty" class="mon-chart-empty" style="display:none">暂无 TPOT 数据（需流式请求）</div>
          </div>
          <div id="monTpotTable" class="mon-lat-table" style="display:none"></div>
        </div>
      </div>
    </div>
  </div>
```

同时把文件顶部（Row 3 处旧注释 `<!-- Row 3: 延迟 P50/P95 双线 (col 12) -->`）替换掉（上面新块自带注释）。

- [ ] **Step 2: `style.css` 末尾追加**

```css
/* v5.4: 延迟双卡 — 卡头双段控件间距 + 表格视图容器 */
.if-card-hdr .mon-seg + .mon-seg { margin-left: 8px; }
.mon-lat-table { margin-top: 8px; }
```

- [ ] **Step 3: 修改 `monitor.js` — 头注释与图表注册**

文件头第 3-4 行注释更新（原 "3 ECharts … 延迟 P50+P95 双线"）：
```
 *   - 4 ECharts: GPU vram+util 时间曲线 / Token prompt+completion 堆叠条 /
 *     TTFT P50+P95 条 / TPOT P50+P95 条（v5.4 双卡小倍数，共用模型 x 轴）
```

`_charts` 声明（`monitor.js:36`）：
```js
  var _charts = { gpu: null, tokenLocal: null, tokenCloud: null, ttft: null, tpot: null };
```

`ensureCharts()` 内 `IFCharts.create('monLatencyChart')` 行替换为：
```js
      _charts.ttft = IFCharts.create('monTtftChart');
      _charts.tpot = IFCharts.create('monTpotChart');
```
null-fallback 段（"图表库不可用" 兜底）中 `_charts.latency` 块替换为等价的两块（`monTtftChart` / `monTpotChart`，写法与现有 `_charts.gpu` 块一致）。

- [ ] **Step 4: 修改 `monitor.js` — 替换 `renderLatencyChart`（`monitor.js:385-429`）为双卡实现**

整段（含上方 `/* ── 3. 延迟 … */` 注释块）替换为：

```js
  /* ── 3. 延迟分布小倍数：TTFT / TPOT 双卡（v5.4 取代原单卡双线）──
   * 两卡共用同一模型 x 轴（后端键序 = requests 降序 → 名称升序，天然对齐）。
   * 每卡：P50/P95 成对条形（IFCharts 固定调色板 蓝→琥珀，CVD 已验证），
   *   x 标签 = 模型名 + 换行 (n=请求数)；ttft/tpot 样本数 <30 → 条形 opacity 0.45（低置信）。
   * 视图：图表 / 表格（data-view）；窗口 1h/24h/7d（data-seg=latwin，两卡镜像同步；
   *   24h 取 snapshot metrics_24h，1h/7d 走 GET /api/metrics?window=（只读 display filter））。
   * 数据源：metrics_24h.models[model] → ttft_p50/p95/p99 + ttft_samples / tpot_* + source。 */
  var _latWin = '24h';
  var _latView = { ttft: 'chart', tpot: 'chart' };
  var _latCache = {};               // window → { data, at }（1h/7d 取数缓存）
  var _latFetchInflight = {};
  var _LAT_TTL = 15000;
  var _latCtx = { axis: [], models: {}, window: '24h' };

  function getLatData(win) {
    if (win === '24h') return store.get('metrics_24h') || {};
    var c = _latCache[win];
    var now = Date.now();
    if (c && now - c.at < _LAT_TTL) return c.data;
    if (!c && !_latFetchInflight[win]) {
      _latFetchInflight[win] = true;
      fetch('/api/metrics?window=' + win, { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (data) {
          _latCache[win] = { data: data || {}, at: Date.now() };
        })
        .catch(function (e) {
          console.warn('[monitor] /api/metrics?window=' + win + ' fetch failed:', e);
        })
        .then(function () {
          _latFetchInflight[win] = false;
          if (isMonitorActive()) renderLatencyCards();
        });
    }
    return c ? c.data : {};
  }

  function buildLatAxis(data) {
    var models = (data && data.models) || {};
    var names = Object.keys(models);
    var axis = [];
    for (var i = 0; i < names.length; i++) {
      var vm = models[names[i]] || {};
      axis.push({
        name: names[i],
        label: shortName(names[i]) + '\n(n=' + (vm.requests != null ? vm.requests : 0) + ')',
        source: vm.source || 'observed',
      });
    }
    return { axis: axis, models: models };
  }

  function srcBadge(src) {
    if (src === 'local') return '本地';
    if (src === 'cloud') return '云端';
    return '未配置';
  }

  function _latTooltip(prefix) {
    return function (params) {
      if (!params || !params.length) return '';
      var a = _latCtx.axis[params[0].dataIndex] || {};
      var vm = (_latCtx.models[a.name]) || {};
      var n = vm[prefix + '_samples'] || 0;
      var rows = '';
      ['p50', 'p95', 'p99'].forEach(function (q) {
        var v = vm[prefix + '_' + q];
        rows += '<div>' + q.toUpperCase() + '：' + (v != null ? v : '—') +
          (prefix === 'ttft' ? ' ms' : ' ms/token') + '</div>';
      });
      return '<div><b>' + escHtml(a.name) + '</b> ' +
        '<span class="badge info">' + srcBadge(a.source) + '</span></div>' +
        '<div>' + (vm.requests || 0) + ' 请求 · ' + n + ' 样本' +
        (n > 0 && n < 30 ? '（低置信）' : '') + ' · ' + _latCtx.window + ' 窗口</div>' + rows;
    };
  }

  function renderLatTable(isTtft) {
    var prefix = isTtft ? 'ttft' : 'tpot';
    var el = $(isTtft ? 'monTtftTable' : 'monTpotTable');
    if (!el) return;
    var ctx = _latCtx;
    var rows = '';
    for (var i = 0; i < ctx.axis.length; i++) {
      var a = ctx.axis[i], vm = ctx.models[a.name] || {};
      var n = vm[prefix + '_samples'] || 0;
      var v50 = vm[prefix + '_p50'], v95 = vm[prefix + '_p95'], v99 = vm[prefix + '_p99'];
      var low = n > 0 && n < 30;
      var cls = (vm.requests || 0) === 0 ? ' class="muted"' : '';
      rows += '<tr' + cls + '>' +
        '<td>' + escHtml(a.name) + '</td>' +
        '<td><span class="badge info">' + srcBadge(a.source) + '</span></td>' +
        '<td class="mono num">' + (vm.requests || 0) + '</td>' +
        '<td class="mono num">' + (v50 != null ? v50 + (low ? ' †' : '') : '—') + '</td>' +
        '<td class="mono num">' + (v95 != null ? v95 : '—') + '</td>' +
        '<td class="mono num">' + (v99 != null ? v99 : '—') + '</td>' +
      '</tr>';
    }
    el.innerHTML =
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl">' +
      '<thead><tr><th>模型</th><th>来源</th><th>请求</th>' +
      '<th>P50</th><th>P95</th><th>P99</th></tr></thead>' +
      '<tbody>' + rows + '</tbody>' +
      '</table></div>' +
      '<div class="muted" style="font-size:11px;margin-top:4px">' +
      '† 低置信（样本 &lt; 30）；单位 ms' + (isTtft ? '' : '/token') + '；n=0 行无窗口内请求</div>';
  }

  function renderLatCard(metric) {
    var isTtft = metric === 'ttft';
    var prefix = isTtft ? 'ttft' : 'tpot';
    var tableEl = $(isTtft ? 'monTtftTable' : 'monTpotTable');
    var chartEl = $(isTtft ? 'monTtftChart' : 'monTpotChart');
    var wrapEl = chartEl ? chartEl.parentNode : null;   // .mon-chart-wrap

    if (_latView[metric] === 'table') {
      if (wrapEl) wrapEl.style.display = 'none';
      renderLatTable(isTtft);
      if (tableEl) tableEl.style.display = '';
      return;
    }
    if (tableEl) tableEl.style.display = 'none';
    if (wrapEl) wrapEl.style.display = '';
    if (!_charts[metric]) return;

    var ctx = _latCtx;
    var p50 = [], p95 = [];
    for (var i = 0; i < ctx.axis.length; i++) {
      var vm = ctx.models[ctx.axis[i].name] || {};
      var n = vm[prefix + '_samples'] || 0;
      var v50 = vm[prefix + '_p50'], v95 = vm[prefix + '_p95'];
      // 低置信（0 < n < 30）：半透明条；无样本（n=0）：不画
      p50.push(v50 != null ? (n < 30 ? { value: v50, itemStyle: { opacity: 0.45 } } : v50) : null);
      p95.push(v95 != null ? (n < 30 ? { value: v95, itemStyle: { opacity: 0.45 } } : v95) : null);
    }
    var hasData = p50.some(function (v) { return v != null; });
    showEmpty(isTtft ? 'monTtftEmpty' : 'monTpotEmpty', !hasData);
    IFCharts.update(_charts[metric], {
      xAxis: {
        data: ctx.axis.map(function (a) { return a.label; }),
        boundaryGap: true,
        axisLabel: { interval: 0, fontSize: 11 },
      },
      yAxis: { axisLabel: { formatter: '{value} ms' } },
      tooltip: { trigger: 'axis', formatter: _latTooltip(prefix) },
      series: [
        { type: 'bar', name: 'P50', data: p50, barMaxWidth: 24 },
        { type: 'bar', name: 'P95', data: p95, barMaxWidth: 24 },
      ],
    });
  }

  function renderLatencyCards() {
    ensureCharts();
    var data = getLatData(_latWin);
    var built = buildLatAxis(data);
    _latCtx = { axis: built.axis, models: built.models, window: _latWin };
    var sub1 = $('monTtftSub'), sub2 = $('monTpotSub');
    if (sub1) sub1.textContent = 'ms · ' + _latWin + ' 滚动窗口';
    if (sub2) sub2.textContent = 'ms/token · ' + _latWin + ' 滚动窗口';
    renderLatCard('ttft');
    renderLatCard('tpot');
  }
```

- [ ] **Step 5: 修改 `monitor.js` — 调用点接线**

`renderMonitor()`（`monitor.js:636` 附近）内 `renderLatencyChart();` 替换为 `renderLatencyCards();`。

主题变更回调（`monitor.js:723` 附近 `IFCharts.onThemeChange` 内）`renderLatencyChart();` 替换为 `renderLatencyCards();`。

segment 事件委托（`monitor.js:700-720` 的 `if (segType === 'win') ... else if (segType === 'gran') ...` 链末尾）追加分支：
```js
      } else if (segType === 'latwin') {
        var latWin = btn.getAttribute('data-win');
        if (latWin && latWin !== _latWin) {
          _latWin = latWin;
          // 两卡 latwin 段镜像同步 active 态
          tabEl.querySelectorAll('.mon-seg[data-seg="latwin"]').forEach(function (seg) {
            seg.querySelectorAll('.mon-seg-btn').forEach(function (b) {
              b.classList.toggle('active', b.getAttribute('data-win') === _latWin);
            });
          });
          renderLatencyCards();
        }
      } else if (segType === 'lattf' || segType === 'latpt') {
        var which = (segType === 'lattf') ? 'ttft' : 'tpot';
        var view = btn.getAttribute('data-view') === 'table' ? 'table' : 'chart';
        if (view !== _latView[which]) {
          _latView[which] = view;
          renderLatCard(which);
        }
      }
```
（注意：现有委托开头已对"被点击的那段"更新 active 类；latwin 的镜像同步会覆盖两段，幂等无冲突。）

**只读契约自检**（test_monitor_readonly 会查）：新增代码中不得出现字符串 `POST`（fetch 一律 GET、无 method 参数）；注释同样避免该词。

- [ ] **Step 6: node 语法自检 + dashboard 契约测试转绿**

```bash
cd /home/vince/inferfabric/sandbox
node --check inferfabric/dashboard/js/monitor.js && echo SYNTAX-OK
python3 -m pytest tests/unit/dashboard/test_dashboard_v2.py -v --tb=short
```
Expected: SYNTAX-OK；Task 3 三个红灯测试全绿，且原 `test_monitor_readonly` / `test_monitor_chart_series_limit` / `test_monitor_js_node_syntax` 保持通过。

- [ ] **Step 7: Commit（sandbox dev）**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/dashboard/fragments/monitor.html \
        inferfabric/dashboard/fragments/style.css \
        inferfabric/dashboard/js/monitor.js \
        tests/unit/dashboard/test_dashboard_v2.py
git commit -m "feat(dashboard/monitor): 延迟分布双卡小倍数 TTFT+TPOT 共用模型 x 轴

- 单卡 monLatencyChart 双线 → 双卡 col-6（monTtftChart/monTpotChart），
  双卡而非双 y 轴（量纲差 ~2 个数量级，双轴比例任意）
- x 轴配置驱动（后端 axis_models），404 观测模型 source=observed 不再漏成空槽
- 每卡 P50/P95 成对条形 + n 标注 + 低置信(opacity) + 表格视图 + 1h/24h/7d 窗口
  （24h 走 snapshot，1h/7d GET /api/metrics?window=，只读）
- 契约测试同步更新（test_dashboard_v2）"
```

---

### Task 5: 质量门（sandbox 内全量验证）

**Files:** 无新文件（只跑门）。

- [ ] **Step 1: 全量 pytest（unit + integration）**

```bash
cd /home/vince/inferfabric/sandbox
python3 -m pytest tests/unit/ tests/integration/ -v --tb=short 2>&1 | tail -20
```
Expected: 全部通过（含新 3 个测试文件/用例、既有 snapshot etag / metrics 相关测试）。

- [ ] **Step 2: import smoke**

```bash
cd /home/vince/inferfabric/sandbox && python3 -c "import inferfabric; print('import OK')"
```

- [ ] **Step 3: 运行时 smoke（dev 服务器，避让生产 8999）**

```bash
cd /home/vince/inferfabric/sandbox
EDGE_PROXY_PORT=8998 python3 -m inferfabric.proxy.handler &
sleep 6
# /api/metrics 新字段（source / ttft_samples / tpot_* / 零请求 axis 占位）
curl -s "http://127.0.0.1:8998/api/metrics?window=24h" | python3 -c "
import json,sys
d = json.load(sys.stdin)
ms = d.get('models', {})
print('models:', list(ms.keys()))
for k, v in list(ms.items())[:3]:
    print(k, '->', {x: v[x] for x in ('requests','source','ttft_samples','tpot_samples','tpot_p50') if x in v})
"
# dashboard HTML 含新卡
curl -s http://127.0.0.1:8998/ | grep -o 'monTtftChart\|monTpotChart' | sort -u
# snapshot 正常
curl -s -o /dev/null -w "snapshot %{http_code}\n" http://127.0.0.1:8998/api/snapshot
kill %1
```
Expected: models 键序 = 请求数降序；`source` 出现 local/cloud/observed；`tpot_p50` 在有流式样本的模型上非空；两个新卡 id 都在 HTML 中；snapshot 200。
（注：`mgr.status()` 健康探测首调可能慢数秒，curl 加 `--max-time 30` 兜底。）

- [ ] **Step 4（可选，视觉验收）: 浏览器截图双卡**

用 playwright：`http://127.0.0.1:8998/` → 点 Monitor tab → 对两张延迟卡各截图（图表态 + 表格态），肉眼核对：x 轴对齐、n 标注、低置信半透明、claude-* 观测模型行灰显且无断线假象、tooltip 完整。

---

### Task 6: 合并到生产 + 服务重启

**Files:**
- 8 个被改文件从 sandbox → 生产根目录（同相对路径）

**Interfaces:**
- Consumes: Task 5 全门通过。
- Produces: 生产 main 新 commit；`inferfabric-proxy.service` 重启后 :8999 上生效。

- [ ] **Step 1: 同步文件到生产**

```bash
cd /home/vince/inferfabric
for f in inferfabric/metrics_aggregator.py \
         inferfabric/proxy/handler.py \
         inferfabric/telemetry.py \
         inferfabric/dashboard/fragments/monitor.html \
         inferfabric/dashboard/fragments/style.css \
         inferfabric/dashboard/js/monitor.js \
         tests/unit/infra/test_metrics_aggregator.py \
         tests/unit/proxy/test_metrics_axis.py \
         tests/unit/dashboard/test_dashboard_v2.py; do
  cp "sandbox/$f" "$f"
done
git status --short
```

- [ ] **Step 2: 生产侧质量门**

```bash
cd /home/vince/inferfabric
python3 -m pytest tests/unit/ tests/integration/ --tb=short 2>&1 | tail -5
python3 -c "import inferfabric; print('import OK')"
```
Expected: 全绿。

- [ ] **Step 3: commit 生产 main（不 push，由用户决定）**

```bash
cd /home/vince/inferfabric
git add inferfabric/metrics_aggregator.py inferfabric/proxy/handler.py inferfabric/telemetry.py \
        inferfabric/dashboard/fragments/monitor.html inferfabric/dashboard/fragments/style.css \
        inferfabric/dashboard/js/monitor.js \
        tests/unit/infra/test_metrics_aggregator.py tests/unit/proxy/test_metrics_axis.py \
        tests/unit/dashboard/test_dashboard_v2.py
git commit -m "feat(dashboard/monitor): 延迟分布双卡小倍数——TTFT+TPOT 共用配置驱动模型 x 轴

- get_metrics: axis_models（local+云预设，source=local/cloud/observed）+ 稳定排序
  + TPOT 分位从现有列推导（零 schema 变更）+ ttft/tpot_samples 计数
- /api/snapshot 与 /api/metrics 接入 _metrics_axis(pm)
- 前端单卡双线 → 双卡 col-6 小倍数（不引入双 y 轴），n 标注 + 低置信 +
  表格视图 + 1h/24h/7d 窗口切换（GET-only 只读）
- 契约测试同步（test_dashboard_v2 + 新增 2 个测试文件）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 4: 回写 sandbox（保持双仓同步）**

```bash
cd /home/vince/inferfabric/sandbox
git add inferfabric/ tests/unit/
git commit -m "chore: sync merged production commit (latency dual-cards)"
```

- [ ] **Step 5: 重启生产 proxy 服务并验证**

```bash
systemctl --user restart inferfabric-proxy.service
sleep 5
curl -s --max-time 30 "http://127.0.0.1:8999/api/metrics?window=24h" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print('models:', list(d.get('models', {}).keys()))
"
curl -s -o /dev/null -w "dashboard %{http_code}\n" http://127.0.0.1:8999/
systemctl --user status inferfabric-proxy.service | head -5
```
Expected: metrics 200 且含 `source`/`tpot_*` 字段；dashboard 200；服务 active。
（dashboard 改动需重启才生效——dashboard HTML 启动时装配缓存。）

- [ ] **Step 6（可选，用户执行）: 跨评审**

按 CLAUDE.md 质量门，可用 AtomCode GLM-5.2 对 `git show <commit>` 做交叉评审。

---

## Self-Review 记录

1. **Spec 覆盖**：双卡小倍数 ✓（Task 4）；TPOT 数据源 ✓（Task 1）；配置驱动 x 轴 + 404 模型不消失（observed 灰显）✓（Task 1/2）；n 显示 + n<30 低置信 ✓（Task 4）；表格视图 ✓（Task 4）；窗口切换 1h/24h/7d ✓（Task 4，后端 `/api/metrics?window=` 现成）；tooltip 完整（P50/P95/P99 + n + source + 窗口）✓（Task 4 `_latTooltip`）；不引入新颜色（复用已验证 PALETTES，免 validator 重跑）✓；质量门 ✓（Task 5/6）；服务重启 ✓（Task 6 Step 5）。
   未纳入（有意的 non-goal）：非流式 TTFT 补齐与 OpenAI 非流式"总耗时当 TTFT"口径修正（数据质量 P0 项，独立任务）；模型名归一化落库（P0，独立任务）；分位数改查询时 SQLite 计算（P2，缓）。
2. **Placeholder 扫描**：无 TBD/TODO；每个代码步骤含完整代码；`test_metrics_axis.py` 中"以 test_handler.py 惯例为准"是明确的构造方式指路，非占位。
3. **类型一致性**：`axis_models: list[tuple[str, str]]` 在 Task 1 定义，Task 2 `_metrics_axis` 同型；前端字段名 `ttft_samples`/`tpot_samples`/`source`/`requests` 与 Task 1 产出一致；DOM id（monTtftChart/monTpotChart/monTtftEmpty/monTpotEmpty/monTtftTable/monTpotTable/monTtftSub/monTpotSub/latwin/lattf/latpt）在 Task 3 断言与 Task 4 实现中一致。
