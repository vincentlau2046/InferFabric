/* InferFabric Console — Chart factory (v2, Task 4)
 * window.IFCharts: theme-aware ECharts wrapper.
 *   - create(containerId, theme?) → echarts 实例或 null（echarts 缺失时）
 *   - update(target, optionPatch[, opts]) → 合并 option + setOption（target = 实例或 id）。
 *     opts.replaceSeries: series 整数组替换——默认累积合并（_deepMerge）按索引保留旧
 *     series 尾部（series 数收缩时残留幽灵 series）；series 数动态变化的调用方传
 *     { replaceSeries: true }（包装层整替 + ECharts ≥5.4 replaceMerge，vendor 已含）
 *   - dispose(target)              → 销毁实例（TAB 卸载用）
 *   - palettes = { dark:[5], light:[5] }  CVD 全对验证通过，固定顺序 蓝→琥珀→青→绿→粉
 *   - onThemeChange(cb)            → 注册主题变更回调；主题切换时 dispose+重建实例（spec §7）
 *
 * 图表规则（spec §7 / dataviz，不可协商）：
 *   单 y 轴（禁 dual-axis；唯一受权例外 update(..., {dualAxis:true})——
 *   仅限「流速↔存量」积分因果对：功耗/电费卡 功耗W↔累计kWh、Token 双卡
 *   每桶tokens↔窗口累积（本地token量/云端费用¥），见 _applyRules）、线宽 2px、
 *   网格/坐标轴退隐、crosshair tooltip、≥2 系列必有 legend、系列色固定顺序
 *   不循环、状态色绝不充当系列色、颜色跟随实体不跟随排序。
 *
 * 依赖：echarts（由 __init__.py 在本模块之前内联为 window.echarts）、window.store（theme 事件）。
 * 两者缺失均 graceful 降级：create 返回 null，调用方显示 empty state。
 */
(function () {
  'use strict';

  /* ── CVD 全对验证通过的统一调色板 v7.0 ──────────────
   * 固定顺序：蓝 → 琥珀 → 青 → 绿 → 粉。不循环；第 6+ 系列折叠为"其他"。
   * dark/light 同 hex（消除原 dark #3b82f6 vs light #2563eb 两套蓝；dark 蓝对齐
   *   light 深蓝后，蓝↔青正常视力 ΔE 16.3，双主题全对过 15）。
   * 语义分层：[1] 琥珀 = 累计/存量线专用槽（功耗电量 + Token 累计，跨卡锚点，
   *   暖色与冷色柱天然分层）；[0]/[2]/[3]/[4] = 数据系列槽（柱 + 趋势折线）。
   * 放弃红（与琥珀暖色撞 ΔE 6.8）与紫（与蓝 deutan ΔE 0.4 同色）——物理不可用。
   * 状态色（ok/warn/crit/info）与系列色严格分离，不混用——切勿把状态色写进这里。
   * 改动任一值必须重跑 dataviz validate_palette.js --pairs all --mode dark|light 保持 ALL PASS。 */
  var PALETTES = {
    dark:  ['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777'],
    light: ['#2563eb', '#b45309', '#0891b2', '#15803d', '#db2777'],
  };

  var _instances = {};          // id → { id, chart, containerId, theme, userOption }
  var _seq = 0;
  var _themeCallbacks = [];
  var _echartsMissingLogged = false;
  var _resizeTimer = null;

  function _echarts() {
    if (typeof window.echarts === 'undefined' || !window.echarts) {
      if (!_echartsMissingLogged) {
        console.warn('[IFCharts] echarts not loaded — charts disabled (empty state)');
        _echartsMissingLogged = true;
      }
      return null;
    }
    return window.echarts;
  }

  function currentTheme() {
    var t = document.documentElement.getAttribute('data-theme');
    return t === 'light' ? 'light' : 'dark';
  }

  function paletteFor(theme) {
    return (theme === 'light' ? PALETTES.light : PALETTES.dark).slice();
  }

  /* 读 CSS 自定义属性，失败回退到主题硬编码值（保证 echarts 即使在 CSS 未加载时也有合理色）。 */
  function _cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  /* 基座 option：编码 spec §7 通用规则。series 由调用方经 update 注入。 */
  function baseOption(theme) {
    var th = theme === 'light' ? 'light' : 'dark';
    var border = _cssVar('--border', th === 'light' ? '#e2e5ea' : '#2a3340');
    var ink2 = _cssVar('--ink-2', th === 'light' ? '#5b6572' : '#9aa5b5');
    var ink3 = _cssVar('--ink-3', th === 'light' ? '#8a93a3' : '#66707f');
    var surface = th === 'light' ? '#ffffff' : '#161c24';
    return {
      color: paletteFor(th),                 // 系列色固定顺序（_applyRules 会再次强制）
      // grid 紧凑、退隐
      grid: { top: 36, right: 18, bottom: 32, left: 52, containLabel: true },
      // 坐标轴：axisLine 隐藏、splitLine 退隐（仅 y 轴虚线 --border 色）
      xAxis: {
        type: 'category',
        boundaryGap: false,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: ink3, fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: ink3, fontSize: 11 },
        splitLine: { show: true, lineStyle: { color: border, type: 'dashed' } },
      },
      // tooltip：十字线（trigger axis）——单 y 轴
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross', snap: true,
          lineStyle: { color: border, width: 1 },
          label: { color: ink2, backgroundColor: surface },
        },
        backgroundColor: surface,
        borderColor: border,
        borderWidth: 1,
        textStyle: { color: ink2, fontSize: 12 },
        extraCssText: 'box-shadow: 0 2px 8px rgba(0,0,0,.18);',
      },
      // legend：≥2 系列时显示（_applyRules 按 series 数量定 show）
      legend: {
        show: 'auto',
        textStyle: { color: ink2, fontSize: 11 },
        icon: 'roundRect',
        itemWidth: 10, itemHeight: 10,
        itemGap: 16, top: 4,
      },
      series: [],
    };
  }

  /* 深合并：对象递归合并；数组按下标合并（对象元素合并，标量替换），对齐 ECharts setOption 语义。
   * 例外：ECharts 的 `data` 字段（xAxis.data / yAxis.data / series[].data / legend.data）
   * 永远是"整体替换"语义——一组数据点代表完整数据集，不是增量。按下标合并会导致
   * 切换粒度/窗口时旧数据点残留（如 hour 60 点 → day 14 点，残留 46 个旧时间标签 +
   * ghost 数据条）。故 `data` key 整体替换。 */
  function _clone(v) {
    if (v == null || typeof v !== 'object') return v;
    if (Array.isArray(v)) return v.map(_clone);
    var o = {}; for (var k in v) o[k] = _clone(v[k]); return o;
  }
  function _mergeArray(bv, pv) {
    if (!Array.isArray(bv)) return _clone(pv);
    var len = Math.max(bv.length, pv.length);
    var out = [];
    for (var i = 0; i < len; i++) {
      var b = bv[i], p = pv[i];
      if (p === undefined) out[i] = _clone(b);
      else if (p != null && typeof p === 'object' && !Array.isArray(p) &&
               b != null && typeof b === 'object' && !Array.isArray(b)) {
        out[i] = _deepMerge(b, p);
      } else {
        out[i] = _clone(p);
      }
    }
    return out;
  }
  function _deepMerge(base, patch) {
    if (patch == null) return base;
    if (typeof patch !== 'object' || Array.isArray(patch)) return _clone(patch);
    if (typeof base !== 'object' || base === null || Array.isArray(base)) base = {};
    var out = {}; for (var k in base) out[k] = _clone(base[k]);
    for (var key in patch) {
      var pv = patch[key], bv = base[key];
      // ECharts data 数组整体替换（见上方注释）——不按下标合并残留旧点
      if (key === 'data') {
        out[key] = _clone(pv);
      } else if (pv != null && typeof pv === 'object' && !Array.isArray(pv)) {
        out[key] = _deepMerge(bv || {}, pv);
      } else if (Array.isArray(pv)) {
        out[key] = _mergeArray(bv, pv);
      } else {
        out[key] = pv;
      }
    }
    return out;
  }

  /* 强制 spec §7 规则（opinionated，调用方无法绕过系列色顺序/dual-axis）：
   *   - 系列色固定为调色板顺序，不循环；状态色绝不混入（color 被覆盖为 palette）
   *   - line 系列：线宽 2px、symbol 'none'（选择性直接标注由调用方按需加 label/markPoint，不逐点标）
   *   - 单 y 轴：yAxis 数组 >1 时截断为首个并告警（禁 dual-axis）。
   *     唯一受权例外：opts.dualAxis === true 时放行双 y 轴——仅限「流速↔存量」
   *     积分因果对：①「功耗/电费」卡（功耗 W 与累计电量 kWh 是积分关系）；
   *     ②「Token 双卡」（每桶 tokens 与窗口累积——本地 token 量 / 云端费用 ¥，
   *     同为流速对时间的积分）。双轴同图不误导；monitor.js 封装处注明。
   *     其他图表保持铁律。
   *   - ≥2 系列必有 legend；未显式指定 show 时按系列数自动 */
  function _applyRules(option, theme, opts) {
    var th = theme === 'light' ? 'light' : 'dark';
    option.color = paletteFor(th);
    var series = option.series || [];
    for (var i = 0; i < series.length; i++) {
      var s = series[i];
      if (!s) continue;
      var t = s.type || 'line';
      if (t === 'line') {
        s.lineStyle = s.lineStyle || {};
        if (s.lineStyle.width == null) s.lineStyle.width = 2;   // 细线条 2px
        if (s.symbol == null) s.symbol = 'none';                // 无逐点标记
        if (s.smooth == null) s.smooth = false;
      }
    }
    if (Array.isArray(option.yAxis) && option.yAxis.length > 1) {
      if (opts && opts.dualAxis) {
        // 显式放行的双轴：以 baseOption 的单轴样式为模板补齐每个轴（主题一致），
        // 调用方只需给数值/名称/刻度差异。
        var tmpl = baseOption(th).yAxis;
        option.yAxis = option.yAxis.map(function (ax) {
          return (ax && typeof ax === 'object')
            ? _deepMerge(_clone(tmpl), ax)
            : _clone(tmpl);
        });
      } else {
        console.warn('[IFCharts] dual y-axis rejected (spec §7); keeping first axis only');
        option.yAxis = option.yAxis.slice(0, 1);
      }
    }
    option.legend = option.legend || {};
    if (option.legend.show === 'auto' || option.legend.show == null) {
      option.legend.show = series.length >= 2;
    }
    return option;
  }

  function _resolveEntry(target) {
    if (!target) return null;
    if (typeof target === 'string') return _instances[target] || null;
    var id = target._ifcId;
    if (id && _instances[id]) return _instances[id];
    for (var k in _instances) {
      if (_instances[k] && _instances[k].chart === target) return _instances[k];
    }
    return null;
  }

  /* ── 公开 API ─────────────────────────────────────────────── */

  function create(containerId, theme) {
    var ec = _echarts();
    var el = typeof containerId === 'string'
      ? document.getElementById(containerId) : containerId;
    if (!ec || !el) return null;
    var th = theme === 'light' ? 'light' : (theme === 'dark' ? 'dark' : currentTheme());
    var chart = ec.init(el, null, { renderer: 'canvas' });
    var id = 'ifc' + (++_seq);
    chart._ifcId = id;
    var entry = { id: id, chart: chart, containerId: containerId, theme: th, userOption: {} };
    _instances[id] = entry;
    var opt = _applyRules(baseOption(th), th);
    chart.setOption(opt);
    return chart;
  }

  function update(target, optionPatch, opts) {
    var entry = _resolveEntry(target);
    if (!entry || !optionPatch || typeof optionPatch !== 'object') return false;
    entry.userOption = _deepMerge(entry.userOption, optionPatch);
    /* opts.replaceSeries：series 整数组替换（包装层）。累积 _deepMerge 按索引合并
     * series，数组变短时旧尾部残留进 opt（幽灵 series，数据还是上一窗口的）。
     * 此处以 optionPatch.series 整体覆盖 userOption.series，并经 setOption 的
     * replaceMerge:['series'] 让 ECharts 侧同样整替（vendor 5.5.1 已含该能力）。 */
    if (opts && opts.replaceSeries && optionPatch.series) {
      entry.userOption.series = _clone(optionPatch.series);   // 整替但保持 house 克隆惯例
    }
    var opt = _deepMerge(baseOption(entry.theme), entry.userOption);
    _applyRules(opt, entry.theme, opts || {});
    var sopt = { notMerge: false, lazyUpdate: false };
    if (opts && opts.replaceSeries) sopt.replaceMerge = ['series'];
    entry.chart.setOption(opt, sopt);
    return true;
  }

  function dispose(target) {
    var entry = _resolveEntry(target);
    if (!entry) return false;
    try { entry.chart.dispose(); } catch (e) { /* noop */ }
    delete _instances[entry.id];
    return true;
  }

  function onThemeChange(cb) {
    if (typeof cb === 'function') _themeCallbacks.push(cb);
  }

  /* ── 主题切换：dispose + 重建（spec §7：不是换色滤镜） ─────── */
  function _rebuildAll(newTheme) {
    var ec = _echarts();
    if (!ec) return;
    var th = newTheme === 'light' ? 'light' : 'dark';
    for (var id in _instances) {
      var e = _instances[id];
      if (!e) continue;
      var userOpt = e.userOption;
      var cid = e.containerId;
      try { e.chart.dispose(); } catch (err) { /* noop */ }
      var el = typeof cid === 'string' ? document.getElementById(cid) : cid;
      if (!el) { delete _instances[id]; continue; }
      var chart = ec.init(el, null, { renderer: 'canvas' });
      chart._ifcId = id;
      _instances[id] = { id: id, chart: chart, containerId: cid, theme: th, userOption: userOpt };
      var opt = _deepMerge(baseOption(th), userOpt);
      /* 重建沿用原 option 的双轴授权：调用方 update 时已显式 {dualAxis:true}
       * 声明过，重建时不重传 opts 会把 yAxis 截成单轴而 series 的 yAxisIndex:1
       * 残存——echarts 首渲染抛 cartesian2d 异常（功耗/电费卡首例）。 */
      var rebuildOpts = Array.isArray(userOpt.yAxis) && userOpt.yAxis.length > 1
        ? { dualAxis: true } : {};
      _applyRules(opt, th, rebuildOpts);
      chart.setOption(opt);
    }
  }

  function _onThemeChanged(newTheme) {
    var th = newTheme === 'light' ? 'light' : 'dark';
    _rebuildAll(th);
    for (var i = 0; i < _themeCallbacks.length; i++) {
      try { _themeCallbacks[i](th); } catch (e) { console.warn('[IFCharts] theme cb error:', e); }
    }
  }

  /* 主题订阅：首选 store.on('theme')（store.js toggle/init 已 set 'theme' key）；
   * store 不可用时回退 MutationObserver 监听 <html data-theme>，保证任何来源的切换都能重建。 */
  function _setupThemeWatch() {
    if (window.store && typeof window.store.on === 'function') {
      window.store.on('theme', function (newTheme) { _onThemeChanged(newTheme); });
      return;
    }
    if (typeof MutationObserver === 'undefined') return;
    try {
      var obs = new MutationObserver(function (mutations) {
        for (var i = 0; i < mutations.length; i++) {
          if (mutations[i].attributeName === 'data-theme') {
            _onThemeChanged(currentTheme());
            return;
          }
        }
      });
      obs.observe(document.documentElement,
        { attributes: true, attributeFilter: ['data-theme'] });
    } catch (e) { /* noop */ }
  }

  /* 窗口尺寸变化时统一 resize 所有实例（轻量防抖）。 */
  function _onResize() {
    if (_resizeTimer) clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function () {
      _resizeTimer = null;
      resizeAll();
    }, 150);
  }

  /* 立即重算所有实例尺寸（A2：视图切换后调用——隐藏壳内初始化的
     ECharts 实例为 0 尺寸，显示后需 resize 才能正确绘制）。 */
  function resizeAll() {
    for (var id in _instances) {
      if (_instances[id]) { try { _instances[id].chart.resize(); } catch (e) {} }
    }
  }

  window.IFCharts = {
    palettes: PALETTES,
    create: create,
    update: update,
    dispose: dispose,
    resizeAll: resizeAll,
    onThemeChange: onThemeChange,
    currentTheme: currentTheme,
  };

  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('resize', _onResize);
  }
  _setupThemeWatch();
})();
