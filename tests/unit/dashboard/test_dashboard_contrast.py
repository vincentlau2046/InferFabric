"""
unit/dashboard/test_dashboard_contrast.py — 全局对比度地基钉住

背景（2026-10-26 用户实测坐实，v7.2.0–7.4.0 翻车第 4 缺口）：
  - 全页未声明 color-scheme → 原生 select 弹层按浅色平台渲染，
    而 option 继承 .mp-inline-sel 的 #eee → 两主题下拉列表均白底白字（≈1.0:1）
  - --if-text/--if-border/--if-text-dim 幽灵 token 从未定义、回退值按深色硬编码
    → 浅色主题 mp-* 全族白字白底（实测 1.04:1）
本文件为 CSS 文本级钉住；真实浏览器双主题 computed contrast ≥4.5:1 由
tests/interactive/test_model_pools_browser.py 硬门禁覆盖。
"""

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[3]
       / "inferfabric" / "dashboard" / "fragments" / "style.css")


def _css():
    return CSS.read_text(encoding="utf-8")


def _block(selector):
    """取 selector 规则块体（大括号内），支持含引号的属性选择器。"""
    css = _css()
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, "rule {} missing".format(selector)
    return m.group(1)


class TestContrastFoundation:
    """T1：color-scheme 双主题声明 + 幽灵 token 全灭 + select 显式背景。"""

    def test_color_scheme_declared_per_theme(self):
        # 原生控件（select 弹层/checkbox/滚动条）配色必须跟随主题
        root = _block(":root")
        assert "color-scheme: dark" in root, (
            ":root 必须声明 color-scheme: dark — 否则原生弹层按浅色平台渲染"
        )
        light = _block('[data-theme="light"]')
        assert "color-scheme: light" in light, (
            "[data-theme=\"light\"] 必须覆盖 color-scheme: light"
        )

    def test_phantom_tokens_eliminated(self):
        # --if-* 前缀 token（text/border/text-dim/accent…）从未定义，
        # 回退值按深色硬编码 — 全灭，改用主题 token（--ink/--border/--ink-2/--accent）。
        # 验收覆盖全部 --if-*（builder-check 抓漏：--if-accent 第 4 幽灵 token）
        css = _css()
        leftovers = re.findall(r"--if-[a-z0-9-]+", css)
        assert not leftovers, (
            "幽灵 token 残留 {} — 必须替换为主题 token".format(sorted(set(leftovers)))
        )

    def test_selects_have_explicit_background(self):
        # 透明背景会回退到平台色（浅色 OS 上白底）+ option 继承浅字 → 白底白字
        rule = _block(".mp-inline-sel")
        assert "background: var(--surface-2)" in rule, (
            ".mp-inline-sel 必须有显式背景 var(--surface-2)"
        )
        assert "background: transparent" not in rule, (
            ".mp-inline-sel 不得用透明背景"
        )

    def test_mp_controls_use_theme_tokens(self):
        # mp-* 规则的文字/边框必须走 --ink/--border（随主题翻转），不再硬编码
        css = _css()
        bodies = re.findall(r"\.mp-[a-z0-9-]+[^{}]*\{([^}]*)\}", css)
        assert bodies, "未找到任何 .mp-* 规则"
        joined = "\n".join(bodies)
        assert "color: var(--ink)" in joined, "mp-* 文字必须用 var(--ink)"
        assert "var(--border)" in joined, "mp-* 边框必须用 var(--border)"
