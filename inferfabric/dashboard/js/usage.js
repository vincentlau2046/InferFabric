/* usage.js — dashboard UX v3 D2：用量页（一个窗口驱动整页）。
 *
 * 规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md D2 +
 * specs/dashboard-ux-v3/interface-contract.md §D。
 *
 * 职责与契约：
 *   - 数据源：/api/monitor-report?window=1h|24h|7d|30d&scope=all（D1 同快照
 *     报告，单遍键集扫描——非 snapshot 轮询面，绝不挂周期轮询）+
 *     /api/device_power?gran=（窗口→gran 映射：1h→hour…30d→month）。
 *   - 一个窗口驱动整页：四 KPI（请求/Tokens/估算成本/KV 前缀缓存命中 token
 *     份额）、趋势堆叠图、模型与客户端分解成组换新；两路响应都落定才渲染。
 *   - 旧窗口响应晚到无效（seq + 离页守卫）；无周期轮询，刷新入口 =
 *     激活 / 切窗 / 全局刷新 / 认领成功事件。
 *   - KPI 语义：分母无数据 → 「暂无数据」不画 0%；无计价证据 → 「未计价」
 *     不冒认 ¥0；KV 前缀缓存 ≠ 响应缓存命中率（卡上明示）。
 *   - 趋势图：本地=蓝 #2563eb / 云=琥珀 #b45309（IFCharts 调色板固定序），
 *     堆叠柱；7d 用报告桶 4×6h 等分合成日图、30d 桶已是日粒度（不冒称本地
 *     自然日，同 monitor_report 契约）；unknown 不入图，另作覆盖说明。
 *   - 电费：device_power 仅是设备功耗估算（非整机电费）；桶缺测/覆盖不足
 *     如实标注；云费（报告窗口）与电费（device_power 聚合区间）单列来源，
 *     不求和不伪造完整合计。
 *   - 客户端「未识别」分组保留认领路径：data-claim 按钮复用 monitor.js 的
 *     全局认领弹窗委托；认领/删除成功后 monitor.js 派发 iff-agents-changed，
 *     本模块重取报告（裁决：monitor.js 2 行增量事件，见 ledger）。
 *
 * 暴露：window.ifUsage = { setWindow, refresh }。
 */
(function () {
  'use strict';

  var WINDOWS = ['1h', '24h', '7d', '30d'];
  var WIN_GRAN = { '1h': 'hour', '24h': 'day', '7d': 'week', '30d': 'month' };
  var WIN_LABEL = { '1h': '近 1 小时', '24h': '近 24 小时', '7d': '近 7 天', '30d': '近 30 天' };
  var GRAN_LABEL = { hour: '小时', day: '天', week: '周', month: '月' };
  /* IFCharts 调色板固定序 [0]=蓝 [1]=琥珀（dark/light 同 hex，charts.js v7.0）。
   * 显式取值钉住「本地/云用指定蓝/琥珀」，颜色跟随实体不跟随排序。 */
  var PAL = (window.IFCharts && window.IFCharts.palettes &&
             window.IFCharts.palettes.dark) || ['#2563eb', '#b45309'];
  var COLOR_LOCAL = PAL[0];
  var COLOR_CLOUD = PAL[1];

  var store = window.store || null;
  var _charts = { trend: null };

  var _win = '24h';
  var _seq = 0;            // 取数序号：旧窗口迟到响应丢弃
  var _report = null;
  var _power = null;
  var _error = null;

  /* ── 工具 ── */
  function $(id) { return document.getElementById(id); }
  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fmtNum(n) {
    if (n == null || isNaN(Number(n))) return '0';
    return Number(n).toLocaleString('en-US');
  }
  function fmtYuan(n) {
    if (n == null || !isFinite(Number(n))) return null;
    var v = Number(n);
    // 极小成本保留 6 位（0.0002 → ¥0.000200），否则两位
    return '¥' + (v > 0 && v < 0.01 ? v.toFixed(6) : v.toFixed(2));
  }
  function fmtPct(x) {
    if (x == null || isNaN(Number(x))) return null;
    return (Number(x) * 100).toFixed(1) + '%';
  }
  function fmtMs(v) { return (v == null || isNaN(Number(v))) ? '—' : Math.round(Number(v)) + 'ms'; }
  function dayLabel(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '—';
    function p2(n) { return (n < 10 ? '0' : '') + n; }
    return p2(d.getMonth() + 1) + '-' + p2(d.getDate());
  }
  function timeLabel(ts, win) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '—';
    function p2(n) { return (n < 10 ? '0' : '') + n; }
    return win === '1h'
      ? p2(d.getHours()) + ':' + p2(d.getMinutes())
      : p2(d.getHours()) + ':00';
  }

  /* 内容组守卫：用量页（旧组键沿用 VIEW_GROUPS['usage']） */
  function isActive() {
    try {
      if (store && typeof store.isGroupActive === 'function') {
        return store.isGroupActive('tab-monitor.usage');
      }
    } catch (e) { /* fallthrough */ }
    return true;
  }

  /* ── 取数：report + device_power 两路，都落定才渲染 ── */
  function fetchAll() {
    if (!isActive()) return;
    _seq++;
    var seq = _seq;
    var gran = WIN_GRAN[_win] || 'day';
    var gotReport, gotPower;      // undefined=在途，null=失败，值=成功
    var settle = function () {
      if (gotReport === undefined || gotPower === undefined) return;
      if (seq !== _seq || !isActive()) return;   // 迟到/离页：整组丢弃
      if (gotReport == null) {
        _error = '读取失败 — 用量报告暂不可取';
      } else {
        _error = null;
        _report = gotReport;
        _power = gotPower;       // 功耗失败降级为不可用态，不拖垮整页
      }
      renderAll();
    };
    fetch('/api/monitor-report?window=' + _win + '&scope=all')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { gotReport = d; settle(); })
      .catch(function () { gotReport = null; settle(); });
    fetch('/api/device_power?gran=' + gran)
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { gotPower = d; settle(); })
      .catch(function () { gotPower = null; settle(); });
  }

  function setWindow(w) {
    if (WINDOWS.indexOf(w) < 0 || w === _win) return;
    _win = w;
    renderControls();
    fetchAll();
  }

  /* ── 渲染 ── */
  function renderControls() {
    for (var i = 0; i < WINDOWS.length; i++) {
      var el = $('useWin-' + WINDOWS[i]);
      if (el) {
        el.classList.toggle('on', _win === WINDOWS[i]);
        el.setAttribute('aria-pressed', _win === WINDOWS[i] ? 'true' : 'false');
      }
    }
  }

  function renderMeta() {
    var el = $('useMetaLine');
    if (!el) return;
    if (_error) {
      el.innerHTML = '<span class="bad">' + escHtml(_error) + '</span>' +
        ' <button type="button" class="if-btn-ghost" id="useRetry">重试</button>';
      var retry = $('useRetry');
      if (retry) retry.addEventListener('click', function () { fetchAll(); });
      return;
    }
    var rep = _report;
    var cov = rep && rep.coverage || {};
    el.textContent = WIN_LABEL[_win] +
      ' · 请求 ' + fmtNum(cov.rows_scanned) + ' 行 · 报告 ' +
      (rep ? new Date((rep.collected_at || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—');
  }

  function renderTrunc() {
    var el = $('useTruncBanner');
    if (!el) return;
    var truncated = !!(_report && _report.coverage && _report.coverage.truncated);
    el.innerHTML = truncated ? '⚠️ 报告不完整：达到扫描安全上限，统计可能少于实际（如实标注，不冒认完整）' : '';
    el.style.display = truncated ? '' : 'none';
  }

  function renderKpis() {
    var req = $('useKpiReq'), tok = $('useKpiTok'), cost = $('useKpiCost'),
        cache = $('useKpiCache'), cacheLbl = $('useKpiCacheLbl');
    if (cacheLbl) cacheLbl.textContent = 'KV 前缀缓存命中 token 份额 · 非响应缓存命中率';
    var t = (_report && _report.totals) || {};
    if (_error) {
      if (req) req.innerHTML = '—';
      if (tok) tok.innerHTML = '—';
      if (cost) cost.innerHTML = '—';
      if (cache) cache.innerHTML = '—';
      return;
    }
    if (req) {
      req.innerHTML = fmtNum(t.requests) +
        ' <span class="use-kpi-sub">成功率 ' +
        escHtml(t.success_rate != null ? (Number(t.success_rate) * 100).toFixed(1) + '%' : '—') +
        '</span>';
    }
    if (tok) {
      tok.innerHTML = fmtNum(t.tokens_in) + ' <span class="use-kpi-sub">/ out ' +
        fmtNum(t.tokens_out) + ' · cached ' + fmtNum(t.tokens_in_cached) + '</span>';
    }
    if (cost) {
      // 无计价证据 → 「未计价」标注（不冒认 ¥0/免费）
      cost.innerHTML = (t.cost_estimate != null)
        ? escHtml(fmtYuan(t.cost_estimate))
        : '<span class="use-badge-warn">未计价</span>';
    }
    if (cache) {
      // 分母无数据 → 无值「暂无数据」，不画 0%
      cache.innerHTML = (t.cache_share != null)
        ? escHtml(fmtPct(t.cache_share)) +
          ' <span class="use-kpi-sub">' + fmtNum(t.tokens_in_cached) + ' / ' +
          fmtNum(t.tokens_in) + ' tokens</span>'
        : '暂无数据';
    }
  }

  /* 趋势：1h/24h 报告桶直出；7d 28×6h 桶 4 桶/组合成 7 日组；30d 桶已是
   * 24h 日粒度直出（等分合成，不冒称本地自然日对齐）。 */
  function renderTrend() {
    var note = $('useTrendNote'), emptyEl = $('useTrendEmpty');
    if (_charts.trend == null) _charts.trend = window.IFCharts.create('useTrendChart');
    if (note) note.innerHTML = '';
    var series = (_report && _report.series) || [];
    var cats = [], local = [], cloud = [];
    var unkReq = 0, unkTok = 0;
    var acc = function (b) {
      var lo = (b && b.local) || {}, cl = (b && b.cloud) || {}, uk = (b && b.unknown) || {};
      unkReq += uk.requests || 0;
      unkTok += uk.tokens_in || 0;
      return [lo.requests || 0, cl.requests || 0];
    };
    if (_win === '7d') {
      for (var i = 0; i < series.length; i += 4) {
        var grp = series.slice(i, i + 4);
        var lr = 0, cr = 0;
        for (var k = 0; k < grp.length; k++) {
          var v = acc(grp[k]); lr += v[0]; cr += v[1];
        }
        cats.push(dayLabel(grp[0] && grp[0].bucket_start));
        local.push(lr); cloud.push(cr);
      }
    } else {
      for (var j = 0; j < series.length; j++) {
        var b = series[j] || {};
        var v2 = acc(b);
        cats.push(timeLabel(b.bucket_start, _win));
        local.push(v2[0]); cloud.push(v2[1]);
      }
    }
    if (emptyEl) emptyEl.style.display = ((_report && _report.totals && _report.totals.requests) || 0) ? 'none' : '';
    if (note) {
      // unknown 另作覆盖说明（不作图系列，不并入本地）
      note.innerHTML = unkReq > 0
        ? '另有 <b>' + fmtNum(unkReq) + '</b> 请求（' + fmtNum(unkTok) +
          ' tokens）路由未知，未计入图——可在请求页按「状态/模型」排查 route 缺失行'
        : '';
    }
    if (_charts.trend) {
      window.IFCharts.update(_charts.trend, {
        grid: { left: 40, right: 12, top: 24, bottom: 26 },
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: cats, boundaryGap: true },
        yAxis: { type: 'value', name: '请求' },
        series: [
          { name: '本地', type: 'bar', stack: 'trend', barWidth: '70%',
            itemStyle: { color: COLOR_LOCAL }, data: local },
          { name: '云端', type: 'bar', stack: 'trend', barWidth: '70%',
            itemStyle: { color: COLOR_CLOUD }, data: cloud },
        ],
      }, { replaceSeries: true });
    }
  }

  function renderModelTable() {
    var el = $('useModelTable');
    if (!el) return;
    if (_error) { el.innerHTML = '<div class="if-empty">' + escHtml(_error) + '</div>'; return; }
    if (!_report) {
      el.innerHTML = '<div class="skeleton"><div class="skeleton-row"></div>' +
        '<div class="skeleton-row"></div></div>';
      return;
    }
    var models = _report.models || [];
    if (!models.length) {
      el.innerHTML = '<div class="if-empty">窗口内暂无模型用量</div>';
      return;
    }
    var totalIn = (_report.totals && _report.totals.tokens_in) || 0;
    var rows = '';
    for (var i = 0; i < models.length; i++) {
      var m = models[i] || {};
      var share = (totalIn > 0 && m.tokens_in != null) ? fmtPct(m.tokens_in / totalIn) : '—';
      var pos = m.location === 'local' ? '本地'
        : (m.location === 'cloud' ? '云端 · ' + escHtml(m.provider || '—') : '未知路由');
      var cost = (m.cost_estimate != null) ? fmtYuan(m.cost_estimate) : '未计价';
      rows += '<tr><td>' + escHtml(m.model || '—') + '</td>' +
        '<td>' + pos + '</td>' +
        '<td class="mono num">' + escHtml(share) + '</td>' +
        '<td class="mono num">' + fmtNum(m.tokens_in) + ' / ' + fmtNum(m.tokens_out) + '</td>' +
        '<td class="mono num">' + fmtNum(m.requests) + '</td>' +
        '<td class="mono num">' + escHtml(cost) + '</td>' +
        '<td class="mono num">' + escHtml(fmtMs(m.p50_ttft_ms)) + '</td>' +
        '<td class="mono num">' + escHtml(fmtMs(m.p95_ttft_ms)) + '</td></tr>';
    }
    el.innerHTML = '<div class="cp-table-wrap"><table class="if-table mon-tbl">' +
      '<thead><tr><th>模型</th><th>位置</th><th class="num">份额</th>' +
      '<th class="num">Tokens in/out</th><th class="num">请求</th>' +
      '<th class="num">估算成本</th><th class="num">TTFT P50</th><th class="num">P95</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function renderAgentTable() {
    var el = $('useAgentTable');
    if (!el) return;
    if (_error) { el.innerHTML = '<div class="if-empty">' + escHtml(_error) + '</div>'; return; }
    if (!_report) {
      el.innerHTML = '<div class="skeleton"><div class="skeleton-row"></div>' +
        '<div class="skeleton-row"></div></div>';
      return;
    }
    var agents = _report.agents || [];
    if (!agents.length) {
      el.innerHTML = '<div class="if-empty">窗口内暂无客户端数据——经代理发起请求后在此统计</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < agents.length; i++) {
      var a = agents[i] || {};
      // 未识别分组（D1 契约键 "unidentified"）→ 待认领 + 认领按钮。
      // 认领弹窗复用 monitor.js 全局委托（data-claim），不重建弹窗。
      var isUnk = a.agent === 'unidentified';
      var cost = (a.cost_estimate != null) ? fmtYuan(a.cost_estimate) : '未计价';
      var cache = (a.cache_share != null) ? fmtPct(a.cache_share) : '—';
      rows += '<tr' + (isUnk ? ' class="agent-unk-row"' : '') + '><td>' +
        (isUnk ? '未识别 <span class="agent-unk-tag">待认领</span>' : escHtml(a.agent)) +
        '</td>' +
        '<td class="mono num">' + fmtNum(a.requests) + '</td>' +
        '<td class="mono num">' + fmtNum(a.tokens_in) + ' / ' + fmtNum(a.tokens_out) + '</td>' +
        '<td class="mono num">' + escHtml(cache) + '</td>' +
        '<td class="mono num">' + escHtml(cost) + '</td>' +
        '<td>' + (isUnk ? '<button type="button" class="mon-mini-btn" data-claim="unidentified" title="识别/认领">识别</button>' : '') +
        '</td></tr>';
    }
    el.innerHTML = '<div class="cp-table-wrap"><table class="if-table mon-tbl">' +
      '<thead><tr><th>客户端</th><th class="num">请求</th>' +
      '<th class="num">Tokens in/out</th><th class="num">KV 份额</th>' +
      '<th class="num">估算成本</th><th></th></tr></thead><tbody>' + rows +
      '</tbody></table></div>';
  }

  function renderPower() {
    var tot = $('usePowerTotals'), note = $('usePowerNote'),
        empty = $('usePowerEmpty'), sub = $('usePowerSub'), cc = $('useCloudCost');
    var p = _power;
    if (tot) {
      // 无数据/覆盖不足 → 「—」，不冒认 ¥0
      tot.innerHTML = (p && p.available && p.totals && p.totals.yuan != null)
        ? escHtml(fmtYuan(p.totals.yuan)) +
          ' <span class="use-kpi-sub">' + fmtNum(p.totals.kwh) + ' 度 · 平均 ' +
          (p.totals.avg_w != null ? p.totals.avg_w + 'W' : '—') + '</span>'
        : '—';
    }
    if (note) {
      var html = '';
      if (p && p.available) {
        var missing = 0;
        var buckets = p.buckets || [];
        for (var i = 0; i < buckets.length; i++) {
          if (buckets[i] && buckets[i].avg_w == null) missing++;
        }
        html = '来源：/api/device_power · GPU 板卡功耗估算（非整机电费）· 按' +
          escHtml(GRAN_LABEL[p.gran] || p.gran || '—') + '聚合' +
          (missing ? ' · <span class="bad">缺测 ' + missing + ' 桶</span>' : '');
      } else {
        html = '<span class="bad">功耗数据暂不可用（覆盖不足）</span>——采样每 60s 进行，' +
          '覆盖不足时不计入合计，不把缺测当免费';
      }
      note.innerHTML = html;
    }
    if (empty) empty.style.display = (p && p.available) ? 'none' : '';
    if (sub) {
      sub.textContent = p && p.price_yuan_per_kwh != null
        ? '¥' + p.price_yuan_per_kwh + '/度' : '';
    }
    if (cc) {
      // 云费（报告窗口）与电费（device_power 聚合区间）单列来源，不求和
      var cost = _report && _report.totals && _report.totals.cost_estimate;
      cc.innerHTML = '云费（' + escHtml(WIN_LABEL[_win]) + '报告）：' +
        (cost != null ? escHtml(fmtYuan(cost)) : '<span class="use-badge-warn">未计价</span>') +
        ' · 电费见上（聚合区间与报告窗口可不同，不与云费求和）';
    }
  }

  function renderAll() {
    renderMeta();
    renderTrunc();
    renderKpis();
    renderTrend();
    renderModelTable();
    renderAgentTable();
    renderPower();
  }

  function syncVisibility() {
    if (isActive()) fetchAll();     // 激活/回页即取（无轮询；去重由 seq 语义承担）
  }

  /* ── 公共 API ── */
  window.ifUsage = {
    setWindow: setWindow,
    refresh: function () { fetchAll(); },
  };

  /* 全局刷新按钮触达用量页（覆盖 monitor.js 旧卡注册——旧卡已由 D2 撤除） */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-monitor.usage'] = function () {
    if (isActive()) fetchAll();
  };

  /* ── 接线 ── */
  function bind() {
    for (var i = 0; i < WINDOWS.length; i++) {
      (function (w) {
        var el = $('useWin-' + w);
        if (el) el.addEventListener('click', function () { setWindow(w); });
      })(WINDOWS[i]);
    }
    /* 认领/删除成功（monitor.js 派发）→ 报告重取，分解表换新 */
    if (typeof window.addEventListener === 'function') {
      window.addEventListener('iff-agents-changed', function () {
        if (isActive()) fetchAll();
      });
    }
  }

  renderControls();
  bind();
  renderAll();     // 初始骨架占位（真实 DOM 与测试桩同形）

  if (store && typeof store.on === 'function') {
    store.on('tab_active', function () { syncVisibility(); });
  }
  if (isActive()) fetchAll();
})();
