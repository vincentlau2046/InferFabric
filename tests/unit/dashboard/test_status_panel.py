"""dashboard UX v3 C2 — 状态页行为契约（node vm，无网络/DOM）。

规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md C 节 +
specs/dashboard-ux-v3/interface-contract.md §C。
被测对象：inferfabric/dashboard/js/overview.js（状态页 renderer）+ store.js
（采样年龄跟踪）+ inference.js（模型操作严格结果判定），与真实 ui.js 同装。

契约要点（C 期补充裁决：平台适配层供数、UI 按能力消费、不新增 Mac GPU 探针）：
- 遥测按 device.snapshot/capabilities 消费：memory_kind 决定标签、capabilities
  门控行卡、真 0 照实显示、null「暂不可用」、不按 OS 名写死版式
- 采样年龄：X-IFF-Device-Sample-Age 200/304 均消费，本地累加；>30s 过期、
  无头「新鲜度未确认」；不由轮询频率反推
- 态势：UTC 当天请求数、已保留警告、最近五条不冒充总数、空闲快捷项取历史
- 操作严格结果判定：200 中 status:error / failed 不报成功；浏览器时间不冒充
  模型运行时长；sys.uptime_seconds 不挂「网关运行时长」
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "inferfabric" / "dashboard" / "js"
UI_JS = JS / "ui.js"
STORE_JS = JS / "store.js"
OVERVIEW_JS = JS / "overview.js"
INFERENCE_JS = JS / "inference.js"

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

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

function makeEl(id, attrs) {
  const classes = new Set();
  const listeners = {};
  const attribs = Object.assign({}, attrs || {});
  const children = {};   // querySelector 子桩：卡片体等内部节点可写可断言
  const el = {
    id, _classes: classes, _attrs: attribs,
    value: '', disabled: false, hidden: false, title: '', checked: false,
    innerHTML: '', textContent: '',
    get className() { return Array.from(classes).join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
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
    querySelector: (sel) => { if (!children[sel]) children[sel] = makeEl(id + '::' + sel); return children[sel]; },
    querySelectorAll: () => [],
    closest: () => null,
    focus: () => {}, appendChild: () => {},
    _fire(type, ev) {
      const base = { target: el, preventDefault: () => {}, stopPropagation: () => {} };
      (listeners[type] || []).forEach((fn) => fn(ev ? Object.assign(base, ev) : base));
    },
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
const els = {
  'modeBadge': makeEl('modeBadge'), 'syncMeta': makeEl('syncMeta'), 'navVer': makeEl('navVer'),
  'apiErrorBanner': makeEl('apiErrorBanner'), 'switchOverlay': makeEl('switchOverlay'),
  'switchOverlayMsg': makeEl('switchOverlayMsg'), 'sidebarStatusDot': makeEl('sidebarStatusDot'),
  'sidebarSyncTxt': makeEl('sidebarSyncTxt'), 'themeToggle': makeEl('themeToggle'),
  'toast': makeEl('toast'),
};
const document = {
  readyState: 'loading',
  documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
  getElementById: (id) => (id === 'confirmModal') ? null : (panels[id] || els[id] || el(id)),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {}, removeEventListener: () => {},
  body: { classList: { toggle: () => {} }, appendChild: () => {} },
};
const window = {
  addEventListener: (ev, fn) => { if (ev === 'hashchange') hashListener = fn; },
  confirm: () => true,   // 无 #confirmModal 容器 → 原生确认桩（直接放行）
  location: location,
};
window.window = window;

// ── 可编程 fetch：snapshot 队列（带响应头）、anomalies 按 severity 分流、
// POST ops 按路径前缀脚本 ──
let snapshotFetches = 0, warnFetches = 0, anomFetches = 0;
const warnUrls = [];
const postCalls = [];
// 7.12.20: 可挂起响应（wait:true → 由 flush 步骤按序放行），用于乱序 304 用例
const pendingFns = [];
const startedFetches = [];
const captures = [];
function mkRes(spec) {
  const hmap = {};
  for (const [k, v] of Object.entries(spec.headers || {})) hmap[k.toLowerCase()] = v;
  const status = spec.status || 200;
  return {
    ok: status < 400, status,
    headers: { get: (n) => (hmap[String(n).toLowerCase()] !== undefined ? hmap[String(n).toLowerCase()] : null) },
    json: async () => {
      if (spec.text != null) throw new SyntaxError('Unexpected token in JSON');   // 不可解析响应体
      return spec.json;
    },
  };
}
const snapQueue = (input.snapshotResponses || []).slice();
const postQueue = {};
for (const [k, v] of Object.entries(input.postResponses || {})) {
  postQueue[k] = Array.isArray(v) ? v.slice() : [v];
}
const fetch = (url) => {
  const u = String(url);
  if (u.indexOf('/api/snapshot') === 0) {
    snapshotFetches += 1;
    const spec = snapQueue.length ? snapQueue.shift()
      : (input.snapshotJson ? { json: input.snapshotJson } : {});
    if (spec.wait) {
      return new Promise((resolve) => { pendingFns.push(() => resolve(mkRes(spec))); });
    }
    return Promise.resolve(mkRes(spec));
  }
  if (u.indexOf('/api/anomalies') === 0) {
    if (u.indexOf('severity=warning') >= 0) { warnFetches += 1; warnUrls.push(u); return Promise.resolve(mkRes({ json: input.anomaliesWarnJson || { events: [] } })); }
    anomFetches += 1;
    return Promise.resolve(mkRes({ json: input.anomaliesJson || { events: [] } }));
  }
  for (const key of Object.keys(postQueue)) {
    if (u === '/' + key || u.indexOf('/' + key + '?') === 0) {
      postCalls.push(u);
      const spec = postQueue[key].shift();
      if (!spec) return Promise.reject(new Error('no scripted POST for ' + u));
      return Promise.resolve(mkRes(spec));
    }
  }
  postCalls.push(u);
  return Promise.resolve(mkRes({ json: {} }));
};

const intervals = [];
const ctx = {
  window, document, location, localStorage: storage, console,
  setTimeout: () => 0, clearTimeout: () => {},
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval: (id) => { if (intervals[id - 1]) intervals[id - 1].dead = true; },
  fetch,
  Date, Math, JSON, Promise, Number, Object, Array, String, isNaN, parseInt, parseFloat,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);   // ui.js
vm.runInNewContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);   // store.js
assert.ok(window.store && window.UI, 'store and UI must load');
window.tabRenderers = window.tabRenderers || {};
vm.runInNewContext(fs.readFileSync(process.argv[4], 'utf8'), ctx);   // overview.js
assert.ok(window.tabRenderers['tab-overview'], 'overview renderer must register');
vm.runInNewContext(fs.readFileSync(process.argv[5], 'utf8'), ctx);   // inference.js
assert.ok(window.doModelAction, 'doModelAction must expose');

const drain = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

async function run() {
  for (const step of (input.steps || [])) {
    if (step.seedStore) for (const [k, v] of Object.entries(step.seedStore)) window.store.set(k, v);
    if (step.fetchSnapshot) await window.store.fetchSnapshot(!!step.force);
    if (step.startFetch) startedFetches.push(window.store.fetchSnapshot(!!step.force));
    if (step.flush) {
      await drain();
      const n = (typeof step.flush === 'number') ? step.flush : pendingFns.length;
      pendingFns.splice(0, n).forEach((f) => f());
      await drain(20);
    }
    if (step.capture) {
      captures.push({
        syncMeta: window.store.get('sync_meta') || null,
        deviceSample: window.store.get('device_sample') || null,
        apiError: window.store.get('api_error') || null,
      });
    }
    if (step.renderOverview) window.tabRenderers['tab-overview']();
    if (step.ops) await window.doSystemOps(step.ops);
    if (step.modelAction) await window.doModelAction(step.modelAction[0], step.modelAction[1]);
    if (step.tick) {
      for (let i = 0; i < intervals.length; i++) {
        const iv = intervals[i];
        if (!iv.dead && iv.ms <= step.tick) iv.fn();
      }
    }
    if (step.click) {
      // 事件委托模拟：在指定卡片上触发 click，target = 带 data-action 的按钮桩
      const card = el(step.click.card);
      const btn = makeEl(step.click.card + '::btn', step.click.attrs || {});
      btn.closest = (sel) => (sel.indexOf('data-action') >= 0 ? btn : null);
      card.contains = () => true;
      card._fire('click', { target: btn });
    }
    await drain();
  }
  await drain();
  const segDisp = (id) => { const s = el(id); return s.style.display === undefined ? 'MISSING' : s.style.display; };
  const sparkBody = el('ovSpark24h').querySelector('.if-card-body');
  const memSeg = el('trSegMem');
  const out = {
    teleHTML: el('ovTeleBody').innerHTML,
    teleFresh: el('ovTeleFresh').textContent,
    sitHTML: el('ovSitBody').innerHTML + '|' + el('ovSwitchLine').textContent,
    reqToday: el('ovReqToday') ? el('ovReqToday').textContent : null,
    warnCount: el('ovWarnCount') ? el('ovWarnCount').textContent : null,
    recent5: el('ovRecent5') ? el('ovRecent5').innerHTML : null,
    shortcuts: el('ovIdleShortcuts') ? el('ovIdleShortcuts').innerHTML : null,
    activeHTML: el('ovActiveCard').innerHTML + el('ovActiveBody').innerHTML,
    toastText: els['toast'].textContent,
    toastClass: els['toast'].className,
    deviceSample: window.store.get('device_sample') || null,
    sampleAge: (typeof window.store.deviceSampleAge === 'function') ? window.store.deviceSampleAge() : 'NO_METHOD',
    etag: window.store._etag === undefined ? 'NO_FIELD' : window.store._etag,
    syncMeta: window.store.get('sync_meta') || null,
    apiError: window.store.get('api_error') || null,
    tabActive: window.store.get('tab_active') || null,
    rail: {
      vram: el('trVram').textContent,
      memLabel: memSeg.querySelector('.tr-label').textContent,
      memUnit: memSeg.querySelector('.tr-unit').textContent,
      fillWidth: el('trVramFill').style.width,
      segMem: segDisp('trSegMem'),
      segUtil: segDisp('trSegUtil'), segPwr: segDisp('trSegPwr'),
      segTemp: segDisp('trSegTemp'), segClock: segDisp('trSegClock'),
    },
    spark: sparkBody.innerHTML,
    postCalls, snapshotFetches, warnFetches, anomFetches, warnUrls,
    intervalCount: intervals.length,
  };
  out.captures = captures;
  console.log(JSON.stringify(out));
}
run().catch((e) => { console.error(e && e.stack || e); process.exitCode = 1; });
"""


def _run_harness(tmp_path, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for p in (UI_JS, STORE_JS, OVERVIEW_JS, INFERENCE_JS):
        if not p.exists():
            pytest.fail(f"{p.name} missing")
    harness = tmp_path / "status_panel_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(UI_JS), str(STORE_JS), str(OVERVIEW_JS), str(INFERENCE_JS)],
        input=json.dumps(payload).encode(), capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"harness failed rc={proc.returncode}\nSTDERR:{proc.stderr.decode()[-2000:]}")
    return json.loads(proc.stdout.decode())


def _apple_dev(**over):
    snap = {
        "platform": "darwin", "accelerator": "apple_silicon", "memory_kind": "unified",
        "memory_used_mb": 12345, "memory_total_mb": 32768, "memory_budget_mb": 24576,
        "utilization_pct": None, "power_w": None, "temperature_c": None,
        "clock_mhz": None, "pressure_level": 1, "timestamp": 1764300000.0,
        "sources": {"memory_used_mb": "vm_stat"},
    }
    snap.update(over)
    caps = {"memory": True, "utilization": False, "power": False,
            "temperature": False, "clock": False, "pressure": True, "reset": False}
    return {"snapshot": snap, "capabilities": caps}


def _nvidia_dev(**over):
    snap = {
        "platform": "linux", "accelerator": "nvidia", "memory_kind": "vram",
        "memory_used_mb": 8192, "memory_total_mb": 32768, "memory_budget_mb": 28672,
        "utilization_pct": 12.0, "power_w": 96.0, "temperature_c": 48,
        "clock_mhz": 2595, "pressure_level": None, "timestamp": 1764300000.0,
        "sources": {"memory_used_mb": "nvidia-smi"},
    }
    snap.update(over)
    caps = {"memory": True, "utilization": True, "power": True,
            "temperature": True, "clock": True, "pressure": False, "reset": True}
    return {"snapshot": snap, "capabilities": caps}


# ── 遥测卡：按能力消费（契约 §C1）─────────────────────────────

def test_apple_unified_memory_pressure_budget(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _apple_dev()}, "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "统一内存" in html, html
    assert "12.1" in html and "32.0" in html          # used/total GB（可显示 GiB）
    assert "推理预算" in html and "24.0" in html       # 预算单列
    assert "正常" in html                              # pressure_level 1
    # capabilities 未声明的 GPU 指标不占默认卡片
    for absent in ("利用率", "功耗", "温度", "频率"):
        assert absent not in html, f"{absent} 不应出现（capabilities 门控）"
    assert "GPU 在线" not in html


def test_nvidia_five_metrics_no_pressure_row(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _nvidia_dev()}, "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "VRAM" in html
    for label in ("利用率", "功耗", "温度", "频率"):
        assert label in html, f"{label} 行应在（NVIDIA 五项）"
    assert "12" in html and "96" in html and "48" in html and "2,595" in html
    assert "压力" not in html                          # 此后端不支持 pressure


def test_zero_is_valid_reading(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _nvidia_dev(utilization_pct=0, power_w=0)},
                   "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "暂不可用" not in html
    assert "0" in html   # 真 0 照实显示（利用率 0% / 功耗 0W）


def test_partial_null_shows_unavailable(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _nvidia_dev(utilization_pct=None, power_w=None)},
                   "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "暂不可用" in html                          # null → 暂不可用
    assert "8.0" in html or "8192" in html             # 其他项继续显示


def test_nvidia_no_driver_not_online(tmp_path):
    """Linux 选到 NVIDIA 但无驱动/读数：accelerator=nvidia + 全 null →
    不得显示「GPU 在线」，各项暂不可用。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _nvidia_dev(
            memory_used_mb=None, memory_total_mb=None, memory_budget_mb=None,
            utilization_pct=None, power_w=None, temperature_c=None, clock_mhz=None)},
            "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "在线" not in html
    assert "暂不可用" in html


def test_all_unsupported_no_default_cards(tmp_path):
    caps = {"memory": False, "utilization": False, "power": False,
            "temperature": False, "clock": False, "pressure": False, "reset": False}
    dev = _apple_dev(memory_used_mb=None, memory_total_mb=None,
                     memory_budget_mb=None, pressure_level=None)
    dev["capabilities"] = caps
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": dev}, "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "暂无" in html or "不支持" in html or "未声明" in html   # 健康提示
    assert "统一内存" not in html and "VRAM" not in html            # 无猜硬件


def test_legacy_response_no_device(tmp_path):
    """无 device（旧接口或资料未知）：健康提示 + 不猜硬件 + 不崩溃。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": None}, "renderOverview": True}],
    })
    html = out["teleHTML"]
    assert "不可用" in html or "未知" in html or "暂无" in html
    assert "统一内存" not in html and "VRAM" not in html
    assert "12.1" not in html


def test_layout_by_capabilities_not_os_name(tmp_path):
    """不按 OS 名写死版式：darwin + utilization 能力 → 显示利用率行；
    linux + 无能力 → 不显示。"""
    dev = _apple_dev()
    dev["capabilities"] = dict(dev["capabilities"], utilization=True)
    dev["snapshot"]["utilization_pct"] = 33.0
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": dev}, "renderOverview": True}],
    })
    assert "利用率" in out["teleHTML"]

    dev2 = _nvidia_dev(utilization_pct=None, power_w=None, temperature_c=None,
                       clock_mhz=None, memory_used_mb=None, memory_total_mb=None,
                       memory_budget_mb=None)
    dev2["capabilities"] = {"memory": False, "utilization": False, "power": False,
                            "temperature": False, "clock": False,
                            "pressure": False, "reset": True}
    out2 = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": dev2}, "renderOverview": True}],
    })
    assert "利用率" not in out2["teleHTML"]


# ── 采样年龄（契约 §C2：三时钟分开）──────────────────────────

def test_sample_age_from_header_on_200(tmp_path):
    out = _run_harness(tmp_path, {
        "snapshotResponses": [{"json": {}, "headers": {"X-IFF-Device-Sample-Age": "42"}}],
        "steps": [{"fetchSnapshot": True}],
    })
    assert out["deviceSample"] and out["deviceSample"].get("age") == 42
    assert isinstance(out["sampleAge"], (int, float)) and 42 <= out["sampleAge"] <= 43


def test_sample_age_updates_on_304(tmp_path):
    out = _run_harness(tmp_path, {
        "snapshotResponses": [
            {"json": {}, "headers": {"X-IFF-Device-Sample-Age": "10"}},
            {"status": 304, "headers": {"X-IFF-Device-Sample-Age": "45"}},
        ],
        "steps": [{"fetchSnapshot": True}, {"fetchSnapshot": True}],
    })
    assert out["deviceSample"]["age"] == 45, "304 的年龄头必须更新（不假定 304 即新采样）"
    assert 45 <= out["sampleAge"] <= 46


def test_sample_age_unconfirmed_without_header(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"fetchSnapshot": True}],
    })
    assert out["deviceSample"] is None or out["deviceSample"].get("age") is None
    assert out["sampleAge"] is None


def test_stale_badge_over_30s(tmp_path):
    out = _run_harness(tmp_path, {
        "snapshotResponses": [{"json": {}, "headers": {"X-IFF-Device-Sample-Age": "99"}}],
        "steps": [{"fetchSnapshot": True}, {"seedStore": {"device": _apple_dev()},
                                             "renderOverview": True}],
    })
    assert "已过期" in out["teleFresh"], out["teleFresh"]


def test_unconfirmed_badge_without_header(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"fetchSnapshot": True}, {"seedStore": {"device": _apple_dev()},
                                             "renderOverview": True}],
    })
    assert "未确认" in out["teleFresh"], out["teleFresh"]


# ── 态势（契约 §C3）──────────────────────────────────────────

def _token_stats(today, yesterday=None):
    def bucket(n):
        return {"m1": {"requests": n}}
    local = {}
    cloud = {}
    if today is not None:
        local[today] = bucket(3)
        cloud[today] = bucket(4)
    if yesterday is not None:
        local[yesterday] = bucket(50)
    return {"local": local, "cloud": cloud}


def test_utc_today_request_count(tmp_path):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = "2020-01-01"
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"token_stats": _token_stats(today, yesterday)},
                   "renderOverview": True}],
    })
    assert out["reqToday"] is not None
    assert "7" in out["reqToday"], out["reqToday"]      # local 3 + cloud 4
    assert "UTC" in out["sitHTML"], "界面须注明 UTC 当天"
    assert "50" not in out["reqToday"], "昨日桶不得冒充今日"


def test_utc_today_zero_when_no_bucket(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"token_stats": _token_stats(None, "2020-01-01")},
                   "renderOverview": True}],
    })
    assert "0" in (out["reqToday"] or ""), out["reqToday"]   # 真 0 是有效读数
    assert "50" not in (out["reqToday"] or "")


def test_retained_warning_count(tmp_path):
    out = _run_harness(tmp_path, {
        "anomaliesWarnJson": {"events": [{"id": 1, "severity": "warning"},
                                          {"id": 2, "severity": "warning"}], "count": 2},
        "steps": [{"renderOverview": True}],
    })
    assert out["warnFetches"] >= 1
    assert "2" in (out["warnCount"] or ""), out["warnCount"]
    assert "已保留" in (out["warnCount"] or "") or "已保留" in out["sitHTML"]


def test_recent_five_not_total(tmp_path):
    rows = [{"timestamp": 1764300000.0 + i, "model": f"m{i}", "status": 200,
             "tokens_in": 1, "tokens_out": 1, "route": "local"}
            for i in range(7)]
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"request_log": rows}, "renderOverview": True}],
    })
    html = out["recent5"] or ""
    assert html.count("ov-req-row") == 5, f"最近五条 = 恰 5 行（got {html.count('ov-req-row')}）"
    assert "m6" not in html
    assert "5" in (out["sitHTML"] or ""), "最近五条计数应注明只统计所显示条数"


def test_idle_shortcuts_from_history(tmp_path):
    # db.py:296 真实行形状 = {timestamp, from, to, duration, status}
    history = [
        {"timestamp": 3, "from": "idle", "to": "alpha", "duration": 1.0, "status": "ok"},
        {"timestamp": 2, "from": "idle", "to": "beta", "duration": 1.0, "status": "ok"},
        {"timestamp": 1, "from": "idle", "to": "alpha", "duration": 1.0, "status": "ok"},
    ]
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"active_services": [], "history": history,
                                 "models": [{"name": "alpha"}, {"name": "beta"}]},
                   "renderOverview": True}],
    })
    html = out["shortcuts"] or ""
    assert "alpha" in html and "beta" in html
    assert html.count('data-model="alpha"') == 1, "去重（最近三个不同模型）"


def test_idle_no_history_no_crash(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"active_services": [], "history": []},
                   "renderOverview": True}],
    })
    assert out["shortcuts"] is not None          # 渲染了容器，无崩溃


def test_switching_state_shown(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"switch_target": "gamma", "active_services": []},
                   "renderOverview": True}],
    })
    assert "切换" in (out["sitHTML"] or "") and "gamma" in (out["sitHTML"] or "")


# ── 严格结果判定 + 时长诚实（契约 §C3）───────────────────────

def test_reload_partial_failure_reports_partial(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"reload-config": {"json": {"status": "reloaded",
                                                      "failed": ["a.yaml", "b.yaml"]}}},
        "steps": [{"ops": "reload"}],
    })
    assert "部分" in out["toastText"], out["toastText"]
    assert out["toastClass"].find("error") >= 0
    assert "已重载" not in out["toastText"] and "配置已重载" not in out["toastText"]


def test_sleep_200_status_error_not_success(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"sleep": {"json": {"status": "error", "message": "模型未运行"}}},
        "steps": [{"modelAction": ["m1", "sleep"]}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "休眠中" not in out["toastText"]


def test_wake_200_status_error_not_success(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"wake": {"json": {"status": "error", "message": "无法唤醒"}}},
        "steps": [{"modelAction": ["m1", "wake"]}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "唤醒中" not in out["toastText"]


def test_reconcile_200_status_error_not_success(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"reconcile": {"json": {"status": "error", "message": "对账失败"}}},
        "steps": [{"ops": "reconcile"}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "已重新校准" not in out["toastText"]


def test_browser_uptime_not_shown_as_model_runtime(tmp_path):
    """C3：无可信启动时刻不把浏览器打开时间算作模型运行时长。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {
            "active_services": ["m1"],
            "services_info": {"m1": {"type": "vllm", "port": 8000}},
            "status": {"active_services": ["m1"], "services_info": {"m1": {"type": "vllm", "port": 8000}}},
        }, "renderOverview": True}],
    })
    assert ">uptime<" not in out["activeHTML"], "浏览器会话时长不得显示为模型 uptime"


def test_gateway_uptime_not_displayed(tmp_path):
    """system.uptime_seconds 是主机时长或默认值，不挂「网关运行时长」。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _apple_dev()}, "renderOverview": True}],
    })
    html = out["teleHTML"] + out["sitHTML"]
    assert "网关运行" not in html and "网关时长" not in html


# ── 终审修复轮（No-with-fixes：F1–F13）────────────────────────

def test_stale_response_keeps_prior_etag_and_age(tmp_path):
    """F2：乱序迟到 200 被丢弃时，不得改写正在显示样本的 etag/采样年龄
    （否则下一次 If-None-Match 指向被丢弃响应，且新鲜度读数倒退）。"""
    out = _run_harness(tmp_path, {
        "snapshotResponses": [
            {"json": {"ts": 2000, "etag": "\"B\""}, "headers": {"X-IFF-Device-Sample-Age": "5"}},
            {"json": {"ts": 1000, "etag": "\"A\""}, "headers": {"X-IFF-Device-Sample-Age": "0"}},
        ],
        "steps": [{"fetchSnapshot": True}, {"fetchSnapshot": True}],
    })
    assert out["etag"] == "\"B\"", out["etag"]
    assert out["deviceSample"]["age"] == 5, out["deviceSample"]
    assert out["sampleAge"] >= 5, out["sampleAge"]


def test_sample_age_never_runs_backward_before_new_anchor(tmp_path):
    """F3 客户端：本地累加须单调 —— 墙钟回拨不回退读数；新锚（新响应）才允许重置。"""
    import time as _time
    now = _time.time()
    out = _run_harness(tmp_path, {
        "steps": [
            {"seedStore": {"device_sample": {"age": 25, "at": now - 3600}},
             "renderOverview": True},
            {"seedStore": {"device_sample": {"age": 25, "at": now + 100}},
             "renderOverview": True},
        ],
    })
    assert out["sampleAge"] is not None and out["sampleAge"] > 3600, out["sampleAge"]


def test_sample_age_resets_on_fresh_anchor(tmp_path):
    """F3 客户端（反向）：新采样到达允许合法下降 —— 单调下限须随锚重置。"""
    out = _run_harness(tmp_path, {
        "snapshotResponses": [{"json": {}, "headers": {"X-IFF-Device-Sample-Age": "5"}}],
        "steps": [
            {"seedStore": {"device_sample": {"age": 25, "at": 0}}, "renderOverview": True},
            {"fetchSnapshot": True},
        ],
    })
    assert out["sampleAge"] < 30, out["sampleAge"]


def test_freshness_badge_updates_while_poll_hangs(tmp_path):
    """F4：轮询在途/挂起时新鲜度徽章仍按本地时钟走表（1s 自走），不冻结。"""
    import time as _time
    out = _run_harness(tmp_path, {
        "snapshotResponses": [{"json": {}, "headers": {"X-IFF-Device-Sample-Age": "10"}}],
        "steps": [
            {"fetchSnapshot": True},
            {"seedStore": {"tab_active": "tab-overview",
                           "device_sample": {"age": 10, "at": _time.time() - 40}}},
            {"tick": 1000},
        ],
    })
    assert out["intervalCount"] >= 1, "须有本地走表 interval"
    assert "已过期" in out["teleFresh"], out["teleFresh"]


def test_rail_unified_total_is_physical_not_budget(tmp_path):
    """F5：统一内存遥测带总量 = 物理内存（device 快照），非推理预算。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _apple_dev(), "sync_meta": {"changed": True}}}],
    })
    r = out["rail"]
    assert "32.0" in r["memUnit"], r
    assert "24.0" not in r["memUnit"], "推理预算不得冒充遥测带总量"
    assert r["memLabel"] == "统一内存", r


def test_rail_hides_unsupported_segments_with_device(tmp_path):
    """F5：device 信封在场时，未声明能力的遥测带段不占位。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _apple_dev(), "sync_meta": {"changed": True}}}],
    })
    r = out["rail"]
    for k in ("segUtil", "segPwr", "segTemp", "segClock"):
        assert r[k] == "none", (k, r)


def test_rail_shows_supported_segments_with_device(tmp_path):
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _nvidia_dev(), "sync_meta": {"changed": True}}}],
    })
    r = out["rail"]
    for k in ("segUtil", "segPwr", "segTemp", "segClock"):
        assert r[k] == "", (k, r)


def test_rail_legacy_response_keeps_segments(tmp_path):
    """F5（兼容）：无 device 信封的旧响应不做能力隐藏（保持既有行为）。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"gpu": {"used": 100, "total": 200, "pct": 50},
                                 "sync_meta": {"changed": True}}}],
    })
    r = out["rail"]
    for k in ("segUtil", "segPwr", "segTemp", "segClock"):
        assert r[k] != "none", (k, r)


def test_rail_total_visible_without_used(tmp_path):
    """F5：used 采集失败但总量可信 → 「— / 32.0」而非只画 —。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": _apple_dev(memory_used_mb=None),
                                 "sync_meta": {"changed": True}}}],
    })
    r = out["rail"]
    assert "32.0" in r["memUnit"] and r["vram"].strip() == "—", r


def test_idle_shortcuts_use_to_key_and_gate(tmp_path):
    """F1：真实 history 行是 {from,to,...}（db.py:296）而非 {model,...}；
    状态缺失/失败行不得混入；目录中已不存在的模型不出现。"""
    history = [
        {"timestamp": 4, "from": "idle", "to": "alpha", "duration": 1.0, "status": "ok"},
        {"timestamp": 3, "from": "idle", "to": "beta", "duration": 1.0, "status": ""},
        {"timestamp": 2, "from": "idle", "to": "gone-model", "duration": 1.0, "status": "ok"},
        {"timestamp": 1, "from": "idle", "to": "gamma", "duration": 1.0, "status": "error"},
    ]
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"active_services": [], "history": history,
                                 "models": [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]},
                   "renderOverview": True}],
    })
    html = out["shortcuts"] or ""
    assert "alpha" in html, html
    assert "gone-model" not in html, "目录中不存在的模型不得出现"
    assert "gamma" not in html, "error 状态行不得混入"
    assert "beta" not in html, "状态缺失行不得当作成功"


def test_idle_shortcut_click_navigates_to_inference(tmp_path):
    """F1：快捷项按钮必须可点（委托 → 推理页），不是死按钮。"""
    history = [{"timestamp": 1, "from": "idle", "to": "alpha", "duration": 1.0, "status": "ok"}]
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"active_services": [], "history": history,
                                 "models": [{"name": "alpha"}]},
                   "renderOverview": True},
                  {"click": {"card": "ovSituation",
                             "attrs": {"data-action": "goto-inference", "data-model": "alpha"}}}],
    })
    assert out["tabActive"] == "tab-inference", out["tabActive"]


def test_warning_count_uses_full_ring_limit(tmp_path):
    """F6：已保留警告请求 limit=500（环形缓冲全量），非 100 截断值。"""
    out = _run_harness(tmp_path, {
        "anomaliesWarnJson": {"events": [], "count": 137},
        "steps": [{"renderOverview": True}],
    })
    assert out["warnUrls"] and "limit=500" in out["warnUrls"][0], out["warnUrls"]
    assert "137" in (out["warnCount"] or ""), out["warnCount"]


def test_recent_five_exact_rows_and_escape(tmp_path):
    """F9：status 原文进徽章前必须转义（XSS 面）；行数恰为 5。"""
    rows = [{"timestamp": 1764300000.0 + i, "model": f"m{i}", "status": 200,
             "tokens_in": 1, "tokens_out": 1, "route": "local"} for i in range(6)]
    rows[0]["status"] = "<b>x</b>"
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"request_log": rows}, "renderOverview": True}],
    })
    html = out["recent5"] or ""
    assert html.count("ov-req-row") == 5, f"恰 5 行（got {html.count('ov-req-row')}）"
    assert "&lt;b&gt;" in html and "<b>x</b>" not in html, html[:200]


def test_recent_empty_notes_one_hour_cutoff(tmp_path):
    """F13：空列表须注明快照请求日志只看最近 1 小时。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"request_log": []}, "renderOverview": True}],
    })
    assert "1 小时" in (out["recent5"] or ""), out["recent5"]


def test_unparseable_200_not_success(tmp_path):
    """F8：200 但响应体不可解析 → 不得报成功（结果不可确认 ≠ 成功）。"""
    out = _run_harness(tmp_path, {
        "postResponses": {"reconcile": {"text": "<html>gateway oops</html>"}},
        "steps": [{"ops": "reconcile"}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "无法确认" in out["toastText"], out["toastText"]
    assert "已重新校准" not in out["toastText"]


def test_release_without_status_not_success(tmp_path):
    """F8：release 200 缺 status → 白名单不通过（无 `j && j.status` 逃逸）。"""
    out = _run_harness(tmp_path, {
        "postResponses": {"switch": {"json": {"message": "done"}}},
        "steps": [{"ops": "release"}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "GPU 已释放" not in out["toastText"]


def test_release_unparseable_not_success(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"switch": {"text": "oops"}},
        "steps": [{"ops": "release"}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "GPU 已释放" not in out["toastText"]


def test_sleep_unparseable_not_success(tmp_path):
    out = _run_harness(tmp_path, {
        "postResponses": {"sleep": {"text": "oops"}},
        "steps": [{"modelAction": ["m1", "sleep"]}],
    })
    assert out["toastClass"].find("error") >= 0, out["toastText"]
    assert "休眠中" not in out["toastText"]


def test_release_confirm_mentions_shared_mode(tmp_path):
    """F8：释放确认文案须如实覆盖共享模式（会停多个在役模型）。"""
    src = OVERVIEW_JS.read_text(encoding="utf-8")
    assert "共享模式" in src, "释放确认文案须覆盖共享模式（_stop_all_active 语义）"


def test_error_toast_message_bounded(tmp_path):
    """F11：错误 toast 截断上游诊断原文（长度上限、去换行）。"""
    out = _run_harness(tmp_path, {
        "postResponses": {"reconcile": {"json": {"status": "error",
                                                  "message": "x" * 300 + "\nline2"}}},
        "steps": [{"ops": "reconcile"}],
    })
    t = out["toastText"]
    assert "\n" not in t, t
    assert len(t) <= 140, len(t)


def test_today_sum_coerces_numeric_strings(tmp_path):
    """F12：requests 字符串数字不得字符串拼接（"3"+4 ≠ "34"）。"""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = {"local": {today: {"m1": {"requests": "3"}, "m2": {"requests": 4}}}, "cloud": {}}
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"token_stats": stats}, "renderOverview": True}],
    })
    assert "7" in (out["reqToday"] or ""), out["reqToday"]
    assert "34" not in (out["reqToday"] or "")


def test_spark_caption_not_today_when_last_bucket_stale(tmp_path):
    """F12：spark 尾点非 UTC 今日 → caption 不得写「今日」（写尾点日期）。"""
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"token_stats": _token_stats(None, "2020-01-01")},
                   "renderOverview": True}],
    })
    assert "今日" not in (out["spark"] or ""), out["spark"]
    assert "2020-01-01" in (out["spark"] or ""), "尾点日期应可见"


# ═══════════════════════════════════════════════════════════════
# 7.12.20 修复轮（#91 合并态补验：F-B / F-C / F-E / F-F）
# ═══════════════════════════════════════════════════════════════

def test_dropped_out_of_order_response_not_marked_stale(tmp_path):
    """F-B：乱序丢弃 ≠ 断连 —— 丢弃路径不得置 sync_meta.stale（否则顶栏
    「已断线」假报警），且 lastOk 须刷新（API 刚刚应答过）。"""
    out = _run_harness(tmp_path, {
        "snapshotResponses": [
            {"json": {"ts": 2000, "etag": "\"B\""}, "headers": {"X-IFF-Device-Sample-Age": "5"}},
            {"json": {"ts": 1000, "etag": "\"A\""}, "headers": {"X-IFF-Device-Sample-Age": "0"}},
        ],
        "steps": [{"fetchSnapshot": True}, {"fetchSnapshot": True}],
    })
    meta = out["syncMeta"] or {}
    assert meta.get("stale") is False, "乱序丢弃不得显示已断线: %r" % (meta,)
    assert out["apiError"] is None, out["apiError"]
    assert meta.get("lastOk"), "lastOk 应刷新（丢弃也是成功应答）"

def test_late_304_does_not_reanchor_sample_age(tmp_path):
    """F-C：迟到的旧 304 不得重锚采样年龄（否则新鲜度徽章被旧请求拉回退）。"""
    out = _run_harness(tmp_path, {
        "snapshotResponses": [
            {"status": 304, "wait": True, "headers": {"X-IFF-Device-Sample-Age": "9"}},
            {"json": {"ts": 2000, "etag": "\"B\""}, "headers": {"X-IFF-Device-Sample-Age": "2"}},
        ],
        "steps": [{"startFetch": True}, {"fetchSnapshot": True}, {"flush": True}],
    })
    assert out["deviceSample"] and out["deviceSample"]["age"] == 2, out["deviceSample"]
    assert out["sampleAge"] < 5, "旧 304 的年龄 9 不得覆盖新锚 2: %r" % out["sampleAge"]

def test_rail_segmem_hidden_without_memory_capability(tmp_path):
    """F-E：未声明 memory 能力 → rail 内存段不占位（与遥测卡门控一致）。"""
    dev = _nvidia_dev()
    dev["capabilities"]["memory"] = False
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": dev, "sync_meta": {"changed": True}}}],
    })
    assert out["rail"]["segMem"] == "none", out["rail"]

def test_telemetry_used_without_total_has_unit(tmp_path):
    """F-E：total 未知、used 已知 → 内存行也必须带单位（不得裸「12.1」）。"""
    dev = _nvidia_dev()
    dev["snapshot"]["memory_total_mb"] = None
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"device": dev}, "renderOverview": True}],
    })
    assert '<dd class="mono">8.0 <span' in out["teleHTML"], \
        "used 无 total 时缺 GB 单位: %r" % out["teleHTML"]

def test_warning_count_fetched_once_per_ttl(tmp_path):
    """F-F：3s 轮询渲染不得每次都拉 /api/anomalies（15s TTL + 在途去重），
    且已取到的警告数不得闪回「—」。"""
    out = _run_harness(tmp_path, {
        "anomaliesWarnJson": {"count": 7, "events": [{"id": 1}, {"id": 2}]},
        "steps": [{"renderOverview": True}, {"renderOverview": True},
                  {"renderOverview": True}],
    })
    assert out["warnFetches"] == 1, "TTL 内重复拉取: %r 次" % out["warnFetches"]
    assert "7" in (out["warnCount"] or ""), out["warnCount"]

def test_superseded_304_still_reports_online(tmp_path):
    """终审 Important：被更新请求取代的迟到 304 不重锚采样年龄，但连通性
    证据仍须更新（lastOk/在线）——持续 >3s 迟延时不得假报「已断线」。"""
    out = _run_harness(tmp_path, {
        "snapshotResponses": [
            {"status": 304, "wait": True, "headers": {"X-IFF-Device-Sample-Age": "9"}},
            {"status": 304, "wait": True, "headers": {"X-IFF-Device-Sample-Age": "3"}},
        ],
        "steps": [
            {"startFetch": True}, {"startFetch": True},
            {"flush": 1}, {"capture": True},     # 旧 304 先回（被新请求取代）
            {"flush": True}, {"capture": True},  # 新 304 回
        ],
    })
    cap0 = out["captures"][0]
    assert cap0["apiError"] is None, cap0
    assert (cap0["syncMeta"] or {}).get("stale") is False, cap0
    assert (cap0["syncMeta"] or {}).get("lastOk"), "旧 304 也须刷新 lastOk: %r" % (cap0,)
