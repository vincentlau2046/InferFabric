"""dashboard UX v3 B2 — 请求日志工作台行为契约（node vm，无网络/DOM）。

规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md B2 +
specs/dashboard-ux-v3/interface-contract.md §B。
被测对象：inferfabric/dashboard/js/requests.js（与真实 store.js/router.js 同装，
harness 结构对齐 test_router.py 的 _STORE_ROUTER_HARNESS：readyState='loading'
推迟 store 轮询，router 在脚本加载时即 boot，requests.js 靠 store.on 回放取数）。

契约要点（B1 window 端点消费方）：
- 深链过滤参数 → 控件态 + 恰一次取数；URL 与 store.route_params 单源
- 红色失败计数（total/failed_total）；列选择 iff_req_cols 持久化；协议列取消
- 分页用首次响应固定 until；Live 仅在可见+开启+第一页时轮询；离页停止
- 迟到响应按序失效（切筛选/离页不被覆盖）；加载/空/失败三态
- 池/非池展开（安全投影「—」）；异常关联标注「按模型/时间推断」
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "inferfabric" / "dashboard" / "js"
REQUESTS_JS = JS / "requests.js"
STORE_JS = JS / "store.js"
ROUTER_JS = JS / "router.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

// ── localStorage / location / hashchange 桩（与 test_router.py 组合 harness 同款）──
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
let hashListener = null;
const location = {
  get hash() { return hashValue; },
  set hash(v) { if (v === hashValue) return; hashValue = v; if (hashListener) hashListener(); },
  replace(url) { const i = url.indexOf('#'); hashValue = i >= 0 ? url.slice(i) : ''; },
};

// ── 元素桩：已知壳节点 + 未知 id 自动补建（requests.js 工作台控件） ──
function makeEl(id, attrs) {
  const classes = new Set();
  const listeners = {};
  const attribs = Object.assign({}, attrs || {});
  const el = {
    id, _classes: classes, _attrs: attribs,
    value: '', disabled: false, hidden: false, title: '',
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
for (const v of ['status', 'requests', 'usage', 'routing', 'models', 'config']) {
  panels['tab-' + v] = makeEl('tab-' + v);
}
const configSections = ['cloud', 'auth'].map((s) => makeEl('sub-' + s, { 'data-sub': s }));
panels['tab-config'].querySelectorAll = (sel) => sel === '[data-sub]' ? configSections : [];
const btnStatus = makeEl('nav-status', { 'data-tab': 'tab-status' });
const btnRequests = makeEl('nav-requests', { 'data-tab': 'tab-requests' });
const navButtons = [btnStatus, btnRequests];
const els = {
  'modeBadge': makeEl('modeBadge'), 'syncMeta': makeEl('syncMeta'), 'navVer': makeEl('navVer'),
  'apiErrorBanner': makeEl('apiErrorBanner'), 'switchOverlay': makeEl('switchOverlay'),
  'switchOverlayMsg': makeEl('switchOverlayMsg'), 'sidebarStatusDot': makeEl('sidebarStatusDot'),
  'sidebarSyncTxt': makeEl('sidebarSyncTxt'), 'themeToggle': makeEl('themeToggle'),
};
const document = {
  readyState: 'loading',   // 推迟 store 自身 boot 与轮询（避免定时器干扰）
  documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
  getElementById: (id) => panels[id] || els[id] || el(id),
  querySelectorAll: (sel) => {
    if (sel === '.tab-content') return Object.values(panels);
    if (sel === '.if-nav-item') return navButtons;
    return [];
  },
  querySelector: (sel) => {
    const m = /data-tab="([^"]+)"/.exec(sel);
    return m ? (navButtons.find((b) => b._attrs['data-tab'] === m[1]) || null) : null;
  },
  addEventListener: () => {}, removeEventListener: () => {},
  body: { classList: { toggle: () => {} }, appendChild: () => {} },
};
const window = {
  addEventListener: (ev, fn) => { if (ev === 'hashchange') hashListener = fn; },
};

// ── 可编程 fetch：/api/request_log 走脚本队列，其余（snapshot）计数放行 ──
const requests = [];
let snapshotFetches = 0;
const pending = [];            // {resolve, spec}
const queue = (input.responses || []).slice();
function respond(spec) {
  if (spec.error) return Promise.reject(new Error('fetch fail'));
  return Promise.resolve({ ok: true, status: 200, json: async () => spec.json });
}
const fetch = (url) => {
  const u = String(url);
  if (u.indexOf('/api/request_log') !== 0) {
    snapshotFetches += 1;
    return respond({ json: input.snapshotJson || {} });
  }
  requests.push(u);
  const spec = queue.shift();
  if (!spec) return Promise.reject(new Error('no scripted response'));
  if (spec.wait) return new Promise((res) => pending.push({ resolve: res, spec }));
  return respond(spec);
};

const intervals = [];
const ctx = {
  window, document, location, localStorage: storage, console,
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval: (id) => { if (intervals[id - 1]) intervals[id - 1].dead = true; },
  fetch,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);   // store.js
vm.runInNewContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);   // router.js（加载即 boot）
assert.ok(window.store && window.ifRouter, 'store and router must load');
if (!fs.existsSync(process.argv[4])) { console.error('requests.js missing'); process.exit(3); }
vm.runInNewContext(fs.readFileSync(process.argv[4], 'utf8'), ctx);   // requests.js
assert.ok(window.ifRequests, 'requests.js must expose window.ifRequests');

const drain = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

async function run() {
await drain();   // 先泵掉装载期取数（浏览器语义：用户点击必发生在首渲之后）
for (const step of (input.steps || [])) {
  if (step.hashChange) { hashValue = step.hashChange; if (hashListener) hashListener(); }
  if (step.seedStore) for (const [k, v] of Object.entries(step.seedStore)) window.store.set(k, v);
  if (step.setValue) { el(step.setValue[0]).value = step.setValue[1]; el(step.setValue[0])._fire('change'); }
  if (step.fire) el(step.fire[0])._fire(step.fire[1]);
  if (step.click) el(step.click)._fire('click');
  if (step.expand) window.ifRequests.expand(step.expand);
  if (step.flush !== undefined) {
    const idx = step.flush;
    assert.ok(pending[idx], 'no pending fetch #' + idx);
    pending[idx].done = true;
    pending[idx].resolve({ ok: true, status: 200, json: async () => pending[idx].spec.json });
  }
  if (step.advanceTimers) {
    for (let i = 0; i < step.advanceTimers; i++)
      for (const t of intervals) if (!t.dead && t.ms === (step.timerMs || 5000)) t.fn();
  }
  await drain();   // 每步后泵微任务（auto-resolve fetch / 状态落地）
}
await drain();       // 无 steps 场景同样泵到位再取快照
const reqTable = el('reqTable');
const out = {
  requests, snapshotFetches, pendingCount: pending.filter((p) => !p.done).length,
  tableHTML: reqTable.innerHTML,
  countHTML: el('reqCountLine').innerHTML,
  pageInfo: el('reqPageInfo').textContent,
  prevDisabled: el('reqPrev').disabled, nextDisabled: el('reqNext').disabled,
  liveOn: el('reqLiveBtn').classList.contains('on'),
  windowOn: ['1h', '24h', '7d'].filter((w) => el('reqWin-' + w).classList.contains('on')),
  statusValue: el('reqStatus').value, modelValue: el('reqModel').value,
  searchValue: el('reqSearch').value,
  modelOptions: (el('reqModel').innerHTML.match(/<option value="([^"]*)"/g) || [])
    .map((s) => /<option value="([^"]*)"/.exec(s)[1]),
  colsMenuHTML: el('reqColsMenu').innerHTML,
  cols: (() => { try { return JSON.parse(storage.getItem('iff_req_cols')); } catch (e) { return null; } })(),
  hash: hashValue,
  liveMs: intervals.filter((t) => !t.dead).map((t) => t.ms),
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
    for p in (STORE_JS, ROUTER_JS):
        if not p.exists():
            pytest.fail(f"{p.name} missing")
    if not REQUESTS_JS.exists():
        pytest.fail("requests.js missing (B2 not implemented)")
    harness = tmp_path / "requests_panel_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(STORE_JS), str(ROUTER_JS), str(REQUESTS_JS)],
        input=json.dumps(payload).encode(), capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"harness failed rc={proc.returncode}\nSTDERR:{proc.stderr.decode()[-2000:]}")
    return json.loads(proc.stdout.decode())


def _log_row(req_id, ts, model="m1", status=200, pool=None, agent="claude-code"):
    return {"req_id": req_id, "timestamp": ts, "model": model, "status": status,
            "tokens_in": 100, "tokens_in_cached": 50, "tokens_out": 20,
            "ttft_ms": 120.0, "duration_ms": 900.0, "route": "local",
            "key_name": "k", "error": "" if status < 400 else "upstream 5xx",
            "agent": agent, "ua": "ua/1", "pool": pool}


_RESP_OK = {"json": {"logs": [
    _log_row("iff-1", 1700000000.0, pool={
        "name": "web", "route_kind": "local", "target_port": None,
        "retry_count": 2, "fallback_count": 1}),
    _log_row("iff-2", 1699999900.0, model="m2", status=503, pool=None)],
    "count": 2, "total": 1284, "failed_total": 37, "until": 1000.0}}


# ── 深链与控件态 ─────────────────────────────────────────────────

def test_deep_link_filters_applied_once(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests?window=1h&status=4xx&model=m1",
        "responses": [_RESP_OK],
        "steps": [{"seedStore": {"local_models": ["m1", "m2"]}}],
    })
    assert len(out["requests"]) == 1, out["requests"]
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(out["requests"][0]).query)
    assert q["window"] == ["1h"] and q["status"] == ["4xx"] and q["model"] == ["m1"]
    assert q["limit"] == ["50"] and q["offset"] == ["0"]
    assert out["windowOn"] == ["1h"]
    assert out["statusValue"] == "4xx" and out["modelValue"] == "m1"
    assert "m1" in out["modelOptions"] and "m2" in out["modelOptions"]
    assert "1,284" in out["countHTML"] and "37" in out["countHTML"]


# ── 计数、三态与迟到响应 ─────────────────────────────────────────

def test_failed_counter_and_three_states(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [{"wait": True, "json": {"logs": [], "count": 0,
                                               "total": 0, "failed_total": 0,
                                               "until": 5.0}}],
        "steps": [],
    })
    assert "skeleton" in out["tableHTML"] or "加载" in out["tableHTML"]   # 加载态
    out2 = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [{"json": {"logs": [], "count": 0, "total": 0,
                                "failed_total": 0, "until": 5.0}}],
        "steps": [],
    })
    assert "暂无" in out2["tableHTML"]                                    # 空态
    out3 = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [{"error": True}],
        "steps": [],
    })
    assert "读取失败" in out3["tableHTML"]                                # 失败态


def test_late_response_discarded_on_filter_change(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [
            {"wait": True, "json": {"logs": [_log_row("stale-1", 1.0, model="old")],
                                    "count": 1, "total": 1, "failed_total": 0,
                                    "until": 1.0}},
            {"wait": True, "json": {"logs": [_log_row("fresh-1", 2.0, model="new")],
                                    "count": 1, "total": 1, "failed_total": 0,
                                    "until": 2.0}},
        ],
        "steps": [
            {"setValue": ["reqStatus", "2xx"]},   # 触发新取数（#1 在途）
            {"flush": 0},                          # 旧响应迟到 → 不得覆盖
            {"flush": 1},                          # 新响应落地
        ],
    })
    assert len(out["requests"]) == 2
    assert "stale-1" not in out["tableHTML"]
    assert "fresh-1" in out["tableHTML"]


# ── 分页与固定 until ─────────────────────────────────────────────

def test_pagination_uses_fixed_until(tmp_path):
    logs = [_log_row(f"p{i}", 1700000000.0 - i) for i in range(50)]
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [
            {"json": {"logs": logs, "count": 50, "total": 120,
                      "failed_total": 5, "until": 1000.0}},
            {"json": {"logs": logs, "count": 50, "total": 120,
                      "failed_total": 5, "until": 1000.0}},
            {"json": {"logs": logs[:20], "count": 20, "total": 120,
                      "failed_total": 5, "until": 1000.0}},
        ],
        "steps": [{"click": "reqNext"}, {"click": "reqNext"}],
    })
    assert "until=1000" in out["requests"][1]      # 固定 until，不是服务端当前时刻
    assert "offset=50" in out["requests"][1]
    assert "offset=100" in out["requests"][2]
    assert "until=1000" in out["requests"][2]      # 深页继续沿用同一锚
    assert "3 / 3" in out["pageInfo"]
    assert out["prevDisabled"] is False
    assert out["nextDisabled"] is True             # 第 3/3 页 → 下一页禁用


def test_red_failed_counter_marked(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [],
    })
    assert "37" in out["countHTML"]
    assert ("bad" in out["countHTML"]) or ("crit" in out["countHTML"])


# ── 列选择 ───────────────────────────────────────────────────────

def test_column_toggle_persists_no_protocol_column(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"click": "reqColsBtn"},
                  {"fire": ["reqCol-cache", "click"]}],
    })
    assert "协议" not in out["tableHTML"]
    for col in ("时间", "状态", "模型", "客户端", "Tokens"):
        assert col in out["tableHTML"]
    assert out["cols"] is not None and "cache" not in out["cols"]
    assert "缓存" not in out["tableHTML"].split("<tbody>")[0]


def test_columns_persist_across_boot(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "storage": {"data": {"iff_req_cols": json.dumps(["time", "status", "model"])}},
        "responses": [_RESP_OK],
        "steps": [],
    })
    head = out["tableHTML"].split("<tbody>")[0]
    assert "时间" in head and "缓存" not in head and "客户端" not in head


# ── 池展开与异常关联 ─────────────────────────────────────────────

def test_pool_and_nonpool_expansion(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"expand": "iff-1"}],
    })
    assert "web" in out["tableHTML"] and "重试" in out["tableHTML"]
    assert "—" in out["tableHTML"]                      # target_port 「—」
    out2 = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"expand": "iff-2"}],
    })
    assert "iff-2" in out2["tableHTML"]


def test_anomaly_correlation_labeled(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"seedStore": {"anomalies": [
                      {"id": "a1", "ts": 1700000000.0, "model": "m2",
                       "status_code": 503, "severity": "error",
                       "message": "上游 503：引擎过载"}]}},
                  {"expand": "iff-2"}],
    })
    assert "按模型/时间推断" in out["tableHTML"]
    assert "上游 503" in out["tableHTML"]


# ── Live 门控与离页 ──────────────────────────────────────────────

def test_live_polling_gated_by_visibility_and_toggle(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK,
                      {"json": {"logs": [], "count": 0, "total": 0,
                                "failed_total": 0, "until": 1.0}},
                      {"json": {"logs": [], "count": 0, "total": 0,
                                "failed_total": 0, "until": 1.0}}],
        "steps": [{"advanceTimers": 1},          # Live 开 + 可见 + 第一页 → 轮询
                  {"click": "reqLiveBtn"},       # 关 Live
                  {"advanceTimers": 1},          # 不取数
                  {"click": "reqLiveBtn"},       # 再开
                  {"advanceTimers": 1}],         # 恢复轮询
    })
    assert out["liveMs"] == [5000]
    assert len(out["requests"]) == 3, out["requests"]
    assert out["liveOn"] is True
    # 深页不轮询（Live 只服务第一页；深页保持固定 until）
    out2 = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [
            {"json": {"logs": [_log_row(f"p{i}", 1.0 + i) for i in range(50)],
                      "count": 50, "total": 120, "failed_total": 0, "until": 9.0}},
            {"json": {"logs": [_log_row(f"q{i}", 1.0 + i) for i in range(50)],
                      "count": 50, "total": 120, "failed_total": 0, "until": 9.0}},
        ],
        "steps": [{"click": "reqNext"}, {"advanceTimers": 1}],
    })
    assert len(out2["requests"]) == 2               # tick 未加新请求


def test_leaving_page_stops_log_requests(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"hashChange": "#/status"},
                  {"advanceTimers": 1}],
    })
    assert len(out["requests"]) == 1                 # 离开请求页 → 不再轮询
    assert out["liveMs"] == []                       # 工作台定时器已停


def test_switches_subpage_stops_workbench_polling(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [_RESP_OK],
        "steps": [{"hashChange": "#/requests/switches"},
                  {"advanceTimers": 1}],
    })
    assert len(out["requests"]) == 1                 # 离开请求主子页 → 不再轮询


def test_late_response_discarded_after_leaving(tmp_path):
    out = _run_harness(tmp_path, {
        "hash": "#/requests",
        "responses": [{"wait": True, "json": _RESP_OK["json"]}],
        "steps": [{"hashChange": "#/status"}, {"flush": 0}],
    })
    assert out["pendingCount"] == 0                  # 在途请求已消费但…
    assert "iff-1" not in out["tableHTML"]           # …迟到响应不得写入已离开页
