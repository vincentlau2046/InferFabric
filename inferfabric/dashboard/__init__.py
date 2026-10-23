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

# Dashboard v2 JS 模块加载顺序（缺失文件 graceful 跳过 + warning）。
# 后续任务逐步填充 charts/overview/inference/deploy/cloud；monitor/anomaly 已存在。
JS_MODULES = ["ui", "store", "charts", "overview", "inference",
              "monitor", "deploy", "cloud", "model-pools", "anomaly"]


def get_html() -> str:
    """组装 fragments → 返回完整 HTML。启动时缓存到内存。"""
    global _cached_html
    if _cached_html is not None:
        return _cached_html
    try:
        base = (_DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
        # 替换 fragment 占位符
        for frag_name in ("overview", "inference", "monitor", "deploy", "cloud",
                          "model-pools", "anomaly"):
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
