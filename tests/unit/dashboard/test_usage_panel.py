"""dashboard UX v3 D2 — 用量页行为契约（node vm，无网络/DOM）。

规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md D2 +
specs/dashboard-ux-v3/interface-contract.md §D。
被测对象：inferfabric/dashboard/js/usage.js（与真实 store.js 同装；
harness 结构对齐 test_requests_panel.py；IFCharts 用捕获桩——断言图更新的
系列构成/桶合成/颜色，不依赖 echarts 本体）。

契约要点（消费 D1 /api/monitor-report 增量输出）：
- 一个窗口（1h/24h/7d/30d）驱动整页：KPI×4、趋势图、模型/客户端分解成组换新
- 旧窗口响应晚到无效（seq 失效）；离页守卫
- KV 前缀缓存命中 token 份额：分母无数据 → 无值不画 0%；不混响应缓存命中率
- 未计价 → 「未计价」标注，不冒认 ¥0；coverage.truncated → 提示不完整
- 趋势图 本地=蓝 #2563eb / 云=琥珀 #b45309，unknown 不入图只作覆盖说明
- 7d/30d 用报告桶合成日图；1h/24h 保留报告桶粒度
- 电费 /api/device_power：窗口→gran 映射（1h→hour…30d→month）；云费与电费
  单列来源；缺测/覆盖不足如实标注，不伪造合计
- 客户端「未识别」分组保留认领路径（data-claim 委托旧 monitor.js 弹窗）
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "inferfabric" / "dashboard" / "js"
USAGE_JS = JS / "usage.js"
STORE_JS = JS / "store.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

// ── localStorage / location 桩（与 test_requests_panel.py 同款）──
function makeStorage(opts) {
  const map = Object.assign({}, (opts && opts.data) || {});
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(map, k) ? map[k] : null),
    setItem: (k, v) => { map[k] = String(v); },
    removeItem: (k) => { delete map[k]; },
    _map: map,
  };
}
const storage = makeStorage(input.storage);
let hashValue = input.hash || '';
const location = {
  get hash() { return hashValue; },
  set hash(v) { hashValue = v; },
  replace(url) { const i = url.indexOf('#'); hashValue = i >= 0 ? url.slice(i) : ''; },
};

// ── 元素桩：未知 id 自动补建 ──
function makeEl(id, attrs) {
  const classes = new Set();
  const listeners = {};
  const attribs = Object.assign({}, attrs || {});
  const el = {
    id, _classes: classes, _attrs: attribs,
    value: '', disabled: false, hidden: false, title: '', checked: false,
    innerHTML: '', textContent: '',
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, on = !classes.has(c)) => { if (on) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: () => {},
    setAttribute: (k, v) => { attribs[k] = v; },
    removeAttribute: (k) => { delete attribs[k]; },
    getAttribute: (k) => (k in attribs ? attribs[k] : null),
    querySelector: () => null, querySelectorAll: () => [],
    focus: () => {}, appendChild: () => {},
    _fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev || { target: el })); },
    _listeners: listeners,
    style: {}, options: [],
  };
  return el;
}
const elCache = {};
const el = (id) => { if (!elCache[id]) elCache[id] = makeEl(id); return elCache[id]; };

const panels = {};
for (const v of ['usage', 'status']) panels['tab-' + v] = makeEl('tab-' + v);
const els = {
  'syncMeta': makeEl('syncMeta'), 'navVer': makeEl('navVer'),
  'apiErrorBanner': makeEl('apiErrorBanner'), 'confirmModal': makeEl('confirmModal'),
};
const document = {
  readyState: 'loading',
  documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
  getElementById: (id) => panels[id] || els[id] || el(id),
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener: () => {}, removeEventListener: () => {},
  body: { classList: { toggle: () => {} }, appendChild: () => {} },
};
const window = {
  addEventListener: (ev, fn) => {
    if (ev === 'iff-agents-changed') (window._iffAgentsChangedSubs = window._iffAgentsChangedSubs || []).push(fn);
  },
  dispatchEvent: () => true,
};

// ── IFCharts 捕获桩：create/update 记录，palettes 对齐 charts.js v7.0 ──
const chartUpdates = [];
const created = [];
const IFCharts = {
  palettes: { dark: ['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777'],
              light: ['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777'] },
  create: (id) => { created.push(id); return { _stub: id }; },
  update: (target, patch, opts) => {
    chartUpdates.push({ target: (target && (target._ifcId || target._stub)) || String(target),
                        patch, opts: opts || {} });
    return true;
  },
  dispose: () => true, resizeAll: () => {}, currentTheme: () => 'dark',
};
window.IFCharts = IFCharts;

// ── 可编程 fetch：/api/monitor-report 与 /api/device_power 各自脚本队列 ──
const reportReqs = [];
const powerReqs = [];
let snapshotFetches = 0;
const pending = [];
const reportQueue = (input.reports || []).slice();
const powerQueue = (input.powers || []).slice();
function respond(spec) {
  if (spec.error) return Promise.reject(new Error('fetch fail'));
  return Promise.resolve({ ok: true, status: 200, json: async () => spec.json });
}
const fetch = (url) => {
  const u = String(url);
  if (u.indexOf('/api/monitor-report') === 0) {
    reportReqs.push(u);
    const spec = reportQueue.shift();
    if (!spec) return Promise.reject(new Error('no scripted report'));
    if (spec.wait) return new Promise((res) => pending.push({ resolve: res, spec, kind: 'report' }));
    return respond(spec);
  }
  if (u.indexOf('/api/device_power') === 0) {
    powerReqs.push(u);
    const spec = powerQueue.shift();
    if (!spec) return Promise.reject(new Error('no scripted power'));
    if (spec.wait) return new Promise((res) => pending.push({ resolve: res, spec, kind: 'power' }));
    return respond(spec);
  }
  snapshotFetches += 1;
  return respond({ json: input.snapshotJson || {} });
};

const intervals = [];
const ctx = {
  window, document, location, localStorage: storage, console,
  setTimeout: (fn) => 0, clearTimeout: () => {},
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval: (id) => { if (intervals[id - 1]) intervals[id - 1].dead = true; },
  fetch, IFCharts,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);   // store.js
if (!fs.existsSync(process.argv[3])) { console.error('usage.js missing'); process.exit(3); }
vm.runInNewContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);   // usage.js
assert.ok(window.store, 'store must load');
assert.ok(window.ifUsage, 'usage.js must expose window.ifUsage');

const drain = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

async function run() {
  await drain();
  for (const step of (input.steps || [])) {
    if (step.seedStore) for (const [k, v] of Object.entries(step.seedStore)) window.store.set(k, v);
    if (step.click) el(step.click)._fire('click');
    if (step.fire) el(step.fire[0])._fire(step.fire[1]);
    if (step.flushReport !== undefined) {
      const idx = step.flushReport;
      assert.ok(pending[idx], 'no pending fetch #' + idx);
      pending[idx].done = true;
      pending[idx].resolve({ ok: true, status: 200, json: async () => pending[idx].spec.json });
    }
    if (step.agentsChanged) { /* 模拟 monitor.js 认领成功后派发的事件 */
      for (const fn of (window._iffAgentsChangedSubs || [])) fn();
    }
    await drain();
  }
  await drain();
  const out = {
    reportReqs, powerReqs, snapshotFetches,
    pendingCount: pending.filter((p) => !p.done).length,
    chartUpdates, created,
    kpiReq: el('useKpiReq').innerHTML,
    kpiTok: el('useKpiTok').innerHTML,
    kpiCost: el('useKpiCost').innerHTML,
    kpiCache: el('useKpiCache').innerHTML,
    kpiCacheLabel: el('useKpiCacheLbl') ? el('useKpiCacheLbl').textContent : '',
    trendNote: el('useTrendNote').innerHTML,
    trendEmpty: el('useTrendEmpty').style.display,
    modelTable: el('useModelTable').innerHTML,
    agentTable: el('useAgentTable').innerHTML,
    powerTotals: el('usePowerTotals').innerHTML,
    powerNote: el('usePowerNote').innerHTML,
    powerEmpty: el('usePowerEmpty') ? el('usePowerEmpty').style.display : null,
    cloudCost: el('useCloudCost') ? el('useCloudCost').innerHTML : '',
    metaLine: el('useMetaLine') ? el('useMetaLine').innerHTML : '',
    truncBanner: el('useTruncBanner') ? el('useTruncBanner').innerHTML : '',
    winOn: ['1h', '24h', '7d', '30d'].filter((w) => el('useWin-' + w).classList.contains('on')),
    consoleErrors: [],
  };
  console.log(JSON.stringify(out));
}
run().catch((e) => { console.error(e && e.stack || e); process.exitCode = 1; });
"""


def _run_harness(tmp_path, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for p in (STORE_JS,):
        if not p.exists():
            pytest.fail(f"{p.name} missing")
    if not USAGE_JS.exists():
        pytest.fail("usage.js missing (D2 not implemented)")
    harness = tmp_path / "usage_panel_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(STORE_JS), str(USAGE_JS)],
        input=json.dumps(payload).encode(), capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"harness failed rc={proc.returncode}\nSTDERR:{proc.stderr.decode()[-2000:]}")
    return json.loads(proc.stdout.decode())


# ── 报告/功耗响应构造 ─────────────────────────────────────────────

def _sub(requests=0, tin=0, tout=0, cached=0, cost=None):
    return {"requests": requests, "tokens_in": tin, "tokens_out": tout,
            "tokens_in_cached": cached, "cost_estimate": cost}


def _bucket(start, req=0, tin=0, tout=0, loc=None):
    b = {"bucket_start": start, "bucket_end": start + 300,
         "requests": req, "success": req, "tokens_in": tin, "tokens_out": tout,
         "local": _sub(), "cloud": _sub(), "unknown": _sub()}
    if loc:
        b[loc] = _sub(req, tin, tout)
    return b


def _model(name, *, location="local", provider="", requests=2, tin=100, tout=50,
           cached=40, cost=None, p50=120.0, p95=300.0):
    return {"location": location, "provider": provider, "model": name,
            "requests": requests, "success": requests,
            "tokens_in": tin, "tokens_out": tout, "tokens_in_cached": cached,
            "cache_share": (cached / tin) if tin else None,
            "p50_ttft_ms": p50, "p95_ttft_ms": p95, "ttft_samples": requests,
            "p50_duration_ms": 900.0, "p95_duration_ms": 2000.0,
            "duration_samples": requests, "success_rate": 1.0,
            "cost_estimate": cost}


def _agent(name, *, requests=2, tin=100, tout=50, cached=40, cost=None):
    return {"agent": name, "requests": requests, "success": requests,
            "tokens_in": tin, "tokens_out": tout, "tokens_in_cached": cached,
            "cache_share": (cached / tin) if tin else None,
            "p50_ttft_ms": 110.0, "p95_ttft_ms": 280.0,
            "p50_duration_ms": 850.0, "p95_duration_ms": 1900.0,
            "success_rate": 1.0, "cost_estimate": cost}


def _report(win="24h", *, requests=5, tin=1000, tout=500, cached=400,
            cost=0.0002, coverage="priced", cache_share=0.4,
            models=None, agents=None, series=None, truncated=False):
    if models is None:
        models = [_model("glm-5")]
    if agents is None:
        agents = [_agent("alice")]
    if series is None:
        series = [_bucket(1700000000.0, req=requests, tin=tin, tout=tout,
                          loc="local")]
    return {
        "contract": "monitor-report/1", "window": win, "scope": "all",
        "since": 1700000000.0 - 86400, "until": 1700000000.0,
        "collected_at": 1700000000.0,
        "coverage": {"rows_scanned": requests, "pool_summary_rows": 0,
                     "truncated": truncated},
        "totals": {"requests": requests, "success": requests,
                   "success_rate": 1.0, "tokens_in": tin, "tokens_out": tout,
                   "tokens_in_cached": cached, "usage_unknown_rows": 0,
                   "cost_estimate": cost, "cost_coverage": coverage,
                   "cache_share": cache_share},
        "models": models, "agents": agents, "series": series,
        "pools": [], "errors": [],
    }


def _power(gran="day", *, available=True, nbuckets=24, filled=20, yuan=0.5):
    buckets = []
    for i in range(nbuckets):
        buckets.append({"t": 1700000000.0 + i * 3600,
                        "avg_w": (90.0 if i < filled else None),
                        "kwh": (0.002 if i < filled else 0.0),
                        "cum_kwh": 0.001 * (i + 1), "cum_yuan": 0.001 * (i + 1)})
    return {"gran": gran, "price_yuan_per_kwh": 1.0, "buckets": buckets,
            "totals": {"kwh": 0.04, "yuan": yuan, "avg_w": 90.0,
                       "count": filled if available else 0},
            "available": available, "stream": "aggregate"}


_DEFAULT_SEED = [{"seedStore": {"tab_active": "tab-monitor.usage"}}]


# ── 一个窗口驱动整页 ─────────────────────────────────────────────

def test_window_default_24h_drives_report_and_power(tmp_path):
    """装载即取数：monitor-report(window=24h&scope=all) + device_power(gran=day)
    恰各一次；KPI 由报告 totals 驱动。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report()}],
        "powers": [{"json": _power(gran="day")}],
        "steps": _DEFAULT_SEED,
    })
    assert len(out["reportReqs"]) == 1
    assert "window=24h" in out["reportReqs"][0]
    assert "scope=all" in out["reportReqs"][0]
    assert len(out["powerReqs"]) == 1
    assert "gran=day" in out["powerReqs"][0]
    assert "5" in out["kpiReq"]
    assert "1,000" in out["kpiTok"]
    assert "24h" in out["winOn"]


def test_window_switch_refetches_whole_page(tmp_path):
    """切窗口成组换新：report(window=7d) + device_power(gran=week) 重取，
    KPI/表/图全部反映新窗口数据。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(win="24h")},
                    {"json": _report(win="7d", requests=99, tin=9999)}],
        "powers": [{"json": _power(gran="day")},
                   {"json": _power(gran="week")}],
        "steps": _DEFAULT_SEED + [{"click": "useWin-7d"}],
    })
    assert len(out["reportReqs"]) == 2 and len(out["powerReqs"]) == 2
    assert "window=7d" in out["reportReqs"][1]
    assert "gran=week" in out["powerReqs"][1]
    assert "99" in out["kpiReq"] and "9,999" in out["kpiTok"]
    assert out["winOn"] == ["7d"]


def test_late_window_response_discarded(tmp_path):
    """旧窗口响应晚到不得覆盖新窗口（seq 失效）：
    切 1h（在途）→ 切 24h 落地 → 1h 响应迟到，不得覆盖。"""
    out = _run_harness(tmp_path, {
        "reports": [
            {"json": _report(win="24h", requests=11)},                # 初载
            {"wait": True, "json": _report(win="1h", requests=777)},  # 1h 在途
            {"json": _report(win="24h", requests=555)},               # 24h 落地
        ],
        "powers": [{"json": _power(gran="day")}, {"json": _power(gran="hour")},
                   {"json": _power(gran="day")}],
        "steps": _DEFAULT_SEED + [{"click": "useWin-1h"}, {"click": "useWin-24h"},
                                  {"flushReport": 0}],
    })
    assert "777" not in out["kpiReq"]
    assert "555" in out["kpiReq"]
    assert out["winOn"] == ["24h"]


# ── KPI 契约 ─────────────────────────────────────────────────────

def test_kpi_cache_share_null_shows_no_data_not_zero(tmp_path):
    """分母无数据 → cache_share null → 「暂无数据」，不画 0%。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(cache_share=None, cached=0,
                                     models=[_model("glm-5", cached=0)])}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    assert "0%" not in out["kpiCache"]
    assert ("暂无" in out["kpiCache"]) or ("—" in out["kpiCache"])


def test_kpi_cost_unpriced_badge_not_zero_yuan(tmp_path):
    """无计价证据 → 「未计价」标注，不显示 ¥0.00。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(cost=None, coverage="unpriced")}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    assert "¥0.00" not in out["kpiCost"]
    assert "未计价" in out["kpiCost"]


def test_kpi_cache_label_distinguishes_response_cache(tmp_path):
    """KV 前缀缓存命中 ≠ 响应缓存命中率：KPI 卡须有 KV 语义标注。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report()}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    label = out["kpiCacheLabel"] + out["kpiCache"]
    assert "KV" in label or "前缀" in label
    assert "响应缓存" in label   # 明示「非响应缓存命中率」


def test_truncated_report_banner(tmp_path):
    """coverage.truncated → 报告不完整提示。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(truncated=True)}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    assert "不完整" in out["truncBanner"]


# ── 趋势图 ───────────────────────────────────────────────────────

def test_trend_series_local_blue_cloud_amber_stacked(tmp_path):
    """本地=蓝 #2563eb、云=琥珀 #b45309，堆叠柱；unknown 不入图。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report()}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    updates = [u for u in out["chartUpdates"] if u["target"] == "useTrendChart"]
    assert updates, "trend chart must be updated"
    series = updates[-1]["patch"]["series"]
    assert [s["name"] for s in series] == ["本地", "云端"]
    assert series[0]["itemStyle"]["color"] == "#2563eb"
    assert series[1]["itemStyle"]["color"] == "#b45309"
    assert all(s.get("stack") for s in series)


def test_trend_7d_synthesizes_daily_buckets(tmp_path):
    """7d：28 个 6h 桶 → 合成 7 个日桶。"""
    series = []
    for d in range(7):
        for k in range(4):
            series.append(_bucket(1700000000.0 + (d * 4 + k) * 21600.0,
                                  req=k + 1, tin=100, loc="local"))
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(win="7d", series=series)}],
        "powers": [{"json": _power(gran="week")}],
        "steps": _DEFAULT_SEED + [{"click": "useWin-7d"}],
    })
    updates = [u for u in out["chartUpdates"] if u["target"] == "useTrendChart"]
    assert updates
    cats = updates[-1]["patch"]["xAxis"]["data"]
    assert len(cats) == 7, cats


def test_trend_short_window_keeps_report_buckets(tmp_path):
    """1h：12 个 5min 桶按报告桶直出（不合成）。"""
    series = [_bucket(1700000000.0 + i * 300.0, req=1, loc="local")
              for i in range(12)]
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(win="1h", series=series)}],
        "powers": [{"json": _power(gran="hour")}],
        "steps": _DEFAULT_SEED + [{"click": "useWin-1h"}],
    })
    updates = [u for u in out["chartUpdates"] if u["target"] == "useTrendChart"]
    assert updates
    cats = updates[-1]["patch"]["xAxis"]["data"]
    assert len(cats) == 12, cats


def test_unknown_overlay_note_not_a_series(tmp_path):
    """unknown 桶不作图系列，另作覆盖说明（数量可见）。"""
    series = [_bucket(1700000000.0, req=6, tin=60, loc="local")]
    series[0]["unknown"] = _sub(requests=2, tin=20)
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(requests=8, series=series)}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    updates = [u for u in out["chartUpdates"] if u["target"] == "useTrendChart"]
    names = [s["name"] for s in updates[-1]["patch"]["series"]]
    assert "unknown" not in names and "未知" not in names
    assert "2" in out["trendNote"]


# ── 模型 / 客户端分解 ────────────────────────────────────────────

def test_model_table_share_tokens_cost_latency(tmp_path):
    """模型分解：份额、tokens、请求、成本、p50/p95；云端带厂商；未计价标注。"""
    models = [
        _model("glm-5", location="local", requests=3, tin=300, cached=150),
        _model("glm-5", location="cloud", provider="zhipu", requests=1,
               tin=100, cost=0.0002),
        _model("qwen3", location="local", requests=1, tin=100, cost=None),
    ]
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(models=models, tin=500)}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    t = out["modelTable"]
    assert "60.0" in t                # 300/500
    assert "zhipu" in t               # 云端厂商身份保留
    assert "120" in t and "300" in t  # p50/p95（ms）
    assert t.count("未计价") >= 1      # qwen3 无价格
    assert "0.0002" in t or "0.0002" in json.dumps(out["modelTable"])


def test_client_table_unidentified_claim_path(tmp_path):
    """客户端分解：未识别分组保留 + 认领按钮（data-claim 委托旧认领弹窗）；
    已识别客户端无按钮。"""
    agents = [_agent("alice", requests=3),
              _agent("unidentified", requests=1)]
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(agents=agents)}],
        "powers": [{"json": _power()}],
        "steps": _DEFAULT_SEED,
    })
    t = out["agentTable"]
    assert "alice" in t
    assert "未识别" in t or "待认领" in t
    assert "data-claim" in t          # 认领入口保留
    # 已识别行不出现第二个 data-claim（按行最小断言：alice 行无按钮）
    rows = t.split("<tr")
    alice_rows = [r for r in rows if "alice" in r]
    assert alice_rows and "data-claim" not in alice_rows[0]


# ── 电费卡 ───────────────────────────────────────────────────────

def test_power_gran_mapping_all_windows(tmp_path):
    """窗口→gran 映射：1h→hour、24h→day、7d→week、30d→month。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(win="24h")}, {"json": _report(win="1h")},
                    {"json": _report(win="30d")}],
        "powers": [{"json": _power(gran="day")}, {"json": _power(gran="hour")},
                   {"json": _power(gran="month")}],
        "steps": _DEFAULT_SEED + [{"click": "useWin-1h"}, {"click": "useWin-30d"}],
    })
    assert "gran=hour" in out["powerReqs"][1]
    assert "gran=month" in out["powerReqs"][2]


def test_power_cloud_cost_separate_sources_no_fake_sum(tmp_path):
    """云费（报告窗口）与电费（device_power 聚合区间）单列来源，不求和；
    桶缺测如实标注。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report(cost=0.0002)}],
        "powers": [{"json": _power(nbuckets=24, filled=20)}],
        "steps": _DEFAULT_SEED,
    })
    assert "0.0002" in out["cloudCost"] or "云费" in out["cloudCost"]
    assert "缺测" in out["powerNote"] or "20" in out["powerNote"]   # 24 桶仅 20 有样本
    # 电费来源标注：设备功耗估算，非整机电费
    assert ("估算" in out["powerNote"]) or ("设备" in out["powerNote"])


def test_power_unavailable_noted_not_zero(tmp_path):
    """device_power 无数据 → 覆盖不足/暂不可用，不冒认 ¥0。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report()}],
        "powers": [{"json": _power(available=False)}],
        "steps": _DEFAULT_SEED,
    })
    assert "¥0.00" not in out["powerTotals"]
    assert ("覆盖" in out["powerNote"]) or ("暂不可用" in out["powerNote"]) \
        or ("暂无" in out["powerNote"])


# ── 认领后刷新 ───────────────────────────────────────────────────

def test_claim_success_triggers_report_refresh(tmp_path):
    """认领成功（monitor.js 派发 iff-agents-changed）→ 报告重取，分解表换新。"""
    out = _run_harness(tmp_path, {
        "reports": [{"json": _report()},
                    {"json": _report(agents=[_agent("alice", requests=4)])}],
        "powers": [{"json": _power()}, {"json": _power()}],
        "steps": _DEFAULT_SEED + [{"agentsChanged": True}],
    })
    assert len(out["reportReqs"]) == 2
    assert "4" in out["agentTable"]
