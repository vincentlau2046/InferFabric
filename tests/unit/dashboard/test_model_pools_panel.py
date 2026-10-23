"""模型池面板：运行真实 model-pools.js，不访问网络（Node VM 模式）。

契约（Task 9，只读 + 开关）：
- 渲染池列表：池名 / enabled 开关 / revision / 候选与排除原因
- 双协议候选解释：OpenAI 与 Anthropic 的当前选择视图都展示
- 开关走 POST /admin/model-pools/toggle（服务端原子翻转，携带 expected_revision）
- 409 冲突 → 提示刷新并重新拉取；成功 → toast + 重新渲染
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PANEL_JS = ROOT / "inferfabric" / "dashboard" / "js" / "model-pools.js"

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
  store: {on: () => {}},
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
  document: {getElementById: el},
  fetch,
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
  // v7.1.0 编辑器：任意 window 函数动作序列（模拟用户在模态内的操作流）
  for (const act of (input.actions || [])) {
    const fn = window[act.fn];
    if (typeof fn !== 'function') {
      throw new Error('unknown action: ' + act.fn);
    }
    await fn.apply(window, act.args || []);
  }
  const modalHtml = elements['mpModal'] ? elements['mpModal'].innerHTML : '';
  const previewHtml = elements['mpPreview'] ? elements['mpPreview'].innerHTML : '';
  console.log(JSON.stringify({html, modalHtml, previewHtml, toasts, calls}));
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

    def run(routes, toggle=None, actions=None):
        proc = subprocess.run(
            [node, str(harness), str(PANEL_JS)],
            input=json.dumps({"routes": routes, "toggle": toggle,
                              "actions": actions}),
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
