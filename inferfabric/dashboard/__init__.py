"""InferFabric Dashboard — fragment-based assembly

G-3a: 拆分 dashboard.py 单文件 → fragments + js 目录
- 启动时一次性加载所有 fragments 和 JS 到内存缓存
- get_html() 返回完整 HTML，与原 dashboard.py 字节级等价
- fragment 加载失败 graceful degradation
"""

import logging
from pathlib import Path

log = logging.getLogger("inferfabric.dashboard")

_DASHBOARD_DIR = Path(__file__).parent
_cached_html: str | None = None

# Dashboard UX v3 六区 fragment 装配顺序（A2，2026-11-27）：
# - 前六项 = 六区壳，被 base.html 直接引用；其后为嵌套子 fragment。
# - 替换顺序契约：父级先于子级（config → cloud/security/connect/packs/settings；
#   packs → wizard；requests → anomaly），嵌套占位符随父级落入 base 后按序替换。
# - 缺失文件 graceful 降级（warning + 占位提示），不残留占位符。
FRAGMENTS = ["status", "requests", "usage", "routing", "models", "config",
             "anomaly", "cloud", "security", "settings", "connect",
             "packs", "wizard"]

# Dashboard v2 JS 模块加载顺序（缺失文件 graceful 跳过 + warning）。
# 后续任务逐步填充 charts/overview/inference/deploy/cloud；monitor/anomaly 已存在。
# A1（2026-11-27）：router 紧随 store —— 路由/恢复/旧键迁移唯一实现在 router.js，
# store.switchTab 仅为兼容入口；router 载入即 boot（hash 优先）。
# B2（2026-11-28）：requests（请求工作台）紧随 monitor —— 消费 /api/request_log
# window 模式，接管请求页主子页；须在 router 之后（依赖 parseHash/navigate）。
JS_MODULES = ["ui", "store", "router", "charts", "overview", "inference",
              "monitor", "requests", "deploy", "cloud", "model-pools", "anomaly",
              "packs", "security", "settings", "connect", "wizard"]
# 注意：deploy.js 仍在 JS_MODULES（部署表单已并入能力包页 deploySection，
# 但其事件委托/进度逻辑仍由 deploy.js 承载；deploy 不再是独立 fragment/tab）。


def get_html() -> str:
    """组装 fragments → 返回完整 HTML。启动时缓存到内存。"""
    global _cached_html
    if _cached_html is not None:
        return _cached_html
    try:
        base = (_DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
        # 替换 fragment 占位符（六区壳 + 嵌套子 fragment，按 FRAGMENTS 顺序）
        for frag_name in FRAGMENTS:
            frag_path = _DASHBOARD_DIR / "fragments" / f"{frag_name}.html"
            try:
                frag = frag_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log.warning("Dashboard fragment missing: %s", frag_path)
                frag = f'<div class="tab-content" id="tab-{frag_name}">'
                frag += f'<p style="padding:2em;color:#9ca3af;">⚠️ Fragment {frag_name} 加载失败，查看 iff 日志</p>'
                frag += '</div>'
            base = base.replace(f"<!-- FRAGMENT:{frag_name} -->", frag)
        # 替换 CSS 占位符
        css_path = _DASHBOARD_DIR / "fragments" / "style.css"
        try:
            css = css_path.read_text(encoding="utf-8")
            base = base.replace("<!-- CSS:main -->", css)
        except FileNotFoundError:
            log.warning("Dashboard CSS missing: %s", css_path)
        # 内联 ECharts vendor（在 JS_MODULES 之前，保证 echarts 先于 charts.js 加载）。
        # 缺失文件 graceful 降级：清空占位符 + warning；charts.js 检测到 echarts 缺失
        # 时 IFCharts.create 返回 null，调用方显示 empty state。
        echarts_path = _DASHBOARD_DIR / "vendor" / "echarts.min.js"
        try:
            echarts_js = echarts_path.read_text(encoding="utf-8")
            base = base.replace("<!-- JS:echarts -->", echarts_js)
        except FileNotFoundError:
            log.warning("ECharts vendor missing: %s (charts will degrade to empty state)",
                        echarts_path)
            base = base.replace("<!-- JS:echarts -->", "")
        # 替换 JS 占位符（缺失文件 graceful 降级：清空占位符 + warning，不残留标记）
        for js_name in JS_MODULES:
            js_path = _DASHBOARD_DIR / "js" / f"{js_name}.js"
            placeholder = f"<!-- JS:{js_name} -->"
            try:
                js = js_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log.warning("Dashboard JS missing: %s", js_path)
                js = ""
            base = base.replace(placeholder, js)
        _cached_html = base
        return base
    except Exception as e:
        log.error("Dashboard fragment load failed: %s", e)
        return _fallback_html()


def invalidate_cache():
    """使缓存失效（热重载用）"""
    global _cached_html
    _cached_html = None


def _fallback_html() -> str:
    """最小可用 HTML — fragment 加载全部失败时的降级页面"""
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>InferFabric</title>
<style>body{font-family:monospace;padding:2em;background:#fafaf9;color:#1f2937;}</style>
</head><body>
<h1>⚠️ InferFabric Dashboard</h1>
<p>Dashboard 资源加载失败，查看 iff 日志获取详情。</p>
<p>API 端点仍可正常使用：<code>/v1/models</code> <code>/api/metrics</code></p>
</body></html>"""
