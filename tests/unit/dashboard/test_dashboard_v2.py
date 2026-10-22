"""Dashboard v2 (InferFabric Console) — 结构断言。增量随任务扩展。"""
from pathlib import Path
from inferfabric.dashboard import get_html, _DASHBOARD_DIR

ROOT = Path(__file__).resolve().parents[3]  # sandbox 根


def _html():
    return get_html()


def test_no_leftover_placeholders():
    html = _html()
    assert "<!-- FRAGMENT:" not in html
    assert "<!-- JS:" not in html
    assert "<!-- CSS:main" not in html


def test_single_head_close_for_token_stats_injection():
    """_serve_dashboard 向 </head> 前注入 __TOKEN_STATS__，必须恰好一个。"""
    assert _html().count("</head>") == 1


def test_dual_theme_tokens():
    html = _html()
    assert '[data-theme="light"]' in html
    for token in ("--accent", "--ink", "--ok", "--warn", "--crit", "--info"):
        assert token in html


def test_shell_structure():
    html = _html()
    for el in ('id="telemetryRail"', 'id="themeToggle"', 'class="if-nav"'):
        assert el in html


def test_overview_structure():
    """总览页 DOM 契约：Task 3 接数据时按这些 id 取节点。"""
    html = _html()
    for el in ('id="ovActiveCard"', 'id="ovActiveBody"', 'id="ovSpark24h"',
               'id="ovAnomTop"', 'id="sysOpsCard"'):
        assert el in html, "missing overview contract id: %s" % el
    # 系统操作按钮 data-action 契约
    for act in ('release', 'reconcile', 'reload', 'reset'):
        assert 'data-action="%s"' % act in html, "missing sys-ops data-action: %s" % act


def test_no_emoji_in_shell():
    import re
    # 顶栏/侧栏区域不得含 emoji 区段（U+1F300-1FAFF, U+2600-27BF）
    m = re.search(r'<header.*?</header>', _html(), re.S)
    assert m and not re.findall(r'[\U0001F300-\U0001FAFF☀-➿]', m.group(0))


def test_icon_symbols_present():
    """新增 SVG symbol id 必须就位（扩集抽样）。"""
    html = _html()
    for sym in ('id="s-play"', 'id="s-stop"', 'id="s-key"', 'id="s-trash"',
                'id="s-search"', 'id="s-shield"', 'id="s-database"'):
        assert sym in html


def test_js_modules_list():
    """__init__.py 必须用显式 JS_MODULES 列表。"""
    import inferfabric.dashboard as dash
    assert getattr(dash, "JS_MODULES", None) == [
        "ui", "store", "charts", "overview", "inference",
        "monitor", "deploy", "cloud", "model-pools", "anomaly",
    ]


def test_legacy_js_deleted():
    """bindings.js / state.js 已删除。"""
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "bindings.js").exists()
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "state.js").exists()


def test_new_js_present():
    """ui.js / store.js 已创建。"""
    assert (ROOT / "inferfabric" / "dashboard" / "js" / "ui.js").exists()
    assert (ROOT / "inferfabric" / "dashboard" / "js" / "store.js").exists()


# ── UI.confirm 监听器泄漏回归测试（Node 行为级 harness） ──────────
# 手写 mock DOM：每个 El 记录 addEventListener；holder.innerHTML 被赋值时
# 清空子缓存（模拟子节点销毁）。验证两次连续 confirm 后，仅当前 onOk 触发。
_CONFIRM_LEAK_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function El() {
  this._html = '';
  this._childCache = {};
  this._listeners = {};
  this.style = {};
  this.textContent = '';
  this.className = '';
  this._attrs = {};
  var self = this;
  Object.defineProperty(this, 'innerHTML', {
    get: function () { return self._html; },
    set: function (v) { self._html = v; self._childCache = {}; }
  });
}
El.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
El.prototype.removeEventListener = function (type, fn) {
  var arr = this._listeners[type]; if (!arr) return;
  var i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
};
El.prototype.getAttribute = function (k) { return this._attrs[k] != null ? this._attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this._attrs[k] = v; };
El.prototype.focus = function () {};
El.prototype.querySelector = function (sel) {
  if (!this._html) return null;
  if (!this._childCache[sel]) this._childCache[sel] = new El();
  return this._childCache[sel];
};
El.prototype.querySelectorAll = function (sel) {
  var el = this.querySelector(sel); return el ? [el] : [];
};
El.prototype.clickListeners = function () { return this._listeners['click'] || []; };

var holder = new El();
var docListeners = {};
var document = {
  getElementById: function (id) { return id === 'confirmModal' ? holder : new El(); },
  addEventListener: function (type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  removeEventListener: function (type, fn) {
    var arr = docListeners[type]; if (!arr) return;
    var i = arr.indexOf(fn); if (i >= 0) arr.splice(i, 1);
  }
};

global.window = global;
global.document = document;
global.setTimeout = function () {};
global.confirm = function () { return true; };

var code = fs.readFileSync(process.argv[2], 'utf8');
vm.runInThisContext(code);

var onOk1 = 0, onOk2 = 0;

// Cycle 1: open confirm, then close via Escape (must NOT fire onOk)
global.UI.confirm({ title: 't1', body: 'b1', onOk: function () { onOk1++; } });
(docListeners['keydown'] || []).slice().forEach(function (fn) { fn({ key: 'Escape' }); });

// Cycle 2: open confirm (the current one)
global.UI.confirm({ title: 't2', body: 'b2', onOk: function () { onOk2++; } });
var backdrop2 = holder.querySelector('.if-modal-backdrop');

// Simulate an OK click bubbling to wherever the listener lives (holder or backdrop)
var okEvent = { target: { getAttribute: function (k) { return k === 'data-act' ? 'ok' : null; } } };
holder.clickListeners().slice().forEach(function (fn) { fn(okEvent); });
if (backdrop2) backdrop2.clickListeners().slice().forEach(function (fn) { fn(okEvent); });

console.log(JSON.stringify({
  onOk1: onOk1,
  onOk2: onOk2,
  holderClickListeners: holder.clickListeners().length,
  backdropClickListeners: backdrop2 ? backdrop2.clickListeners().length : 0
}));
"""


def test_ui_confirm_no_listener_leak(tmp_path):
    """UI.confirm 不得在持久 #confirmModal holder 上累积 click 监听器（否则
    会重复触发先前已关闭 confirm 的 onOk 闭包）。

    行为级测试：在 node harness 中加载 ui.js + mock DOM，跑两轮 confirm，
    断言仅第二轮 onOk 触发，且 holder 上无残留 click 监听器。"""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    ui_js = ROOT / "inferfabric" / "dashboard" / "js" / "ui.js"
    harness = tmp_path / "confirm_leak_harness.js"
    harness.write_text(_CONFIRM_LEAK_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(ui_js)],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, (
        "node harness failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)
    )
    result = json.loads(proc.stdout.strip())
    # 仅当前（第二轮）onOk 触发；第一轮的 onOk 不得因泄漏重复触发
    assert result["onOk1"] == 0, "stale onOk leaked (re-fired): %s" % result
    assert result["onOk2"] == 1, "current onOk did not fire exactly once: %s" % result
    # holder 上不得残留 click 监听器（泄漏的特征）
    assert result["holderClickListeners"] == 0, (
        "click listener leaked onto persistent holder: %s" % result
    )


# ── Task 3: 总览页接真实数据 ──────────────────────────────────────


def test_overview_js_present():
    """overview.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + doSystemOps）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "overview.js"
    assert js_path.exists(), "overview.js missing"
    html = _html()
    # overview.js 在 JS_MODULES 中，由 __init__.py 内联进 HTML
    assert "window.tabRenderers['tab-overview'] = renderOverview" in html, (
        "overview.js tab renderer registration not inlined into HTML"
    )
    assert "window.doSystemOps" in html, (
        "window.doSystemOps not inlined into HTML"
    )
    # 遥测带绑定 + 系统操作端点契约
    assert "renderRail" in html
    assert "'/switch'" in html and "'/reset'" in html
    assert "'/reconcile'" in html and "'/reload-config'" in html


def test_overview_no_fake_script():
    """overview.html 不得残留 Task 2 静态原型假数据 <script>（已由 overview.js 接管）。

    断言：fragment 无任何内联 <script>；无 'prototype-only' 注释标记；
    无假值 '24.1'（原型 VRAM 假值）/ MutationObserver 原型代码。
    """
    frag = (_DASHBOARD_DIR / "fragments" / "overview.html").read_text(encoding="utf-8")
    assert "<script" not in frag, "overview.html must not contain an inline <script> block"
    assert "prototype-only" not in frag
    assert "24.1" not in frag
    assert "MutationObserver" not in frag
    # 占位结构保留（overview.js 按 id 填充）
    assert 'id="ovActiveCard"' in frag
    assert 'id="ovActiveBody"' in frag
    assert 'id="ovSpark24h"' in frag


# ── Task 4: ECharts vendor + 主题图表工厂 + CVD 调色板 ────────────────


def test_echarts_vendored():
    """ECharts 5.5.1 vendor 文件存在，并被 __init__.py 内联进 get_html()。

    断言：vendor 文件 > 900KB；get_html() 含 echarts UMD 标识且体积 > 1MB
    （echarts.min.js 本体 ~1.03MB；未内联时 get_html 仅 ~94KB，故 >1MB 即证明已内联）；
    无残留 <!-- JS:echarts --> 占位符。"""
    vendor = ROOT / "inferfabric" / "dashboard" / "vendor" / "echarts.min.js"
    assert vendor.exists(), "echarts.min.js vendor missing"
    assert vendor.stat().st_size > 900_000, "echarts vendor too small (<900KB)"
    html = _html()
    # echarts UMD 包标识（5.5.1）——内联后必然出现在 HTML 中
    assert "echarts" in html
    assert "5.5.1" in html
    # 内联后单文件体积 ~1.12MB（echarts 1.03MB + shell/css/js ~94KB）
    assert len(html) > 1_000_000, "get_html() size %d < 1MB (echarts not inlined?)" % len(html)
    # 占位符已被替换（test_no_leftover_placeholders 也覆盖）
    assert "<!-- JS:echarts -->" not in html


def test_echarts_loads_before_charts_module():
    """echarts 必须在 charts.js 之前内联（charts.js 依赖 window.echarts）。"""
    html = _html()
    i_echarts = html.find("typeof exports")   # echarts UMD 头部特征
    i_ifcharts = html.find("window.IFCharts")
    assert i_echarts > 0 and i_ifcharts > 0, "echarts UMD head or IFCharts not found"
    assert i_echarts < i_ifcharts, "echarts must be inlined before charts.js (IFCharts)"


def test_charts_js_present():
    """charts.js 模块存在并被 get_html() 内联装配（window.IFCharts 工厂）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "charts.js"
    assert js_path.exists(), "charts.js missing"
    html = _html()
    # IFCharts 工厂契约
    assert "window.IFCharts" in html, "IFCharts factory not inlined"
    for member in ("create:", "update:", "dispose:", "onThemeChange:", "palettes:"):
        assert member in html, "IFCharts member not inlined: %s" % member
    # spec §7 规则编码进工厂
    assert "axisPointer" in html and "type: 'cross'" in html   # crosshair tooltip
    assert "symbol = 'none'" in html                            # 无逐点标记
    assert "lineStyle.width = 2" in html                        # 细线 2px


def test_chart_palette_values():
    """CVD 全对验证通过的统一调色板 v7.0（5 槽）必须逐字出现在 get_html()。

    固定顺序 蓝→琥珀→青→绿→粉；dark/light 同 hex（v7.0 统一，消除原两套蓝）。
    语义：[1] 琥珀 = 累计线专用槽；[0]/[2]/[3]/[4] = 数据系列槽。改动任一值须重跑
    dataviz validate_palette.js --pairs all --mode dark|light 保持 ALL PASS。"""
    html = _html()
    palette = ['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777']
    for hexv in palette:
        assert html.count(hexv) >= 2, "palette hex %s must appear in both dark & light arrays" % hexv
    # 两组调色板作为连续数组字面量出现（强断言：顺序、5 槽、dark/light 同值）
    expected = "['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777']"
    assert html.count(expected) >= 2, "v7.0 5-slot palette literal must appear for both dark & light"


def test_charts_js_node_syntax():
    """charts.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "charts.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check charts.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_charts_data_array_replaced_not_merged():
    """IFCharts._deepMerge 对 ECharts `data` 数组整体替换，不按下标合并残留旧点。

    回归：Token 图表从 hour(60 个 HH:MM 点)切到 day(14 个 MM-DD 点)时，
    _mergeArray 按下标合并会保留第 15-60 个旧时间标签 → 横坐标前段日期、
    后段时间，尺度不统一。series.data 同理残留 ghost 数据条。
    修复：_deepMerge 遇 key==='data' 时整体替换。
    """
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "charts.js"
    js = js_path.read_text(encoding="utf-8")

    # 用 node 加载 charts.js + echarts stub，模拟 hour→day 切换，断言 data 不残留。
    # charts.js 是浏览器 IIFE（引用 window/document），node -e 无这些全局，
    # 故先声明 stub 全局再注入脚本。
    script = (
        r"""
        var window = {};
        var _lastOpt = null;
        window.echarts = {
          init: function () {
            return {
              _ifcId: null,
              setOption: function (opt) { _lastOpt = opt; },
              dispose: function () {},
              resize: function () {},
            };
          },
        };
        var document = {
          getElementById: function () { return { id: 'c1' }; },
          documentElement: { getAttribute: function () { return null; } },
        };
        window.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
        window.addEventListener = function () {};
        window.store = { on: function () {} };
        var MutationObserver = undefined;
        """
        + "\n"
        + js
        + "\n"
        + r"""
        var c = window.IFCharts.create('c1');
        // 第一次 update：hour 模式 60 个时间点
        var hourXs = []; for (var i = 0; i < 60; i++) hourXs.push('0' + (i%24) + ':00');
        window.IFCharts.update(c, {
          xAxis: { data: hourXs, boundaryGap: true },
          series: [
            { type: 'bar', name: 'Prompt', stack: 'tok', data: new Array(60).fill(10) },
            { type: 'bar', name: 'Completion', stack: 'tok', data: new Array(60).fill(5) },
          ],
        });
        // 第二次 update：day 模式 14 个日期点
        var dayXs = []; for (var j = 0; j < 14; j++) dayXs.push('09-' + (j+1));
        window.IFCharts.update(c, {
          xAxis: { data: dayXs, boundaryGap: true },
          series: [
            { type: 'bar', name: 'Prompt', stack: 'tok', data: new Array(14).fill(20) },
            { type: 'bar', name: 'Completion', stack: 'tok', data: new Array(14).fill(8) },
          ],
        });
        // 断言：xAxis.data 恰好 14 个，全是 MM-DD，无 HH:MM 残留
        var xd = _lastOpt.xAxis.data;
        var res = xd.length === 14
          && xd.every(function (x) { return /^09-/.test(x); })
          && _lastOpt.series[0].data.length === 14
          && _lastOpt.series[1].data.length === 14;
        console.log(res ? 'PASS' : 'FAIL: xAxis.data len=' + xd.length + ' sample=' + JSON.stringify(xd.slice(0,3)) + '..' + JSON.stringify(xd.slice(-3)));
        process.exit(res ? 0 : 1);
        """
    )
    proc = subprocess.run([node, "-e", script],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "charts data-replace regression FAILED:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_theme_state_hooked_in_store():
    """store.js 必须把主题状态注入 store('theme') 供 charts.js 订阅重建。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "store.js").read_text(encoding="utf-8")
    # toggleTheme 与 initTheme 都 set 'theme'
    assert "store.set('theme', 'dark')" in js
    assert "store.set('theme', 'light')" in js
    html = _html()
    # charts.js 订阅 store.on('theme')
    assert "store.on('theme'" in html or "window.store.on('theme'" in html


# ── Task 5: 监控 TAB（纯遥测，只读） ────────────────────────────────


def test_monitor_structure():
    """监控页 DOM 契约：7 个面板 id 必须在 get_html() 中出现（spec §4.3）。"""
    import re as _re
    html = _html()
    for panel_id in (
        'id="monPowerChart"',   # 功耗 / 电费：单图双轴（柱=平均功耗 W，阶梯线=累计电量/电费 度=元）
        'id="monTokenLocalChart"',  # Token 用量 · 本地引擎（vllm/sglang/ninfer…）
        'id="monTokenCloudChart"',  # Token 用量 · 云端透传
        'id="monTtftChart"',    # 延迟小倍数 · TTFT P50/P95（v6.0 双卡）
        'id="monTpotChart"',    # 延迟小倍数 · TPOT P50/P95（v6.0 双卡）
        'id="monKpis"',         # 五联 KPI
        'id="monLogTable"',     # 请求日志表
        'id="monHistTable"',    # 切换历史表
    ):
        assert panel_id in html, "missing monitor panel id: %s" % panel_id
    # v6.4: 费用概览卡整体删除（滚动 24h 无参考价值）；Token 改左右双卡 +
    # 共享粒度控制条（本地累计 token 量 / 云端累计费用 ¥）
    assert 'id="monCostCard"' not in html, "费用概览卡必须已删除"
    assert 'id="monTokenLocalTotal"' in html, "本地卡头缺窗口累计 token 读数"
    assert 'id="monTokenCloudTotal"' in html, "云端卡头缺窗口累计费用读数"
    assert 'data-seg="gran"' in html, "Token 共享粒度控制条缺失"
    # 窗口/粒度切换按钮 data-win / data-gran 契约（display filter，单位语义统一 v6.3）
    # 功耗/电费 pgran: 小时/天/周（月整体弃用）；Token gran: 分钟/小时/天/周；
    # 延迟 latwin: 分钟/小时/天（数据仅 30d 不做周）
    assert 'data-gran="hour"' in html and 'data-gran="day"' in html and 'data-gran="week"' in html
    assert 'data-gran="month"' not in html, "month granularity must be dropped from Token card"
    for gran in ('minute', 'hour', 'day', 'week'):
        assert 'data-gran="%s"' % gran in html, "missing token granularity toggle: %s" % gran
    # Token 四档按钮按单位升序排列（分钟 → 小时 → 天 → 周）——锚定分钟后紧邻的同卡按钮，
    # 避免误匹配功耗卡（hour/day/week）在前的位置
    assert _re.search(
        r'data-gran="minute"[\s\S]*?data-gran="hour"[\s\S]*?data-gran="day"[\s\S]*?data-gran="week"',
        html), "token granularity buttons must render in ascending unit order: 分钟/小时/天/周"
    for win in ('minute', 'hour', 'day'):
        assert 'data-win="%s"' % win in html, "missing latency window toggle: %s" % win
    assert 'data-win="week"' not in html, "latency must NOT offer week (data only 30d)"
    # v6.0: 延迟趋势双卡 — 共享控制条（窗口 latwin 单条不再镜像 + 分位 latq）+ 模型 chip 选择器
    assert 'data-seg="latwin"' in html
    assert 'data-seg="latq"' in html
    assert 'id="monLatChips"' in html
    assert 'id="monLatSub"' in html
    assert 'id="monTtftEmpty"' in html and 'id="monTpotEmpty"' in html


def test_monitor_js_present():
    """monitor.js 模块存在并被 get_html() 内联装配（tabRenderers 注册）。

    断言：文件存在；get_html() 含 tabRenderers['tab-monitor'] 注册；
    三图通过 IFCharts.create 创建（null-check）；store sync_meta 订阅。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js"
    assert js_path.exists(), "monitor.js missing"
    html = _html()
    assert "window.tabRenderers['tab-monitor'] = renderMonitor" in html, (
        "monitor.js tab renderer registration not inlined into HTML"
    )
    js = js_path.read_text(encoding="utf-8")
    # 五图均通过 IFCharts.create 创建（null-check each）；Token 拆本地/云端两张
    assert "IFCharts.create('monPowerChart')" in js
    assert "IFCharts.create('monTokenLocalChart')" in js
    assert "IFCharts.create('monTokenCloudChart')" in js
    assert "IFCharts.create('monTtftChart')" in js
    assert "IFCharts.create('monTpotChart')" in js
    # 订阅 store sync_meta（snapshot 到达时刷新）
    assert "store.on('sync_meta'" in js
    # 事件委托：窗口/粒度切换
    assert "data-win" in js and "data-gran" in js


def test_monitor_token_curve_all_tiers_endpoint():
    """Token 卡四档（分钟/小时/天/周）统一走 /api/token-curve 端点；token_stats
    的按天/按月聚合代码与 __TOKEN_STATS__ 引用已从 monitor.js 移除（overview.js
    7 天趋势仍用 __TOKEN_STATS__，不受影响）；月档整体弃用。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    assert "/api/token-curve?granularity=" in js, "all tiers must fetch token-curve endpoint"
    # 桶语义常量（与服务端 spec 字典一一对应）：minute=12×5min / hour=24×1h /
    # day=30×1d / week=13×1周。fetch URL 动态拼接（granularity=' + gran），
    # 档位覆盖以 _TOKEN_N/_TOKEN_W 双映射为静态契约断言。
    import re as _re
    assert _re.search(r"_TOKEN_N\s*=\s*\{\s*minute:\s*12,\s*hour:\s*24,\s*day:\s*30,\s*week:\s*13", js), \
        "bucket-count map must cover minute/hour/day/week"
    assert _re.search(r"_TOKEN_W\s*=\s*\{\s*minute:\s*5\s*\*\s*60000,\s*hour:\s*3600000,\s*day:\s*86400000,\s*week:\s*7\s*\*\s*86400000", js), \
        "bucket-width map must cover minute/hour/day/week"
    # token_stats 按天/按月聚合已移除（周档只有端点有 90d 数据）
    for dead in ("buildTokenDay", "buildTokenMonth", "_tokenStatsSource",
                 "buildTokenHour", "_tokenHourFromBuckets"):
        assert dead not in js, "monitor.js still contains removed token-stats builder: %s" % dead
    # 月整体弃用：monitor.js 不得再出现 month 档位分支
    assert "granularity=month" not in js and "gran === 'month'" not in js, \
        "month tier must be dropped from monitor.js"


def test_monitor_token_dual_cards():
    """Token 用量左右双卡（v6.4，取代单卡上下堆叠 + 费用概览卡）。

    布局：col-12 共享粒度控制条（data-seg=gran，驱动本地/云端双卡同粒度联动）
    + col-6×2 双卡。每卡双轴图（{dualAxis:true} 受权例外，与功耗/电费卡同构，
    流速↔存量积分对）：柱 = 每桶 Prompt/Completion 堆叠（左轴 tokens），
    折线 = 窗口起点累积（右轴）——本地 = 累计 token 量（自己的 GPU 免费跑，
    量有意义），云端 = 累计费用 ¥（按量付费，钱有意义；桶 cost 由
    /api/token-curve 服务端按价格表计算）。费用概览卡整体删除。"""
    import re as _re
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    html = _html()

    # 1. 费用概览卡彻底移除（渲染函数 + DOM 契约 + 内联 CSS 类）
    assert "renderCostCard" not in js, "monitor.js 仍含费用概览卡渲染函数"
    assert "monCostCard" not in html and "monCostBody" not in html
    assert "mon-cost-total" not in html, "费用卡 CSS 类应随卡删除"

    # 2. 左右双卡 DOM 契约（本地左 / 云端右，各卡头：窗口累计读数 + Cache Hit 徽章）
    for el in ('id="monTokenLocalChart"', 'id="monTokenCloudChart"',
               'id="monTokenLocalTotal"', 'id="monTokenCloudTotal"',
               'id="monTokenLocalCacheHit"', 'id="monTokenCloudCacheHit"'):
        assert el in html, "missing token dual-card contract id: %s" % el
    # 旧上下堆叠容器不再存在
    assert "mon-token-split" not in html, "旧双 scope 上下堆叠容器应已移除"

    # 3. 双轴受权：功耗卡 2 分支 + Token 双卡 1 处（scope 循环单 update 路径，
    #    空态与正常态同一 yAxis 双轴结构，无需独立空态骨架分支）
    assert js.count('{ dualAxis: true }') >= 3, (
        "power card 2 branches + token dual card must sanction dual-axis "
        "via { dualAxis: true } (>=3 occurrences in monitor.js)"
    )
    # 4. 云端累积费用：桶 cost 字段前缀和（cumCost）；本地累积 token（cumTok）
    assert "cumCost" in js, "cloud cumulative cost prefix-sum missing"
    assert "cumTok" in js, "local cumulative token prefix-sum missing"
    assert _re.search(r"b\.cost\s*\|\|\s*0", js), \
        "cumCost must read bucket cost field (b.cost || 0)"
    # 5. 右轴 = 累积（position:'right'）：功耗卡 2 处 + Token 卡 1 处
    assert js.count("position: 'right'") >= 3, (
        "cumulative right axis (position:'right') must appear on power (2) + token (1)"
    )
    # 6. 统一色系（v7.0）：语义→色号映射跨卡一致，柱 Prompt 蓝[0] / Completion 青[2]
    #    / 累计线 琥珀[1]。「累积/存量」线 = palette[1] 琥珀（跨卡锚点，暖色与冷色柱
    #    天然分层）；「分量/流速」柱跳过琥珀[1] 避免与累计线同色。蓝↔青全对 ΔE 16.3
    #    双主题过 15。趋势卡逐模型色走 _dataColors()（跳过琥珀，[蓝,青,绿,粉]）。
    assert "_cumColor" in js, "token cumulative line must pin color via _cumColor()"
    assert _re.search(r"_paletteColor\(1,\s*'#b45309'\)", js), (
        "_cumColor must resolve palette index 1 (amber #b45309, matches power card)"
    )
    assert _re.search(r"lineStyle:\s*\{\s*color:\s*_cumColor\(\)\s*\}", js), (
        "token cumulative line must pin lineStyle.color (line stroke) — "
        "itemStyle alone leaves the line stroke on auto palette"
    )
    assert _re.search(r"itemStyle:\s*\{\s*color:\s*_cumColor\(\)\s*\}", js), (
        "token cumulative line series must carry itemStyle.color=_cumColor()"
    )
    assert _re.search(r"itemStyle:\s*\{\s*color:\s*_paletteColor\(0,\s*'#2563eb'\)\s*\}", js), (
        "token Prompt bar must pin itemStyle.color to palette[0] blue — "
        "cross-card consistent with power card bar (both auto/pinned blue[0])"
    )
    assert _re.search(r"itemStyle:\s*\{\s*color:\s*_paletteColor\(2,\s*'#0891b2'\)\s*\}", js), (
        "token Completion bar must pin itemStyle.color to palette[2] cyan — "
        "skips amber[1] reserved for the cumulative line"
    )
    # v7.0: 趋势卡色源统一走 PALETTES（_dataColors 跳过琥珀[1]），_MODEL_COLORS 已废弃删除
    assert "_MODEL_COLORS" not in js, (
        "v7.0: _MODEL_COLORS must be removed — trend card colors now via _dataColors()/PALETTES"
    )
    assert "_dataColors" in js, "v7.0: _dataColors() must supply trend card rank colors"
    assert _re.search(r"\[p\[0\],\s*p\[2\],\s*p\[3\],\s*p\[4\]\]", js), (
        "_dataColors must return [blue, cyan, green, pink] — skipping amber[1]"
    )


def test_monitor_latency_cards():
    """v6.0 延迟双卡趋势契约：TTFT/TPOT 各单 y 轴（两指标量纲差 ~2 个数量级，禁双 y 轴，
    spec 反模式 #1）；窗口数据走 GET /api/latency（Task 2 端点，只读 display filter）；
    图表/表格视图切换（data-view）已移除。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    assert "monTtftChart" in js and "monTpotChart" in js
    assert "/api/latency?window=" in js      # minute/hour/day 窗口取数（GET-only）
    assert "data-view" not in js             # 图表/表格视图切换已移除
    html = _html()
    assert 'id="monTtftChart"' in html and 'id="monTpotChart"' in html


def test_monitor_readonly():
    """监控 TAB 遥测只读（spec §4.3），唯一例外 = v6.5 一键认领/管理删除。

    设计文档 2026-09-28-agent-aware-clients §5：认领闭环（POST /api/agents）
    与管理删除（DELETE /api/agents?id=）落进监控 TAB，但走 control-plane
    admin-token 保护（UI.adminHeaders，同 /switch /stop）——写操作仅此一条，
    其余所有 fetch 必须为 GET（display filter 不触达服务端状态）。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    # v6.5 例外：认领 POST + 管理 DELETE 必须在 adminHeaders 保护的 /api/agents fetch 中
    assert 'method: \'POST\'' in js and "'/api/agents'" in js, \
        "monitor.js 缺少 admin 保护的一键认领 POST /api/agents"
    assert 'method: \'DELETE\'' in js and "'/api/agents?id='" in js, \
        "monitor.js 缺少 admin 保护的管理删除 DELETE /api/agents?id="
    # 除认领行外不得出现任何其它 POST/DELETE（非 admin 写操作 = 违反只读契约）。
    # fetch 的 URL 与 method 常在相邻行（Object.assign 拆行）→ 按 ±3 行邻域判断。
    lines = js.splitlines()
    bad = []
    for i, ln in enumerate(lines):
        if "POST" not in ln and "DELETE" not in ln:
            continue
        nb = "\n".join(lines[max(0, i - 3):i + 3])
        if "adminHeaders" in nb or "'/api/agents'" in nb:
            continue
        bad.append(ln.strip())
    assert not bad, f"monitor.js 存在非 admin 写操作: {bad}"
    # HTML fragment 不得有 form action 或 method
    frag = (_DASHBOARD_DIR / "fragments" / "monitor.html").read_text(encoding="utf-8")
    assert "method=" not in frag, "monitor.html contains method= — read-only violation"
    assert "<form" not in frag, "monitor.html contains <form> — read-only violation"


def test_monitor_latency_trend():
    """v6.0 延迟趋势 DOM/JS 契约：共享控制条（窗口 latwin + 分位 latq）+ 模型 chip
    选择器（#monLatChips）+ TTFT/TPOT 双趋势卡（#monTtftChart/#monTpotChart 保留）；
    只读——fragment 无 <form。"""
    frag = (_DASHBOARD_DIR / "fragments" / "monitor.html").read_text(encoding="utf-8")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    # 控制条契约
    assert 'id="monLatChips"' in frag
    assert 'data-seg="latq"' in frag
    assert 'id="monLatSub"' in frag
    # 趋势双卡容器仍在
    assert 'id="monTtftChart"' in frag and 'id="monTpotChart"' in frag
    assert 'id="monTtftSub"' in frag and 'id="monTpotSub"' in frag
    # 视图切换/表格已移除
    assert 'data-seg="lattf"' not in frag and 'data-seg="latpt"' not in frag
    assert 'monTtftTable' not in frag and 'monTpotTable' not in frag
    # JS 侧：/api/latency 取数（GET-only）+ chip / 分位切换接线
    assert "/api/latency?window=" in js
    assert "monLatChips" in js and "data-model" in js
    assert "'latq'" in js
    # v6.0：中间空桶桥接（视觉平滑、不造数据点；首尾空不延伸）
    assert "connectNulls: true" in js
    # 默认选中全部在用模型（后端已按请求数截断 ≤5），不再固定前 4
    assert "_latSel = chips.slice()" in js
    # 只读：无 <form
    assert "<form" not in frag


def test_monitor_latency_series_colors_survive_apply_rules():
    """逐模型折线色（series[].lineStyle.color / itemStyle.color）与低置信 data item
    （symbol:'circle' + itemStyle.opacity）必须存活于 IFCharts._applyRules 之后。

    _applyRules 将顶层 `color` 强制为固定 4 色调色板，但该调色板仅对「无显式
    颜色」的系列做自动分色；per-series 显式 lineStyle.color/itemStyle.color 优先
    （ECharts 语义），故模型趋势线不会被固定调色板覆盖或循环。同理 _applyRules
    只填 series 级 null（symbol='none' 等）、不触碰 data 数组，per-data-item 显式
    symbol（低置信半透明圆点，fix round 1）原样存活。行为级验证：node + echarts
    stub 跑真实 charts.js，断言 setOption 收到的各系列颜色与低置信点样式原样保留。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "charts.js").read_text(encoding="utf-8")
    script = (
        r"""
        var window = {};
        var _lastOpt = null;
        window.echarts = {
          init: function () {
            return {
              setOption: function (opt) { _lastOpt = opt; },
              dispose: function () {},
              resize: function () {},
            };
          },
        };
        var document = {
          getElementById: function () { return { id: 'c1' }; },
          documentElement: { getAttribute: function () { return 'dark'; } },
        };
        window.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
        window.addEventListener = function () {};
        var MutationObserver = undefined;
        """
        + "\n"
        + js
        + "\n"
        + r"""
        var c = window.IFCharts.create('c1');
        // 模型趋势卡形态：3 模型 × (P50 实线 + P95 虚线)，逐模型显式颜色（v7.0 数据槽前 3 色 蓝/青/绿）；
        // m1 首点 = 低置信点（0<n<30）：_lowPt 产物 { value, symbol:'circle', symbolSize:6, itemStyle:{color,opacity:.45} }
        window.IFCharts.update(c, {
          xAxis: { type: 'category', data: ['00:00', '01:00'], boundaryGap: false },
          yAxis: { type: 'value' },
          series: [
            { name: 'm1', type: 'line', data: [{ value: 1, symbol: 'circle', symbolSize: 6, itemStyle: { color: '#2563eb', opacity: 0.45 } }, 2], lineStyle: { color: '#2563eb', width: 2 }, itemStyle: { color: '#2563eb' } },
            { name: 'm2', type: 'line', data: [3, 4], lineStyle: { color: '#0891b2', width: 2 }, itemStyle: { color: '#0891b2' } },
            { name: 'm3', type: 'line', data: [5, 6], lineStyle: { color: '#15803d', width: 2 }, itemStyle: { color: '#15803d' } },
            { name: 'm1 P95', type: 'line', data: [10, 12], lineStyle: { color: '#2563eb', width: 1, type: 'dashed' }, itemStyle: { color: '#2563eb' } },
          ],
        });
        var expected = ['#2563eb', '#0891b2', '#15803d', '#2563eb'];
        var ok = _lastOpt.series.length === 4;
        for (var i = 0; ok && i < _lastOpt.series.length; i++) {
          var s = _lastOpt.series[i];
          if (!s.lineStyle || s.lineStyle.color !== expected[i]) { ok = false; break; }
          if (!s.itemStyle || s.itemStyle.color !== expected[i]) { ok = false; break; }
          if (i === 3 && s.lineStyle.type !== 'dashed') { ok = false; }
        }
        // 低置信 data item：_applyRules 只填 series 级 null（symbol='none'），
        // 不触碰 data 数组 → 逐点显式 symbol 必须原样存活（否则低置信点不可见）
        var low = _lastOpt.series[0] && _lastOpt.series[0].data && _lastOpt.series[0].data[0];
        if (!low || low.symbol !== 'circle' || low.symbolSize !== 6 ||
            !low.itemStyle || low.itemStyle.color !== '#2563eb' || low.itemStyle.opacity !== 0.45) {
          ok = false;
        }
        console.log(ok ? 'PASS' : 'FAIL: ' + JSON.stringify({
          series: _lastOpt.series.map(function (s) {
            return { line: s.lineStyle && s.lineStyle.color, item: s.itemStyle && s.itemStyle.color };
          }),
          low: low
        }));
        process.exit(ok ? 0 : 1);
        """
    )
    proc = subprocess.run([node, "-e", script],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, (
        "per-model series colors clobbered by _applyRules/merge:\n"
        "stdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)
    )


def test_monitor_latency_replace_series_no_ghosts():
    """IFCharts.update(..., { replaceSeries: true }) 必须整替 series（fix round 1）。

    回归：延迟趋势卡 series 数随 chip 选择 / P95 开关 / 窗口收缩动态变化（4→2）。
    旧行为：IFCharts 累积 userOption（_deepMerge 按索引保留旧 series 尾部）+ ECharts
    setOption(notMerge:false) 按索引合并 → 收缩后残留幽灵 series（旧窗口数据，错位
    贴到新 x 轴）。修复：包装层 userOption.series 整数组替换 + setOption 经
    replaceMerge:['series']（ECharts ≥5.4，vendor bundle 已含）让 ECharts 侧同样整替。

    行为级验证（node + echarts stub 跑真实 charts.js，捕获 setOption 收到的
    option 与第二参数）：
      1. 4 series（replaceSeries）→ 再 2 series（replaceSeries）：setOption 收到
         恰好 2 series（无幽灵尾部），且 sopt.replaceMerge === ['series']。
      2. 不传 opts 的既有调用方保持字节级兼容：累积合并（3→1 series 后 opt 仍 3，
         sopt 无 replaceMerge）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "charts.js").read_text(encoding="utf-8")
    script = (
        r"""
        var window = {};
        var _log = [];
        window.echarts = {
          init: function () {
            return {
              setOption: function (opt, sopt) { _log.push({ opt: opt, sopt: sopt || null }); },
              dispose: function () {},
              resize: function () {},
            };
          },
        };
        var document = {
          getElementById: function (id) { return { id: id }; },
          documentElement: { getAttribute: function () { return 'dark'; } },
        };
        window.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
        window.addEventListener = function () {};
        var MutationObserver = undefined;
        """
        + "\n"
        + js
        + "\n"
        + r"""
        function mk(name, data, color) {
          return { name: name, type: 'line', data: data, connectNulls: false,
                   lineStyle: { color: color, width: 2 }, itemStyle: { color: color } };
        }
        var ok = true;
        // 1) replaceSeries：4 series → 2 series，setOption 必须收到恰好 2 条（无幽灵尾部）
        var c = window.IFCharts.create('c1');
        window.IFCharts.update(c, {
          xAxis: { type: 'category', data: ['00:00', '01:00'], boundaryGap: false },
          series: [mk('m1', [1, 2], '#2563eb'), mk('m2', [3, 4], '#0891b2'),
                   mk('m3', [5, 6], '#15803d'), mk('m4', [7, 8], '#db2777')],
        }, { replaceSeries: true });
        var last = _log[_log.length - 1];
        if (last.opt.series.length !== 4) ok = false;
        window.IFCharts.update(c, {
          xAxis: { type: 'category', data: ['09:00', '10:00'], boundaryGap: false },
          series: [mk('m1', [10, 11], '#2563eb'), mk('m2', [12, 13], '#0891b2')],
        }, { replaceSeries: true });
        last = _log[_log.length - 1];
        var names = last.opt.series.map(function (s) { return s.name; });
        if (last.opt.series.length !== 2 || names.join(',') !== 'm1,m2') ok = false;
        if (JSON.stringify(last.sopt && last.sopt.replaceMerge) !== JSON.stringify(['series'])) ok = false;
        // 2) 不传 opts 的既有调用方：累积合并语义字节级不变（尾部保留、无 replaceMerge）
        var d = window.IFCharts.create('c2');
        window.IFCharts.update(d, { series: [mk('a', [1], '#2563eb'), mk('b', [2], '#0891b2'), mk('c', [3], '#15803d')] });
        window.IFCharts.update(d, { series: [mk('a', [4], '#2563eb')] });
        var dlast = _log[_log.length - 1];
        if (dlast.opt.series.length !== 3) ok = false;
        if (dlast.sopt && 'replaceMerge' in dlast.sopt) ok = false;
        console.log(ok ? 'PASS' : 'FAIL: ' + JSON.stringify(_log.map(function (l) {
          return { n: l.opt.series.length, names: l.opt.series.map(function (s) { return s.name; }), sopt: l.sopt };
        })));
        process.exit(ok ? 0 : 1);
        """
    )
    proc = subprocess.run([node, "-e", script],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, (
        "replaceSeries whole-array replacement FAILED (ghost series or default path changed):\n"
        "stdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)
    )


def test_monitor_js_node_syntax():
    """monitor.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check monitor.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_monitor_chart_series_limit():
    """每张图表系列数 ≤4（spec §7 / Task 4 review 约束：
    IFCharts 不折叠第 5 系列，会循环回蓝色，故调用方必须自行限制 ≤4）。

    断言 monitor.js 中 series 数组字面量每处 ≤4 个元素。这里用保守启发：
    检查每处 'series:' 后的数组，确认没有超过 4 个 { type: 行。"""
    import re
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")
    # 找所有 series: [ ... ] 块，统计每块内的 type: 出现次数
    for m in re.finditer(r'series:\s*\[', js):
        # 取到匹配的 ] 为止（简单扫描）
        start = m.end()
        depth = 1
        i = start
        while i < len(js) and depth > 0:
            if js[i] == '[':
                depth += 1
            elif js[i] == ']':
                depth -= 1
            i += 1
        block = js[start:i - 1]
        type_count = len(re.findall(r'type:\s*[\'"]', block))
        assert type_count <= 4, (
            "monitor.js series block has %d series (>4): %s" % (type_count, block[:120])
        )


def test_monitor_power_card_contract():
    """功耗/电费卡契约（客户端 GPU ring buffer 已删除——采样移到服务端
    PowerSampler，60s 落 v007 表，独立于前端是否打开页面）。

    用户约束「不要刷新太快」双层落地：后端 5min TTL 缓存 + 前端 5min TTL
    + 已渲染档位守卫——3s 轮询的 snapshot 不得触发图表重绘。

    断言（源文本级，逻辑为订阅驱动难以隔离单测）：
    1. 客户端采样环彻底移除：无 pushGpuSample / renderGpuChart / _gpuBuf。
    2. 数据源只读 GET /api/power?gran=（右侧驱动为服务端分桶 display filter）。
    3. 前端 5min TTL（_POWER_TTL = 300000）与后端 _power_series_cache 对齐。
    4. 双轴为显式受权例外：{dualAxis:true} + 右轴累计（position:'right'），
       量纲流速↔存量因果对（功耗 W 与累计电量 度 是积分关系）。"""
    import re
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "monitor.js").read_text(encoding="utf-8")

    # 1. 客户端 ring buffer 采样彻底删除（采样职责移交服务端 PowerSampler）
    for dead in ('pushGpuSample', 'renderGpuChart', '_gpuBuf', '_gpuWin'):
        assert dead not in js, "monitor.js still contains removed GPU ring buffer ref: %s" % dead

    # 2. 数据源：GET /api/power（只读 fetch，无 POST）
    assert "/api/power?gran=" in js, "monitor.js must fetch /api/power?gran= for power series"
    # 2b. 档位语义：小时/天/周（月整体弃用）；_pgranWinText 周 → 近 90 天
    assert re.search(r"_pgranWinText\([^)]*\)\s*\{\s*if \(gran === 'day'\) return '近 30 天';", js), (
        "monitor.js power window text must map day → 近 30 天"
    )
    assert "近 90 天" in js, "monitor.js must show 周 as 近 90 天 (_pgranWinText)"
    if 'method:"POST"' in js or 'method: "POST"' in js:
        raise AssertionError("monitor.js power fetch must be GET-only (read-only monitor tab)")

    # 3. 前端 5min TTL + 已渲染档位守卫（snapshot 3s 轮询不得触发重绘）
    assert re.search(r'_POWER_TTL\s*=\s*300000', js), (
        "monitor.js frontend power cache must be 5min TTL (300000ms) to honor "
        "「不要刷新太快」"
    )
    assert re.search(r'_pgranRendered\s*===?\s*_pgran', js), (
        "monitor.js must guard re-render with a per-gran rendered marker so 3s "
        "snapshot polls don't redraw the power chart"
    )

    # 4. 双轴显式受权：dualAxis:true（charts.js 唯一放行点）+ 右轴累计电量。
    #    空态分支（!available || !buckets.length）也必须给出双轴骨架 + 授权——
    #    缺失时首渲染 yAxisIndex:1 引用不存在轴，echarts cartesian2d 抛异常
    #    （线上首例回归：tab_active 时报 Cannot read properties of undefined 'get'）。
    #    故断言 power 卡两个 update 分支均出现 'dualAxis: true'。
    assert js.count("dualAxis: true") >= 2, (
        "every _charts.power update branch (real + empty-state) must sanction "
        "dual-axis via {dualAxis:true}; a yAxisIndex:1 series without both axes "
        "crashes echarts on first render"
    )
    assert "position: 'right'" in js and "'度'" in js, (
        "cumulative kWh must render on the right axis (position:'right')"
    )
    assert js.count('{ dualAxis: true }') >= 2, (
        "both power update calls must pass { dualAxis: true } opts (regression: "
        "empty-state branch omitted it, crashing on first render)"
    )

    # 5. 累计线逐桶打点（方案 B，用户拍板）：全部桶对象式点位带 symbol:'circle'
    #    ——house 规则 series 级 symbol:'none' 下普通数值点位不可见，必须对象式
    #    声明才出点；前导空桶 cum=0 也打点，线从窗口起点连续到末端总量。
    #    标签小数位自适应（_pgranKwhFmt 去尾零）；line z:3 > bar z:2，
    #    曲线在柱之上不被遮盖。
    assert "symbol: 'circle'" in js and 'value: b.cum_kwh' in js, (
        "power cumulative line must map EVERY bucket to {value:b.cum_kwh, symbol:'circle'} "
        "(all-dots 方案 B); a bare number under series symbol:'none' renders no dot"
    )
    assert 'symbolSize: 5' in js, "power line dots must declare symbolSize"

    # 5b. 双轴单网格 + 整数坐标（用户拍板）：左 W 固定 0–600（卡 TDP 封顶），
    #     右 度 max=_niceCeil(累计量,6) nice 上取整到 6 等分干净步长，
    #     两轴同 splitNumber:6 + min:0 → 网格位置重合，右轴 splitLine 隐藏只印标签。
    #     原 splitNumber:1 一刀切设计已废弃（单刻度过秃），此处防的是退化回去。
    assert 'splitNumber: 6' in js and 'min: 0' in js, (
        "both power y-axes must share splitNumber:6 + min:0 so the 6-division "
        "pixel positions coincide — right axis reuses left gridline positions"
    )
    assert 'max: 600' in js and "'dataMax'" not in js, (
        "left W axis must be FIXED 0–600 (card TDP ceiling, never exceeded) — "
        "dynamic dataMax top would give non-integer/non-adapted tick values"
    )
    assert 'splitLine: { show: false }' in js, (
        "right kWh axis must hide its own split lines — ONE scale-line set "
        "(left W axis only); two independent grids read as messy double axes"
    )
    assert re.search(r'_niceCeil\s*\(', js), (
        "right kWh max must be nice-ceiled via _niceCeil (max = 6×{1,2,5}×10^k)"
        "so 6 equal divisions land on clean values: integers when total large,"
        "0.1/0.2/0.5 steps when total < 1 度"
    )
    assert 'splitNumber: 1' not in js, (
        "the obsolete splitNumber:1 single-tick design must stay gone"
    )
    assert '_pgranKwhFmt' in js, (
        "right axis label formatter must adapt decimals (>=1 度 → 1 位；<1 度 → 2 位)"
        "and strip trailing zeros (0.20 → 0.2) so small totals (0.02 度)"
        "don't collapse to '0.0 度'"
    )
    assert 'z: 3' in js and 'z: 2' in js, (
        "cumulative line must render ABOVE the bars (line z:3 > bar z:2) so the "
        "step curve is never occluded by bar tops"
    )
    assert "step: 'end'" not in js, (
        "cumulative line must connect dots STRAIGHT (点与点直连，用户拍板) — "
        "step:'end' renders 90° staircase at each bucket boundary, reading like "
        "a bar chart and violating cumulative-curve intuition"
    )


# ── Task 6: 推理 TAB（模型卡片 + 网关控制卡） ──────────────────────


def test_inference_structure():
    """推理页 DOM 契约：网关控制卡 + 缓存控件 + 速率限制 + 三组模型容器。

    断言 get_html() 含：
      - #gwCard（网关控制卡）
      - #cacheToggle（LRU 缓存开关按钮）
      - #cacheHits（缓存信息标签）
      - #rlMeta（速率限制元信息）
      - 三组模型容器（独占/共享/空闲）
    """
    html = _html()
    for el in (
        'id="gwCard"',
        'id="cacheToggle"',
        'id="cacheHits"',
        'id="rlMeta"',
        'id="infExclGroup"',
        'id="infShrdGroup"',
        'id="infFreeGroup"',
    ):
        assert el in html, "missing inference contract id: %s" % el
    # 部署入口按钮（data-action 契约）
    assert 'data-action="goto-deploy"' in html, "missing deploy entry button"
    # 缓存开关 data-action 契约
    assert 'data-action="cache-toggle"' in html, "missing cache toggle data-action"


def test_inference_js_present():
    """inference.js 模块存在并被 get_html() 内联装配（tabRenderers 注册 + doModelAction）。

    断言：文件存在；get_html() 含 tabRenderers['tab-inference'] 注册；
    doModelAction 全局函数；四个操作端点契约；store sync_meta 订阅。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"
    assert js_path.exists(), "inference.js missing"
    html = _html()
    assert "window.tabRenderers['tab-inference'] = renderInference" in html, (
        "inference.js tab renderer registration not inlined into HTML"
    )
    assert "window.doModelAction" in html, (
        "window.doModelAction not inlined into HTML"
    )
    # 四个模型操作端点契约
    for ep in ("'/switch'", "'/stop'", "'/sleep'", "'/wake'"):
        assert ep in html, "inference.js missing endpoint: %s" % ep
    # 缓存开关端点契约
    assert "'/admin/cache/toggle'" in html, "inference.js missing cache toggle endpoint"
    # sync_meta 订阅（snapshot 到达时刷新）
    assert "store.on('sync_meta'" in html or "window.store.on('sync_meta'" in html
    # admin header 调用（UI.adminHeaders，R6）
    assert "UI.adminHeaders()" in html


def test_inference_no_inline_onclick():
    """inference.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "inference.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "inference.html contains inline onclick= — must use event delegation"
    )


def test_inference_js_node_syntax():
    """inference.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "inference.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check inference.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_inference_cache_hits_from_snapshot_stats():
    """#cacheHits 命中数必须来自 snapshot cache_stats（方案 A，R10 契约更新）。

    R10 旧契约（后端不暴露命中数 → 前端不显示）已作废：后端现在真实提供
    ResponseCache.stats()（经 snapshot local_models.cache_stats），且缓存
    命中不再落 RequestLog —— 命中计数是 LRU 生效的唯一可见通道，必须显示。

    仍锁死的纪律：
      * 不得出现 hit_rate（命中率 = hits/requests 无意义分母，禁止臆造）
      * 不得 fetch /api/metrics（数据走 snapshot 3s 轮询，不新增端点）
      * 缓存关闭 / 字段缺失时回退「状态 + 上限 500 条」信息性标签"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    # 命中计数走 snapshot 真字段（cache_stats），不得臆造 hit_rate
    assert "cache_stats" in js, "cacheHits must read hits from snapshot cache_stats (plan A)"
    assert "hit_rate" not in js, "inference.js fabricates hit_rate (forbidden)"
    # 不得从 /api/metrics 读取 cache 数据（API 冻结：走 snapshot 轮询）
    import re
    metrics_fetch = re.findall(r"fetch\([^)]*api/metrics", js)
    assert not metrics_fetch, (
        "inference.js fetches /api/metrics for cache data (API frozen: use snapshot)"
    )
    # 回退标签：缓存关闭 / cache_stats 缺失时仍显示「上限 500 条」
    assert "上限" in js, "cacheHits fallback label missing maxsize info"
    assert "500" in js, "cacheHits fallback label missing maxsize value 500"
    # 命中行文本（开 + stats 时的插值契约）
    assert "命中" in js, "cacheHits label missing hits text"
    assert "在用" in js, "cacheHits label missing size/max text"


def test_inference_no_new_api():
    """推理 TAB 不得新增任何 API 端点（R10/R11：API 冻结）。

    inference.js 的 fetch 调用必须仅指向既有端点：
    /switch /stop /sleep /wake /admin/cache/toggle，
    以及 R-AS 有意新增的 /admin/auto-switch/toggle（自动切换开关，
    用户明确要求置于推理 tab，非冻结违规），
    以及场景控件（任务：iff tune Dashboard 落地，用户批准）有意新增的
    /admin/tune（POST 应用场景并重启；同族 GET /admin/tune/scenarios 与
    /admin/tune/preview 经 adminGet 拼接 query 调用，非字面 fetch，不受本断言约束）。
    不得出现 /api/cache-hits /api/rate-limit 等未授权新端点。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    import re
    # 提取所有 fetch 调用的 URL
    urls = re.findall(r"fetch\(\s*['\"]([^'\"]+)['\"]", js)
    allowed = {'/switch', '/stop', '/sleep', '/wake', '/admin/cache/toggle',
               '/admin/auto-switch/toggle', '/admin/tune'}
    for u in urls:
        assert u in allowed, (
            "inference.js fetches non-allowed endpoint %r (API must stay frozen)" % u
        )


# ── Task 7: 部署 TAB（部署表单 + 拉取表单 + 长任务进度态） ──────────


def test_deploy_structure():
    """部署页 DOM 契约：部署表单卡 + 拉取表单卡 + 长任务进度态。

    断言 get_html() 含（R12：仅 name + engine 类型，无 dead fields）：
      - 部署表单：#depName（name input）、#depType（engine type select）、
        data-action="deploy"（部署按钮）
      - 拉取表单：#pullName（name input）、#pullFw（framework select）、
        data-action="pull"（拉取按钮）
      - #deployProgress（长任务进度态容器）
    """
    html = _html()
    for el in (
        'id="depName"',          # 部署表单：模型名称输入
        'id="depType"',          # 部署表单：引擎类型选择
        'data-action="deploy"',  # 部署表单：部署按钮
        'id="pullName"',         # 拉取表单：模型名称输入
        'id="pullFw"',           # 拉取表单：框架选择
        'data-action="pull"',    # 拉取表单：拉取按钮
        'id="deployProgress"',   # 长任务进度态容器
    ):
        assert el in html, "missing deploy contract id: %s" % el
    # 引擎类型选项契约（R14：仅 auto_deploy 支持的类型 vllm + ollama_cpp；
    # sglang/ninfer/ollama 服务不被 auto_deploy 支持，提供即误导——会返回 200+error）
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    for opt in ('value="vllm"', 'value="ollama_cpp"'):
        assert opt in frag, "missing auto-deployable engine option: %s" % opt
    for bad in ('value="sglang"', 'value="ninfer"'):
        assert bad not in frag, (
            "deploy.html offers %r — auto_deploy rejects it (R14: only vllm/ollama_cpp)" % bad
        )


def test_deploy_js_present():
    """deploy.js 模块存在并被 get_html() 内联装配（tabRenderers 注册 + doDeploy/doPull）。

    断言：文件存在；get_html() 含 tabRenderers['tab-deploy'] 注册；
    doDeploy / doPull 全局函数；两个端点契约；admin header 调用。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js"
    assert js_path.exists(), "deploy.js missing"
    html = _html()
    assert "window.tabRenderers['tab-deploy'] = renderDeploy" in html, (
        "deploy.js tab renderer registration not inlined into HTML"
    )
    assert "window.doDeploy" in html, "window.doDeploy not inlined into HTML"
    assert "window.doPull" in html, "window.doPull not inlined into HTML"
    # 两个端点契约
    assert "'/deploy'" in html, "deploy.js missing /deploy endpoint"
    assert "'/pull'" in html, "deploy.js missing /pull endpoint"
    # admin header 调用（R6）
    assert "UI.adminHeaders()" in html
    # 请求体契约：{name, type} 与 {name, framework}（R12）
    assert "name: name, type: type" in html or "name:name,type:type" in html, (
        "deploy.js must POST {name, type} (R12)"
    )
    assert "name: name, framework: fw" in html or "name:name,framework:fw" in html, (
        "deploy.js must POST {name, framework} (R12)"
    )
    # UI.confirm 在部署与拉取前调用（破坏性/长操作确认）
    js = js_path.read_text(encoding="utf-8")
    assert js.count("UI.confirm(") >= 2, (
        "deploy.js must call UI.confirm before both deploy and pull"
    )
    # 成功后跳转推理 TAB（spec §4.4）
    assert "switchTab('tab-inference')" in js, (
        "deploy.js must switch to inference tab on success"
    )


def test_deploy_no_dead_fields():
    """部署表单不得含旧 dead fields（R12）：model_dir / port / gpu_mem / slider。
    后端 auto_deploy 自动生成 YAML，不读这些字段（handler.py:1055 _handle_deploy
    仅取 name + type）。旧字段误导用户以为可配置部署参数。"""
    import re
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    # dead field 标识符不得出现（含 id= / name= 形式或裸标识符）
    for dead in ("model_dir", "gpu_mem", "slider"):
        assert dead not in frag, (
            "deploy.html contains dead field %r (R12: removed — backend auto-generates)" % dead
        )
    # port 不得作为表单字段标识出现（id="...port" / name="port"）
    assert not re.search(r'(id|name)\s*=\s*"[^"]*port[^"]*"', frag, re.I), (
        "deploy.html contains a 'port' form field (R12: dead field removed)"
    )


def test_deploy_no_inline_onclick():
    """deploy.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "deploy.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "deploy.html contains inline onclick= — must use event delegation"
    )


def test_deploy_js_node_syntax():
    """deploy.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check deploy.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


# ── Task 8: 云端 TAB（预设网格 + provider 表 + 手动配置，零 emoji） ──

# 常见 emoji 区段（U+1F300-1FAFF 符号 + U+2600-27BF 杂项/制表符号，
# 覆盖 legacy 的 ✓ ✗ 🔍 👁 🔧 与 preset icon 的 🟦🌋🐋🌙🟢🟠🔗）
_CLOUD_EMOJI_RE = __import__("re").compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_CLOUD_LEGACY_EMOJI = [
    "\U0001F50D", "\U0001F441", "\U0001F527",   # 🔍 👁 🔧
    "✓", "✗",                                       # 状态标记
    "\U0001F7E6", "\U0001F30B", "\U0001F40B",     # 🟦 🌋 🐋
    "\U0001F319", "\U0001F7E2", "\U0001F410",     # 🌙 🟢 🔗
]


def _cloud_sources():
    frag = (_DASHBOARD_DIR / "fragments" / "cloud.html").read_text(encoding="utf-8")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js").read_text(encoding="utf-8")
    return frag, js


def test_cloud_structure():
    """云端页 DOM 契约（spec §4.5）：预设网格 + 内联 Key 表单 + provider 表
    + 手动配置表单 + 发现模型区。

    断言 get_html() 含契约 id（presetGrid, presetForm, provTable, cpName,
    cpApiKey, cpOpenaiBase, cpAnthropicBase, cloudModels）与静态 data-action 值
    （行级 provider-test/provider-delete/preset-select 由 cloud.js 动态渲染）。"""
    html = _html()
    for el in (
        'id="presetGrid"',       # 预设网格容器
        'id="presetForm"',       # 内联 API Key 表单
        'id="provTable"',        # provider 表
        'id="cpName"',
        'id="cpApiKey"',
        'id="cpOpenaiBase"',
        'id="cpAnthropicBase"',
        'id="cloudModels"',      # 发现模型区
    ):
        assert el in html, "missing cloud contract id: %s" % el
    # 静态 data-action 契约
    for act in ("preset-add", "preset-cancel", "manual-test", "manual-add",
                "reload", "provider-discover"):
        assert 'data-action="%s"' % act in html, "missing cloud data-action: %s" % act


def test_cloud_js_present():
    """cloud.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + 5 端点
    + 6 个暴露函数 + UI.adminHeaders）。"""
    import re
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js"
    assert js_path.exists(), "cloud.js missing"
    html = _html()
    assert "window.tabRenderers['tab-cloud'] = renderCloud" in html, (
        "cloud.js tab renderer registration not inlined into HTML"
    )
    # 五个端点契约（API 冻结，不得新增）
    for ep in ("'/admin/cloud/presets'", "'/admin/cloud/providers'",
               "'/admin/cloud/test'", "'/admin/cloud/discover'", "'/admin/cloud/reload'"):
        assert ep in html, "cloud.js missing endpoint: %s" % ep
    assert "UI.adminHeaders()" in html, "cloud.js must use UI.adminHeaders()"
    # 暴露的 6 个操作函数
    for fn in ("window.doCloudAdd", "window.doCloudAddPreset", "window.doCloudTest",
               "window.doCloudDiscover", "window.doCloudDelete", "window.doCloudReload"):
        assert fn in html, "cloud.js missing global: %s" % fn
    # 动态渲染的 data-action 契约（预设卡 / 行内 测试 / 行内 删除）
    js = js_path.read_text(encoding="utf-8")
    for act in ("preset-select", "provider-test", "provider-delete"):
        assert 'data-action="%s"' % act in js or "'%s'" % act in js, (
            "cloud.js missing data-action: %s" % act
        )
    # 请求体契约：preset 路径 {preset, api_key}；手动路径 {name, api_key, openai_base, anthropic_base}
    assert "preset: preset.id, api_key: apiKey" in js or "preset: preset.id" in js, (
        "cloud.js must POST {preset, api_key} for preset path"
    )
    assert re.search(r"openai_base:\s*openaiBase,\s*anthropic_base:\s*anthropicBase", js), (
        "cloud.js must POST {name, api_key, openai_base, anthropic_base} for manual path"
    )


def test_cloud_no_emoji():
    """cloud.html + cloud.js 零 emoji（R13）：无常见 emoji 区段码位、
    无 legacy emoji（🔍👁🔧✓✗🟦🌋🐋🌙🟢🟠🔗）；
    且预设卡不得渲染 preset 的 icon 字段（p.icon 不进入 innerHTML）。"""
    import re
    frag, js = _cloud_sources()
    for src, name in ((frag, "cloud.html"), (js, "cloud.js")):
        m = _CLOUD_EMOJI_RE.search(src)
        assert not m, "%s contains emoji codepoint U+%04X" % (name, ord(m.group(0)))
        for ch in _CLOUD_LEGACY_EMOJI:
            assert ch not in src, "%s contains legacy emoji %r" % (name, ch)
    # preset icon 字段（emoji）不得被渲染：p.icon / pr.icon 不得出现
    assert "p.icon" not in js, "cloud.js renders preset icon field (p.icon)"
    assert "pr.icon" not in js, "cloud.js renders preset icon field (pr.icon)"
    # 除 UI.icon( SVG 助手外，不得出现 .icon 字段访问
    assert not re.search(r"\.icon\b", js.replace("UI.icon", "")), (
        "cloud.js renders .icon field into HTML (R13: preset icon is emoji)"
    )


def test_cloud_delete_uses_confirm():
    """cloudDeleteProvider 必须走 UI.confirm danger 确认，不得使用原生 confirm()。"""
    js = _cloud_sources()[1]
    assert "UI.confirm(" in js, "cloud.js must use UI.confirm for provider deletion"
    assert "danger: true" in js, "cloud.js deletion confirm must be danger"
    # 移除 UI.confirm( 后不得残留任何 confirm( 调用（原生 confirm 违规）
    stripped = js.replace("UI.confirm(", "")
    assert "confirm(" not in stripped, (
        "cloud.js uses native confirm() — must use UI.confirm (R13)"
    )


def test_cloud_no_inline_onclick():
    """cloud.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = _cloud_sources()[0]
    assert "onclick=" not in frag, (
        "cloud.html contains inline onclick= — must use event delegation"
    )


def test_cloud_js_node_syntax():
    """cloud.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "cloud.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check cloud.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_deploy_success_guard():
    """doDeploy 不得仅凭 HTTP 200 判定成功——后端 _handle_deploy 对逻辑失败也返回
    200（_send_json 默认 200）。必须查 j.status：成功仅 {switched, already_active}
    （model_lifecycle.py:333 / manager.py:399）；其余（"Unsupported type for
    auto-deploy" / "Unknown model" / "Invalid transition"）须走失败分支。

    回归守卫：若有人改回 `if (res.ok)` 即成功，下面 'switched'/'already_active'
    字面量与 j.status 检查会消失，测试失败。
    """
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "deploy.js").read_text(encoding="utf-8")
    # 必须检查响应体 status（不能只看 res.ok）
    assert "j.status" in js, "doDeploy must inspect j.status (backend returns 200 on logical errors)"
    # 成功状态白名单（与 model_lifecycle.py:333 一致）
    assert "'switched'" in js, "doDeploy success set must include 'switched'"
    assert "'already_active'" in js, "doDeploy success set must include 'already_active'"
    # 非白名单状态须抛错走失败分支（不能静默成功）
    assert "throw new Error" in js, "doDeploy must throw on non-success status"


# ── Task 9: 异常 TAB（结构化事件表 + 类别/严重度过滤 + 全文搜索 + critical 高亮） ──

# 异常页 emoji 区段（与 cloud 同口径：U+1F300-1FAFF 符号 + U+2600-27BF 杂项/制表符号）
_ANOM_EMOJI_RE = __import__("re").compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_ANOM_LEGACY_EMOJI = [
    "\U0001F50D", "\U0001F441", "\U0001F527",   # 🔍 👁 🔧
    "✓", "✗",                                       # 状态标记
    "\U0001F7E2", "\U0001F7E1", "\U0001F534",     # 🟢 🟡 ⚫（旧 anomaly 严重度 emoji）
]


def _anomaly_sources():
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js").read_text(encoding="utf-8")
    return frag, js


def test_anomaly_structure():
    """异常页 DOM 契约（spec §4.6）：事件表 + 过滤栏 + 计数。

    断言 get_html() 含契约 id（anomTable/anomTbody/anomCatFilter/anomSevFilter/
    anomSearch/anomCount）与过滤选项值（category 5 值 + severity 4 值）。"""
    html = _html()
    for el in (
        'id="anomTable"',       # 事件表
        'id="anomTbody"',       # 事件表 tbody
        'id="anomCatFilter"',   # 类别过滤
        'id="anomSevFilter"',   # 严重度过滤
        'id="anomSearch"',      # 全文搜索
        'id="anomCount"',       # 总计数
    ):
        assert el in html, "missing anomaly contract id: %s" % el
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    for opt in ('value="routing"', 'value="model"', 'value="auth"',
                'value="config"', 'value="cloud"'):
        assert opt in frag, "missing category option: %s" % opt
    for opt in ('value="info"', 'value="warning"', 'value="error"', 'value="critical"'):
        assert opt in frag, "missing severity option: %s" % opt


def test_anomaly_js_present():
    """anomaly.js 模块存在且被 get_html() 内联装配（tabRenderers 注册 + 端点 + fetch）。

    断言：文件存在；get_html() 含 tabRenderers['tab-anomaly'] 注册；
    /api/anomalies 端点契约；fetch 调用存在（/api/anomalies 为公开只读端点，无需 admin header）。"""
    js_path = ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js"
    assert js_path.exists(), "anomaly.js missing"
    html = _html()
    assert "window.tabRenderers['tab-anomaly'] = renderAnomaly" in html, (
        "anomaly.js tab renderer registration not inlined into HTML"
    )
    js = js_path.read_text(encoding="utf-8")
    assert "'/api/anomalies'" in js, "anomaly.js missing /api/anomalies endpoint"
    # /api/anomalies 为公开只读 GET：允许 UI.adminHeaders() 或裸 fetch，二者其一即可
    assert ("UI.adminHeaders()" in js) or ("fetch(" in js), (
        "anomaly.js must fetch /api/anomalies (UI.adminHeaders or fetch)"
    )
    # 暴露的操作函数
    for fn in ("window.renderAnomaly", "window.doAnomRefresh"):
        assert fn in html, "anomaly.js missing global: %s" % fn


def test_anomaly_no_emoji():
    """anomaly.html + anomaly.js 零 emoji：无常见 emoji 区段码位、无 legacy 严重度
    emoji（🟢🟡⚫ 等）——严重度一律 SVG 图标 + 文字标签双编码。"""
    frag, js = _anomaly_sources()
    for src, name in ((frag, "anomaly.html"), (js, "anomaly.js")):
        m = _ANOM_EMOJI_RE.search(src)
        assert not m, "%s contains emoji codepoint U+%04X" % (name, ord(m.group(0)))
        for ch in _ANOM_LEGACY_EMOJI:
            assert ch not in src, "%s contains legacy emoji %r" % (name, ch)


def test_anomaly_no_inline_onclick():
    """anomaly.html 不得含任何 inline onclick= 属性（事件委托约束）。"""
    frag = (_DASHBOARD_DIR / "fragments" / "anomaly.html").read_text(encoding="utf-8")
    assert "onclick=" not in frag, (
        "anomaly.html contains inline onclick= — must use event delegation"
    )


def test_anomaly_js_node_syntax():
    """anomaly.js 必须通过 node --check 语法校验（零构建工具链，CI 前置门）。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js"
    proc = subprocess.run([node, "--check", str(js)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, \
        "node --check anomaly.js failed:\nstdout:%s\nstderr:%s" % (proc.stdout, proc.stderr)


def test_anomaly_critical_highlight():
    """critical 行须整行高亮：anomaly.js 对 severity==='critical' 追加 row-crit 类
    （spec §4.6；row-crit 为低 alpha --crit 背景 tint，文字色不变）。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "anomaly.js").read_text(encoding="utf-8")
    assert "row-crit" in js, "anomaly.js must add row-crit class to critical rows"
    assert "'critical'" in js, "anomaly.js must branch on severity === 'critical'"


# ── Task 10 收口断言集（巨石删除 / 骨架标准化 / 断连 banner / 全 TAB 装配） ──

def test_app_js_deleted():
    """legacy app.js 巨石（1432 行死代码）已删除；JS 模块化为 9 个模块（无 app）。"""
    assert not (ROOT / "inferfabric" / "dashboard" / "js" / "app.js").exists(), (
        "legacy app.js monolith must be deleted (Task 10)"
    )
    import inferfabric.dashboard as dash
    assert "app" not in getattr(dash, "JS_MODULES", ["app"]), (
        "app must not be in JS_MODULES"
    )


def test_no_app_js_reference():
    """served HTML（get_html() 内联全部 JS）不得含 app.js 字面引用——
    含 store.js/ui.js 内的历史注释（已改写为“旧巨石”表述）。"""
    html = _html()
    assert "app.js" not in html, (
        "served HTML still references app.js (stale comment or script tag)"
    )


def test_all_tab_renderers_registered():
    """全部 6 个 TAB 的 tabRenderers 注册必须出现在 served HTML（Task 10 收口）。"""
    html = _html()
    for tab in ("tab-overview", "tab-inference", "tab-monitor",
                "tab-deploy", "tab-cloud", "tab-anomaly"):
        assert "window.tabRenderers['%s']" % tab in html, (
            "missing tabRenderers registration: %s" % tab
        )


def test_skeleton_in_data_driven_tabs():
    """spec §5 全 TAB 骨架态：数据驱动区域（cloud 预设网格 / provider 表、
    anomaly 事件表）用 UI.skeleton + .skeleton 占位，不得残留静态“加载中…”。
    deploy TAB 为静态表单（无数据依赖）——不要求骨架。"""
    js_dir = ROOT / "inferfabric" / "dashboard" / "js"
    cloud_js = (js_dir / "cloud.js").read_text(encoding="utf-8")
    anomaly_js = (js_dir / "anomaly.js").read_text(encoding="utf-8")
    assert "UI.skeleton" in cloud_js, "cloud.js must call UI.skeleton on initial load"
    assert "UI.skeleton" in anomaly_js, "anomaly.js must call UI.skeleton on load"
    frag_dir = ROOT / "inferfabric" / "dashboard" / "fragments"
    for name in ("cloud.html", "anomaly.html", "deploy.html"):
        frag = (frag_dir / name).read_text(encoding="utf-8")
        assert "加载中" not in frag, (
            "%s still uses static 加载中 placeholder (use skeleton)" % name
        )
    cloud_frag = (frag_dir / "cloud.html").read_text(encoding="utf-8")
    anomaly_frag = (frag_dir / "anomaly.html").read_text(encoding="utf-8")
    assert 'class="skeleton"' in cloud_frag, "cloud.html initial markup must be skeleton"
    assert 'class="skeleton"' in anomaly_frag, "anomaly.html initial markup must be skeleton"


def test_api_error_banner_last_sync_text():
    """spec §5 断连 banner：#apiErrorBanner 显示时须带“最后数据更新于 X”
    （X = sync_meta.ts，最近一次成功同步时间；从未同步过则 “—”）。"""
    html = _html()
    assert 'id="apiErrorBanner"' in html, "disconnect banner missing from shell"
    assert "最后数据更新于" in html, (
        "store.js api_error handler must show 最后数据更新于 X in banner"
    )
    frag = (_DASHBOARD_DIR / "base.html").read_text(encoding="utf-8")
    assert "apiErrorBanner" in frag
    assert "重试" in frag, "banner must offer a retry action"


def test_do_model_action_checks_res_ok_not_just_body():
    """回归：doModelAction 必须在 !res.ok 时走错误分支（throw + 操作失败 toast），
    不能仅凭 HTTP 200 判成功——否则 exclusive 模型 stop error 会被误报"已停止"。

    契约来源：后端 _handle_stop 对 status:error 返回 4xx（Task 0.1，handler.py）。
    前端 doModelAction 若只看 data.status 或忽略 res.ok，会回退到假成功。
    此测试锁住"非 OK 响应 → 不显示 op.ok 成功 toast"的源码契约。"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    # doModelAction 的 run() 必须检查 res.ok 并在非 OK 时 throw（不 fall-through 到 op.ok toast）
    assert "if (!res.ok)" in js, (
        "doModelAction must check res.ok — regression: silent 200 on stop error"
    )
    # 非 OK 分支必须解析 error/message 并 throw（驱动 catch 块的 操作失败 toast）
    assert "throw new Error" in js, (
        "doModelAction non-OK branch must throw to trigger 操作失败 toast"
    )
    # op.ok 成功 toast 必须在 res.ok 检查之后（非 OK 时不可达）
    ok_idx = js.find("UI.toast(op.ok")
    check_idx = js.find("if (!res.ok)")
    throw_idx = js.find("throw new Error", check_idx)
    assert ok_idx > check_idx and throw_idx > check_idx < ok_idx, (
        "op.ok toast must come after the !res.ok check + throw, so error path can't reach it"
    )


def test_all_dashboard_stop_paths_route_via_do_model_action():
    """回归：所有 dashboard stop 按钮必须经 doModelAction（它做 res.ok 检查），
    不得有路径直调 fetch('/stop') 绕过错误处理。

    契约来源：overview.js 总览页 stop 按钮引导至推理页（不直调 /stop）；
    inference.js stop 按钮经 doModelAction。若新增直调路径会绕过 Task 0.1 的 4xx 错误浮现。"""
    import re
    # inference.js：fetch('/stop') 必须出现在 doModelAction 的 run() 内（经 OPS.stop.url）
    inf_js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    # /stop 仅作为 OPS 配置的 url，非裸 fetch 调用
    bare_stop_fetch = re.findall(r"fetch\(\s*['\"]/stop['\"]", inf_js)
    assert not bare_stop_fetch, (
        "inference.js must not bare-fetch /stop — must go through doModelAction OPS"
    )
    # overview.js：stop 按钮不直调 /stop，引导至推理页
    ov_js = (ROOT / "inferfabric" / "dashboard" / "js" / "overview.js").read_text(encoding="utf-8")
    bare_stop_fetch_ov = re.findall(r"fetch\(\s*['\"]/stop['\"]", ov_js)
    assert not bare_stop_fetch_ov, (
        "overview.js must not bare-fetch /stop — routes stop via doModelAction on inference tab"
    )


def test_gateway_rate_limit_is_readonly_indicator():
    """R11b：速率限制行是只读状态指示器（非开关），与 LRU/自动切换两张真开关卡区分。

    契约（与用户确认的方案一致）：
      * 数据源 = snapshot local_models.rate_limit（DualGateLimiter.describe()），
        前端读该字段渲染徽章 + 实况副标题，不得写死静态文案当唯一内容
      * 徽章按 mode 变色：reject → ok「生效中」/ observe → warn「观察中」
      * 副标题展示实际生效的并发排队上限 + RPM 门状态（server_rpm 优先）
      * 刻意不做开关：不得新增 rate-limit 的 data-action 按钮 / admin toggle 端点
        （配置改动仍走 iff.yaml + 重启 proxy，无热加载）
      * 字段缺失（旧 proxy / 异常）→ 回退静态标签「配置见 iff.yaml」不白屏"""
    js = (ROOT / "inferfabric" / "dashboard" / "js" / "inference.js").read_text(encoding="utf-8")
    html = (ROOT / "inferfabric" / "dashboard" / "fragments" / "inference.html").read_text(encoding="utf-8")

    # 数据源：必须读 snapshot 的 rate_limit 字段（非写死）
    assert "rate_limit" in js, "renderGateway must read snapshot local_models.rate_limit"
    assert "rlMeta" in js and "rlBadge" in js, "rate-limit indicator ids (rlMeta/rlBadge) missing"

    # 徽章按 mode 变色：reject→ok / observe→warn，且文案区分
    assert "reject" in js, "badge must key on mode === 'reject'"
    assert "'ok'" in js and "'warn'" in js, "badge must use ok/warn color classes"
    assert "生效中" in js and "观察中" in js, "badge labels (生效中/观察中) missing"

    # 副标题：并发排队上限 + RPM 门状态
    assert "并发" in js and "限流" in js, "rlMeta must show concurrency + RPM status"

    # 字段缺失回退静态标签
    assert "配置见 iff.yaml" in js, "fallback static label (配置见 iff.yaml) missing"

    # HTML 结构：徽章节点存在且无 rate-limit 开关按钮（只读指示器，非开关）
    assert 'id="rlBadge"' in html, "rlBadge element missing in gateway card"
    assert 'id="rlMeta"' in html, "rlMeta element missing in gateway card"
    # 刻意不做开关：不得有 rate-limit 的 data-action 按钮
    assert 'data-action="rate-limit' not in html, (
        "rate-limit must stay a read-only indicator — no toggle button"
    )
    assert 'data-action="rate-limit' not in js, (
        "rate-limit must stay a read-only indicator — no toggle action"
    )


def test_gateway_rate_limit_snapshot_contract():
    """R11b 后端侧：snapshot local_models.rate_limit 来自 dual_gate.describe()。

    锁死字段集（前端只读展示，无写入入口）：
      mode / server_rpm / model_rpm_default / max_concurrent / global_max_concurrent。
    与 tests/unit/proxy/test_snapshot_rate_limit.py（_handle_snapshot 行为）互为
    契约锚点：此处断言 handler 源码确实把 dual_gate.describe() 接进 payload。"""
    src = (ROOT / "inferfabric" / "proxy" / "handler.py").read_text(encoding="utf-8")
    # 速率限制数据源 = dual_gate.describe()，且缺 dual_gate 时回退 None
    assert "dual_gate" in src and "describe()" in src, (
        "snapshot must source rate_limit from pm.dual_gate.describe()"
    )
    assert '"rate_limit"' in src, "snapshot payload must carry a rate_limit field"
    # 无 dual_gate（旧 proxy / 最小桩）时不得抛错 → getattr 兜底 None
    assert "getattr(pm, 'dual_gate'" in src, (
        "dual_gate access must be getattr-guarded (absent → None, no crash)"
    )


