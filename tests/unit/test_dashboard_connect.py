"""Task 3 — Dashboard Connect 页组装与源码契约（F10，M3 T3）。

后端合同由 test_connect_admin.py（Task 2）钉住；本文件钉 UI 装配与
connect.js 行为契约（浏览器沙箱验收另行执行，Playwright 文本断言）：

- 导航 / fragment / JS 三处装配齐全（JS_MODULES + fragment 循环 + 占位符）；
- Key 名称/ID 只取 /admin/security/status，绝不调用发 key API
  （/admin/security/keys、/admin/security/enable）；
- api_key 走一次性密码输入，弹窗关闭/请求结束即清空；不显示原配置正文；
- 托管/回退都显式确认才 POST；409 要求重读状态，不自动重试；
- 401 专用提示不被通用错误覆盖；写完提示需重启客户端；
- 无 inline onclick（XSS 面收敛，同其他页）。
"""
from pathlib import Path

from inferfabric import dashboard

JS_DIR = Path(dashboard._DASHBOARD_DIR) / "js"


def _src(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


# ── 组装契约 ─────────────────────────────────────────────────────

def test_connect_tab_is_assembled():
    assert "connect" in dashboard.JS_MODULES
    init_src = (Path(dashboard._DASHBOARD_DIR) / "__init__.py").read_text(
        encoding="utf-8")
    assert '"connect"' in init_src          # fragment 循环注册
    html = dashboard.get_html()
    assert 'id="tab-connect"' in html       # fragment 已装配
    assert 'data-tab="tab-connect"' in html  # 导航入口
    assert "admin/connect/status" in html   # connect.js 已内联
    assert "<!-- FRAGMENT:connect -->" not in html   # 占位符已替换
    assert "<!-- JS:connect -->" not in html


# ── F7 元数据来源契约 ────────────────────────────────────────────

def test_connect_uses_f7_metadata_not_minting():
    src = _src("connect.js")
    assert "admin/security/status" in src   # 只读 Security 状态取 key 名/ID
    assert "admin/security/keys" not in src  # 绝不发 key
    assert "admin/security/enable" not in src


# ── 交互契约 ─────────────────────────────────────────────────────

def test_connect_secret_input_is_one_time_password():
    src = _src("connect.js")
    assert 'type="password"' in src
    # 弹窗关闭 / 请求结束清空输入（一次性，不留明文）
    assert "connKeyInput" in src
    assert ".value = ''" in src


def test_connect_explicit_confirm_post():
    src = _src("connect.js")
    assert "admin/connect/apply" in src
    assert "admin/connect/rollback" in src
    assert "confirmed: true" in src          # 显式确认才 POST
    assert "expected_revision" in src        # CAS revision 随请求


def test_connect_401_dedicated_and_409_no_retry():
    src = _src("connect.js")
    # 401 专用提示不被通用错误覆盖（同 deploy.js 守卫模式）
    assert src.count("err.unauthorized") >= 1
    # 409 → 重读状态要求重新确认，不自动重试（无定时重试路径）
    assert "409" in src
    assert "setTimeout" not in src


def test_connect_state_and_privacy_copy():
    src = _src("connect.js")
    # 目标分类四态文案
    assert "本机网关" in src
    assert "指向其他服务" in src
    assert "未配置" in src
    assert "未知" in src
    # 托管状态文案
    assert "已托管" in src
    # 写完需重启客户端
    assert "重启" in src


def test_connect_no_inline_onclick():
    src = _src("connect.js")
    assert "onclick=" not in src
