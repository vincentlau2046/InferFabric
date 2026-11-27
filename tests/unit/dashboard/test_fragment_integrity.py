"""dashboard UX v3 A2 — fragment 装配完整性与六区导航（结构断言）。

规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md A2 +
specs/dashboard-ux-v3/design.md §3（六区 IA / 侧栏 / 子页）。
"""
import re
from pathlib import Path

import inferfabric.dashboard as dash
from inferfabric.dashboard import get_html

DASH_DIR = Path(dash.__file__).parent
BASE = DASH_DIR / "base.html"

SIX_VIEWS = ["status", "requests", "usage", "routing", "models", "config"]
CONFIG_SUBS = ["cloud", "auth", "connect", "packs", "sys"]
REQUEST_SUBS = ["main", "switches", "anomalies"]


def test_registered_fragments_exist_and_referenced_exactly_once():
    frags = getattr(dash, "FRAGMENTS", None)
    assert frags, "dashboard.__init__ 必须暴露显式 FRAGMENTS 列表"
    # 装配顺序契约：六区壳先于其子 fragment（嵌套替换按序生效）
    assert frags[:6] == SIX_VIEWS, "前六项必须是六区壳"
    src = {"base": BASE.read_text(encoding="utf-8")}
    for name in frags:
        p = DASH_DIR / "fragments" / f"{name}.html"
        assert p.exists(), f"fragment missing: {p}"
        src[name] = p.read_text(encoding="utf-8")
    # base 只引用六区壳，且每壳恰好一次
    for name in SIX_VIEWS:
        assert src["base"].count(f"<!-- FRAGMENT:{name} -->") == 1, name
    # 其余 fragment（配置五子页 / 异常 / 向导）必须在某个已注册父级被唯一引用
    for name in frags[6:]:
        parents = [k for k, v in src.items()
                   if k != name and v.count(f"<!-- FRAGMENT:{name} -->") > 0]
        assert len(parents) == 1, f"{name} 必须且只能被一个已注册 fragment 引用: {parents}"
        assert src["base"].count(f"<!-- FRAGMENT:{name} -->") == 0, name
    # pack→wizard 嵌套引用保留
    assert src["packs"].count("<!-- FRAGMENT:wizard -->") == 1
    # 装配后无残留占位符
    html = get_html()
    assert "<!-- FRAGMENT:" not in html
    assert "<!-- JS:" not in html
    assert "<!-- CSS:main" not in html


def test_assembled_document_has_unique_element_ids():
    html = get_html()
    # 只检查 DOM 标记中的 id：内联 JS 的字符串字面量（innerHTML 重建）会重现已存在 id，
    # 不算装配重复。剥离 <script> 块后提取。
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'\bid="([^"]+)"', markup)
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups, f"装配后存在重复 id（搬运重复或包装未去）: {dups}"


def test_primary_nav_has_six_views():
    html = BASE.read_text(encoding="utf-8")
    tabs = re.findall(r'class="if-nav-item[^"]*"\s+data-tab="([^"]+)"', html)
    assert tabs == ["tab-status", "tab-requests", "tab-usage",
                    "tab-routing", "tab-models", "tab-config"]
    assert 'class="nav-cap"' not in html          # 六区无分组标题
    assert html.count('class="if-nav-item') == 6  # 旧 10 项结构移除


def test_six_view_shells_are_the_only_tab_contents():
    html = get_html()
    # 六区壳是仅有的 .tab-content 面板（旧平铺 tab 包装全部移除）
    shells = re.findall(r'<div class="tab-content[^"]*" id="(tab-[a-z-]+)"', html)
    assert shells == [f"tab-{v}" for v in SIX_VIEWS]


def test_request_and_config_sub_sections_and_entries():
    html = get_html()
    # 每子页 = 入口按钮 + 内容节（至少各一，共 ≥2 次 data-sub）
    for sub in REQUEST_SUBS:
        assert html.count(f'data-sub="{sub}"') >= 2, sub
    for sub in CONFIG_SUBS:
        assert html.count(f'data-sub="{sub}"') >= 2, sub
    # 临时页内入口走单一导航源（router），不自持状态
    assert html.count("ifRouter.navigate('requests'") >= 3
    assert html.count("ifRouter.navigate('config'") >= 5


def test_legacy_delegation_roots_preserved():
    html = get_html()
    # 既有 JS 事件委托根（overview/inference）随内容平移保留
    assert 'id="tab-overview"' in html
    assert 'id="tab-inference"' in html
    # 全局遥测带保留在壳层
    assert 'id="telemetryRail"' in html