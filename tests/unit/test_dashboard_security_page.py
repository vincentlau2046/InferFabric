"""M2-T2 — security 页组装与源码契约（计划 2026-11-19 Task 2）。

组装契约：get_html() 真实装配 tab-security（nav/容器/JS 内联）。
源码契约：
- 消费 status/enable/keys/keys-revoke 全端点，走 UI.adminHeaders；
- 明文一次性回显：textContent 渲染、不落浏览器存储/全局状态；
- 切页/关闭/Esc 丢弃迟到回显（tab_active 卸载监听）；
- policy=off ≠ 免鉴权（三态分离展示，禁止把 policy off 渲染成“免鉴权”）；
- 无 inline onclick。
"""
from pathlib import Path

from inferfabric import dashboard

JS_DIR = Path(dashboard._DASHBOARD_DIR) / "js"


def _src(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


# ── 组装契约 ─────────────────────────────────────────────────────

def test_security_page_is_assembled():
    html = dashboard.get_html()
    assert 'data-tab="tab-security"' in html
    assert 'id="tab-security"' in html
    assert "security" in dashboard.JS_MODULES
    assert "admin/security/status" in html
    assert "<!-- FRAGMENT:security -->" not in html
    assert "<!-- JS:security -->" not in html
    assert "Fragment security 加载失败" not in html


# ── 源码契约 ─────────────────────────────────────────────────────

def test_security_js_consumes_full_surface():
    src = _src("security.js")
    for endpoint in ("admin/security/status", "admin/security/enable",
                     "admin/security/keys", "admin/security/keys/revoke"):
        assert endpoint in src, f"missing endpoint: {endpoint}"
    assert "UI.adminHeaders" in src
    assert "tabRenderers['tab-security']" in src
    assert "UI.confirm" in src          # 吊销确认（最后一把未吊销 key 有警告）


def test_security_js_one_time_reveal_hygiene():
    src = _src("security.js")
    # 明文只经 textContent 临时显示（不进 innerHTML/全局状态）
    assert "textContent" in src
    # 不落浏览器存储、不进 URL
    for banned in ("localStorage", "sessionStorage", "history.pushState",
                   "location.search =", "document.cookie"):
        assert banned not in src, f"forbidden sink: {banned}"
    # 切页/关闭路径丢弃迟到回显（监听 tab_active 并清空回显区）
    assert "tab_active" in src


def test_security_js_policy_off_is_not_auth_free():
    src = _src("security.js")
    # 三态分离：active/policy/enforced 各自独立渲染；
    # policy 'off' 不得渲染为「免鉴权」（off = 迁移宽限，legacy 可能仍在拦截）
    assert "off" in src
    assert "免鉴权" not in src
    # 401 明确提示（严格模式 GET/写均可 401）
    assert "401" in src


def test_security_js_no_inline_onclick():
    src = _src("security.js")
    assert "onclick=" not in src
