"""dashboard UX v3 A1 — hash router 行为契约（node vm，无网络/DOM）。

规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md A1 +
specs/dashboard-ux-v3/interface-contract.md（A 区）。
被测对象：inferfabric/dashboard/js/router.js（window.ifRouter）。
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
ROUTER_JS = ROOT / "inferfabric" / "dashboard" / "js" / "router.js"
STORE_JS = ROOT / "inferfabric" / "dashboard" / "js" / "store.js"

_VALID_VIEWS = ("status", "requests", "usage", "routing", "models", "config")
# view → 默认 DOM tab（A1 过渡期沿用旧 tab id；A2 换新壳后由装配层对齐）
_VIEW_DEFAULT_TAB = {
    "status": "tab-overview",
    "requests": "tab-monitor",
    "usage": "tab-usage",
    "routing": "tab-model-pools",
    "models": "tab-inference",
    "config": "tab-config",
}
# 旧 localStorage iff_active_tab → 新路由（含 2026-11-20 tab-deploy→packs 前置迁移）
_TAB_ROUTE_MAP = {
    "tab-overview": ("status", None),
    "tab-monitor": ("requests", None),
    "tab-anomaly": ("requests", "anomalies"),
    "tab-inference": ("models", None),
    "tab-model-pools": ("routing", None),
    "tab-packs": ("config", "packs"),
    "tab-deploy": ("config", "packs"),
    "tab-security": ("config", "auth"),
    "tab-connect": ("config", "connect"),
    "tab-settings": ("config", "sys"),
    "tab-cloud": ("config", "cloud"),
}
# view+sub → 旧 DOM tab（A1 过渡期 router 借此驱动既有装配）
_ROUTE_TAB_MAP = {
    ("status", None): "tab-overview",
    ("requests", None): "tab-monitor",
    ("requests", "anomalies"): "tab-anomaly",
    ("requests", "switches"): "tab-monitor",
    ("usage", None): "tab-usage",
    ("routing", None): "tab-model-pools",
    ("models", None): "tab-inference",
    ("config", None): "tab-config",
    ("config", "packs"): "tab-packs",
    ("config", "auth"): "tab-security",
    ("config", "connect"): "tab-connect",
    ("config", "sys"): "tab-settings",
    ("config", "cloud"): "tab-cloud",
}

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

// ── 可编程 localStorage 桩 ──
function makeStorage(opts) {
  const map = Object.assign({}, (opts && opts.data) || {});
  return {
    getItem: (k) => {
      if (opts && opts.throwOn === 'get') throw new Error('storage denied');
      return Object.prototype.hasOwnProperty.call(map, k) ? map[k] : null;
    },
    setItem: (k, v) => {
      if (opts && opts.throwOn === 'set') throw new Error('storage denied');
      map[k] = String(v);
    },
    removeItem: (k) => { delete map[k]; },
    _map: map,
  };
}

const storage = makeStorage(input.storage);
let hashValue = input.hash || '';
let hashListener = null;
const location = {
  get hash() { return hashValue; },
  // 浏览器语义：hash 赋值触发 hashchange（同步派发，模拟真实导航）
  set hash(v) {
    if (v === hashValue) return;
    hashValue = v;
    if (hashListener) hashListener();
  },
  replace(url) {
    const i = url.indexOf('#');
    hashValue = i >= 0 ? url.slice(i) : '';
  },
};
const applied = [];
const window = {
  addEventListener: (ev, fn) => { if (ev === 'hashchange') hashListener = fn; },
  dispatchHashChange() { if (hashListener) hashListener(); },
};
const document = { addEventListener: () => {}, querySelectorAll: () => [] };
if (input.store) {
  window.store = {
    applyTab: (tabId) => { applied.push(tabId); },
    isSwitchLocked: () => !!input.store.locked,
  };
}
const ctx = { window, document, location, localStorage: storage, console };
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

const R = window.ifRouter;
assert.ok(R, 'router must expose window.ifRouter');
const out = {
  parse: null, toHash: null, applied,
  legacy_removed: !Object.prototype.hasOwnProperty.call(storage._map, 'iff_active_tab'),
};
if (input.call && input.call.parse !== undefined) out.parse = R.parseHash(input.call.parse);
if (input.call && input.call.toHash) out.toHash = R.toHash(input.call.toHash.view, input.call.toHash.sub, input.call.toHash.params);
if (input.call && input.call.routeForTab) out.routeForTab = R.routeForTab(input.call.routeForTab);
if (input.call && input.call.tabForRoute) out.tabForRoute = R.tabForRoute(input.call.tabForRoute.view, input.call.tabForRoute.sub);
if (input.call && input.call.navigate) {
  const c = input.call.navigate;
  out.navigateResult = R.navigate(c.view, c.sub, c.params);
}
if (input.call && input.call.hashChange) { hashValue = input.call.hashChange; window.dispatchHashChange(); }
if (input.call && input.call.hashChanges) {
  out.applyCounts = [];
  for (const h of input.call.hashChanges) { hashValue = h; window.dispatchHashChange(); out.applyCounts.push(applied.length); }
}
// navigate 等调用会改写存储与地址：持久化/地址统一在调用后取值（throwOn get 也不抛）
try { out.iff_view = storage.getItem('iff_view'); } catch (e) { out.iff_view = null; }
out.hash = hashValue;
out.finalHash = hashValue;
console.log(JSON.stringify(out));
"""


@pytest.fixture
def run_router(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    if not ROUTER_JS.exists():
        pytest.fail("router.js missing (A1 not implemented)")
    harness = tmp_path / "router_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(scenario):
        proc = subprocess.run(
            [node, str(harness), str(ROUTER_JS)],
            input=json.dumps(scenario),
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, (
            f"node harness failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        return json.loads(proc.stdout)

    return run


# ── 1. parseHash / toHash 纯函数 ──

def test_parse_hash_views_subs_and_params(run_router):
    out = run_router({"call": {"parse": "#/requests/anomalies?x=1&y=2"}})
    assert out["parse"] == {"view": "requests", "sub": "anomalies",
                            "params": {"x": "1", "y": "2"}}
    out = run_router({"call": {"parse": "#/status"}})
    assert out["parse"] == {"view": "status", "sub": None, "params": {}}
    out = run_router({"call": {"parse": "garbage"}})
    assert out["parse"] is None


def test_to_hash_canonical_encoding(run_router):
    out = run_router({"call": {"toHash": {"view": "requests", "sub": "anomalies",
                                          "params": {"model": "a b", "status": "4xx"}}}})
    assert out["toHash"] == "#/requests/anomalies?model=a%20b&status=4xx"
    out = run_router({"call": {"toHash": {"view": "usage", "sub": None, "params": {}}}})
    assert out["toHash"] == "#/usage"


def test_route_tab_roundtrip(run_router):
    out = run_router({"call": {"routeForTab": "tab-model-pools"}})
    assert out["routeForTab"] == {"view": "routing", "sub": None}
    out = run_router({"call": {"routeForTab": "tab-deploy"}})
    assert out["routeForTab"] == {"view": "config", "sub": "packs"}
    out = run_router({"call": {"tabForRoute": {"view": "requests", "sub": "anomalies"}}})
    assert out["tabForRoute"] == "tab-anomaly"
    out = run_router({"call": {"tabForRoute": {"view": "requests", "sub": None}}})
    assert out["tabForRoute"] == "tab-monitor"


# ── 2. 启动解析：hash 优先 → iff_view → 旧键迁移 → status ──

def test_boot_hash_wins_over_storage(run_router):
    out = run_router({"hash": "#/models", "store": {},
                      "storage": {"data": {"iff_view": "usage"}}})
    assert out["applied"] == ["tab-inference"]
    assert out["finalHash"] == "#/models"


def test_boot_hash_params_preserved(run_router):
    out = run_router({"hash": "#/requests/switches?model=a", "store": {}})
    assert out["applied"] == ["tab-monitor"]
    assert out["finalHash"] == "#/requests/switches?model=a"


def test_boot_restores_iff_view(run_router):
    out = run_router({"store": {},
                      "storage": {"data": {"iff_view": "requests/anomalies"}}})
    assert out["applied"] == ["tab-anomaly"]
    assert out["iff_view"] == "requests/anomalies"
    assert out["finalHash"] == "#/requests/anomalies"


def test_boot_migrates_legacy_active_tab_and_removes_old_key(run_router):
    for tab, (view, sub) in _TAB_ROUTE_MAP.items():
        out = run_router({"store": {}, "storage": {"data": {"iff_active_tab": tab}}})
        expect_tab = _ROUTE_TAB_MAP[(view, sub)]
        assert out["applied"] == [expect_tab], tab
        expected = view if sub is None else f"{view}/{sub}"
        assert out["iff_view"] == expected, tab
        assert out["legacy_removed"], tab


def test_boot_defaults_to_status(run_router):
    out = run_router({"store": {}})
    assert out["applied"] == ["tab-overview"]
    assert out["finalHash"] == "#/status"


# ── 3. 无效与畸形输入 ──

def test_boot_invalid_stored_view_falls_back_to_status(run_router):
    out = run_router({"store": {}, "storage": {"data": {"iff_view": "bogus/zzz"}}})
    assert out["applied"] == ["tab-overview"]
    assert out["finalHash"] == "#/status"


def test_boot_invalid_sub_dropped_to_view_default(run_router):
    out = run_router({"store": {}, "storage": {"data": {"iff_view": "requests/bogus"}}})
    assert out["applied"] == ["tab-monitor"]
    assert out["finalHash"] == "#/requests"


def test_boot_storage_unavailable_never_throws(run_router):
    out = run_router({"store": {}, "storage": {"throwOn": "get"}})
    assert out["applied"] == ["tab-overview"]
    out = run_router({"store": {}, "storage": {"throwOn": "set"}})
    assert out["applied"] == ["tab-overview"]


def test_boot_hash_unknown_view_falls_back(run_router):
    out = run_router({"store": {}, "hash": "#/nope"})
    assert out["applied"] == ["tab-overview"]
    assert out["finalHash"] == "#/status"


# ── 4. navigate 与切换锁 ──

def test_navigate_updates_hash_and_persists_view(run_router):
    out = run_router({"store": {}, "call": {"navigate": {"view": "requests",
                                                         "sub": "anomalies"}}})
    assert out["navigateResult"] is True
    assert out["finalHash"] == "#/requests/anomalies"
    assert out["iff_view"] == "requests/anomalies"
    # boot 先应用默认区（tab-overview）；navigate 后目标 tab 恰好再应用一次
    # （显式应用与浏览器随后派发的 hashchange 由幂等去重合并，无双渲染）
    assert out["applied"] == ["tab-overview", "tab-anomaly"]


def test_navigate_locked_is_refused(run_router):
    out = run_router({"store": {"locked": True},
                      "hash": "#/status",
                      "call": {"navigate": {"view": "models"}}})
    assert out["navigateResult"] is False
    assert out["finalHash"] == "#/status"
    assert out["iff_view"] is None or out["iff_view"] != "models"
    assert out["applied"] == []


def test_navigate_unknown_view_refused(run_router):
    out = run_router({"store": {}, "call": {"navigate": {"view": "bogus"}}})
    assert out["navigateResult"] is False
    # 启动默认已把地址规范为 #/status；拒绝导航后地址不变
    assert out["finalHash"] == "#/status"


def test_hashchange_locked_does_not_apply(run_router):
    out = run_router({"store": {"locked": True}, "hash": "#/status",
                      "call": {"hashChange": "#/usage"}})
    assert out["applied"] == []


def test_hashchange_applies_and_does_not_loop(run_router):
    out = run_router({"store": {}, "hash": "#/status",
                      "call": {"hashChanges": ["#/requests", "#/requests/anomalies",
                                               "#/status"]}})
    # 每次合法 hashchange 恰好应用一次目标 tab（boot 先应用 status 一次），无循环放大
    assert out["applyCounts"] == [2, 3, 4]
    assert out["applied"] == ["tab-overview", "tab-monitor", "tab-anomaly", "tab-overview"]


def test_navigate_params_never_persisted(run_router):
    out = run_router({"store": {},
                      "call": {"navigate": {"view": "requests", "sub": None,
                                            "params": {"model": "secret-agent"}}}})
    assert out["finalHash"] == "#/requests?model=secret-agent"
    assert out["iff_view"] == "requests"


# ── 5. 接线：store.switchTab 兼容入口走 router（A1 单源） ──

_STORE_ROUTER_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function makeStorage(data) {
  const map = Object.assign({}, data || {});
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(map, k) ? map[k] : null),
    setItem: (k, v) => { map[k] = String(v); },
    removeItem: (k) => { delete map[k]; },
    _map: map,
  };
}
const storage = makeStorage(input.storage);
let hashValue = '';
let hashListener = null;
const location = {
  get hash() { return hashValue; },
  set hash(v) { if (v === hashValue) return; hashValue = v; if (hashListener) hashListener(); },
  replace(url) { const i = url.indexOf('#'); hashValue = i >= 0 ? url.slice(i) : ''; },
};

function makeEl(attrs) {
  const classes = new Set();
  const attribs = Object.assign({}, attrs || {});
  return {
    _classes: classes,
    _attrs: attribs,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c) => { classes.has(c) ? classes.delete(c) : classes.add(c); },
      contains: (c) => classes.has(c),
    },
    setAttribute: (k, v) => { attribs[k] = v; },
    removeAttribute: (k) => { delete attribs[k]; },
    getAttribute: (k) => (k in attribs ? attribs[k] : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    removeEventListener: () => {},
    style: {}, title: '', innerHTML: '', textContent: '',
  };
}

const panels = {
  'tab-overview': makeEl(), 'tab-monitor': makeEl(), 'tab-anomaly': makeEl(),
};
const btnOverview = makeEl({ 'data-tab': 'tab-overview' });
const btnAnomaly = makeEl({ 'data-tab': 'tab-anomaly' });
const navButtons = [btnOverview, btnAnomaly];
const els = {
  'modeBadge': makeEl(), 'syncMeta': makeEl(), 'navVer': makeEl(),
  'apiErrorBanner': makeEl(), 'switchOverlay': makeEl(), 'switchOverlayMsg': makeEl(),
  'sidebarStatusDot': makeEl(), 'sidebarSyncTxt': makeEl(), 'themeToggle': makeEl(),
};
const document = {
  readyState: 'loading',   // 不触发 DOMContentLoaded boot（避免定时器与自动轮询）
  documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
  getElementById: (id) => panels[id] || els[id] || null,
  querySelectorAll: (sel) => {
    if (sel === '.tab-content') return Object.values(panels);
    if (sel === '.if-nav-item') return navButtons;
    return [];
  },
  querySelector: (sel) => {
    const m = /data-tab="([^"]+)"/.exec(sel);
    return m ? (navButtons.find((b) => b._attrs['data-tab'] === m[1]) || null) : null;
  },
  addEventListener: () => {},
  removeEventListener: () => {},
};
const window = {
  addEventListener: (ev, fn) => { if (ev === 'hashchange') hashListener = fn; },
};
const ctx = {
  window, document, location, localStorage: storage, console,
  setTimeout: () => {}, setInterval: () => {}, clearInterval: () => {},
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);   // store.js
vm.runInNewContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);   // router.js
assert.ok(window.store && window.ifRouter, 'store and router must both load');
if (input.locked) window.store.setSwitchLocked(true);
const callResult = window.switchTab(input.tab);
const out = {
  callResult: callResult === undefined ? null : callResult,
  hash: hashValue,
  tab_active: window.store.get('tab_active'),
  activePanels: Object.keys(panels).filter((id) => panels[id]._classes.has('active')),
  navCurrent: navButtons.filter((b) => b._attrs['aria-current'] === 'page')
    .map((b) => b._attrs['data-tab']),
};
try { out.iff_view = storage.getItem('iff_view'); } catch (e) { out.iff_view = null; }
console.log(JSON.stringify(out));
"""


@pytest.fixture
def run_store_router(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    if not ROUTER_JS.exists() or not STORE_JS.exists():
        pytest.fail("router.js or store.js missing (A1 not implemented)")
    harness = tmp_path / "store_router_harness.js"
    harness.write_text(_STORE_ROUTER_HARNESS, encoding="utf-8")

    def run(scenario):
        proc = subprocess.run(
            [node, str(harness), str(STORE_JS), str(ROUTER_JS)],
            input=json.dumps(scenario),
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, (
            f"node harness failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        return json.loads(proc.stdout)

    return run


def test_store_switch_tab_routes_through_hash(run_store_router):
    """兼容入口 window.switchTab(tabId) 单源走 router：更新 hash 与 iff_view，
    且 DOM 应用仍落在同一 tab。"""
    out = run_store_router({"tab": "tab-anomaly"})
    assert out["hash"] == "#/requests/anomalies"
    assert out["iff_view"] == "requests/anomalies"
    assert out["tab_active"] == "tab-anomaly"
    assert out["activePanels"] == ["tab-anomaly"]
    assert out["navCurrent"] == ["tab-anomaly"]


def test_store_switch_tab_locked_is_refused(run_store_router):
    out = run_store_router({"tab": "tab-anomaly", "locked": True})
    assert out["hash"] == "#/status"          # boot 默认地址，未因点击而变
    assert out["activePanels"] == ["tab-overview"]
    assert out["iff_view"] == "status"
