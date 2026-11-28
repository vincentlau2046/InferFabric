/* requests.js — dashboard UX v3 B2：请求日志工作台。
 *
 * 规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md B2 +
 * specs/dashboard-ux-v3/interface-contract.md §B（消费 B1 /api/request_log window 模式）。
 *
 * 职责与契约：
 *   - 过滤参数单源 ifRouter：控件变更 → navigate('requests', null, params) →
 *     route_params 发布 → 本模块取数。深链初始参数在装载时自 location.hash 解析
 *     （boot 不发布 route_params，见 ledger Ruling）。
 *   - 过滤先于分页：total/failed_total 同快照；分页固定首次响应的 until；
 *     第一页 Live 轮询（5s）不带 until（滚动窗），深页不轮询。
 *   - 迟到响应按序失效（seq + 离页守卫）；加载/空/失败三态。
 *   - 列选择 iff_req_cols 持久化（协议列取消）；Live 开关 iff_req_live 持久化。
 *   - 池安全投影展开（B1：name/route_kind/target_port/retry/fallback）；
 *     异常关联按 模型+状态码+300s 推断，标注「按模型/时间推断」。
 *   - 内容组守卫 tab-monitor.log（仅请求页主子页可见；VIEW_GROUPS B2 拆组）。
 *
 * 暴露：window.ifRequests = { expand, collapseAll, refresh }。
 */
(function () {
  'use strict';

  var PAGE_SIZE = 50;
  var POLL_MS = 5000;
  var CORRELATION_WINDOW_S = 300;
  var DEFAULT_COLS = ['time', 'status', 'model', 'agent', 'tokens', 'dur', 'cache'];
  var COL_LABELS = {
    time: '时间', status: '状态', model: '模型', agent: '客户端',
    tokens: 'Tokens', dur: '耗时', cache: '缓存',
  };

  var store = window.store || null;

  /* ── 状态 ── */
  var _params = { window: '24h', status: '', model: '', agent: '', q: '' };
  var _page = 1;
  var _until = null;          // 首次响应固定的窗口上界（翻页/Live 语义见规格）
  var _state = null;          // { logs, total, failed, until }
  var _error = null;
  var _seq = 0;               // 取数序号：迟响应丢弃
  var _inflightKey = null;
  var _loadedKey = null;
  var _live = true;
  var _timer = null;
  var _cols = loadCols();
  var _expanded = {};
  var _anomalies = [];        // 修复轮：异常关联数据源 = /api/anomalies（有界环）
  var _anomaliesInflight = false;

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
  function fmtTs(ts) {
    if (ts == null || isNaN(Number(ts))) return '—';
    try {
      return new Date(Number(ts) * 1000).toLocaleTimeString('zh-CN',
        { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) { return '—'; }
  }

  function loadCols() {
    try {
      var raw = localStorage.getItem('iff_req_cols');
      if (raw) {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          // 修复轮：校验合法列 id 且去重（损坏偏好不产生重复表头）
          var ok = arr.filter(function (k, i) {
            return COL_LABELS[k] && arr.indexOf(k) === i;
          });
          if (ok.length) return ok;
        }
      }
    } catch (e) { /* 存储不可用用默认列 */ }
    return DEFAULT_COLS.slice();
  }
  function saveCols() {
    try { localStorage.setItem('iff_req_cols', JSON.stringify(_cols)); } catch (e) { /* 忽略 */ }
  }

  /* 内容组守卫：请求页主子页（router 未载入时保持旧「全渲染」兜底） */
  function isActive() {
    try {
      if (store && typeof store.isGroupActive === 'function') {
        return store.isGroupActive('tab-monitor.log');
      }
    } catch (e) { /* fallthrough */ }
    return true;
  }

  /* ── 取数 ── */
  function paramsKey(p) {
    // 修复轮：与 router.paramsKey 同编码 —— q 含字面 '&'/'=' 不与他参数同键
    return Object.keys(p).sort().map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(p[k]);
    }).join('&');
  }
  function currentKey() {
    return paramsKey(_params) + '#' + _page + '#' + (_until == null ? '' : _until);
  }
  function buildQuery(params, page, until) {
    var qs = [];
    if (params.window) qs.push('window=' + encodeURIComponent(params.window));
    if (params.status) qs.push('status=' + encodeURIComponent(params.status));
    if (params.model) qs.push('model=' + encodeURIComponent(params.model));
    if (params.agent) qs.push('agent=' + encodeURIComponent(params.agent));
    if (params.q) qs.push('q=' + encodeURIComponent(params.q));
    qs.push('limit=' + PAGE_SIZE);
    qs.push('offset=' + ((page - 1) * PAGE_SIZE));
    if (until != null) qs.push('until=' + until);
    return '/api/request_log?' + qs.join('&');
  }

  function fetchPage(force, opts) {
    if (!isActive()) return;
    var isLive = !!(opts && opts.live);
    var key = currentKey();
    // 修复轮：任何取数入口都使在途响应失效 —— 即使同键去重命中（如浏览器
    // 后退到已加载数据），迟到的旧筛选响应也不得覆盖当前数据。
    _seq++;
    if (!force) {
      if (key === _inflightKey) return;             // 同键在途：去重
      if (key === _loadedKey && _state) return;     // 同键已有数据
    }
    // Live 轮询不带 until（滚动窗）；翻页沿用已锚定的 until
    var url = buildQuery(_params, _page, isLive ? null : _until);
    var seq = _seq;
    _inflightKey = key;
    fetch(url).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      // 修复轮：簿记移到守卫之后 —— 迟到响应不得改写 in/loaded 键
      if (seq !== _seq || !isActive()) return;
      _inflightKey = null;
      _loadedKey = key;
      _state = {
        logs: (data && data.logs) || [],
        total: (data && data.total) || 0,
        failed: (data && data.failed_total) || 0,
        until: data && data.until,
      };
      _error = null;
      // until 锚定：页 1 响应（含 Live tick）刷新锚点 —— 深页翻页据此保持
      // 同一窗口快照；Live 每轮重锚即滚动语义。
      if (_page === 1 && data && data.until != null) _until = data.until;
      renderData();
    }).catch(function (e) {
      if (seq !== _seq || !isActive()) return;
      _inflightKey = null;
      _error = e;
      renderData();
    });
  }

  /* ── 异常关联数据源（修复轮）：/api/anomalies 有界环，激活时刷新 ── */
  function loadAnomalies() {
    if (_anomaliesInflight) return;
    _anomaliesInflight = true;
    fetch('/api/anomalies?since=0&limit=200').then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      _anomaliesInflight = false;
      _anomalies = (data && data.events) || [];
      if (_state && Object.keys(_expanded).length) renderTable();
    }).catch(function () { _anomaliesInflight = false; /* 缺数据 → 「异常关联 —」 */ });
  }

  /* ── 参数应用 ── */
  function applyParams(p) {
    _params = {
      window: (p && p.window) || '24h',
      status: (p && p.status) || '',
      model: (p && p.model) || '',
      agent: (p && p.agent) || '',
      q: (p && p.q) || '',
    };
    _page = 1;
    _until = null;
    renderControls();
  }

  function navWith(patch) {
    var p = {};
    if (_params.window) p.window = _params.window;
    if (_params.status) p.status = _params.status;
    if (_params.model) p.model = _params.model;
    if (_params.agent) p.agent = _params.agent;
    if (_params.q) p.q = _params.q;
    Object.keys(patch).forEach(function (k) {
      if (patch[k]) p[k] = patch[k]; else delete p[k];
    });
    if (window.ifRouter && typeof window.ifRouter.navigate === 'function') {
      window.ifRouter.navigate('requests', null, p);
    }
  }

  /* ── 渲染 ── */
  function renderControls() {
    var seg = ['1h', '24h', '7d'];
    for (var i = 0; i < seg.length; i++) {
      var el = $('reqWin-' + seg[i]);
      if (el) el.classList.toggle('on', _params.window === seg[i]);
    }
    var st = $('reqStatus');
    if (st) st.value = _params.status;
    var md = $('reqModel');
    if (md) md.value = _params.model;
    var ag = $('reqAgent');
    if (ag) ag.value = _params.agent;
    var se = $('reqSearch');
    if (se && document.activeElement !== se) se.value = _params.q;
    var lb = $('reqLiveBtn');
    if (lb) {
      lb.classList.toggle('on', _live);
      lb.setAttribute('aria-pressed', _live ? 'true' : 'false');
    }
    var keys = Object.keys(COL_LABELS);
    for (var j = 0; j < keys.length; j++) {
      var mi = $('reqCol-' + keys[j]);
      if (mi) {
        var on = _cols.indexOf(keys[j]) >= 0;
        mi.classList.toggle('on', on);
        mi.checked = on;   // 修复轮：原生 checked 同步（显示状态跟随偏好）
      }
    }
  }

  function renderCount() {
    var el = $('reqCountLine');
    if (!el) return;
    if (!_state) { el.innerHTML = ''; return; }
    var html = '共 <b>' + fmtNum(_state.total) + '</b> 条 · ' +
      '<span class="bad">' + fmtNum(_state.failed) + '</span> 失败';
    if (_page >= 2) html += ' · <span title="翻页固定窗口上界，改筛选或回 Live 恢复滚动">固定窗口</span>';
    el.innerHTML = html;
  }

  function totalPages() {
    var total = _state ? _state.total : 0;
    return Math.max(1, Math.ceil(total / PAGE_SIZE));
  }

  function renderPager() {
    var prev = $('reqPrev'), next = $('reqNext'), info = $('reqPageInfo');
    if (info) info.textContent = '第 ' + _page + ' / ' + totalPages() + ' 页';
    if (prev) prev.disabled = _page <= 1;
    if (next) next.disabled = _page >= totalPages();
  }

  function statusCls(s) {
    if (s == null || s === '') return '';   // 修复轮：状态未知用中性徽章，不冒认 2xx
    var n = Number(s) || 0;
    return n < 400 ? 'ok' : (n < 500 ? 'warn' : 'crit');
  }

  function cellHtml(col, l) {
    switch (col) {
      case 'time': return '<td class="mono">' + escHtml(fmtTs(l.timestamp)) + '</td>';
      case 'status': {
        var cls = statusCls(l.status);
        return '<td><span class="badge' + (cls ? ' ' + cls : '') + '">' +
               escHtml(l.status == null ? '—' : l.status) + '</span></td>';
      }
      case 'model': return '<td>' + escHtml(l.model || '—') + '</td>';
      case 'agent': return '<td class="mono">' + escHtml(l.agent || '—') + '</td>';
      case 'tokens':
        return '<td class="mono num">' + escHtml(fmtNum(l.tokens_in) + ' / ' + fmtNum(l.tokens_out)) + '</td>';
      case 'dur':
        return '<td class="mono num">' +
               escHtml(l.duration_ms != null ? Math.round(l.duration_ms) + 'ms' : '—') + '</td>';
      case 'cache': {
        var rate = '—';
        if (l.tokens_in > 0) {
          var c = Math.min(l.tokens_in_cached || 0, l.tokens_in);
          rate = (c / l.tokens_in * 100).toFixed(1) + '%';
        }
        return '<td class="mono num">' + escHtml(rate) + '</td>';
      }
      default: return '<td>—</td>';
    }
  }

  /* 异常关联：模型 + 状态码 + ±300s 推断（B1 anomaly ring 不含 req_id 关联）。
   * 修复轮：数据源 = /api/anomalies 端点（loadAnomalies），非 store 种子。 */
  function correlateAnomaly(l) {
    var list = _anomalies;
    if (!list.length || (Number(l.status) || 0) < 400) return null;
    var best = null, bestGap = Infinity;
    for (var i = 0; i < list.length; i++) {
      var a = list[i] || {};
      if (a.model !== l.model) continue;
      if (Number(a.status_code) !== Number(l.status)) continue;
      var gap = Math.abs(Number(a.ts) - Number(l.timestamp));
      if (isNaN(gap) || gap > CORRELATION_WINDOW_S) continue;
      if (gap < bestGap) { bestGap = gap; best = a; }
    }
    return best;
  }

  function detailHtml(l) {
    var parts = [];
    parts.push('req <span class="mono">' + escHtml(l.req_id || '—') + '</span>');
    if (l.pool && typeof l.pool === 'object') {
      var p = l.pool;
      parts.push('池 <b>' + escHtml(p.name || '—') + '</b>');
      parts.push(escHtml(p.route_kind || '—'));
      parts.push('重试 ' + (p.retry_count != null ? p.retry_count : '—'));
      parts.push('回退 ' + (p.fallback_count != null ? p.fallback_count : '—'));
      parts.push('目标 ' + (p.target_port != null ? p.target_port : '—'));
    } else {
      parts.push('池 —');
    }
    if (l.error) parts.push('<span class="bad">' + escHtml(l.error) + '</span>');
    var a = correlateAnomaly(l);
    if (a) {
      parts.push('异常关联（按模型/时间推断）：<span class="bad">' +
                 escHtml(a.message || a.category || '—') + '</span>');
    } else {
      parts.push('异常关联 —');
    }
    return '<tr class="req-detail"><td colspan="' + _cols.length + '">' +
           parts.join(' · ') + '</td></tr>';
  }

  function renderTable() {
    var el = $('reqTable');
    if (!el) return;
    if (_error) {
      el.innerHTML = '<div class="if-empty">读取失败 — 请求日志暂不可取' +
        ' <button type="button" class="if-btn-ghost" id="reqRetry">重试</button></div>';
      var retry = $('reqRetry');
      if (retry) retry.addEventListener('click', function () { fetchPage(true); });
      return;
    }
    if (!_state) {
      el.innerHTML = '<div class="skeleton req-skeleton">' +
        '<div class="skeleton-row"></div><div class="skeleton-row"></div>' +
        '<div class="skeleton-row"></div></div>';
      return;
    }
    if (!_state.logs.length) {
      el.innerHTML = '<div class="if-empty">暂无请求记录 — 经代理发起请求后在此出现</div>';
      return;
    }
    var head = _cols.map(function (c) {
      return '<th>' + escHtml(COL_LABELS[c]) + '</th>';
    }).join('');
    var body = '';
    var logs = _state.logs;
    for (var i = 0; i < logs.length; i++) {
      var l = logs[i] || {};
      var rid = l.req_id || ('row-' + i);
      var cls = _expanded[rid] ? ' req-row expanded' : ' req-row';
      body += '<tr data-req-id="' + escHtml(rid) + '" class="' + cls.trim() + '">' +
        _cols.map(function (c) { return cellHtml(c, l); }).join('') + '</tr>';
      if (_expanded[rid]) body += detailHtml(l);
    }
    el.innerHTML =
      '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl req-tbl">' +
      '<thead><tr>' + head + '</tr></thead>' +
      '<tbody>' + body + '</tbody>' +
      '</table></div>';
  }

  function renderData() {
    renderCount();
    renderPager();
    renderAgentOptions();
    renderTable();
  }

  /* ── 模型选项（快照 local_models；数组或 {discovered, configured} 两形兼容） ── */
  function renderModelOptions() {
    var md = $('reqModel');
    if (!md) return;
    var raw = (store && typeof store.get === 'function') ? store.get('local_models') : null;
    var names = [];
    if (Array.isArray(raw)) names = raw;
    else if (raw && typeof raw === 'object') {
      names = [].concat(raw.discovered || [], raw.configured || []);
    }
    names = names.map(function (m) {
      return (typeof m === 'string') ? m : ((m && (m.name || m.served_name || m.model)) || '');
    }).filter(Boolean);
    var seen = {}, uniq = [];
    for (var i = 0; i < names.length; i++) {
      if (!seen[names[i]]) { seen[names[i]] = 1; uniq.push(names[i]); }
    }
    // 修复轮：选中模型不在当前目录（退役/云端书签）时注入 option，控件不显示为空
    if (_params.model && !seen[_params.model]) uniq.unshift(_params.model);
    md.innerHTML = '<option value="">全部模型</option>' + uniq.map(function (n) {
      return '<option value="' + escHtml(n) + '">' + escHtml(n) + '</option>';
    }).join('');
    md.value = _params.model || '';
  }

  /* ── 客户端选项（修复轮，design §3.2 过滤行「客户端」）：
   * 当前页去重 agent + 选中项兜底注入（页内 facet；D 期分解出全量）。 ── */
  function renderAgentOptions() {
    var ag = $('reqAgent');
    if (!ag) return;
    var seen = {}, names = [];
    var logs = (_state && _state.logs) || [];
    for (var i = 0; i < logs.length; i++) {
      var a = (logs[i] || {}).agent;
      if (a && !seen[a]) { seen[a] = 1; names.push(a); }
    }
    if (_params.agent && !seen[_params.agent]) names.unshift(_params.agent);
    ag.innerHTML = '<option value="">全部客户端</option>' + names.map(function (n) {
      return '<option value="' + escHtml(n) + '">' + escHtml(n) + '</option>';
    }).join('');
    ag.value = _params.agent || '';
  }

  /* ── Live 门控 ── */
  function tick() {
    if (!_live || _page !== 1 || !isActive()) return;   // Live 只服务第一页
    if (_inflightKey) return;   // 修复轮：单飞 —— 前一取数在途时跳过本轮
    fetchPage(true, { live: true });
  }
  function startTimer() {
    if (_timer) return;
    _timer = setInterval(tick, POLL_MS);
  }
  function stopTimer() {
    if (_timer) { clearInterval(_timer); _timer = null; }
  }
  function syncVisibility() {
    if (isActive()) {
      renderControls();
      startTimer();
      loadAnomalies();   // 修复轮：激活时刷新异常关联环
      fetchPage();       // 去重守卫：数据已新鲜时为 no-op（seq 仍失效在途）
    } else {
      stopTimer();
    }
  }

  /* ── 公共 API（展开/收起供测试与键盘路径复用；表格行点击事件等价） ── */
  window.ifRequests = {
    expand: function (reqId) {
      _expanded[reqId] = !_expanded[reqId];
      renderTable();
    },
    collapseAll: function () { _expanded = {}; renderTable(); },
    refresh: function () { fetchPage(true); },
  };

  /* 修复轮：全局刷新按钮触达工作台（store.forceRefresh 按 tabRenderers 分发） */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-monitor.log'] = function () {
    if (isActive()) fetchPage(true);
  };

  /* ── 接线 ── */
  function initFromHash() {
    try {
      if (!window.ifRouter || typeof window.ifRouter.parseHash !== 'function') return;
      var r = window.ifRouter.parseHash(location.hash || '');
      if (r && r.view === 'requests' && r.params) applyParams(r.params);
    } catch (e) { /* 畸形地址用默认参数 */ }
  }

  function bind() {
    var segs = ['1h', '24h', '7d'];
    for (var i = 0; i < segs.length; i++) {
      (function (w) {
        var el = $('reqWin-' + w);
        if (el) el.addEventListener('click', function () { navWith({ window: w }); });
      })(segs[i]);
    }
    var st = $('reqStatus');
    if (st) st.addEventListener('change', function () { navWith({ status: st.value }); });
    var md = $('reqModel');
    if (md) md.addEventListener('change', function () { navWith({ model: md.value }); });
    var ag = $('reqAgent');
    if (ag) ag.addEventListener('change', function () { navWith({ agent: ag.value }); });
    var se = $('reqSearch');
    if (se) se.addEventListener('change', function () { navWith({ q: se.value }); });

    var colsBtn = $('reqColsBtn'), menu = $('reqColsMenu');
    if (colsBtn && menu) {
      colsBtn.addEventListener('click', function () {
        menu.hidden = !menu.hidden;
        colsBtn.classList.toggle('on', !menu.hidden);
      });
    }
    Object.keys(COL_LABELS).forEach(function (k) {
      var mi = $('reqCol-' + k);
      if (mi) mi.addEventListener('click', function () {
        var idx = _cols.indexOf(k);
        if (idx >= 0) _cols.splice(idx, 1); else _cols.push(k);
        if (!_cols.length) _cols = [k];       // 至少保留一列
        saveCols();
        renderControls();
        renderTable();
      });
    });

    var live = $('reqLiveBtn');
    if (live) live.addEventListener('click', function () {
      _live = !_live;
      try { localStorage.setItem('iff_req_live', _live ? '1' : '0'); } catch (e) { /* 忽略 */ }
      renderControls();
      // 修复轮：深页开 Live 即回第一页滚动取数（契约「Live 回到第一页」）
      if (_live && _page !== 1) {
        _page = 1;
        _until = null;
        fetchPage();
      }
    });

    var prev = $('reqPrev'), next = $('reqNext');
    if (prev) prev.addEventListener('click', function () {
      if (_page > 1) { _page -= 1; fetchPage(); }
    });
    if (next) next.addEventListener('click', function () {
      if (_page < totalPages()) { _page += 1; fetchPage(); }
    });

    var table = $('reqTable');
    if (table) table.addEventListener('click', function (ev) {
      if (!ev.target || typeof ev.target.closest !== 'function') return;
      var tr = ev.target.closest('tr[data-req-id]');
      if (tr) window.ifRequests.expand(tr.getAttribute('data-req-id'));
    });
  }

  /* ── 装载顺序：初始控件 → 深链参数（boot 不发布，自 hash 解析）→ 订阅回放 ── */
  try { _live = localStorage.getItem('iff_req_live') !== '0'; } catch (e) { /* 默认开 */ }
  renderControls();
  initFromHash();
  bind();
  renderModelOptions();
  renderData();      // 初始骨架/空态占位（真实 DOM 与测试桩同形）

  if (store) {
    store.on('route_params', function (p) {
      applyParams(p || {});
      if (isActive()) fetchPage();
    });
    store.on('tab_active', function () { syncVisibility(); });
    if (typeof store.on === 'function') store.on('local_models', renderModelOptions);
  }
})();
