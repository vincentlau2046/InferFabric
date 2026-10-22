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
const fetch = async (url, options) => {
  options = options || {};
  calls.push({url, method: options.method || 'GET',
              body: options.body ? JSON.parse(options.body) : null});
  const route = input.routes[url];
  const r = (typeof route === 'function') ? route(calls.length) : route;
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
  console.log(JSON.stringify({html, toasts, calls}));
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

    def run(routes, toggle=None):
        proc = subprocess.run(
            [node, str(harness), str(PANEL_JS)],
            input=json.dumps({"routes": routes, "toggle": toggle}),
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def _routes(toggle_status=200, toggle_body=None):
    return {
        "/admin/model-pools": {"status": 200, "body": _POOLS_BODY},
        "/admin/model-pools/status": {"status": 200, "body": _STATUS_BODY},
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
