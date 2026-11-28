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
const postCalls = [];
function mkRes(spec) {
  const hmap = {};
  for (const [k, v] of Object.entries(spec.headers || {})) hmap[k.toLowerCase()] = v;
  const status = spec.status || 200;
  return {
    ok: status < 400, status,
    headers: { get: (n) => (hmap[String(n).toLowerCase()] !== undefined ? hmap[String(n).toLowerCase()] : null) },
    json: async () => spec.json,
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
    return Promise.resolve(mkRes(spec));
  }
  if (u.indexOf('/api/anomalies') === 0) {
    if (u.indexOf('severity=warning') >= 0) { warnFetches += 1; return Promise.resolve(mkRes({ json: input.anomaliesWarnJson || { events: [] } })); }
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
    if (step.renderOverview) window.tabRenderers['tab-overview']();
    if (step.ops) await window.doSystemOps(step.ops);
    if (step.modelAction) await window.doModelAction(step.modelAction[0], step.modelAction[1]);
    await drain();
  }
  await drain();
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
    postCalls, snapshotFetches, warnFetches, anomFetches,
  };
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
    assert html.count("m") >= 5 or "<tr" in html or "m5" in html or "m0" in html
    assert "m6" not in html.split("查看全部")[0] or html.count("<li") == 5 or html.count("ov-req-row") == 5
    assert "5" in (out["sitHTML"] or ""), "最近五条计数应注明只统计所显示条数"


def test_idle_shortcuts_from_history(tmp_path):
    history = [
        {"model": "alpha", "status": "ok", "ts": 3},
        {"model": "beta", "status": "ok", "ts": 2},
        {"model": "alpha", "status": "ok", "ts": 1},
    ]
    out = _run_harness(tmp_path, {
        "steps": [{"seedStore": {"active_services": [], "history": history},
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
