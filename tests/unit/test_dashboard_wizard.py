"""wizard.js / wizard.html — 五步部署向导（部署向导计划 Task 4，源码契约）。

风格沿 fork test_dashboard_security_page：对组装产物与 JS 源码做契约断言。
Review Focus 5（切 tab/Esc 重置在途态、防双击）在源码契约层钉住。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

_DASH = _ROOT / "inferfabric" / "dashboard"
_JS = (_DASH / "js" / "wizard.js").read_text(encoding="utf-8")
_FRAG = (_DASH / "fragments" / "wizard.html").read_text(encoding="utf-8")


class TestAssembly:
    def test_registered_in_js_modules_and_fragments(self):
        import inferfabric.dashboard as dash
        assert "wizard" in dash.JS_MODULES
        html = dash.get_html()
        assert 'id="tab-deploy-wizard"' in html, "向导卡必须被组装进页面"
        assert "FRAGMENT:wizard" not in html, "占位符必须被替换（不得静默残留）"

    def test_mounted_inside_model_pools(self):
        mp = (_DASH / "fragments" / "model-pools.html").read_text(
            encoding="utf-8")
        assert "FRAGMENT:wizard" in mp

    def test_fragment_is_wizard_card(self):
        assert 'id="tab-deploy-wizard"' in _FRAG
        assert 'id="wizBody"' in _FRAG


class TestSourceContract:
    def test_consumes_backend_chain(self):
        # 一键序列：下载 → 轮询 → 装载（install 语义，非 /deploy 的 switch）→ 自启
        for token in ("admin/deploy/options", "admin/deploy/search",
                      "admin/deploy/download", "admin/deploy/download/status",
                      "admin/packs/install", "admin/models/set-autostart"):
            assert token in _JS, token

    def test_select_file_variant_picker(self):
        # 多量化变体：消费结构化 400（code select_file + choices），不解析文案
        assert "select_file" in _JS
        assert "choices" in _JS

    def test_no_inline_onclick(self):
        for name in ("js/wizard.js", "fragments/wizard.html",
                     "fragments/model-pools.html"):
            text = (_DASH / name).read_text(encoding="utf-8")
            assert "onclick=" not in text, name

    def test_busy_anti_doubleclick(self):
        assert "_busy" in _JS

    def test_tab_switch_and_escape_reset(self):
        # Review Focus 5：切出 tab 停轮询/重置在途态；Esc 关闭重置
        assert "tab_active" in _JS
        assert "Escape" in _JS

    def test_errors_use_toast(self):
        assert "UI.toast(" in _JS

    def test_five_steps(self):
        # 五步状态机：能力→框架→部署方式→搜索→参数一键
        assert "renderStep" in _JS
        for label in ("能力", "框架", "部署方式", "搜索", "参数"):
            assert label in _JS, label

    def test_search_debounce_and_pagination(self):
        assert "300" in _JS          # 防抖 300ms
        assert "加载更多" in _JS

    def test_autostart_default_off(self):
        # 「自启」是显式勾选项，非默认（Mac 语义）
        assert "autostart" in _JS
        assert "checked" in _JS
