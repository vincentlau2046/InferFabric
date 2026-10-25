"""模型池面板：运行真实 model-pools.js，不访问网络（Node VM 模式）。

契约（Task 9，只读 + 开关）：
- 渲染池列表：池名 / enabled 开关 / revision / 候选与排除原因
- 双协议候选解释：OpenAI 与 Anthropic 的当前选择视图都展示
- 开关走 POST /admin/model-pools/toggle（服务端原子翻转，携带 expected_revision）
- 409 冲突 → 提示刷新并重新拉取；成功 → toast + 重新渲染
"""

import json
import re
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PANEL_JS = ROOT / "inferfabric" / "dashboard" / "js" / "model-pools.js"
DASHBOARD_DIR = ROOT / "inferfabric" / "dashboard"

_POOLS_BODY = {
    "revision": 3,
    "pools": {
        "fast": {
            "enabled": True,
            "strategy": "priority",
            "allowed_locations": ["local", "cloud"],
            "accept_declared": True,
            "allow_cross_location": False,
            "max_attempts": 2,
            "deadline_ms": 60000,
            "issues": [],
            "candidates": [
                {"id": "loc", "priority": 1, "kind": "local",
                 "target": "qwen-small", "features": ["tools"],
                 "evidence": {"openai": "declared"}},
                {"id": "cld", "priority": 2, "kind": "cloud",
                 "target": "prov/cloud-m", "features": [],
                 "evidence": {"openai": "declared"}},
            ],
        },
    },
}

_STATUS_BODY = {
    "revision": 3,
    "pools": {
        "fast": {
            "enabled": True,
            "issues": [],
            "protocols": {
                "openai": {"chosen": "loc", "error": None,
                           "decisions": [
                               {"id": "loc", "included": True, "reason": "ok"},
                               {"id": "cld", "included": True, "reason": "ok"}]},
                "anthropic": {"chosen": None, "error": "no_evidence",
                              "decisions": [
                                  {"id": "loc", "included": False,
                                   "reason": "no_evidence"},
                                  {"id": "cld", "included": False,
                                   "reason": "no_evidence"}]},
            },
        },
    },
}

# 渲染后应出现在面板里的契约片段
_RENDER_MUST_CONTAIN = [
    "fast",                 # 池名
    "qwen-small",           # 候选目标
    "prov/cloud-m",
    "openai",
    "anthropic",
    "no_evidence",          # 排除原因（解释性，非只给结果）
    'type="checkbox"',      # 原生开关（无自制伪 checkbox）
    'data-mp-edit="fast"',  # 编辑入口（卡片 → 三段式编辑器）
    'data-mp-snippet="fast"',  # 接入片段入口（卡片 → 复制即用代码）
]

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const toasts = [];
const elements = {};
const writeCounts = {};
function el(id) {
  if (!elements[id]) {
    const e = {
      id: id, textContent: '', className: '',
      style: {setProperty: function (k, v) { this[k] = v; }},
      addEventListener: function () {}, classList: {add(){}, remove(){}},
    };
    let _html = '';
    Object.defineProperty(e, 'innerHTML', {
      get: () => _html,
      set: (v) => { _html = v; writeCounts[id] = (writeCounts[id] || 0) + 1; },
    });
    elements[id] = e;
  }
  return elements[id];
}
const window = {
  tabRenderers: {},
  UI: {
    toast: (message, kind) => toasts.push({message, kind}),
    adminHeaders: () => ({'Content-Type': 'application/json'}),
  },
  store: {
    on: () => {},
    get: (k) => (input.storeState || {})[k],
  },
};
const clipboardWrites = [];
const timers = [];
const navigator = {
  clipboard: {writeText: async (t) => { clipboardWrites.push(t); }},
};
const docListeners = {};   // P2 Task 4：document 级监听捕获（fireKey 派发用）
const calls = [];
const fetchSnapshots = [];   // P2 Task 2 加法：captureAtFetch — 每次请求
                             // 瞬间快照指定元素 className（乐观态在途断言）
const seen = {};
let fakeNow = 0;
const timeStepMs = input.timeStepMs || 1000;
const fetch = async (url, options) => {
  options = options || {};
  calls.push({url, method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null});
  if (input.captureAtFetch && input.captureAtFetch.length) {
    const cls = {};
    for (const id of input.captureAtFetch) cls[id] = el(id).className;
    fetchSnapshots.push({url, method: options.method || 'GET', cls});
  }
  const method = options.method || 'GET';
  const mkey = method + ' ' + url;
  seen[mkey] = (seen[mkey] || 0) + 1;
  const route = input.routes[mkey] || input.routes[url];
  let r;
  if (typeof route === 'function') {
    r = route(seen[mkey]);           // 第 N 次命中该 METHOD+URL
  } else if (route && Array.isArray(route.bodies)) {
    // 时序路由：按命中次序取 body（越界取最后一个）
    r = {status: route.status || 200,
         statuses: route.statuses,   // P1 Task 3 加法：保留时序 status
         body: route.bodies[Math.min(seen[mkey] - 1, route.bodies.length - 1)]};
  } else if (route) {
    r = route;
  } else {
    // P3 Task 8：未 mock 的路由 → 404（真实浏览器语义；面板 best-effort
    // 降级路径依赖 fetch 不抛异常）
    r = {status: 404, body: null};
  }
  // P1 Task 3 加法：statuses 时序（按命中次序取 status，越界取最后一个）
  const status = Array.isArray(r.statuses)
    ? r.statuses[Math.min(seen[mkey] - 1, r.statuses.length - 1)]
    : r.status;
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => r.body,
  };
};
const documentMock = {
  getElementById: el,
  hidden: !!input.documentHidden,
  // P2 Task 4 加法：捕获 document 级监听（keydown）
  addEventListener: (type, fn) => {
    (docListeners[type] = docListeners[type] || []).push(fn);
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window,
  document: documentMock,
  fetch,
  navigator,
  location: input.location || {protocol: 'http:', hostname: '127.0.0.1'},
  setInterval: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
  // 修复 pass 加法：可控时钟（倒计时按真实流逝时间锚定的测试用）
  Date: {now: () => fakeNow},
  console,
});
// P2 Task 4 加法：fireKey — 派发 keydown 到已注册 document 监听。
// attrs 给 mock 元素播种 getAttribute/closest（扁平 mock 无 DOM 树，
// closest 只近似自匹配）；tagName 模拟表单控件聚焦。
window.fireKey = async (key, targetId, attrs, tagName) => {
  const t = targetId ? el(targetId) : null;
  if (t) {
    if (tagName) t.tagName = tagName;
    if (attrs) {
      t._attrs = Object.assign(t._attrs || {}, attrs);
      t.getAttribute = (k) => (k in t._attrs ? t._attrs[k] : null);
      t.closest = (sel) => (String(sel).indexOf('data-mp-card') >= 0
        && t._attrs['data-mp-card'] !== undefined ? t : null);
    }
  }
  documentMock.activeElement = t;
  // 等待监听器返回的 promise（onKeyDown 透传 doMpInlineCancel/doMpEdit
  // 的 promise — 与 pollTick 返回渲染 promise 同模式，供调用方等待）
  const rs = (docListeners['keydown'] || []).map((fn) => fn({key, target: t}));
  await Promise.all(rs);
};
(async () => {
  assert.ok(window.tabRenderers['tab-model-pools'],
            'must register tabRenderers["tab-model-pools"]');
  await window.renderModelPools();
  const html = elements['mpPools'] ? elements['mpPools'].innerHTML : '';
  if (input.toggle) {
    await window.doMpToggle(input.toggle.pool, input.toggle.enable);
  }
  // v7.1.0 编辑器：任意 window 函数动作序列（模拟用户在模态内的操作流）。
  // noAwait: true → 不等待完成（模拟慢网络下并发开第二个编辑会话）
  for (const act of (input.actions || [])) {
    const fn = window[act.fn];
    if (typeof fn !== 'function') {
      throw new Error('unknown action: ' + act.fn);
    }
    const p = fn.apply(window, act.args || []);
    if (!act.noAwait) await p;
  }
  // 模拟 5s 轮询滴答（每次滴答触发全部已注册 interval；
  // 每个计时器触发前推进 timeStepMs 毫秒 —— 模拟真实流逝）
  for (let i = 0; i < (input.pollTicks || 0); i++) {
    for (const t of timers) { fakeNow += timeStepMs; await t.fn(); }
  }
  const modalHtml = elements['mpModal'] ? elements['mpModal'].innerHTML : '';
  const previewHtml = elements['mpPreview'] ? elements['mpPreview'].innerHTML : '';
  // UI/UX P1：末态池 html（动作/轮询后）+ innerHTML 写计数 + 动态元素快照
  const htmlFinal = elements['mpPools'] ? elements['mpPools'].innerHTML : '';
  const dyn = {};
  for (const [id, e] of Object.entries(elements)) {
    // P3 Task 8 加法：innerHTML — by-id 定点更新（spark/时间线）的内容断言
    dyn[id] = {textContent: e.textContent, className: e.className,
               style: e.style, innerHTML: e.innerHTML};
  }
  console.log(JSON.stringify({html, modalHtml, previewHtml, toasts, calls,
                              fetchSnapshots, clipboardWrites, htmlFinal,
                              writeCounts, dyn}));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""


@pytest.fixture
def run_panel(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = tmp_path / "model_pools_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(routes, toggle=None, actions=None, location=None,
            poll_ticks=0, store_state=None, document_hidden=False,
            time_step_ms=None, capture_at_fetch=None):
        proc = subprocess.run(
            [node, str(harness), str(PANEL_JS)],
            input=json.dumps({"routes": routes, "toggle": toggle,
                              "actions": actions,
                              "location": location,
                              "pollTicks": poll_ticks,
                              "storeState": store_state,
                              "documentHidden": document_hidden,
                              "timeStepMs": time_step_ms,
                              "captureAtFetch": capture_at_fetch}),
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def _routes(toggle_status=200, toggle_body=None):
    return {
        "/admin/model-pools": {"status": 200, "body": _POOLS_BODY},
        "/admin/model-pools/status": {"status": 200, "body": _STATUS_BODY},
        # v7.1.0：面板合并拉取三端点（runtime 缺失时旧后端会 404 → 面板容忍；
        # 测试契约按新后端给 200 空运行态）
        "/admin/model-pools/runtime": {"status": 200,
                                       "body": {"revision": 3, "pools": {}}},
        "/admin/model-pools/toggle": {
            "status": toggle_status,
            "body": toggle_body or {"status": "updated", "revision": 4,
                                    "pool": "fast", "enabled": False},
        },
    }


class TestRender:
    def test_renders_pool_list_and_candidates(self, run_panel):
        out = run_panel(_routes())
        for fragment in _RENDER_MUST_CONTAIN:
            assert fragment in out["html"], f"missing in panel: {fragment}"
        # 池启用 → 开关选中态
        assert "checked" in out["html"]

    def test_renders_both_protocol_explanations(self, run_panel):
        out = run_panel(_routes())
        # OpenAI 有 chosen，Anthropic 全排除且带原因 — 两套视图都要可见
        assert "loc" in out["html"]
        assert out["html"].count("no_evidence") >= 2   # 双候选排除原因

    def test_disabled_pool_renders_unchecked(self, run_panel):
        pools = json.loads(json.dumps(_POOLS_BODY))
        pools["pools"]["fast"]["enabled"] = False
        routes = _routes()
        routes["/admin/model-pools"] = {"status": 200, "body": pools}
        out = run_panel(routes)
        assert "checked" not in out["html"]


class TestToggle:
    def test_toggle_sends_expected_revision_and_refreshes(self, run_panel):
        out = run_panel(_routes(), toggle={"pool": "fast", "enable": False})

        posts = [c for c in out["calls"]
                 if c["url"] == "/admin/model-pools/toggle"]
        assert len(posts) == 1
        assert posts[0]["method"] == "POST"
        assert posts[0]["body"] == {"pool": "fast", "enabled": False,
                                    "expected_revision": 3}
        # 成功 → toast + 重新拉取渲染
        assert any(t["kind"] == "ok" for t in out["toasts"])
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools" and c["method"] == "GET"]
        assert len(gets) == 2                       # 初次渲染 + 开关后刷新

    def test_toggle_conflict_409_prompts_refresh(self, run_panel):
        routes = _routes(toggle_status=409,
                         toggle_body={"error": "revision conflict",
                                      "status": "revision_conflict",
                                      "current_revision": 9})
        out = run_panel(routes, toggle={"pool": "fast", "enable": False})

        assert any("刷新" in t["message"] or "冲突" in t["message"]
                   for t in out["toasts"]), out["toasts"]
        # 冲突后仍重新拉取（让用户看到最新状态）
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools" and c["method"] == "GET"]
        assert len(gets) == 2

    def test_toggle_error_toasts_failure(self, run_panel):
        routes = _routes(toggle_status=500,
                         toggle_body={"error": "boom"})
        out = run_panel(routes, toggle={"pool": "fast", "enable": False})
        assert any(t["kind"] == "error" for t in out["toasts"])


# ── Phase 1 Task 5+：运营视图（健康灯 / 候选链运行态 / 三态空态）──

_RUNTIME_BODY = {
    "revision": 3,
    "pools": {
        "fast": {"candidates": [
            {"id": "loc", "kind": "local", "target": "qwen-small",
             "fingerprint": "local|qwen-small||qwen-small", "active": True,
             "cooling": {"remaining_s": 42.0, "reason": "cooldown_401",
                         "probing": False},
             "last_attempt": {"ok": False, "status": 503, "age_s": 187.0}},
            {"id": "cld", "kind": "cloud", "target": "prov/cloud-m",
             "fingerprint": "cloud|prov|https://b/v1|cloud-m", "active": False,
             "cooling": None, "last_attempt": None},
        ]},
    },
}


def _ops_routes(pools_body=None, status_body=None, runtime_body=None):
    return {
        "/admin/model-pools": {"status": 200, "body": pools_body or _POOLS_BODY},
        "/admin/model-pools/status": {"status": 200, "body": status_body or _STATUS_BODY},
        "/admin/model-pools/runtime": {"status": 200,
                                       "body": runtime_body or _RUNTIME_BODY},
    }


class TestOpsRender:
    def test_runtime_states_rendered(self, run_panel):
        out = run_panel(_ops_routes())
        html = out["html"]
        # 候选 loc：冷却徽标 + 原因 + 最近失败
        assert "冷却" in html
        assert "cooldown_401" in html
        assert "HTTP 503" in html

    def test_health_lamp_ok_when_first_choice_serving(self, run_panel):
        # openai chosen=loc（priority 1）→ mp-lamp-ok
        out = run_panel(_ops_routes())
        assert "mp-lamp-ok" in out["html"]

    def test_health_lamp_warn_on_fallback(self, run_panel):
        # openai chosen=cld（priority 2 → 回退中）；anthropic 无候选 → err
        status = json.loads(json.dumps(_STATUS_BODY))
        status["pools"]["fast"]["protocols"]["openai"]["chosen"] = "cld"
        out = run_panel(_ops_routes(status_body=status))
        assert "mp-lamp-warn" in out["html"]
        assert "mp-lamp-err" in out["html"]

    def test_unconfigured_state_message(self, run_panel):
        pools = json.loads(json.dumps(_POOLS_BODY))
        pools["pools"] = {}
        pools["configured"] = False
        pools["load_error"] = None
        pools["raw"] = None
        out = run_panel(_ops_routes(pools_body=pools))
        assert "尚未配置" in out["html"]

    def test_load_error_banner(self, run_panel):
        pools = json.loads(json.dumps(_POOLS_BODY))
        pools["pools"] = {}
        pools["configured"] = False
        pools["load_error"] = "model_pools.yaml invalid: bad things"
        pools["raw"] = None
        out = run_panel(_ops_routes(pools_body=pools))
        assert "model_pools.yaml invalid: bad things" in out["html"]
        assert "mp-error-banner" in out["html"]
        # 坏文件：不出候选表
        assert "qwen-small" not in out["html"]

    def test_active_badge_local_vs_cloud(self, run_panel):
        out = run_panel(_ops_routes())
        html = out["html"]
        # loc 本地 active=True → 服务中；cld 云 active=False → 未发现
        assert "服务中" in html
        assert "未发现" in html


# ── UI/UX P1 Task 2：候选体检卡（表格 → 横向卡片）──
# 契约：
# - 候选行 = mp-cand-card（data-mp-cand-card），不再是 if-table 表格行
# - 状态灯 mp-dot 五态（用户拍板 #3）：
#     probe（半开探测，快脉冲）> cool（冷却/隔离，暗态+环形倒计时）
#     > flow（last_attempt.ok 且 age_s<30 —— 真实数据交换，呼吸流动）
#     > ok（routable 空闲静默）> off（未启动/未发现，unroutable）
# - 协议徽章（declared_protocols ‖ evidence 键）+ 能力点（features）
# - 运行态语义文本令牌沿用（服务中/已发现/冷却 …）— TestOpsRender 是契约


class TestP1CandidateCards:
    def test_cards_replace_table_markup(self, run_panel):
        out = run_panel(_ops_routes())
        html = out["html"]
        assert 'data-mp-cand-card="loc"' in html
        assert 'class="mp-cand-card' in html
        # 旧候选表格不再出现在池卡片（编辑器模态内 cp-table 不受影响）
        assert "候选</th>" not in html

    def test_dot_cooling_and_off_states(self, run_panel):
        # loc: cooling(cooldown_401, 非探测) → mp-dot-cool
        # cld: 云未发现 → mp-dot-off
        out = run_panel(_ops_routes())
        html = out["html"]
        assert "mp-dot-cool" in html
        assert "mp-dot-off" in html

    def test_dot_flow_on_recent_real_traffic(self, run_panel):
        # 真实数据交换（last_attempt.ok 且 age_s<30）→ 流动态（拍板 #3）
        rt = json.loads(json.dumps(_RUNTIME_BODY))
        c = rt["pools"]["fast"]["candidates"][0]
        c["cooling"] = None
        c["last_attempt"] = {"ok": True, "status": 200, "age_s": 5.0}
        out = run_panel(_ops_routes(runtime_body=rt))
        assert "mp-dot-flow" in out["html"]

    def test_dot_flow_not_for_stale_success(self, run_panel):
        # 成功但久远（age_s>30）→ 空闲静默，不流动
        rt = json.loads(json.dumps(_RUNTIME_BODY))
        c = rt["pools"]["fast"]["candidates"][0]
        c["cooling"] = None
        c["last_attempt"] = {"ok": True, "status": 200, "age_s": 300.0}
        out = run_panel(_ops_routes(runtime_body=rt))
        assert "mp-dot-flow" not in out["html"]
        assert "mp-dot-ok" in out["html"]     # active 本地 → routable 静默

    def test_dot_probe_while_half_open(self, run_panel):
        rt = json.loads(json.dumps(_RUNTIME_BODY))
        rt["pools"]["fast"]["candidates"][0]["cooling"] = {
            "remaining_s": 3.0, "reason": "cooldown_401", "probing": True}
        out = run_panel(_ops_routes(runtime_body=rt))
        assert "mp-dot-probe" in out["html"]

    def test_protocol_badges_from_evidence(self, run_panel):
        out = run_panel(_ops_routes())
        html = out["html"]
        # 候选 loc evidence {openai: declared} → openai 协议徽章 + declared 态
        assert "mp-proto-badge" in html
        assert "openai" in html

    def test_feature_dots_rendered(self, run_panel):
        # loc features ["tools"] → tools 能力点（v7.1.2 目录语义在前端可见）
        out = run_panel(_ops_routes())
        assert "mp-feat" in out["html"]
        assert "tools" in out["html"]

    def test_ring_countdown_on_cooling(self, run_panel):
        out = run_panel(_ops_routes())
        html = out["html"]
        # 冷却候选带环形倒计时元素（--p 百分比由局部渲染任务驱动）
        assert "data-mp-ring" in html
        assert "0:42" in html                  # fmtRemaining(42.0)


# ── Task 6：三段式编辑器（编辑 → 预览 → 应用/回滚）──
# 契约：
# - 编辑基底 = _state.def.raw（GET 视图有损，经视图重建丢数据 — spec §4.3）
# - 替换候选 = 直接替换并继承优先级（D4：宁可丢回退，不做优先级重排）
# - 预览：validate 不通过 → 阻止 dry-run 与应用；通过 → dry-run×2 携带编辑态 config
# - 应用：POST /admin/model-pools {...work, expected_revision}（CAS）
# - 409 → 冲突 toast + 重新拉取；成功 → 记录 _undo（编辑前 raw）
# - 回滚：以最新 revision 重新 POST 编辑前配置
# - 从未配置（raw=null）→ 空基底 + 新建池骨架，expected_revision=0

_EDITOR_RAW = {
    "schema_version": 1,
    "revision": 3,
    "pools": {
        "fast": {
            "enabled": True,
            "strategy": "priority",
            "allowed_locations": ["local", "cloud"],
            "accept_declared": True,
            "allow_cross_location": False,
            "max_attempts": 2,
            "deadline_ms": 60000,
            "candidates": [
                {"id": "loc", "priority": 1, "kind": "local",
                 "model_name": "qwen-small",
                 "declared_protocols": ["openai", "anthropic"],
                 "features": ["tools"]},
                # 悬空候选：视图剔除、raw 保留 — 编辑必须原样带回
                {"id": "dang", "priority": 2, "kind": "local",
                 "model_name": "gone-model",
                 "declared_protocols": ["openai"], "features": []},
            ],
        }
    },
}

# v7.1.2 候选目录 — schema 来自 build_catalog 契约测试的输出快照
# （禁止发明字段：上一版云目录就是 fixture 发明了 providers[].models
#   导致前端解析静默断掉而测试全绿）
_CATALOG_BODY = {
    "local": [
        {"name": "qwen3-4b", "active": True, "engine": "vllm",
         "modality": "text", "context_window": 32768,
         "protocols": ["openai", "anthropic"]},
        {"name": "NI-Qwen38-27B", "active": False, "engine": "ninfer",
         "modality": "text", "context_window": 262144,
         "protocols": ["openai"]},
    ],
    "cloud": [
        {"provider": "tathr-glm", "model_id": "glm-5",
         "protocols": ["openai", "anthropic"], "features": [],
         "active": True, "context_window": None},
        {"provider": "Qianfan-Token", "model_id": "deepseek-v4.1-flash",
         "protocols": ["openai", "anthropic"], "features": ["tools"],
         "active": True, "context_window": None},
    ],
}

_CATALOG_LOCAL_ONLY_CLOUD = {
    "local": [],
    "cloud": [
        {"provider": "tathr-glm", "model_id": "glm-5",
         "protocols": ["openai", "anthropic"], "features": [],
         "active": True, "context_window": None},
    ],
}


# ── UI/UX P1（v7.2.0）：Motion/Depth 令牌 + 体检卡 + 错误即导航 + 局部渲染 ──


class TestMotionTokens:
    """Task 1：动效令牌集中定义 + reduced-motion 全局降级。"""

    def test_motion_tokens_defined_in_root(self):
        css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(
            encoding="utf-8")
        for token in ("--dur-fast", "--dur", "--dur-slow", "--ease"):
            assert token + ":" in css, token
        # macOS 质感缓动曲线
        assert "cubic-bezier(0.2, 0, 0, 1)" in css

    def test_reduced_motion_global_kill_switch(self):
        css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(
            encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in css
        # 降级块内全局关停动画与过渡（一刀切，无零散例外）
        m = re.search(r"prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}",
                      css, re.S)
        assert m, "reduced-motion block missing"
        block = m.group(1)
        assert "animation: none" in block
        assert "transition: none" in block


# ── UI/UX P1 Task 3：A3 错误即导航（常驻 banner + 行动入口）──
# 契约：
# - doMpToggle 409/非 409 失败、doMpWizardSubmit 失败、doMpUndo 失败 →
#   mp-action-banner（原因 + data-mp-banner-action 行动按钮 +
#   data-mp-banner-dismiss 关闭）；toast 保留（即时反馈）
# - 成功操作清除 banner（常驻但非永久）
# - doMpBannerAction 重试原操作；doMpBannerDismiss 清除 + 重渲染


class TestP1ErrorBanner:
    def test_toggle_409_renders_action_banner(self, run_panel):
        routes = _routes(toggle_status=409, toggle_body={
            "error": "revision conflict", "status": "revision_conflict",
            "current_revision": 9})
        out = run_panel(routes, toggle={"pool": "fast", "enable": False})
        html = out["htmlFinal"]
        assert "mp-action-banner" in html
        assert "data-mp-banner-action" in html
        assert "data-mp-banner-dismiss" in html
        assert "冲突" in html
        # toast 保留（即时反馈），banner 承担常驻
        assert any("冲突" in t["message"] for t in out["toasts"])

    def test_toggle_500_renders_action_banner(self, run_panel):
        out = run_panel(_routes(toggle_status=500, toggle_body={"error": "boom"}),
                        toggle={"pool": "fast", "enable": False})
        assert "mp-action-banner" in out["htmlFinal"]

    def test_banner_action_retries_and_success_clears(self, run_panel):
        routes = _routes()
        # 首次 409、重试成功：statuses 按命中次序取（harness Task 3 加法）
        routes["/admin/model-pools/toggle"] = {
            "statuses": [409, 200],
            "bodies": [{"error": "revision conflict",
                        "status": "revision_conflict"},
                       {"status": "updated", "revision": 4,
                        "pool": "fast", "enabled": False}]}
        out = run_panel(routes, toggle={"pool": "fast", "enable": False},
                        actions=[{"fn": "doMpBannerAction"}])
        posts = [c for c in out["calls"]
                 if c["url"] == "/admin/model-pools/toggle"]
        assert len(posts) == 2          # 原操作 + banner 重试
        assert any(t["kind"] == "ok" for t in out["toasts"])
        # 成功 → banner 清除
        assert "mp-action-banner" not in out["htmlFinal"]

    def test_banner_dismiss_clears_banner(self, run_panel):
        routes = _routes(toggle_status=500, toggle_body={"error": "boom"})
        out = run_panel(routes, toggle={"pool": "fast", "enable": False},
                        actions=[{"fn": "doMpBannerDismiss"}])
        assert "mp-action-banner" not in out["htmlFinal"]

    def test_wizard_submit_failure_renders_banner(self, run_panel):
        routes = _wizard_routes()
        routes["POST /admin/model-pools"] = {"status": 500,
                                             "body": {"error": "boom"}}
        out = run_panel(routes, actions=[{"fn": "doMpWizardSubmit"}])
        assert "mp-action-banner" in out["htmlFinal"]

    def test_undo_failure_renders_banner(self, run_panel):
        # 成功 apply（置 _undo）→ 回滚 409 → banner
        routes = _editor_routes()
        routes["POST /admin/model-pools"] = {
            "statuses": [200, 409],
            "bodies": [{"status": "updated", "revision": 4},
                       {"status": "revision_conflict", "current_revision": 9}]}
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
            {"fn": "doMpUndo", "args": []},
        ]
        out = run_panel(routes, actions=actions)
        assert "mp-action-banner" in out["htmlFinal"]


# ── UI/UX P1 Task 4：B1 局部渲染 + 本地倒计时 ──
# 契约：
# - 结构签名（池名 + 每池候选 id 序列 + enabled + issues 数）不变 → 不重写
#   mpPools.innerHTML（轮询零闪烁），只走 applyDynamic 按 id 定点更新
# - applyDynamic 定点更新：mp-lamp-<pool>-<proto>（className）、mp-dot-*、
#   mp-rt-*（textContent）、mp-chosen-<pool>-<proto>、revision
# - 本地倒计时：1s interval 以 runtime 拉取时刻为锚本地递减 remaining_s，
#   更新 mp-rt-* 与环形 --p，不发出任何 fetch


def _pools_body_without_cld():
    body = json.loads(json.dumps(_POOLS_BODY))
    body["pools"]["fast"]["candidates"] = [
        c for c in body["pools"]["fast"]["candidates"] if c["id"] != "cld"]
    return body


class TestP1PartialRender:
    def test_structure_unchanged_skips_innerhtml_rewrite(self, run_panel):
        out = run_panel(_ops_routes(), poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"})
        # 仅首屏重建一次；轮询结构未变 → applyDynamic 定点更新，不重写 innerHTML
        assert out["writeCounts"].get("mpPools") == 1

    def test_structure_change_triggers_rebuild(self, run_panel):
        routes = _ops_routes()
        routes["/admin/model-pools"] = {"status": 200, "bodies": [
            _POOLS_BODY, _pools_body_without_cld()]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        # 候选移除 → 签名变 → 整块重建
        assert out["writeCounts"].get("mpPools") == 2
        assert "prov/cloud-m" not in out["htmlFinal"]

    def test_apply_dynamic_updates_lamp_and_chosen_by_id(self, run_panel):
        # openai chosen: loc → cld（回退）→ lamp warn + chosen 文本更新，
        # 且结构未变（不重写 innerHTML）
        status2 = json.loads(json.dumps(_STATUS_BODY))
        status2["pools"]["fast"]["protocols"]["openai"]["chosen"] = "cld"
        routes = _ops_routes()
        routes["/admin/model-pools/status"] = {"status": 200, "bodies": [
            _STATUS_BODY, status2]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert out["writeCounts"].get("mpPools") == 1
        dyn = out["dyn"]
        assert dyn["mp-lamp-fast-openai"]["className"] == "mp-lamp mp-lamp-warn"
        assert "cld" in dyn["mp-chosen-fast-openai"]["textContent"]

    def test_local_countdown_decrements_without_fetch(self, run_panel):
        out = run_panel(_ops_routes(), poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        dyn = out["dyn"]
        # 冷却 loc：remaining 42 → 1 次本地滴答后 0:41
        assert "0:41" in dyn["mp-rt-fast-loc"]["textContent"], \
            dyn["mp-rt-fast-loc"]["textContent"]
        # 环形 --p = 41/42 ≈ 98（首见锚定总时长，客户端递减）
        assert int(dyn["mp-ring-fast-loc"]["style"]["--p"]) == 98
        # 倒计时不发出任何 fetch：初次渲染 5 GET（latency/events best-effort）
        # + 1 次 poll tick 5 GET（P3 Task 9 起 events 为第 5 个常规拉取）
        gets = [c for c in out["calls"] if c["method"] == "GET"]
        assert len(gets) == 10


# ── UI/UX P1 修复 pass（交叉评审 glm-5.3-flash tathr/gpt-6.1-sol 发现，逐条 RED→GREEN）──
# Important 8 条：ring 被 textContent 删除 / 冷却转换不增删环与降级样式 /
# sig 不含 target 等字段 / decisions 不在 sig / 失败路径不失效 _lastSig /
# banner 内容变化不重建 / 倒计时未按真实时间锚定 / 冷却重锚 + 除零。
# Minor 2 条缓期（wizard/undo catch 会话校验、harness 子节点保真度）。


class TestP1FixPass:
    def test_ring_is_sibling_of_runtime_text(self, run_panel):
        # I1：mp-rt textContent 更新不得删除 mp-ring —— 环必须是兄弟节点
        out = run_panel(_ops_routes())
        m = re.search(r'id="mp-rt-fast-loc">[^<]*</span>\s*'
                      r'(?:<span class="mp-spark-wrap"[^>]*>[^<]*'
                      r'(?:<svg.*?</svg>)?[^<]*</span>\s*)?'
                      r'<span class="mp-ring" id="mp-ring-fast-loc"',
                      out["html"])
        assert m, "ring must be a sibling after the closing mp-rt span"

    def test_cooling_start_adds_ring_and_degraded(self, run_panel):
        # I2：冷却开始（运行态变化）→ 重建：出现环 + 虚线降级
        rt2 = json.loads(json.dumps(_RUNTIME_BODY))
        for rc in rt2["pools"]["fast"]["candidates"]:
            rc["cooling"] = None
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            {"revision": 3, "pools": {"fast": {"candidates": [
                {"id": "loc", "kind": "local", "target": "qwen-small",
                 "active": True, "cooling": None, "last_attempt": None},
                {"id": "cld", "kind": "cloud", "target": "prov/cloud-m",
                 "active": False, "cooling": None, "last_attempt": None}]}}},
            _RUNTIME_BODY]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert "mp-ring-fast-loc" in out["htmlFinal"]
        assert "mp-cand-degraded" in out["htmlFinal"]

    def test_cooling_end_removes_ring_and_degraded(self, run_panel):
        # I2：冷却结束 → 环与虚线降级消失
        rt_none = json.loads(json.dumps(_RUNTIME_BODY))
        for rc in rt_none["pools"]["fast"]["candidates"]:
            rc["cooling"] = None
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            _RUNTIME_BODY, rt_none]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert "mp-ring-fast-loc" not in out["htmlFinal"]
        assert "mp-cand-degraded" not in out["htmlFinal"]

    def test_candidate_target_change_rebuilds(self, run_panel):
        # I3：sig 不含 target → 外部改目标后刷新仍显示旧目标
        p2 = json.loads(json.dumps(_POOLS_BODY))
        p2["pools"]["fast"]["candidates"][0]["target"] = "qwen-big"
        routes = _ops_routes()
        routes["/admin/model-pools"] = {"status": 200, "bodies": [
            _POOLS_BODY, p2]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert "qwen-big" in out["htmlFinal"]

    def test_fetch_error_invalidates_sig(self, run_panel):
        # I5：失败路径写 innerHTML 后必须失效 _lastSig，否则恢复后停留错误页
        routes = _ops_routes()
        routes["/admin/model-pools"] = {"statuses": [200, 500, 200],
                                        "bodies": [_POOLS_BODY, {}, _POOLS_BODY]}
        out = run_panel(routes, poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"})
        assert "qwen-small" in out["htmlFinal"]
        assert "mp-action-banner" not in out["htmlFinal"]

    def test_banner_message_change_rebuilds(self, run_panel):
        # I6：banner 内容变化 → 重建（否则旧原因 + 新行动闭包不一致）
        routes = _routes()
        routes["/admin/model-pools/toggle"] = {
            "statuses": [500, 500],
            "bodies": [{"error": "boom A"}, {"error": "boom B"}]}
        out = run_panel(routes, toggle={"pool": "fast", "enable": False},
                        actions=[{"fn": "doMpBannerAction"}])
        assert "boom B" in out["htmlFinal"]
        assert "boom A" not in out["htmlFinal"]

    def test_countdown_anchored_to_real_elapsed_time(self, run_panel):
        # I7：以 runtime 拉取时刻为锚按真实流逝递减（每计时器 +5s）
        out = run_panel(_ops_routes(), poll_ticks=1, time_step_ms=5000,
                        store_state={"tab_active": "tab-model-pools"})
        assert "0:37" in out["dyn"]["mp-rt-fast-loc"]["textContent"], \
            out["dyn"]["mp-rt-fast-loc"]["textContent"]
        assert int(out["dyn"]["mp-ring-fast-loc"]["style"]["--p"]) == 88

    def test_cooling_reanchor_on_longer_cycle(self, run_panel):
        # I8a：新一轮更长冷却 → 重锚总时长（--p 不超 100）
        rt2 = json.loads(json.dumps(_RUNTIME_BODY))
        rt2["pools"]["fast"]["candidates"][0]["cooling"]["remaining_s"] = 50.0
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            _RUNTIME_BODY, rt2]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert int(out["dyn"]["mp-ring-fast-loc"]["style"]["--p"]) == 98  # 49/50

    def test_cooling_zero_remaining_no_nan(self, run_panel):
        # I8b：remaining_s=0 → --p 为 0 而非 NaN（除零防护）
        rt0 = json.loads(json.dumps(_RUNTIME_BODY))
        rt0["pools"]["fast"]["candidates"][0]["cooling"]["remaining_s"] = 0.0
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "body": rt0}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert out["dyn"]["mp-ring-fast-loc"]["style"]["--p"] == "0"


# ── UI/UX P2 Task 1：A2 行内 diff 编辑（两步换模，不离开列表）──
# 契约：
# - 每张候选卡内嵌「替换为…」下拉（目录手势惰性预热：普通渲染/轮询零目录
#   请求 — Task 1 裁决，保既有 GET 计数契约；预热失败 → 禁用 + 提示）
# - 选中 → 候选卡下方出 mp-inline-diff 条（旧→新 红/绿）+ 自动预览
#   （validate + dry-run×2 携带替换后 work — 三段式门禁不放松）
# - 应用：CAS POST（基点=选中时刻 revision）；成功 → 记 _undo + 清条 +
#   刷新目录；409 → A3 横幅（重新选择）+ 条保留
# - 预览未通过 → 应用按钮禁用，不 POST


class TestP2InlineEdit:
    def _inline_routes(self, **kw):
        routes = _editor_routes(**kw)
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        return routes

    def test_inline_select_present_with_catalog_options(self, run_panel):
        # 手势预热（box mouseover/focusin → primeInline）后下拉出目录选项；
        # 普通渲染期不产生目录请求（既有轮询 GET 计数契约）
        out = run_panel(self._inline_routes(),
                        actions=[{"fn": "mpPrimeInline"}])
        # 每张候选卡内嵌「替换为…」下拉（行内编辑入口）
        assert 'data-mp-inline-replace="fast|loc"' in out["htmlFinal"]
        assert 'value="local:qwen3-4b"' in out["htmlFinal"]
        assert 'value="cloud:tathr-glm/glm-5"' in out["htmlFinal"]

    def test_inline_select_disabled_without_catalog(self, run_panel):
        # 预热失败（路由缺失 → 拉取失败）→ 下拉禁用 + 明示，不出可选项
        out = run_panel(_editor_routes(),
                        actions=[{"fn": "mpPrimeInline"}])
        assert "目录不可用" in out["htmlFinal"]
        assert 'value="local:' not in out["htmlFinal"]

    def test_prime_throttled_after_catalog_failure(self, run_panel):
        # 终审 I3：目录不可用时 high-frequency 手势（mouseover 风暴）不得
        # 逐次重放 fetch+render — 5s 冷却内至多 1 次目录请求；
        # 失败后的禁用态仍须当轮渲染（契约不变）
        out = run_panel(_editor_routes(),
                        actions=[{"fn": "mpPrimeInline"}] * 5)
        cat_gets = [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools/catalog"]
        assert len(cat_gets) == 1
        assert "目录不可用" in out["htmlFinal"]

    def test_inline_select_shows_diff_bar_with_auto_preview(self, run_panel):
        actions = [{"fn": "doMpInlineSelect",
                    "args": ["fast", "loc", "local:qwen3-4b"]}]
        out = run_panel(self._inline_routes(), actions=actions)
        h = out["htmlFinal"]
        assert "mp-inline-diff" in h
        assert "mp-diff-old" in h and "qwen-small" in h   # 旧目标（红）
        assert "mp-diff-new" in h and "qwen3-4b" in h     # 新目标（绿）
        assert 'data-mp-inline-apply="fast|loc"' in h
        assert 'data-mp-inline-cancel' in h
        # 自动预览：dry-run×2 携带替换后的 work（三段式门禁）
        drys = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/dry-run"]
        assert len(drys) == 2
        for c in drys:
            assert c["body"]["config"]["pools"]["fast"]["candidates"][0] \
                ["model_name"] == "qwen3-4b"

    def test_inline_apply_posts_replaced_candidate_with_cas(self, run_panel):
        actions = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpInlineApply"},
            {"fn": "mpPrimeInline"},   # 应用后目录失效 → 下次手势重取
        ]
        out = run_panel(self._inline_routes(), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        cands = body["pools"]["fast"]["candidates"]
        new = next(c for c in cands if c["id"] == "loc")
        assert new["model_name"] == "qwen3-4b"
        assert new["priority"] == 1                    # D4：继承原优先级
        assert new["declared_protocols"] == ["openai", "anthropic"]
        assert "dang" in [c["id"] for c in cands]      # raw 基底防丢数据
        assert body["expected_revision"] == 3          # 选中时刻 CAS 基点
        # 成功：条清空 + toast；目录缓存失效（下次手势重取）
        assert "mp-inline-diff" not in out["htmlFinal"]
        assert any("已更新 iff/fast" in t["message"] for t in out["toasts"])
        cat_gets = [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools/catalog"]
        assert len(cat_gets) >= 2

    def test_inline_apply_blocked_when_preview_fails(self, run_panel):
        routes = self._inline_routes()
        routes["POST /admin/model-pools/dry-run"] = {
            "status": 200,
            "body": {"chosen": None, "error": "no_compatible_target",
                     "decisions": [], "pool": "fast", "protocol": "openai"}}
        actions = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpInlineApply"},
        ]
        out = run_panel(routes, actions=actions)
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools"
                    and c["method"] == "POST"]
        # 应用按钮禁用（属性级）+ 阻止原因在条内可见
        m = re.search(r'data-mp-inline-apply="fast\|loc"[^>]*>',
                      out["htmlFinal"])
        assert m and "disabled" in m.group(0)
        assert "无可用候选" in out["htmlFinal"]

    def test_inline_apply_409_shows_banner_keeps_bar(self, run_panel):
        routes = self._inline_routes(
            apply_status=409,
            apply_body={"status": "revision_conflict", "current_revision": 5})
        actions = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpInlineApply"},
        ]
        out = run_panel(routes, actions=actions)
        assert "mp-action-banner" in out["htmlFinal"]   # A3 横幅
        assert 'data-mp-banner-action' in out["htmlFinal"]
        assert "mp-inline-diff" in out["htmlFinal"]     # 条保留（可重新选择）

    def test_inline_cancel_clears_bar(self, run_panel):
        actions = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpInlineCancel"},
        ]
        out = run_panel(self._inline_routes(), actions=actions)
        assert "mp-inline-diff" not in out["htmlFinal"]

    def test_inline_apply_pins_select_time_revision(self, run_panel):
        # Review Focus #1：选中后后台轮询推进 revision → apply 仍用选中时
        # 基点（不得借新 revision 让陈旧 work 覆盖他人修改）
        pools_bodies = [_editor_pools_body(), _editor_pools_body(revision=4)]
        routes = self._inline_routes()
        routes["/admin/model-pools"] = {"status": 200, "bodies": pools_bodies}
        actions = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "renderModelPools", "args": []},   # 模拟后台刷新 rev=4
            {"fn": "doMpInlineApply"},
        ]
        out = run_panel(routes, actions=actions)
        body = _last_update_post(out)[-1]["body"]
        assert body["expected_revision"] == 3


class TestP2Optimistic:
    """B2 乐观 UI + 切换中骨架态：请求瞬间即反馈（灯/骨架），完成回真值。

    契约（captureAtFetch — 每次请求瞬间快照元素 className）：
    - toggle 发请求前：池卡进入 mp-busy 骨架态；协议灯直写乐观目标类
      （启用→mp-lamp-ok / 停用→mp-lamp-warn，与 lampForProtocol 同词表）
    - 响应完成（无论成败）：乐观态清除（fetch 段 finally），重渲染回真值
    - 行内应用（A2 apply）同样在途 markBusy、完成清除
    """

    def _cat_routes(self, **kw):
        routes = _editor_routes(**kw)
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        return routes

    def test_toggle_shows_optimistic_lamp_and_busy_during_post(self, run_panel):
        out = run_panel(_routes(), toggle={"pool": "fast", "enable": True},
                        capture_at_fetch=["mp-lamp-fast-openai",
                                          "mp-card-fast"])
        # POST 在途瞬间：灯已是乐观目标类、卡带骨架 busy
        post = [s for s in out["fetchSnapshots"]
                if s["method"] == "POST"
                and s["url"] == "/admin/model-pools/toggle"]
        assert len(post) == 1
        assert "mp-lamp-ok" in post[0]["cls"]["mp-lamp-fast-openai"]
        assert "mp-busy" in post[0]["cls"]["mp-card-fast"]
        # 响应完成后：重拉渲染，乐观态清除回服务端真值
        gets = [s for s in out["fetchSnapshots"]
                if s["method"] == "GET" and s["url"] == "/admin/model-pools"]
        assert gets
        assert "mp-busy" not in gets[-1]["cls"]["mp-card-fast"]
        assert "mp-lamp-ok" not in gets[-1]["cls"]["mp-lamp-fast-openai"]

    def test_toggle_failure_reverts_optimistic_state(self, run_panel):
        routes = _routes(toggle_status=500, toggle_body={"error": "boom"})
        out = run_panel(routes, toggle={"pool": "fast", "enable": False},
                        capture_at_fetch=["mp-lamp-fast-anthropic",
                                          "mp-card-fast"])
        post = [s for s in out["fetchSnapshots"]
                if s["method"] == "POST"
                and s["url"] == "/admin/model-pools/toggle"]
        assert "mp-lamp-warn" in post[0]["cls"]["mp-lamp-fast-anthropic"]
        assert "mp-busy" in post[0]["cls"]["mp-card-fast"]
        # 失败：回滚乐观态 + A3 横幅 + busy 清除
        assert "mp-action-banner" in out["htmlFinal"]
        gets = [s for s in out["fetchSnapshots"]
                if s["method"] == "GET" and s["url"] == "/admin/model-pools"]
        assert gets
        assert "mp-busy" not in gets[-1]["cls"]["mp-card-fast"]
        assert "mp-lamp-warn" not in gets[-1]["cls"]["mp-lamp-fast-anthropic"]
        assert "mp-busy" not in out["htmlFinal"]

    def test_inline_apply_marks_pool_busy(self, run_panel):
        actions = [
            {"fn": "doMpInlineSelect", "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpInlineApply"},
        ]
        out = run_panel(self._cat_routes(), actions=actions,
                        capture_at_fetch=["mp-card-fast"])
        post = [s for s in out["fetchSnapshots"]
                if s["method"] == "POST" and s["url"] == "/admin/model-pools"]
        assert len(post) == 1
        assert "mp-busy" in post[0]["cls"]["mp-card-fast"]
        # 成功后重拉：busy 清除
        gets = [s for s in out["fetchSnapshots"]
                if s["method"] == "GET" and s["url"] == "/admin/model-pools"]
        assert gets
        assert "mp-busy" not in gets[-1]["cls"]["mp-card-fast"]


class TestP2WizardExit:
    """A4 向导出口衔接：提交成功 → 成果卡（不直接跳走），手动「完成」返回。

    契约：
    - 成果卡：mp-wizard-done + 「已创建 N 个逻辑模型池」+ 每池 iff/<pool>
      一行 + 每池「复制 OpenAI 片段」直通按钮（data-mp-copy-pool）+ 完成
      按钮（data-mp-wizard-done）；用户拍板：不自动淡出（复制会被打断）
    - 直通复制 = snippetText(pool, 'openai') 入剪贴板
    - 完成 → 正常池列表重渲染（服务端已配置）
    - Review Focus #5：成果卡在屏时 5s 轮询必须跳过，不得冲掉成果卡
    """

    def test_wizard_submit_success_shows_result_card(self, run_panel):
        out = run_panel(_wizard_routes(),
                        actions=[{"fn": "doMpWizardSubmit"}])
        h = out["htmlFinal"]
        assert "mp-wizard-done" in h
        assert "已创建 4 个逻辑模型池" in h
        for pool in ("fast", "premium", "small", "classifier"):
            assert "iff/" + pool in h, pool
        assert h.count("data-mp-copy-pool=") == 4
        assert "复制 OpenAI 片段" in h
        assert 'data-mp-wizard-done' in h          # 「完成」按钮
        # 不直接跳走：列表未渲染
        assert "data-mp-pool=" not in h

    def test_result_card_copy_uses_snippet_text(self, run_panel):
        actions = [
            {"fn": "doMpWizardSubmit"},
            {"fn": "doMpWizardDoneCopy", "args": ["fast"]},
        ]
        out = run_panel(_wizard_routes(), actions=actions)
        assert out["clipboardWrites"]
        text = out["clipboardWrites"][-1]
        assert "iff/fast" in text                   # 该池的片段
        assert "from openai import OpenAI" in text  # OpenAI 直通
        assert "127.0.0.1:8999/v1" in text

    def test_result_card_done_returns_to_list(self, run_panel):
        # 完成后服务端已有配置（第 2 次 GET 返回已配置池）
        routes = _wizard_routes()
        routes["/admin/model-pools"] = {
            "status": 200, "bodies": [_WIZARD_POOLS_BODY, _POOLS_BODY]}
        actions = [
            {"fn": "doMpWizardSubmit"},
            {"fn": "doMpWizardDone"},
        ]
        out = run_panel(routes, actions=actions)
        h = out["htmlFinal"]
        assert "mp-wizard-done" not in h
        assert 'data-mp-pool="fast"' in h           # 正常池列表回来

    def test_poll_skips_while_result_card_shown(self, run_panel):
        out = run_panel(_wizard_routes(),
                        actions=[{"fn": "doMpWizardSubmit"}],
                        poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"})
        # Review Focus #5：轮询推进不冲掉成果卡
        assert "mp-wizard-done" in out["htmlFinal"]


class TestP2Keyboard:
    """B4 键盘流：E 进编辑 / Esc 分级退出；表单控件内不劫持输入。

    契约（harness fireKey — 派发 keydown 到 document 监听）：
    - E：焦点元素归属 data-mp-card 池 → doMpEdit(pool)；INPUT/SELECT/
      TEXTAREA 聚焦时忽略（不劫持输入）；仅本 tab 激活时生效
    - Esc 一级关行内 diff 条（模态不动）；二级关编辑器模态
    - 未过预览的应用按钮（行内条 + 模态编辑器）disabled + title 原因
    """

    def _kb_routes(self):
        routes = _editor_routes()
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        return routes

    def test_key_e_opens_editor_for_focused_pool(self, run_panel):
        actions = [{"fn": "fireKey",
                    "args": ["e", "mp-card-fast", {"data-mp-card": "fast"}]}]
        out = run_panel(_routes(), actions=actions,
                        store_state={"tab_active": "tab-model-pools"})
        assert 'data-mp-editor="fast"' in out["modalHtml"]

    def test_key_e_ignored_in_form_controls(self, run_panel):
        state = {"tab_active": "tab-model-pools"}
        # SELECT/INPUT 聚焦 → E 被忽略（不劫持输入）
        out = run_panel(_routes(),
                        actions=[{"fn": "fireKey",
                                  "args": ["e", "mp-any-sel", None, "SELECT"]},
                                 {"fn": "fireKey",
                                  "args": ["e", "mp-any-in", None, "INPUT"]}],
                        store_state=state)
        assert 'data-mp-editor=' not in out["modalHtml"]
        # 对照组：非表单控件同键 → 打开（证明忽略是针对性的而非功能缺失）
        out2 = run_panel(_routes(),
                         actions=[{"fn": "fireKey",
                                   "args": ["e", "mp-card-fast",
                                            {"data-mp-card": "fast"}]}],
                         store_state=state)
        assert 'data-mp-editor="fast"' in out2["modalHtml"]

    def test_esc_closes_inline_bar_first_then_modal(self, run_panel):
        routes = self._kb_routes()
        state = {"tab_active": "tab-model-pools"}
        base = [
            {"fn": "doMpInlineSelect",
             "args": ["fast", "loc", "local:qwen3-4b"]},
            {"fn": "doMpEdit", "args": ["fast"]},
        ]
        # 第一次 Esc：只关行内条（模态不动）
        out1 = run_panel(routes, actions=base + [{"fn": "fireKey",
                                                  "args": ["Escape"]}],
                         store_state=state)
        assert "mp-inline-diff" not in out1["htmlFinal"]
        assert 'data-mp-editor="fast"' in out1["modalHtml"]
        # 第二次 Esc：关模态
        out2 = run_panel(routes, actions=base + [
            {"fn": "fireKey", "args": ["Escape"]},
            {"fn": "fireKey", "args": ["Escape"]}],
            store_state=state)
        assert 'data-mp-editor=' not in out2["modalHtml"]
        assert out2["modalHtml"] == ""

    def test_apply_button_disabled_has_reason_title(self, run_panel):
        state = {"tab_active": "tab-model-pools"}
        # 模态编辑器：刚打开未预览 → 应用 disabled + title 原因
        out = run_panel(self._kb_routes(),
                        actions=[{"fn": "doMpEdit", "args": ["fast"]}],
                        store_state=state)
        m = re.search(r'data-mp-act="apply"[^>]*>', out["modalHtml"])
        assert m and "disabled" in m.group(0)
        assert m and "预览" in m.group(0)
        # 行内条：预览失败 → 应用 disabled + title 原因
        routes = self._kb_routes()
        routes["POST /admin/model-pools/dry-run"] = {
            "status": 200,
            "body": {"chosen": None, "error": "no_compatible_target",
                     "decisions": [], "pool": "fast", "protocol": "openai"}}
        out2 = run_panel(routes,
                         actions=[{"fn": "doMpInlineSelect",
                                   "args": ["fast", "loc",
                                            "local:qwen3-4b"]}],
                         store_state=state)
        m2 = re.search(r'data-mp-inline-apply="fast\|loc"[^>]*>',
                       out2["htmlFinal"])
        assert m2 and "disabled" in m2.group(0)
        assert m2 and "预览" in m2.group(0)


class TestCatalogConsumption:
    """v7.1.2 候选目录：向导/编辑器只消费 /admin/model-pools/catalog。"""

    def test_editor_cloud_options_from_catalog(self, run_panel):
        routes = _editor_routes()
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        out = run_panel(routes, actions=[{"fn": "doMpEdit", "args": ["fast"]}])
        # 云候选出现在替换下拉（上一版刮错字段 → 恒空）
        assert "cloud:tathr-glm/glm-5" in out["modalHtml"]
        assert "cloud:Qianfan-Token/deepseek-v4.1-flash" in out["modalHtml"]

    def test_single_catalog_source_no_legacy_scraping(self, run_panel):
        # 架构契约：目录只来自 catalog 端点 — 不得再刮 /models 与
        # /admin/cloud/providers（字段属别的域，逆向猜 schema 会再断）
        routes = _editor_routes()
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        actions = [{"fn": "doMpEdit", "args": ["fast"]},
                   {"fn": "doMpUndo"}]
        run_panel(routes, actions=actions)   # undo 前置 renderModelPools
        wizard_routes = _wizard_routes()
        wizard_routes["/admin/model-pools/catalog"] = {
            "status": 200, "body": _CATALOG_LOCAL_ONLY_CLOUD}
        out = run_panel(wizard_routes)
        for o in (run_panel(routes, actions=actions), out):
            urls = {c["url"] for c in o["calls"]}
            assert "/admin/model-pools/catalog" in urls
            assert "/admin/cloud/providers" not in urls
            assert "/models" not in urls

    def test_wizard_cloud_options_from_catalog(self, run_panel):
        routes = _wizard_routes()
        routes["/admin/model-pools/catalog"] = {
            "status": 200, "body": _CATALOG_LOCAL_ONLY_CLOUD}
        out = run_panel(routes)
        assert "cloud:tathr-glm/glm-5" in out["html"]
        assert "云 · tathr-glm/glm-5" in out["html"]

    def test_wizard_cloud_candidate_carries_catalog_protocols(
            self, run_panel):
        # 云候选协议来自发现结果（openai+anthropic），不再硬编码 openai —
        # 硬编码会让双协议云模型在 anthropic 下被证据系统误排除
        routes = _wizard_routes()
        routes["/admin/model-pools/catalog"] = {
            "status": 200, "body": _CATALOG_LOCAL_ONLY_CLOUD}
        out = run_panel(routes,
                        actions=[{"fn": "doMpWizardSubmit", "args": []}])
        body = _last_update_post(out)[-1]["body"]
        cand = next(iter(body["pools"].values()))["candidates"][0]
        assert cand["provider"] == "tathr-glm"
        assert cand["model_id"] == "glm-5"
        assert cand["declared_protocols"] == ["openai", "anthropic"]

    def test_non_routable_alias_never_offered(self, run_panel):
        # routable-only（用户决策）：目录只含 cloud_models（可路由集），
        # glm-5-failover 这类手动回退别名不在其中 — 面板不得出现它
        routes = _editor_routes()
        routes["/admin/model-pools/catalog"] = {"status": 200,
                                                "body": _CATALOG_BODY}
        out = run_panel(routes, actions=[{"fn": "doMpEdit", "args": ["fast"]}])
        assert "failover" not in out["modalHtml"]

    def test_catalog_fetch_failure_degrades_without_blocking(
            self, run_panel):
        # catalog 端点故障 → 空目录 + 手填提示，页面不崩
        routes = _editor_routes()
        routes["/admin/model-pools/catalog"] = {"status": 500, "body": {}}
        out = run_panel(routes, actions=[{"fn": "doMpEdit", "args": ["fast"]}])
        assert "mp-editor" in out["modalHtml"]
        assert "目录不可用" in out["modalHtml"]


_NEW_CAND = {"id": "newloc", "kind": "local", "model_name": "qwen3-32b",
             "declared_protocols": ["openai", "anthropic"],
             "features": ["tools"]}


def _editor_pools_body(revision=3):
    body = json.loads(json.dumps(_POOLS_BODY))
    body["revision"] = revision
    body["configured"] = True
    body["load_error"] = None
    body["raw"] = json.loads(json.dumps(_EDITOR_RAW))
    return body


def _editor_routes(pools_body=None, pools_bodies=None, validate=None,
                   apply_status=200, apply_body=None, status_body=None):
    """编辑器路由。pools_bodies：时序路由（GET 第 N 次返回第 N 个 body）。"""
    pools_route = ({"status": 200, "bodies": pools_bodies}
                   if pools_bodies else None)
    return {
        "/admin/model-pools": pools_route or {
            "status": 200, "body": pools_body or _editor_pools_body()},
        "/admin/model-pools/status": {
            "status": 200, "body": status_body or {"revision": 3, "pools": {}}},
        "/admin/model-pools/runtime": {"status": 200,
                                       "body": {"revision": 3, "pools": {}}},
        "POST /admin/model-pools/validate": {
            "status": 200,
            "body": validate or {"valid": True, "issues": []}},
        "POST /admin/model-pools/dry-run": {
            "status": 200,
            "body": {"chosen": "newloc", "error": None, "decisions": [],
                     "pool": "fast", "protocol": "openai"}},
        "POST /admin/model-pools": {
            "status": apply_status,
            "body": apply_body or {"status": "updated", "revision": 4}},
    }


def _last_update_post(out):
    posts = [c for c in out["calls"]
             if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
    assert posts, "editor must POST /admin/model-pools"
    return posts


class TestEditor:
    def test_editor_replace_candidate_sends_new_config(self, run_panel):
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        cands = body["pools"]["fast"]["candidates"]
        ids = [c["id"] for c in cands]
        assert "newloc" in ids and "loc" not in ids
        # D4 一键换模：直接替换，继承原优先级
        new = next(c for c in cands if c["id"] == "newloc")
        assert new["priority"] == 1
        assert new["model_name"] == "qwen3-32b"
        assert body["expected_revision"] == 3
        # 预览阶段 dry-run×2（openai + anthropic）携带编辑态 config
        drys = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/dry-run"]
        assert len(drys) == 2
        assert sorted(c["body"]["protocol"] for c in drys) == \
            ["anthropic", "openai"]
        for c in drys:
            assert c["body"]["pool"] == "fast"
            assert c["body"]["config"]["pools"]["fast"]["candidates"][0]["id"] \
                == "newloc"

    def test_editor_preview_blocked_on_issues(self, run_panel):
        validate = {"valid": False, "issues": ["duplicate candidate id 'x'"]}
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(validate=validate), actions=actions)
        # 校验未通过 → 不发 dry-run、不发更新
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools/dry-run"]
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
        # 问题在预览区可见
        assert "duplicate candidate id" in out["previewHtml"]
        assert any("校验" in t["message"] for t in out["toasts"])

    def test_editor_apply_409_prompts_reload(self, run_panel):
        routes = _editor_routes(
            apply_status=409,
            apply_body={"status": "revision_conflict", "current_revision": 5})
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(routes, actions=actions)
        assert any("冲突" in t["message"] for t in out["toasts"])
        # 冲突 → 重新拉取让用户看到最新状态
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools" and c["method"] == "GET"]
        assert len(gets) >= 2

    def test_editor_undo_reposts_previous_config(self, run_panel):
        # 时序路由：首屏 GET revision=3；apply 成功后的重取 revision=4
        pools_bodies = [_editor_pools_body(), _editor_pools_body(revision=4)]

        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
            {"fn": "doMpUndo", "args": []},
        ]
        out = run_panel(_editor_routes(pools_bodies=pools_bodies), actions=actions)
        posts = _last_update_post(out)
        assert len(posts) == 2          # apply + undo
        undo_body = posts[1]["body"]
        ids = [c["id"] for c in undo_body["pools"]["fast"]["candidates"]]
        assert "loc" in ids and "newloc" not in ids   # 回滚到编辑前
        # undo 以重新拉取到的最新 revision 做 CAS（不是 apply 时的旧值）
        assert undo_body["expected_revision"] == 4

    def test_editor_base_is_raw_not_view(self, run_panel):
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        ids = [c["id"] for c in body["pools"]["fast"]["candidates"]]
        # raw 基底：视图剔除的悬空候选必须保留（防丢数据）
        assert "dang" in ids
        # 视图独有、raw 没有的候选不得经编辑混入
        assert "cld" not in ids

    def test_editor_apply_pins_open_revision(self, run_panel):
        # 打开编辑器时 revision=3；随后后台刷新看到 revision=4（他人已更新）
        pools_bodies = [_editor_pools_body(), _editor_pools_body(revision=4)]
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "renderModelPools", "args": []},   # 模拟后台轮询刷新
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(pools_bodies=pools_bodies), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        # apply 必须用编辑器打开时捕获的 revision；后台刷新推进 _revision
        # 不得让陈旧工作副本借新 revision 通过 CAS 覆盖他人修改（安全评审）
        assert body["expected_revision"] == 3

    def test_editor_unconfigured_empty_base(self, run_panel):
        pools = {"revision": 0, "pools": {}, "configured": False,
                 "load_error": None, "raw": None}
        add_cand = {"id": "c1", "kind": "local", "model_name": "qwen-small",
                    "declared_protocols": ["openai"], "features": []}
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditAddCandidate", "args": [add_cand]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(pools_body=pools), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        assert body["schema_version"] == 1
        cands = body["pools"]["fast"]["candidates"]
        assert [c["id"] for c in cands] == ["c1"]
        assert cands[0]["priority"] == 1
        assert body["expected_revision"] == 0   # 从未配置 → revision 0


# ── Task 7：空态初始化向导（从未配置 → 四角色引导创建）──
# 契约：
# - configured=false + raw=null（从未配置）→ 向导：fast/premium/small/classifier
#   四张角色卡（可移除可恢复），每卡候选下拉带预填启发式
#   （fast=活跃小模型 / premium=大上下文本地或云 / small=最小 / classifier=活跃）
# - 提交：CAS POST expected_revision=0；无候选的角色跳过
# - 文件损坏（load_error≠null）→ 只出错误横幅，不出向导、零 POST

# 目录 schema（build_catalog 真实输出契约）— 不再刮 /models（v7.1.2）
_WIZARD_CATALOG = {
    "local": [
        {"name": "NI-Qwen38-27B", "active": False, "engine": "ninfer",
         "modality": "text", "context_window": 262144,
         "protocols": ["openai"]},
        {"name": "qwen3-4b", "active": True, "engine": "vllm",
         "modality": "text", "context_window": 32768,
         "protocols": ["openai", "anthropic"]},
    ],
    "cloud": [],
}

_WIZARD_POOLS_BODY = {"revision": 0, "pools": {}, "configured": False,
                      "load_error": None, "raw": None}


def _wizard_routes(pools_body=None, catalog=None):
    return {
        "/admin/model-pools": {
            "status": 200, "body": pools_body or _WIZARD_POOLS_BODY},
        "/admin/model-pools/status": {
            "status": 200, "body": {"revision": 0, "pools": {}}},
        "/admin/model-pools/runtime": {
            "status": 200, "body": {"revision": 0, "pools": {}}},
        "/admin/model-pools/catalog": {
            "status": 200, "body": catalog if catalog is not None
            else _WIZARD_CATALOG},
        "POST /admin/model-pools": {
            "status": 200, "body": {"status": "updated", "revision": 1}},
    }


class TestWizard:
    def test_fragment_contains_modal_container(self):
        frag = (ROOT / "inferfabric" / "dashboard" / "fragments"
                / "model-pools.html").read_text(encoding="utf-8")
        assert 'id="mpModal"' in frag   # 编辑器模态容器存在

    def test_wizard_renders_four_roles(self, run_panel):
        out = run_panel(_wizard_routes())
        for role in ("fast", "premium", "small", "classifier"):
            assert 'data-mp-role="' + role + '"' in out["html"], role
        # 角色中文说明 + 候选下拉 + 引导文案
        assert "快速" in out["html"]
        assert "旗舰" in out["html"]

    def test_wizard_submit_posts_expected_revision_zero(self, run_panel):
        out = run_panel(_wizard_routes(),
                        actions=[{"fn": "doMpWizardSubmit"}])
        body = _last_update_post(out)[-1]["body"]
        assert body["expected_revision"] == 0     # 从未配置 → revision 0
        assert body["schema_version"] == 1
        assert sorted(body["pools"].keys()) == \
            ["classifier", "fast", "premium", "small"]
        for role, pool in body["pools"].items():
            assert len(pool["candidates"]) == 1
            assert pool["candidates"][0]["priority"] == 1
        # 预填启发式：fast=活跃小模型；premium=大上下文
        assert body["pools"]["fast"]["candidates"][0]["model_name"] == "qwen3-4b"
        assert body["pools"]["premium"]["candidates"][0]["model_name"] \
            == "NI-Qwen38-27B"

    def test_wizard_removed_role_not_posted(self, run_panel):
        actions = [
            {"fn": "doMpWizardToggle", "args": ["classifier"]},
            {"fn": "doMpWizardSubmit"},
        ]
        out = run_panel(_wizard_routes(), actions=actions)
        body = _last_update_post(out)[-1]["body"]
        assert "classifier" not in body["pools"]
        assert "fast" in body["pools"]

    def test_broken_file_no_wizard_no_post(self, run_panel):
        pools = dict(_WIZARD_POOLS_BODY,
                     load_error="model_pools.yaml invalid: bad")
        actions = [{"fn": "doMpWizardSubmit"}]
        out = run_panel(_wizard_routes(pools_body=pools), actions=actions)
        assert "加载失败" in out["html"]            # 只出错误横幅
        assert 'data-mp-role=' not in out["html"]  # 不出向导
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools"
                    and c["method"] == "POST"]     # 零 POST


# ── Task 8：接入片段（客户端复制即用代码）──
# 契约：
# - doMpSnippet(pool)：#mpModal 内 OpenAI + Anthropic 两段可复制代码，
#   base_url 用 location.hostname（缺省 127.0.0.1）+ 8999
# - OpenAI base_url 带 /v1；Anthropic 用裸端口（再加会叠成 /v1/v1）
# - ACL 提示：API key 若配了 models ACL，需含 iff/<pool> 否则 401
# - doMpSnippetCopy(proto) → navigator.clipboard.writeText

class TestSnippet:
    def test_snippet_renders_both_protocols(self, run_panel):
        out = run_panel(_routes(), actions=[{"fn": "doMpSnippet",
                                             "args": ["fast"]}])
        m = out["modalHtml"]
        assert "iff/fast" in m
        assert "127.0.0.1:8999/v1" in m             # OpenAI base_url 带 /v1
        assert "from anthropic import Anthropic" in m
        assert "from openai import OpenAI" in m
        assert "models ACL" in m                    # ACL 提示
        assert "/v1/v1" in m                        # 双 /v1 警告

    def test_snippet_copy_uses_clipboard(self, run_panel):
        actions = [
            {"fn": "doMpSnippet", "args": ["fast"]},
            {"fn": "doMpSnippetCopy", "args": ["openai"]},
            {"fn": "doMpSnippetCopy", "args": ["anthropic"]},
        ]
        out = run_panel(_routes(), actions=actions)
        assert len(out["clipboardWrites"]) == 2
        assert "iff/fast" in out["clipboardWrites"][0]
        assert 'base_url="http://127.0.0.1:8999/v1"' in out["clipboardWrites"][0]
        assert 'base_url="http://127.0.0.1:8999"' in out["clipboardWrites"][1]
        assert any("复制" in t["message"] for t in out["toasts"])

    def test_snippet_pool_name_is_safe_literal(self, run_panel):
        # 敌意池名（含引号/分号/换行类负载）不得逃逸出 model 字面量 —
        # 复制的 Python 代码必须仍是单个完整字符串参数（安全评审）
        hostile = 'fast", max_tokens=1); import os; os.system("echo pwn'
        actions = [
            {"fn": "doMpSnippet", "args": [hostile]},
            {"fn": "doMpSnippetCopy", "args": ["openai"]},
            {"fn": "doMpSnippetCopy", "args": ["anthropic"]},
        ]
        out = run_panel(_routes(), actions=actions)
        assert len(out["clipboardWrites"]) == 2
        for text in out["clipboardWrites"]:
            m = re.search(r'^\s*model=(.*),?\s*$', text, re.M)
            assert m, "snippet missing model arg"
            lit = m.group(1).rstrip(",")
            # JSON 字符串字面量（Python 兼容转义）round-trip 回原名 → 无法注入
            assert json.loads(lit) == "iff/" + hostile

    def test_snippet_scheme_follows_dashboard_origin(self, run_panel):
        # dashboard 经 https 提供时（反代终结 TLS），片段不得降级成 http
        out = run_panel(_routes(),
                        actions=[{"fn": "doMpSnippet", "args": ["fast"]}],
                        location={"protocol": "https:",
                                  "hostname": "iff.example.com"})
        assert "https://iff.example.com:8999" in out["modalHtml"]
        assert "http://" not in out["modalHtml"].replace("https://", "")


# ── 终审修复 pass（评审 f94744b..2e36fdd 后的重要发现，逐条 RED→GREEN）──


class TestFixPassEditorGates:
    """I1 任意变更使预览失效；I2 双协议均无可用候选/HTTP 失败 → 阻止应用。"""

    def test_editor_mutation_after_preview_invalidates_and_blocks_apply(
            self, run_panel):
        # 预览通过后再改动 → validated 失效 → apply 必须拒绝（spec §3.2）
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditReplace", "args": ["loc", _NEW_CAND]},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(_editor_routes(), actions=actions)
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
        assert any("预览" in t["message"] for t in out["toasts"])

    def test_editor_apply_button_disabled_until_validated(self, run_panel):
        # 未通过预览时应用按钮禁用（防止误点绕过三段式纪律）
        actions = [{"fn": "doMpEdit", "args": ["fast"]}]
        out = run_panel(_editor_routes(), actions=actions)
        m = re.search(r'data-mp-act="apply"([^>]*)>', out["modalHtml"])
        assert m, "modal must render apply button"
        assert "disabled" in m.group(1)

    def test_editor_preview_both_protocols_unavailable_blocks_apply(
            self, run_panel):
        # spec §3.2：两协议均无可用候选 → 阻止应用（黄灯单协议缺失仍允许）
        routes = _editor_routes()
        routes["POST /admin/model-pools/dry-run"] = {
            "status": 200,
            "body": {"chosen": None, "error": "no_compatible_target",
                     "decisions": [], "pool": "fast", "protocol": "openai"},
        }
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(routes, actions=actions)
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
        assert "无可用候选" in out["previewHtml"]
        assert any("候选" in t["message"] for t in out["toasts"])

    def test_editor_preview_dryrun_http_error_blocks_apply(self, run_panel):
        # dry-run 请求 HTTP 失败 ≠ 预览通过 — 不得置 validated
        routes = _editor_routes()
        routes["POST /admin/model-pools/dry-run"] = {"status": 500, "body": {}}
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},
        ]
        out = run_panel(routes, actions=actions)
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools" and c["method"] == "POST"]

    def test_stale_apply_completion_preserves_second_session(self, run_panel):
        # I3 会话令牌：慢 apply 完成时编辑器已被重开 → 陈旧完成不得清空
        # 第二会话（toast 用捕获的池名；不碰 _edit/_undo）
        actions = [
            {"fn": "doMpEdit", "args": ["fast"]},
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": [], "noAwait": True},
            {"fn": "doMpEdit", "args": ["fast"]},      # 第二编辑会话
            {"fn": "mpEditPreview", "args": []},
            {"fn": "mpEditApply", "args": []},          # 第二会话必须能正常应用
        ]
        out = run_panel(_editor_routes(), actions=actions)
        posts = [c for c in out["calls"]
                 if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
        validates = [c for c in out["calls"]
                     if c["url"] == "/admin/model-pools/validate"]
        # 第二会话的预览与应用都必须真实发出（陈旧完成未清空 _edit）
        assert len(validates) == 2, "second session preview must run"
        assert len(posts) == 2, "second session apply must reach POST"
        assert sum("已更新 iff/fast" in t["message"] for t in out["toasts"]) == 2


class TestFixPassRuntimePolling:
    """I5 base.html 缺 <!-- JS:model-pools --> 占位符 + spec §6 5s 轮询。"""

    def test_all_js_modules_have_placeholders(self):
        # JS_MODULES 列出的模块必须在 base.html 有对应占位符，否则
        # get_html 静默不内联（本次 model-pools 即漏 — 功能整页不可用）
        import re as _re
        from inferfabric.dashboard import JS_MODULES
        base = (DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
        for name in JS_MODULES:
            assert _re.search(
                r"<!--\s*JS:" + _re.escape(name) + r"\s*-->", base), \
                f"base.html missing <!-- JS:{name} --> placeholder"

    def test_runtime_polling_refreshes_when_tab_active(self, run_panel):
        out = run_panel(_routes(), poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"})
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/runtime"]
        assert len(gets) == 3   # 首屏 1 + 2 次滴答

    def test_runtime_polling_skips_other_tabs(self, run_panel):
        out = run_panel(_routes(), poll_ticks=2,
                        store_state={"tab_active": "tab-overview"})
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/runtime"]
        assert len(gets) == 1   # 仅首屏

    def test_runtime_polling_skips_when_document_hidden(self, run_panel):
        out = run_panel(_routes(), poll_ticks=2, document_hidden=True,
                        store_state={"tab_active": "tab-model-pools"})
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/runtime"]
        assert len(gets) == 1   # 页面隐藏 → 暂停（spec §6）

    def test_runtime_polling_skips_while_wizard_open(self, run_panel):
        # 向导开着时轮询不得重渲染列表（会清掉向导的选择框）
        out = run_panel(_wizard_routes(), poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"})
        gets = [c for c in out["calls"]
                if c["url"] == "/admin/model-pools/runtime"]
        assert len(gets) == 1
        assert "mp-wizard" in out["html"]   # 向导仍在


class TestFixPassWizard:
    """向导 expected_revision 固定 0 + 离开向导态时清 _wizard。"""

    def test_wizard_pins_expected_revision_zero_even_with_stale_revision(
            self, run_panel):
        # 向导只在从未配置时出现；即便快照 revision 非 0（异常残留），
        # 创建语义 = 从零建立，固定 expected_revision=0（错则 409，可重试）
        pools = dict(_WIZARD_POOLS_BODY, revision=7)
        out = run_panel(_wizard_routes(pools_body=pools),
                        actions=[{"fn": "doMpWizardSubmit", "args": []}])
        body = _last_update_post(out)[-1]["body"]
        assert body["expected_revision"] == 0

    def test_wizard_state_cleared_once_configured(self, run_panel):
        # 时序：首屏未配置（向导）→ 外部配置出现 → 重渲染池卡片后，
        # 陈旧 _wizard 必须清除 — 再点提交只提示未初始化，不发 POST
        configured = _editor_pools_body(revision=1)
        pools_bodies = [_WIZARD_POOLS_BODY, configured]
        actions = [
            {"fn": "renderModelPools", "args": []},
            {"fn": "doMpWizardSubmit", "args": []},
        ]
        routes = _wizard_routes(pools_body=None)
        routes["/admin/model-pools"] = {"status": 200, "bodies": pools_bodies}
        out = run_panel(routes, actions=actions)
        assert not [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools" and c["method"] == "POST"]
        assert any("向导未初始化" in t["message"] for t in out["toasts"])


# ── UI/UX P2 Task 5：B3 状态过渡 + reduced-motion 收录 ──────────
# 契约：
# - .mp-dot/.mp-lamp/.mp-cand-card/.mp-chosen 状态切换（绿/黄/红、
#   选中态、劣化态）有 var(--dur)/var(--ease) 过渡，杜绝硬切；
# - .mp-cand-degraded（冷却中虚线）有过渡声明；
# - 全部 P2 动效/过渡类出现在 prefers-reduced-motion 降级块内。


def _css_rule(selector):
    css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(
        encoding="utf-8")
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, "rule {} missing".format(selector)
    return m.group(1)


class TestP2Transitions:
    def test_state_transition_rules_present(self):
        for sel in (".mp-dot", ".mp-lamp", ".mp-cand-card", ".mp-chosen"):
            rule = _css_rule(sel)
            assert "transition:" in rule, sel
            assert "var(--dur)" in rule, sel
            assert "var(--ease)" in rule, sel
        # 劣化态（虚线 + 半透明）单独声明过渡
        assert "transition:" in _css_rule(".mp-cand-degraded")

    def test_all_p2_motion_classes_in_reduced_motion_block(self):
        css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(
            encoding="utf-8")
        blocks = re.findall(
            r"prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
        assert blocks, "reduced-motion block missing"
        joined = "\n".join(blocks)
        # 动效类（B2 骨架扫光、A4 成果卡淡入）
        for cls in ("mp-busy", "mp-wizard-done"):
            assert cls in joined, cls
        # B3 过渡类 — 显式收录（全局一刀切之外的逐类声明，可审计）
        for cls in ("mp-dot", "mp-lamp", "mp-cand-card", "mp-chosen",
                    "mp-cand-degraded"):
            assert cls in joined, cls


class TestP2InlineDiffCss:
    """A2 行内 diff 预览条 CSS（P2 Task 1 终审补漏）：spec 要求
    「旧→新，红/绿」双编码 — 规则缺失时 diff 条渲染为无样式默认色文本。"""

    def test_diff_bar_rules_present(self):
        for sel in (".mp-inline-diff", ".mp-diff-old", ".mp-diff-new",
                    ".mp-inline-sel"):
            _css_rule(sel)   # 内部 assert 规则存在

    def test_diff_old_red_new_green_dual_encoding(self):
        # 双编码契约：旧目标红（--crit）/ 新目标绿（--ok）
        assert "var(--crit)" in _css_rule(".mp-diff-old")
        assert "var(--ok)" in _css_rule(".mp-diff-new")
        # 预览结果文案为辅助信息（次级色）
        assert "var(--ink-2)" in _css_rule(".mp-inline-preview")


# ── UI/UX P3 Task 8：C1 延迟心跳迷你图 ──────────────────────────
# 契约：
# - renderModelPools 第 4 个 best-effort fetch（latency；失败 → null 降级）
# - 卡内 id="mp-spark-<pool>-<candId>" 容器常驻；有数据才出 SVG
# - sparkSvg：60×24、polyline stroke var(--accent) + 末点圆、
#   TTFT 反演（越低越高位）
# - latency 数据不进 structSig → 轮询 by-id 更新，mpPools 零整写


def _lat_body(points, pool="fast", cand="loc"):
    return {"pools": {pool: {cand: points}}}


_LAT3 = [
    {"age_s": 30.0, "wall_ts": 1000.0, "ttft_ms": 200.0, "total_ms": 900.0},
    {"age_s": 20.0, "wall_ts": 1010.0, "ttft_ms": 100.0, "total_ms": 800.0},
    {"age_s": 10.0, "wall_ts": 1020.0, "ttft_ms": 300.0, "total_ms": 950.0},
]


class TestP3Spark:
    def _routes(self, latency_body=None, **kw):
        routes = _ops_routes(**kw)
        if latency_body is not None:
            routes["/admin/model-pools/latency"] = {
                "status": 200, "body": latency_body}
        return routes

    def test_spark_renders_polyline_from_latency_data(self, run_panel):
        out = run_panel(self._routes(latency_body=_lat_body(_LAT3)))
        html = out["htmlFinal"]
        assert 'id="mp-spark-fast-loc"' in html
        m = re.search(r'<polyline points="([^"]*)"', html)
        assert m, "polyline missing"
        assert len(m.group(1).split()) == 3      # 3 点
        assert "stroke:var(--accent)" in html
        assert "<circle" in html                 # 末点圆

    def test_spark_updates_by_id_without_rebuild(self, run_panel):
        lat4 = _LAT3 + [{"age_s": 1.0, "wall_ts": 1029.0,
                         "ttft_ms": 150.0, "total_ms": 700.0}]
        routes = self._routes()
        routes["/admin/model-pools/latency"] = {"status": 200, "bodies": [
            _lat_body(_LAT3), _lat_body(lat4)]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        wc = out["writeCounts"]
        assert wc.get("mpPools") == 1            # 结构不变 → 零整写
        assert wc.get("mp-spark-fast-loc", 0) >= 1   # by-id 定点更新
        # 末态内容 = 第 4 点后的 4 点折线
        m = re.search(r'<polyline points="([^"]*)"',
                      out["dyn"]["mp-spark-fast-loc"]["innerHTML"])
        assert m and len(m.group(1).split()) == 4

    def test_spark_hidden_without_data(self, run_panel):
        # 无该候选 latency 数据（端点 200 空池）→ 容器常驻但无 SVG
        out = run_panel(self._routes(latency_body={"pools": {}}))
        html = out["htmlFinal"]
        assert 'id="mp-spark-fast-loc"' in html
        assert "<svg" not in html and "<polyline" not in html

    def test_latency_fetch_failure_degrades(self, run_panel):
        # latency 404 → 面板正常渲染，无 spark SVG（best-effort）
        routes = self._routes()
        routes["/admin/model-pools/latency"] = {"status": 404, "body": None}
        out = run_panel(routes)
        html = out["htmlFinal"]
        assert "iff/fast" in html                # 池列表照常
        assert "<svg" not in html
        lat_gets = [c for c in out["calls"]
                    if c["url"] == "/admin/model-pools/latency"]
        assert len(lat_gets) == 1                # fetch 确实发生且被降级


# ── UI/UX P3 Task 9：C2 失败时间线 ──────────────────────────────
# 契约：
# - 第 5 个 best-effort fetch（events；失败 → null 降级）
# - 池有事件 → 尾部「故障时间线 · N」toggle（默认折叠）；展开 → 横向轴
#   （mp-tl-dot 按事件、left% 最近 30min 窗口、title 含 reason、
#   mp-tl-cooldown/mp-tl-quarantine 着色）
# - 展开态进 structSig（开合是结构）；事件数据不进 sig → by-id 更新


def _events_body(events, pool="fast"):
    return {"pools": {pool: events}}


_EV2 = [
    {"age_s": 1500.0, "wall_ts": 1000.0, "type": "cooldown",
     "candidate_id": "loc", "target": "qwen-small", "reason": "cooldown_401"},
    {"age_s": 300.0, "wall_ts": 2200.0, "type": "quarantine",
     "candidate_id": "cld", "target": "prov/cloud-m", "reason": "quarantine_404"},
]


class TestP3Timeline:
    def _routes(self, events_body=None, **kw):
        routes = _ops_routes(**kw)
        routes["/admin/model-pools/events"] = {
            "status": 200, "body": events_body or {"pools": {}}}
        return routes

    def test_timeline_toggle_collapsed_by_default(self, run_panel):
        out = run_panel(self._routes(events_body=_events_body(_EV2)))
        html = out["htmlFinal"]
        # 默认折叠：按钮在、轴不在
        assert 'data-mp-tl-toggle="fast"' in html
        assert "故障时间线 · 2" in html
        assert 'id="mp-tl-fast"' not in html
        # 展开 → 轴出现：事件点数 = 2、着色、title 含 reason、left% 在 0–100
        out2 = run_panel(self._routes(events_body=_events_body(_EV2)),
                         actions=[{"fn": "doMpTimelineToggle",
                                   "args": ["fast"]}])
        h2 = out2["htmlFinal"]
        assert 'id="mp-tl-fast"' in h2
        assert h2.count("mp-tl-dot") == 2
        assert "mp-tl-cooldown" in h2 and "mp-tl-quarantine" in h2
        assert "cooldown_401" in h2 and "quarantine_404" in h2
        for m in re.finditer(r'left:([\d.]+)%', h2):
            assert 0.0 <= float(m.group(1)) <= 100.0

    def test_timeline_open_state_in_sig_no_flicker(self, run_panel):
        # 展开 → 重建一次；之后轮询 events 数据变（2→3 条）→ 轴 by-id 更新，
        # mpPools 零整写；toggle 计数文本 by-id 更新
        ev3 = _EV2 + [{"age_s": 60.0, "wall_ts": 2440.0, "type": "probe_fail",
                       "candidate_id": "loc", "target": "qwen-small",
                       "reason": "cooldown_401"}]
        routes = self._routes()
        routes["/admin/model-pools/events"] = {"status": 200, "bodies": [
            _events_body(_EV2), _events_body(_EV2), _events_body(ev3)]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"},
                        actions=[{"fn": "doMpTimelineToggle",
                                  "args": ["fast"]}])
        wc = out["writeCounts"]
        assert wc.get("mpPools") == 2          # 首屏 + 展开重建；轮询零整写
        assert wc.get("mp-tl-fast", 0) == 1    # 轮询 by-id 轴更新
        assert out["dyn"]["mp-tl-fast"]["innerHTML"].count("mp-tl-dot") == 3
        assert out["dyn"]["mp-tl-toggle-fast"]["textContent"] == "故障时间线 · 3"

    def test_timeline_hidden_without_events(self, run_panel):
        out = run_panel(self._routes(events_body={"pools": {}}))
        assert "data-mp-tl-toggle" not in out["htmlFinal"]
        assert "mp-tl-dot" not in out["htmlFinal"]
        ev_gets = [c for c in out["calls"]
                   if c["url"] == "/admin/model-pools/events"]
        assert len(ev_gets) == 1                # fetch 发生且空池 → 无 toggle


# ── UI/UX P3 Task 10：C3 活体光带 + C4 劣化提示 ─────────────────
# 契约：
# - 池卡顶 mp-beam 三态纯函数 beamClass：候选 last_attempt.ok 且 <30s →
#   flow（流动动画）；无新鲜交换但有 chosen → idle；全协议无 chosen →
#   dark（虚线暗态）；applyDynamic by-id className 更新
# - isDegraded 纯函数：首候选 cooling && 任一协议 chosen → 池卡引导条
#   「首选已降级」+ 查看回退链（卡加 mp-highlight）+ × 关闭
#   （_degradeDismissed；劣化解除清键重新武装）
# - 引导条在否 + dismiss 态进 sig；动效进 reduced-motion 降级块


def _rt_with_last_attempt(cand_idx, ok, age_s):
    rt = json.loads(json.dumps(_RUNTIME_BODY))
    rt["pools"]["fast"]["candidates"][cand_idx]["last_attempt"] = {
        "ok": ok, "status": 200 if ok else 503, "age_s": age_s}
    return rt


def _status_no_chosen():
    st = json.loads(json.dumps(_STATUS_BODY))
    st["pools"]["fast"]["protocols"]["openai"] = {
        "chosen": None, "error": "无可用候选", "decisions": []}
    return st


class TestP3Beam:
    def test_beam_flows_only_on_recent_exchange(self, run_panel):
        # cld 新鲜交换（age 5s）→ flow
        out = run_panel(_ops_routes(
            runtime_body=_rt_with_last_attempt(1, True, 5.0)))
        assert 'id="mp-beam-fast"' in out["htmlFinal"]
        assert "mp-beam-flow" in out["htmlFinal"]
        # 交换太旧（120s）→ idle（有 chosen）
        out = run_panel(_ops_routes(
            runtime_body=_rt_with_last_attempt(1, True, 120.0)))
        assert "mp-beam-idle" in out["htmlFinal"]
        # 全协议无 chosen → dark（虚线暗态）
        out = run_panel(_ops_routes(
            status_body=_status_no_chosen(),
            runtime_body=_rt_with_last_attempt(1, True, 5.0)))
        assert "mp-beam-dark" in out["htmlFinal"]

    def test_beam_class_updates_by_id(self, run_panel):
        # 轮询中 last_attempt 变新鲜 → beam className by-id 更新，零整写
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            _RUNTIME_BODY, _rt_with_last_attempt(1, True, 5.0)]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"})
        assert out["writeCounts"].get("mpPools") == 1
        assert out["dyn"]["mp-beam-fast"]["className"] \
            == "mp-beam mp-beam-flow"


class TestP3Degrade:
    def test_degrade_hint_when_first_candidate_cooling_with_chosen(
            self, run_panel):
        # 默认夹具：loc 冷却（首候选）+ openai chosen=loc → 引导条
        out = run_panel(_ops_routes())
        html = out["htmlFinal"]
        assert "mp-degrade" in html
        assert "首选已降级" in html
        assert 'data-mp-degrade-view="fast"' in html
        assert 'data-mp-degrade-dismiss="fast"' in html
        # 首候选健康 → 不出
        rt_ok = json.loads(json.dumps(_RUNTIME_BODY))
        rt_ok["pools"]["fast"]["candidates"][0]["cooling"] = None
        out = run_panel(_ops_routes(runtime_body=rt_ok))
        assert "mp-degrade" not in out["htmlFinal"]
        # 无 chosen → 不出（劣化无服务对象）
        out = run_panel(_ops_routes(status_body=_status_no_chosen()))
        assert "mp-degrade" not in out["htmlFinal"]

    def test_degrade_view_highlights_decisions(self, run_panel):
        out = run_panel(_ops_routes(),
                        actions=[{"fn": "doMpDegradeView", "args": ["fast"]}])
        assert "mp-highlight" in out["dyn"]["mp-card-fast"]["className"]

    def test_degrade_dismiss_until_state_changes(self, run_panel):
        # 关闭 → 同态轮询不重现
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            _RUNTIME_BODY, _RUNTIME_BODY, _RUNTIME_BODY]}
        out = run_panel(routes, poll_ticks=1,
                        store_state={"tab_active": "tab-model-pools"},
                        actions=[{"fn": "doMpDegradeDismiss",
                                  "args": ["fast"]}])
        assert "mp-degrade" not in out["htmlFinal"]
        # 劣化解除（轮询见健康态清 dismiss 键）→ 再劣化 → 重现（重新武装）
        rt_ok = json.loads(json.dumps(_RUNTIME_BODY))
        rt_ok["pools"]["fast"]["candidates"][0]["cooling"] = None
        routes = _ops_routes()
        routes["/admin/model-pools/runtime"] = {"status": 200, "bodies": [
            _RUNTIME_BODY, rt_ok, _RUNTIME_BODY]}
        out = run_panel(routes, poll_ticks=2,
                        store_state={"tab_active": "tab-model-pools"},
                        actions=[{"fn": "doMpDegradeDismiss",
                                  "args": ["fast"]}])
        assert "mp-degrade" in out["htmlFinal"]

    def test_p3_motion_in_reduced_motion_block(self):
        css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(
            encoding="utf-8")
        blocks = re.findall(
            r"prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
        joined = "\n".join(blocks)
        assert "mp-beam-flow" in joined
        assert "mp-highlight" in joined
