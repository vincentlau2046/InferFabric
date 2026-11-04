/* InferFabric Console — 异常 TAB (v2, Task 9)
 * 结构化事件表替代旧 monospace 文本块（spec §4.6）：
 * 时间 / 严重度（图标+文字标签）/ 类别 / 消息，model + status_code 作次级行。
 *
 * 数据源（API 冻结，不新增端点）：
 *   - GET /api/anomalies?since=0&limit=100[&category=<c>][&severity=<s>]
 *     → {events:[{id,ts,category,severity,model,message,status_code,possible_cause,detail}], count}
 *     category ∈ routing/model/auth/config/cloud，severity ∈ info/warning/error/critical
 *     （anomaly_collector.py:22-23；服务端 query 已支持 category/severity 过滤）
 *   - 服务端按时间倒序返回（anomaly_collector.query）；客户端仍按 ts 降序排序
 *     （防御性，保证"最新在上"显示契约）。
 *
 * 交互：
 *   - #anomCatFilter / #anomSevFilter change → 带 query 参数重新 fetch（服务端过滤，减小载荷）
 *   - #anomSearch input → 客户端全文过滤（~200ms debounce，匹配 message/model/category）
 *   - store.on('tab_active') → TAB 激活时拉取（不做长轮询）
 *
 * 设计纪律（spec §5 §7，零 emoji）：
 *   - 严重度图标 + 文字标签双编码（不得仅用颜色表达状态）；
 *     critical/error → s-alert、warning → s-bolt、info → s-message
 *   - critical 行 .row-crit 整行高亮（低 alpha --crit 背景 tint，文字色不变）
 *   - 数字（时间/计数）等宽 + tabular-nums；无 inline onclick
 *
 * 暴露：
 *   - window.tabRenderers['tab-anomaly'] = renderAnomaly
 *   - window.renderAnomaly / window.doAnomRefresh
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[anomaly] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  var EVENTS_URL = '/api/anomalies';
  var LIMIT = 100;
  var COLSPAN = 4;

  var _events = [];      // 已加载事件缓存（服务端过滤后、搜索过滤前）
  var _searchTimer = null;
  var _loadCtrl = null;  // 在途请求 AbortController（review fix#1：快速连切过滤时丢弃 stale 响应）

  /* ── XSS-safe 转义（与 cloud.js esc 一致） ── */
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── 严重度双编码（spec §7）：图标 + 文字标签，class 承载状态色 ── */
  var SEV_META = {
    critical: { cls: 'crit', icon: 'alert',   label: 'critical' },
    error:    { cls: 'err',  icon: 'alert',   label: 'error' },
    warning:  { cls: 'warn', icon: 'bolt',    label: 'warning' },
    info:     { cls: 'info', icon: 'message', label: 'info' },
  };
  function sevHtml(sev) {
    var m = SEV_META[sev] || SEV_META.info;
    return '<span class="anom-sev ' + m.cls + '">' + UI.icon(m.icon, 12) + m.label + '</span>';
  }

  /* ── 时间：epoch float → MM-DD HH:MM:SS（mono tabular-nums，不随 locale 漂移） ── */
  function fmtTs(ts) {
    if (!ts) return '—';
    var d = new Date(Number(ts) * 1000);
    function p(n) { return n < 10 ? '0' + n : String(n); }
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
           p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* ── 单行：时间 / 严重度 / 类别 / 消息（model + status_code 次级行） ── */
  function rowHtml(e) {
    var subParts = [];
    if (e.model) subParts.push(esc(e.model));
    if (e.status_code) subParts.push('HTTP ' + String(e.status_code));
    var sub = subParts.length
      ? '<span class="anom-sub">' + subParts.join(' · ') + '</span>'
      : '';
    var cls = 'anom-row' + (e.severity === 'critical' ? ' row-crit' : '');
    return '<tr class="' + cls + '">' +
      '<td class="mono">' + fmtTs(e.ts) + '</td>' +
      '<td>' + sevHtml(e.severity) + '</td>' +
      '<td><span class="anom-cat">' + esc(e.category) + '</span></td>' +
      '<td><div class="anom-msgcell">' +
        '<span class="anom-msg" title="' + esc(e.message) + '">' + esc(e.message) + '</span>' +
        sub +
      '</div></td>' +
    '</tr>';
  }

  /* ── tbody 渲染：搜索为客户端过滤，计数 #anomCount ── */
  function renderRows() {
    var tbody = $('anomTbody');
    if (!tbody) return;
    var qEl = $('anomSearch');
    var q = qEl && qEl.value ? qEl.value.trim().toLowerCase() : '';
    var rows = _events;
    if (q) {
      rows = _events.filter(function (e) {
        var hay = ((e.message || '') + ' ' + (e.model || '') + ' ' + (e.category || '')).toLowerCase();
        return hay.indexOf(q) !== -1;
      });
    }
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="' + COLSPAN + '">' +
        '<div class="if-empty">' +
        (_events.length
          ? '无匹配事件 — 清空搜索或调整过滤'
          : '暂无异常事件 — 系统正常，性能指标见「监控」TAB') +
        '</div></td></tr>';
    } else {
      var html = '';
      rows.forEach(function (e) { html += rowHtml(e); });
      tbody.innerHTML = html;
    }
    var count = $('anomCount');
    if (count) {
      count.textContent = q
        ? '匹配 ' + UI.fmtNum(rows.length) + ' / ' + UI.fmtNum(_events.length) + ' 条'
        : UI.fmtNum(_events.length) + ' 条';
    }
    renderSummary();
  }

  /* ── 加载骨架（UI.skeleton 同款 .skeleton/.skeleton-row，逐行占位） ── */
  function showSkeleton() {
    var tbody = $('anomTbody');
    if (!tbody) return;
    var html = '';
    for (var i = 0; i < 5; i++) {
      var cell = document.createElement('div');
      UI.skeleton(cell, 1);
      html += '<tr class="anom-skel"><td colspan="' + COLSPAN + '">' + cell.innerHTML + '</td></tr>';
    }
    tbody.innerHTML = html;
  }

  function showFail(msg) {
    var tbody = $('anomTbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="' + COLSPAN + '">' +
      '<div class="if-empty">加载失败 · ' + esc(msg) + '</div></td></tr>';
    var count = $('anomCount');
    if (count) count.textContent = '—';
  }

  /* ── 拉取：category/severity 作为 query 参数（服务端过滤，减小载荷） ──
   *   review fix#1：快速连切过滤/搜索会并发多请求，慢的旧响应可能后到覆盖新结果。
   *   用 AbortController 丢弃 stale 响应——每次发新请求前 abort 前一个。 */
  async function loadAnomalies() {
    var cat = ($('anomCatFilter') || {}).value || '';
    var sev = ($('anomSevFilter') || {}).value || '';
    var url = EVENTS_URL + '?since=0&limit=' + LIMIT;
    if (cat) url += '&category=' + encodeURIComponent(cat);
    if (sev) url += '&severity=' + encodeURIComponent(sev);
    if (_loadCtrl) { try { _loadCtrl.abort(); } catch (_) {} }
    _loadCtrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    showSkeleton();
    try {
      var res = await fetch(url, _loadCtrl ? { signal: _loadCtrl.signal } : undefined);
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        showFail((d && d.error) || ('HTTP ' + res.status));
        return;
      }
      // 防御性：按 ts 降序（最新在上；服务端已倒序，见 anomaly_collector.query）
      _events = (d.events || []).slice().sort(function (a, b) {
        return (Number(b.ts) || 0) - (Number(a.ts) || 0);
      });
      renderRows();
    } catch (e) {
      // 被新请求 abort 掉的旧响应：静默丢弃，不显示错误（新请求会接管渲染）
      if (e && (e.name === 'AbortError' || _loadCtrl && _loadCtrl.signal && _loadCtrl.signal.aborted)) return;
      showFail(e.message || String(e));
    }
  }

  /* ── 严重度摘要：当前（服务端过滤后）事件的严重度分布；点击 tile = 应用该严重度过滤 ── */
  var SEV_KEYS = ['critical', 'error', 'warning', 'info'];
  var SEV_TILE = {
    critical: 'anomSumCrit', error: 'anomSumErr',
    warning: 'anomSumWarn',  info: 'anomSumInfo',
  };
  function renderSummary() {
    var counts = { critical: 0, error: 0, warning: 0, info: 0 };
    for (var i = 0; i < _events.length; i++) {
      var s = _events[i].severity;
      if (counts[s] != null) counts[s]++;
    }
    SEV_KEYS.forEach(function (s) {
      var el = $(SEV_TILE[s]);
      if (el) el.textContent = _events.length ? String(counts[s]) : '—';
    });
  }

  /* ── 全文搜索：客户端过滤（~200ms debounce，匹配 message/model/category） ── */
  function onSearchInput() {
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(renderRows, 200);
  }

  /* ── 渲染入口（tabRenderers 契约；tab_active 刷新亦复用） ── */
  function renderAnomaly() {
    loadAnomalies();
  }
  window.renderAnomaly = renderAnomaly;
  window.tabRenderers['tab-anomaly'] = renderAnomaly;
  window.doAnomRefresh = loadAnomalies;

  store.on('tab_active', function (tab) {
    if (tab === 'tab-anomaly') renderAnomaly();
  });

  /* ── 过滤栏接线（模块初始化，无 inline onclick） ── */
  var catEl = $('anomCatFilter');
  var sevEl = $('anomSevFilter');
  var searchEl = $('anomSearch');
  if (catEl) catEl.addEventListener('change', loadAnomalies);
  if (sevEl) sevEl.addEventListener('change', loadAnomalies);
  if (searchEl) searchEl.addEventListener('input', onSearchInput);

  /* 严重度摘要 tile：点击 = 应用该严重度过滤（与 #anomSevFilter 同走服务端过滤） */
  var sumTiles = document.querySelectorAll('#anomSummary .anom-sum');
  sumTiles.forEach(function (tile) {
    tile.addEventListener('click', function () {
      var sev = tile.getAttribute('data-sev');
      var sevFilter = $('anomSevFilter');
      if (sevFilter) {
        sevFilter.value = (sevFilter.value === sev) ? '' : sev;
      }
      loadAnomalies();
    });
  });
})();
