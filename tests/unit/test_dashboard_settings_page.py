"""PR3 WS4 — 设置页（授权设置）组装与源码契约（仿 test_dashboard_security_page.py）。

组装契约：get_html() 真实装配 tab-settings（nav/容器/JS 内联）。
源码契约：
- 消费 status/enable/console-password/keys-login/keys-password 全端点，
  走 UI.adminHeaders；
- 密码卫生：密码值只进入 login/console-password/keys-password 三个请求体，
  绝不进 GET、innerHTML、浏览器存储；一次性 key 明文只经 textContent；
- 无 inline onclick。
"""
from pathlib import Path

from inferfabric import dashboard
JS_DIR = Path(dashboard._DASHBOARD_DIR) / "js"
FRAG_DIR = Path(dashboard._DASHBOARD_DIR) / "fragments"


def _src(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


# ── 组装契约 ─────────────────────────────────────────────────────

def test_settings_page_is_assembled():
    html = dashboard.get_html()
    assert 'data-tab="tab-settings"' in html
    assert 'id="tab-settings"' in html
    assert "settings" in dashboard.JS_MODULES
    # settings.js 真实内联（security.js 也含 status URL — 需 settings 独有标记）
    assert "admin/security/console-password" in html
    assert "<!-- FRAGMENT:settings -->" not in html
    assert "<!-- JS:settings -->" not in html
    assert "Fragment settings 加载失败" not in html


def test_settings_nav_sits_in_gateway_group():
    html = dashboard.get_html()
    # 位于 鉴权 之后、云端 之前（网关策略组）
    assert 0 < html.find("tab-security") < html.find("tab-settings") < html.find("tab-cloud")


# ── 源码契约 ─────────────────────────────────────────────────────

def test_settings_js_consumes_full_surface():
    src = _src("settings.js")
    for endpoint in ("admin/security/status", "admin/security/enable",
                     "admin/security/console-password",
                     "admin/keys/login", "admin/keys/password"):
        assert endpoint in src, f"missing endpoint: {endpoint}"
    assert "UI.adminHeaders" in src
    assert "tabRenderers['tab-settings']" in src
    assert "UI.confirm" in src          # 推理鉴权开/关写前确认


def test_settings_js_password_hygiene():
    src = _src("settings.js")
    # 一次性 key 明文只经 textContent 临时显示
    assert "textContent" in src
    # 不落浏览器存储、不进 URL/cookie
    for banned in ("localStorage", "sessionStorage", "history.pushState",
                   "location.search =", "document.cookie"):
        assert banned not in src, f"forbidden sink: {banned}"
    # 密码/token 值（.value 读取）只允许出现在请求体构建处 — 含 .value 的行
    # 不得做 innerHTML 拼接（Review Focus 4；settings.js 的 .value 读取
    # 全部来自密码/token 输入）
    value_lines = [ln for ln in src.splitlines() if ".value" in ln]
    assert value_lines, "settings.js must read password inputs via .value"
    for ln in value_lines:
        assert "innerHTML" not in ln, f"password value in innerHTML line: {ln.strip()}"


def test_settings_no_inline_onclick():
    assert "onclick=" not in _src("settings.js")
    frag = (FRAG_DIR / "settings.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag


def test_settings_html_has_password_inputs_and_cards():
    frag = (FRAG_DIR / "settings.html").read_text(encoding="utf-8")
    # 三张卡容器：边界说明/鉴权（setBody）+ 密码（pwBody）
    assert "pwBody" in frag and "setBody" in frag
    # 密码输入由 settings.js 渲染（fragments 只放骨架）— type=password 不回显
    assert 'type="password"' in _src("settings.js")
    assert 'autocomplete="new-password"' in _src("settings.js")
