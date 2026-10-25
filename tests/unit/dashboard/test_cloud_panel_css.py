"""
unit/dashboard/test_cloud_panel_css.py — 云面板 cp-test 系列 CSS 规则钉住

终审 Critical #1（2026-10-25 双流评审）：cp-test JS（测试结果条 + Provider 表
"测试"列）曾随 P2 提交 7929828 误扫入 CSS、又于 95776ee 剥离回 cloud 流 —
本测试钉住「JS 在用的 cp-test 类必须有 CSS 规则」，防止剥离后再落库时再次
漏掉样式（无样式时状态色强调与布局丢失，双编码仍可读但降级）。
"""

import re
from pathlib import Path

DASHBOARD_DIR = (Path(__file__).resolve().parents[3]
                 / "inferfabric" / "dashboard")


def _css_rule(selector):
    css = (DASHBOARD_DIR / "fragments" / "style.css").read_text(encoding="utf-8")
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, "rule {} missing".format(selector)
    return m.group(1)


class TestCpTestCss:
    def test_result_bar_and_status_rules_present(self):
        for sel in (".cp-test-result", ".cp-test-result.ok",
                    ".cp-test-result.warn", ".cp-test-result.fail",
                    ".cp-test-result.stale"):
            _css_rule(sel)

    def test_provider_test_column_rules_present(self):
        for sel in (".cp-test", ".cp-test.ok", ".cp-test.warn",
                    ".cp-test.fail", ".cp-test.untested"):
            _css_rule(sel)

    def test_status_colors_dual_encoded(self):
        # 状态色三通道一致（与 mp 面板词表同色）
        assert "var(--ok)" in _css_rule(".cp-test.ok")
        assert "var(--warn)" in _css_rule(".cp-test.warn")
        assert "var(--crit)" in _css_rule(".cp-test.fail")
        assert "var(--ink-3)" in _css_rule(".cp-test.untested")
