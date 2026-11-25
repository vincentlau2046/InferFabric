"""设置页面板：运行真实 settings.js，不访问网络（Node VM 模式，仿 model-pools 面板）。

契约（PR3 WS4）：
- status.password_set=false → 渲染首次设密表单（管理员凭证 + 新密码）
- status.password_set=true  → 渲染会话改密表单（当前密码 + 新密码）
- 首设提交 → POST /admin/security/console-password（body 携 new_password）
- 改密两跳：POST /admin/keys/login（验旧密码换会话）→ POST /admin/keys/password
  携 X-CSRF-Token + {current_password, new_password}
- 推理鉴权开关：写前 UI.confirm → POST /admin/security/enable
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PANEL_JS = ROOT / "inferfabric" / "dashboard" / "js" / "settings.js"

_STATUS_UNSET = {"active": False, "policy": None, "enforced": False,
                 "key_count": 0, "keys": [], "password_set": False}
_STATUS_SET = {"active": True, "policy": "all", "enforced": True,
               "key_count": 2, "keys": [], "password_set": True}

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const toasts = [];
const confirms = [];
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {id, textContent: '', className: '', value: '',
      style: {}, focus(){}, addEventListener(){},
      classList: {add(){}, remove(){}, toggle(){}}};
  }
  return elements[id];
}
for (const [id, v] of Object.entries(input.seeds || {})) el(id).value = v;
const calls = [];
const fetch = async (url, options) => {
  options = options || {};
  calls.push({url, method: options.method || 'GET',
              headers: options.headers || {},
              body: options.body ? JSON.parse(options.body) : null});
  const r = input.routes[(options.method || 'GET') + ' ' + url]
          || input.routes[url] || {status: 404, body: null};
  const status = r.status || 200;
  return {ok: status >= 200 && status < 300, status, json: async () => r.body};
};
const window = {
  tabRenderers: {},
  UI: {
    toast: (m, k) => toasts.push({m, k}),
    adminHeaders: () => Object.assign({'Content-Type': 'application/json'},
                                      input.adminHeaders || {}),
    confirm: (opts) => { confirms.push(opts);
                         if (input.confirmOk !== false && opts.onOk) opts.onOk(); },
    icon: () => '',
  },
  store: {on: () => {}, get: () => null},
};
const docListeners = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  window,
  document: {getElementById: el,
             addEventListener: (t, fn) => {(docListeners[t] = docListeners[t] || []).push(fn);}},
  fetch,
  navigator: {clipboard: {writeText: async () => {}}},
  location: input.location || {protocol: 'http:', hostname: '127.0.0.1'},
  TextEncoder: TextEncoder,
  console,
});
window.fireClick = (targetId, attrs) => {
  const t = el(targetId);
  if (attrs) { t._attrs = attrs; t.getAttribute = (k) => (k in t._attrs ? t._attrs[k] : null); }
  (docListeners['click'] || []).forEach(fn => fn({target: t}));
};
(async () => {
  assert.ok(window.tabRenderers['tab-settings'],
            'must register tabRenderers["tab-settings"]');
  await window.tabRenderers['tab-settings']();
  // 动作前先排空初始渲染链（真实浏览器点击时状态已加载）
  for (let i = 0; i < 500; i++) await Promise.resolve();
  for (const act of (input.actions || [])) {
    const fn = window[act.fn];
    if (typeof fn !== 'function') throw new Error('unknown action: ' + act.fn);
    await fn.apply(window, act.args || []);
  }
  // 确定性收尾：settings.js 动作是回调式链（fetch→json→cb→render），
  // 排空全部微任务拍后再读终态（同 model-pools harness 的 _drain 语义）
  for (let i = 0; i < 500; i++) await Promise.resolve();
  const dyn = {};
  for (const [id, e] of Object.entries(elements)) {
    dyn[id] = {textContent: e.textContent, className: e.className, innerHTML: e.innerHTML};
  }
  console.log(JSON.stringify({html: dyn['setBody'] ? dyn['setBody'].innerHTML : '',
                              pwHtml: dyn['pwBody'] ? dyn['pwBody'].innerHTML : '',
                              toasts, calls, confirms, dyn}));
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
    harness = tmp_path / "settings_harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(routes, actions=None, seeds=None, admin_headers=None,
            confirm_ok=True):
        proc = subprocess.run(
            [node, str(harness), str(PANEL_JS)],
            input=json.dumps({"routes": routes, "actions": actions,
                              "seeds": seeds, "adminHeaders": admin_headers,
                              "confirmOk": confirm_ok}),
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def _routes(status_body, **extra):
    routes = {"/admin/security/status": {"status": 200, "body": status_body}}
    routes.update(extra)
    return routes


class TestRender:
    def test_unset_renders_first_set_form(self, run_panel):
        out = run_panel(_routes(_STATUS_UNSET))
        # 首设表单：管理员凭证 + 新密码 + 提交动作
        assert 'data-action="st-set-pw"' in out["pwHtml"]
        assert "管理员" in out["pwHtml"]
        assert "iff keys init" in out["pwHtml"]       # 无凭证的本机出路
        # 改密表单不得出现
        assert 'data-action="st-pw-change"' not in out["pwHtml"]

    def test_set_renders_change_form(self, run_panel):
        out = run_panel(_routes(_STATUS_SET))
        assert 'data-action="st-pw-change"' in out["pwHtml"]
        assert "当前密码" in out["pwHtml"]
        # 首设表单不得出现
        assert 'data-action="st-set-pw"' not in out["pwHtml"]
        # 恢复指引可见
        assert "iff keys set-password" in out["pwHtml"]

    def test_inference_auth_card_reflects_status(self, run_panel):
        out = run_panel(_routes(_STATUS_SET))
        assert "all" in out["html"]                    # policy 三态可见
        assert "生效" in out["html"]                   # enforced 徽标

    def test_local_boundary_note_present(self, run_panel):
        # 本机零门槛边界说明：设置页本身不设登录门槛
        out = run_panel(_routes(_STATUS_UNSET))
        assert "能力包" in out["html"]                 # 「装包/部署/切模型不过密码」表述
        assert "登录" in out["html"]


class TestConsolePassword:
    def test_first_set_posts_console_password(self, run_panel):
        out = run_panel(
            _routes(_STATUS_UNSET,
                    **{"POST /admin/security/console-password":
                       {"status": 200,
                        "body": {"status": "ok", "provisioned": True}}}),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "tok-123", "stNewPw": "a-long-password",
                   "stNewPw2": "a-long-password"},
        )
        pw_calls = [c for c in out["calls"]
                    if c["url"] == "/admin/security/console-password"]
        assert len(pw_calls) == 1 and pw_calls[0]["method"] == "POST"
        assert pw_calls[0]["body"] == {"new_password": "a-long-password"}
        # 成功后重读 status（表单切换以重读为准）
        assert any(c["url"] == "/admin/security/status" and c["method"] == "GET"
                   for c in out["calls"][1:])

    def test_first_set_mismatch_rejected_locally(self, run_panel):
        out = run_panel(
            _routes(_STATUS_UNSET),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "tok-123", "stNewPw": "a-long-password",
                   "stNewPw2": "different-long-password"},
        )
        assert out["calls"] == [] or all(
            c["url"] == "/admin/security/status" for c in out["calls"])
        assert any("一致" in t["m"] for t in out["toasts"])

    def test_first_set_short_password_rejected_locally(self, run_panel):
        out = run_panel(
            _routes(_STATUS_UNSET),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "tok-123", "stNewPw": "short",
                   "stNewPw2": "short"},
        )
        assert not any(c["url"] == "/admin/security/console-password"
                       for c in out["calls"])
        assert any(t["k"] == "error" for t in out["toasts"])

    def test_first_set_401_shows_server_guidance(self, run_panel):
        out = run_panel(
            _routes(_STATUS_UNSET,
                    **{"POST /admin/security/console-password":
                       {"status": 401,
                        "body": {"error": "需要管理员凭证（X-Admin-Token）— 无凭证请"
                                          "在网关本机运行 \"iff keys init\""}}}),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "wrong-token", "stNewPw": "a-long-password",
                   "stNewPw2": "a-long-password"},
        )
        assert any("iff keys init" in t["m"] and t["k"] == "error"
                   for t in out["toasts"])


class TestPasswordChange:
    def test_change_two_legs_login_then_password_with_csrf(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET,
                    **{"POST /admin/keys/login": {"status": 200,
                                                  "body": {"csrf": "tok-csrf"}},
                       "POST /admin/keys/password": {"status": 200,
                                                     "body": {"status": "ok"}}}),
            actions=[{"fn": "doPasswordChange"}],
            seeds={"pwCurrent": "old-password-x", "pwNew": "a-new-password",
                   "pwNew2": "a-new-password"},
        )
        login_calls = [c for c in out["calls"] if c["url"] == "/admin/keys/login"]
        assert len(login_calls) == 1
        assert login_calls[0]["body"] == {"password": "old-password-x"}
        pw_calls = [c for c in out["calls"] if c["url"] == "/admin/keys/password"]
        assert len(pw_calls) == 1
        assert pw_calls[0]["headers"].get("X-CSRF-Token") == "tok-csrf"
        assert pw_calls[0]["body"] == {"current_password": "old-password-x",
                                       "new_password": "a-new-password"}
        # 两跳次序：login 先于 password
        assert out["calls"].index(login_calls[0]) < out["calls"].index(pw_calls[0])

    def test_change_login_401_stops_before_password(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET,
                    **{"POST /admin/keys/login": {"status": 401,
                                                  "body": {"error": "invalid login"}}}),
            actions=[{"fn": "doPasswordChange"}],
            seeds={"pwCurrent": "wrong-password", "pwNew": "a-new-password",
                   "pwNew2": "a-new-password"},
        )
        assert not any(c["url"] == "/admin/keys/password" for c in out["calls"])
        assert any(t["k"] == "error" for t in out["toasts"])

    def test_change_mismatch_rejected_locally(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET),
            actions=[{"fn": "doPasswordChange"}],
            seeds={"pwCurrent": "old-password-x", "pwNew": "a-new-password",
                   "pwNew2": "different-password"},
        )
        assert not any(c["url"] == "/admin/keys/login" for c in out["calls"])
        assert any("一致" in t["m"] for t in out["toasts"])


class TestInferenceAuthToggle:
    def test_enable_confirms_then_posts(self, run_panel):
        out = run_panel(
            _routes(_STATUS_UNSET,
                    **{"POST /admin/security/enable":
                       {"status": 200, "body": {"status": "ok", "policy": "all",
                                                "enforced": True, "key": None}}},
                    ),
            actions=[{"fn": "doEnable", "args": [True]}],
        )
        assert len(out["confirms"]) == 1
        assert "401" in out["confirms"][0]["body"]     # 开=客户端将 401
        en = [c for c in out["calls"] if c["url"] == "/admin/security/enable"]
        assert len(en) == 1 and en[0]["body"] == {"enforce": True}

    def test_disable_confirms_keeps_keys(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET,
                    **{"POST /admin/security/enable":
                       {"status": 200, "body": {"status": "ok", "policy": "off"}}}),
            actions=[{"fn": "doEnable", "args": [False]}],
        )
        assert len(out["confirms"]) == 1
        assert "保留" in out["confirms"][0]["body"]    # 关=仅宽限，key 全保留
        en = [c for c in out["calls"] if c["url"] == "/admin/security/enable"]
        assert len(en) == 1 and en[0]["body"] == {"enforce": False}

    def test_declined_confirm_sends_nothing(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET), actions=[{"fn": "doEnable", "args": [False]}],
            confirm_ok=False,
        )
        assert len(out["confirms"]) == 1
        assert not any(c["url"] == "/admin/security/enable" for c in out["calls"])

    def test_zero_keys_blocks_enable(self, run_panel):
        # review F3 附带：库在但只剩已吊销 key → 开启前拦截（security.js 同款守卫）
        zero_keys = {"active": True, "policy": "off", "enforced": False,
                     "key_count": 0, "keys": [], "password_set": True}
        out = run_panel(
            _routes(zero_keys), actions=[{"fn": "doEnable", "args": [True]}],
        )
        assert not any(c["url"] == "/admin/security/enable" for c in out["calls"])
        assert any(t["k"] == "error" for t in out["toasts"])


class TestReviewFixes:
    """fresh-context 评审（2026-11-25）Important 修复批次。"""

    _STATUS_OFF = {"active": True, "policy": "off", "enforced": False,
                   "key_count": 1, "keys": [], "password_set": True}
    _STATUS_POOLS = {"active": True, "policy": "pools", "enforced": False,
                     "key_count": 1, "keys": [], "password_set": True}

    def test_off_transition_shows_enable_button(self, run_panel):
        # review F3：all→off 后设置页必须还能再开启（此前只剩「关闭」按钮）
        out = run_panel(_routes(self._STATUS_OFF))
        assert 'data-action="st-enable"' in out["html"]
        assert "开启鉴权" in out["html"]

    def test_off_transition_hides_noop_disable(self, run_panel):
        out = run_panel(_routes(self._STATUS_OFF))
        assert 'data-action="st-disable"' not in out["html"]

    def test_pools_policy_shows_partial_badge(self, run_panel):
        # review F3：pools 且 legacy 关 → 「部分生效」，不得标「未生效」
        out = run_panel(_routes(self._STATUS_POOLS))
        assert "部分生效" in out["html"]
        assert "实际鉴权：未生效" not in out["html"]

    def test_status_401_renders_error_not_skeleton(self, run_panel):
        # review F7：严格模式 401 → 可操作提示（非骨架/无响应）
        out = run_panel({"/admin/security/status": {"status": 401,
                                                    "body": {"error": "Unauthorized"}}})
        assert any("admin token" in t["m"] and t["k"] == "error" for t in out["toasts"])
        assert "加载失败" in out["html"] and "加载失败" in out["pwHtml"]

    def test_first_set_uses_header_token_when_form_empty(self, run_panel):
        # review F7：空表单 token 不得覆盖右上角有效 token（override-only）
        out = run_panel(
            _routes(_STATUS_UNSET,
                    **{"POST /admin/security/console-password":
                       {"status": 200, "body": {"status": "ok", "provisioned": True}}}),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "", "stNewPw": "a-long-password",
                   "stNewPw2": "a-long-password"},
            admin_headers={"X-Admin-Token": "hdr-tok"},
        )
        pw = [c for c in out["calls"]
              if c["url"] == "/admin/security/console-password"]
        assert len(pw) == 1
        assert pw[0]["headers"].get("X-Admin-Token") == "hdr-tok"

    def test_active_unset_guides_set_password_not_init(self, run_panel):
        # review F7：健康库已存在时 iff keys init 会拒绝 — 指引须为 set-password
        active_unset = {"active": True, "policy": "off", "enforced": False,
                        "key_count": 1, "keys": [], "password_set": False}
        out = run_panel(_routes(active_unset))
        assert "iff keys set-password" in out["pwHtml"]
        assert "iff keys init" not in out["pwHtml"]

    def test_multibyte_password_accepted(self, run_panel):
        # review F8：4 个汉字 = 12 UTF-8 字节，后端合法 — 前端不得按 UTF-16 误拒
        out = run_panel(
            _routes(_STATUS_UNSET,
                    **{"POST /admin/security/console-password":
                       {"status": 200, "body": {"status": "ok", "provisioned": True}}}),
            actions=[{"fn": "doConsoleSet"}],
            seeds={"stToken": "tok", "stNewPw": "密码密码密码密码",
                   "stNewPw2": "密码密码密码密码"},
        )
        assert any(c["url"] == "/admin/security/console-password" for c in out["calls"])

    def test_multibyte_change_accepted(self, run_panel):
        out = run_panel(
            _routes(_STATUS_SET,
                    **{"POST /admin/keys/login": {"status": 200,
                                                  "body": {"csrf": "t"}},
                       "POST /admin/keys/password": {"status": 200,
                                                     "body": {"status": "ok"}}}),
            actions=[{"fn": "doPasswordChange"}],
            seeds={"pwCurrent": "旧的密码旧的密码", "pwNew": "新的密码新的密码",
                   "pwNew2": "新的密码新的密码"},
        )
        assert any(c["url"] == "/admin/keys/login" for c in out["calls"])
        assert any(c["url"] == "/admin/keys/password" for c in out["calls"])
