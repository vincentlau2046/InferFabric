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
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id: id, innerHTML: '', textContent: '',
      addEventListener: function () {}, classList: {add(){}, remove(){}},
    };
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
const calls = [];
const seen = {};
const fetch = async (url, options) => {
  options = options || {};
  calls.push({url, method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null});
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
         body: route.bodies[Math.min(seen[mkey] - 1, route.bodies.length - 1)]};
  } else {
    r = route;
  }
  const status = r.status;
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: async () => r.body,
  };
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window,
  document: {getElementById: el, hidden: !!input.documentHidden},
  fetch,
  navigator,
  location: input.location || {protocol: 'http:', hostname: '127.0.0.1'},
  setInterval: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
  console,
});
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
  // 模拟 5s 轮询滴答（每次滴答触发全部已注册 interval）
  for (let i = 0; i < (input.pollTicks || 0); i++) {
    for (const t of timers) { await t.fn(); }
  }
  const modalHtml = elements['mpModal'] ? elements['mpModal'].innerHTML : '';
  const previewHtml = elements['mpPreview'] ? elements['mpPreview'].innerHTML : '';
  console.log(JSON.stringify({html, modalHtml, previewHtml, toasts, calls,
                              clipboardWrites}));
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
            poll_ticks=0, store_state=None, document_hidden=False):
        proc = subprocess.run(
            [node, str(harness), str(PANEL_JS)],
            input=json.dumps({"routes": routes, "toggle": toggle,
                              "actions": actions,
                              "location": location,
                              "pollTicks": poll_ticks,
                              "storeState": store_state,
                              "documentHidden": document_hidden}),
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
