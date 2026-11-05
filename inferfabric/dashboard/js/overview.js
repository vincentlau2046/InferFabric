/* InferFabric Console — Overview tab (v2, Task 3)
 * 接 /api/snapshot 真实数据：GPU 遥测带 + 活跃模型卡 + 24h 请求趋势 sparkline
 *   + 最近异常 top3 + 系统操作（release/reconcile/reload/reset）。
 *
 * 数据源：
 *   - window.store  /api/snapshot 轮询结果（gpu gpu_util active_services services_info
 *     sleep_states models 等）
 *   - window.__TOKEN_STATS__  代理在 head 末尾注入的 token 统计原始状态
 *       形如 { "YYYY-MM-DD": { model: {prompt_tokens, generation_tokens, requests} } }
 *       （按日聚合；Task 4 ECharts 将换为真正小时级数据源，容器 id #ovSpark24h 不变）
 *   - GET /api/anomalies  异常事件（top3）
 *
 * 设计纪律：总览页 read-mostly；数字等宽 tabular-nums；零 emoji；句首大写；
 *   破坏性操作带确认弹窗；加载态 skeleton；唯一强调色用于"正常"填充条与主操作。
 *
 * 暴露：
 *   - window.tabRenderers['tab-overview'] = renderOverview
 *   - window.doSystemOps(action)
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[overview] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  /* 客户端会话级 uptime 观察：模型首次被观察到 running 时记录开始时间。
   * 后端未暴露进程真实启动时间，此值为"本次页面会话观察到的运行时长"，
   * 页面刷新后从 0 重新计数（真实 uptime ≥ 此值）。Task 6 接 doModelAction 时可复核。 */
  var _startedAt = {};

  /* 异常拉取节流：避免每 3s 轮询都打 /api/anomalies */
  var _lastAnomFetch = 0;
  var _anomCache = null;
  var ANOM_TTL = 15000;

  /* ── 小工具 ── */
  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtClock(mhz) {
    if (mhz == null || mhz === '' || mhz === '—' || isNaN(mhz)) return '—';
    var ghz = Number(mhz) / 1000;
    return ghz.toFixed(2);
  }

  function fmtCtx(n) {
    if (n == null || n === '' || isNaN(n)) return '—';
    n = Number(n);
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(n);
  }

  var ENGINE_LABEL = {
    vllm: 'vLLM', sglang: 'SGLang', ninfer: 'NInfer',
    ollama: 'Ollama', llama_cpp: 'llama.cpp', comfyui: 'ComfyUI',
    tts: 'TTS', asr: 'ASR',
  };

  function engineLabel(type) {
    return ENGINE_LABEL[type] || (type ? type.charAt(0).toUpperCase() + type.slice(1) : '—');
  }

  /* 填充条状态类：>90% crit、>75% warn、否则正常（走 --accent） */
  function fillClass(pct) {
    if (pct > 90) return 'crit';
    if (pct > 75) return 'warn';
    return '';
  }

  function setFill(id, pct) {
    var el = $(id);
    if (!el) return;
    var cls = fillClass(pct);
    el.className = 'tr-fill' + (cls ? ' ' + cls : '');
    el.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function isOverviewActive() {
    var el = $('tab-overview');
    return !!(el && el.classList.contains('active'));
  }

  /* ── 1. GPU 遥测带（全局 shell 元素，每个快照都刷新，不限定 tab） ── */
  function renderRail() {
    var gpu = store.get('gpu') || {};
    var util = store.get('gpu_util') || {};

    var used = gpu.used, total = gpu.total, vramPct = gpu.pct || 0;
    var vram = $('trVram');
    if (vram) vram.textContent = UI.fmtGB(used);
    setFill('trVramFill', vramPct);

    var utilPct = util.pct || 0;
    var utilEl = $('trUtil');
    if (utilEl) utilEl.textContent = UI.fmtNum(Math.round(utilPct));
    setFill('trUtilFill', utilPct);

    var pwr = $('trPwr');
    if (pwr) pwr.textContent = (util.power == null || util.power === '—') ? '—' : UI.fmtNum(Math.round(util.power));
    // 遥测带 5 段统一量级条：功耗/温度/时钟按本机额定上限（5090D）折算
    setFill('trPwrFill', numPct(util.power, 575));

    var temp = $('trTemp');
    if (temp) temp.textContent = (util.temp == null || util.temp === '') ? '—' : UI.fmtNum(Math.round(util.temp));
    setFill('trTempFill', numPct(util.temp, 90));

    var clk = $('trClock');
    if (clk) clk.textContent = fmtClock(util.clock);
    setFill('trClockFill', numPct(util.clock, 3110));
  }

  /* 数值 → 量级百分比（0–100 钳制）；非数字（—/null）→ 0。 */
  function numPct(v, denom) {
    var n = Number(v);
    if (!isFinite(n) || n <= 0 || !denom) return 0;
    return Math.max(0, Math.min(100, (n / denom) * 100));
  }

  /* ── 2. 活跃模型卡 ── */
  function findModelDetail(name, models) {
    if (!models) return null;
    for (var i = 0; i < models.length; i++) {
      if (models[i] && models[i].name === name) return models[i];
    }
    return null;
  }

  function modelBlock(name, info, detail, sleepState) {
    var sleeping = !!sleepState;
    var type = (info && info.type) || (detail && detail.type) || '';
    var port = info && info.port ? info.port : null;
    var quant = detail && detail.quantization ? detail.quantization : '';
    var ctx = detail && detail.context_window != null ? detail.context_window : null;

    // 客户端 uptime
    var now = Date.now();
    if (!sleeping) {
      if (!_startedAt[name]) _startedAt[name] = now;
    } else {
      delete _startedAt[name];
    }
    var upSecs = _startedAt[name] ? Math.max(0, Math.floor((now - _startedAt[name]) / 1000)) : 0;

    var dotCls = sleeping ? 'warn' : 'ok';
    var stateTxt = sleeping ? ('sleeping ' + String(sleepState).toUpperCase()) : 'running';

    var stopDis = sleeping ? ' disabled' : '';
    var sleepDis = sleeping ? ' disabled' : '';
    var wakeDis = sleeping ? '' : ' disabled';

    return '' +
      '<div class="ov-model">' +
        '<div class="if-card-hdr">' +
          '<span class="status-dot ' + dotCls + '" title="' + escHtml(stateTxt) + '"></span>' +
          '<span class="if-card-title">' + escHtml(name) + '</span>' +
          '<span class="badge">' + escHtml(engineLabel(type)) + '</span>' +
          (port != null ? '<span class="if-card-sub">端口 <span class="mono">:' + escHtml(port) + '</span></span>' : '') +
        '</div>' +
        '<dl class="ov-stats">' +
          '<div class="ov-stat"><dt>uptime</dt><dd class="mono">' + UI.fmtDur(upSecs) + '</dd></div>' +
          '<div class="ov-stat"><dt>context</dt><dd class="mono">' + fmtCtx(ctx) + ' <span class="muted">tokens</span></dd></div>' +
          '<div class="ov-stat"><dt>precision</dt><dd class="mono">' + (quant ? escHtml(quant) : '—') + '</dd></div>' +
        '</dl>' +
        '<div class="if-card-actions">' +
          '<button class="btn btn-sec btn-sm" data-action="stop"' + stopDis + '>' + UI.icon('stop', 14) + 'Stop</button>' +
          '<button class="btn btn-sec btn-sm" data-action="sleep"' + sleepDis + '>' + UI.icon('sleep', 14) + 'Sleep</button>' +
          '<button class="btn btn-sec btn-sm" data-action="wake"' + wakeDis + '>' + UI.icon('wake', 14) + 'Wake</button>' +
        '</div>' +
      '</div>';
  }

  function renderActive() {
    var card = $('ovActiveCard');
    if (!card) return;

    var models = store.get('models');
    // 快照尚未到达：skeleton
    if (models === undefined) {
      card.innerHTML =
        '<div class="if-card-hdr"><span class="status-dot"></span><span class="if-card-title muted">加载中…</span></div>' +
        '<div class="if-card-body" id="ovActiveBody"></div>';
      UI.skeleton($('ovActiveBody'), 3);
      return;
    }

    var active = store.get('active_services') || [];
    var info = store.get('services_info') || {};
    var sleep = store.get('sleep_states') || {};

    // 清理已不活跃模型的 uptime 记录
    var live = {};
    for (var k in _startedAt) live[k] = active.indexOf(k) >= 0;
    for (var kk in live) if (!live[kk]) delete _startedAt[kk];

    if (!active.length) {
      card.innerHTML =
        '<div class="if-card-hdr">' +
          '<span class="status-dot"></span>' +
          '<span class="if-card-title">无活跃模型</span>' +
          '<span class="if-card-sub">GPU 空闲</span>' +
        '</div>' +
        '<div class="if-card-body" id="ovActiveBody">' +
          '<p class="muted">当前无模型在运行。切换模型请前往推理页。</p>' +
        '</div>' +
        '<div class="if-card-actions">' +
          '<button class="btn btn-sec btn-sm" data-action="goto-inference">' + UI.icon('cube', 14) + '前往推理</button>' +
        '</div>';
      return;
    }

    var blocks = '';
    for (var i = 0; i < active.length; i++) {
      var name = active[i];
      blocks += modelBlock(name, info[name], findModelDetail(name, models), sleep[name]);
    }
    card.innerHTML =
      '<div class="if-card-body ov-active-list" id="ovActiveBody">' + blocks + '</div>';
  }

  /* ── 3. 请求趋势 sparkline（近 7 天） ──
   * __TOKEN_STATS__ 按日聚合（{YYYY-MM-DD: {model: {requests}}}），取最近 7 个
   * 日期桶绘制趋势线，caption 显示今日总量。后端 __TOKEN_STATS__ 仅按日聚合，
   * 无小时级数据源，故标题如实标"近 7 天"而非"24h"（spec §4.1 原述 24h，
   * 数据源约束下以实际粒度为准）。 */
  function renderSpark() {
    var card = $('ovSpark24h');
    if (!card) return;
    var body = card.querySelector('.if-card-body');
    if (!body) return;

    // 双 scope：{local:{date:{model:bucket}}, cloud:{...}}。
    // 优先 store（/api/snapshot 实时），回退 head 烘焙的 __TOKEN_STATS__。
    var live = store.get('token_stats');
    var stats = (live && (live.local || live.cloud)) ? live
      : (window.__TOKEN_STATS__ && (window.__TOKEN_STATS__.local || window.__TOKEN_STATS__.cloud) ? window.__TOKEN_STATS__ : {});
    if (!stats || typeof stats !== 'object') {
      body.innerHTML = '<div class="spark-empty muted">暂无请求数据 — 经代理发起请求后生成趋势</div>';
      return;
    }
    // 合并 local + cloud 两个 scope，按日汇总请求数（本地 + 云端总流量）
    var byDate = {};
    for (var scope in stats) {
      var scopeData = stats[scope];
      if (!scopeData || typeof scopeData !== 'object') continue;
      for (var date in scopeData) {
        var models = scopeData[date] || {};
        var total = 0;
        for (var m in models) {
          if (models[m] && models[m].requests) total += models[m].requests;
        }
        byDate[date] = (byDate[date] || 0) + total;
      }
    }
    var keys = Object.keys(byDate);
    if (!keys.length) {
      body.innerHTML = '<div class="spark-empty muted">暂无请求数据 — 经代理发起请求后生成趋势</div>';
      return;
    }

    var days = [];
    for (var i = 0; i < keys.length; i++) {
      days.push({ date: keys[i], total: byDate[keys[i]] });
    }
    days.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
    var recent = days.slice(-7);
    if (!recent.length) {
      body.innerHTML = '<div class="spark-empty muted">暂无请求数据 — 经代理发起请求后生成趋势</div>';
      return;
    }

    var max = 1;
    for (var j = 0; j < recent.length; j++) if (recent[j].total > max) max = recent[j].total;
    var W = 240, H = 60, pad = 6;
    var n = recent.length;
    var ptsArr = [];
    for (var p = 0; p < n; p++) {
      var x = n === 1 ? W / 2 : pad + (p / (n - 1)) * (W - 2 * pad);
      var y = H - pad - (recent[p].total / max) * (H - 2 * pad);
      ptsArr.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var pts = ptsArr.join(' ');
    var todayTotal = recent[recent.length - 1].total;

    body.innerHTML =
      '<svg class="ov-spark" viewBox="0 0 240 60" preserveAspectRatio="none" aria-label="请求趋势 sparkline">' +
        '<polyline class="spark-line" points="' + pts + '"/>' +
      '</svg>' +
      '<div class="spark-caption"><b>' + UI.fmtNum(todayTotal) + '</b> <span class="muted">req · 今日</span></div>';
  }

  /* ── 4. 最近异常 top3 ── */
  function sevClass(sev) {
    sev = (sev || 'info').toLowerCase();
    if (sev === 'critical' || sev === 'error') return 'crit';
    if (sev === 'warning') return 'warn';
    return 'info';
  }
  function sevIcon(sev) {
    sev = (sev || 'info').toLowerCase();
    if (sev === 'error' || sev === 'critical') return 'xmark';
    if (sev === 'warning') return 'alert';
    return 'bolt';
  }

  function drawAnoms(events) {
    var card = $('ovAnomTop');
    if (!card) return;
    var body = card.querySelector('.if-card-body');
    if (!body) return;
    var top = (events || []).slice(0, 3);
    if (!top.length) {
      body.innerHTML = '<p class="muted ov-anom-empty">无异常事件</p>';
      return;
    }
    var rows = '';
    for (var i = 0; i < top.length; i++) {
      var e = top[i];
      var t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '—';
      var sev = e.severity || 'info';
      rows += '<tr>' +
        '<td class="mono">' + escHtml(t) + '</td>' +
        '<td><span class="badge ' + sevClass(sev) + '">' + UI.icon(sevIcon(sev), 12) + escHtml(sev) + '</span></td>' +
        '<td>' + escHtml(e.message || '') + '</td>' +
      '</tr>';
    }
    body.innerHTML =
      '<table class="if-table ov-anom"><thead><tr><th>时间</th><th>级别</th><th>消息</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  function renderAnoms() {
    var card = $('ovAnomTop');
    if (!card) return;
    var now = Date.now();
    if (_anomCache && now - _lastAnomFetch < ANOM_TTL) {
      drawAnoms(_anomCache);
      return;
    }
    var body = card.querySelector('.if-card-body');
    if (body && !_anomCache) UI.skeleton(body, 3);
    fetch('/api/anomalies?limit=3', { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        _anomCache = (data && data.events) || [];
        _lastAnomFetch = Date.now();
        drawAnoms(_anomCache);
      })
      .catch(function () {
        // 失败：展示缓存（若有），否则空态
        if (_anomCache) drawAnoms(_anomCache);
        else {
          var b = card.querySelector('.if-card-body');
          if (b) b.innerHTML = '<p class="muted ov-anom-empty">异常数据暂不可用</p>';
        }
      });
  }

  /* ── 总览页渲染入口 ── */
  function renderOverview() {
    renderActive();
    renderSpark();
    renderAnoms();
  }
  window.renderOverview = renderOverview;
  window.tabRenderers['tab-overview'] = renderOverview;

  /* ── 订阅 ──
   * 遥测带是 shell 级全局元素（base.html，所有 tab 可见）→ 每个快照都刷新。
   * 总览页 tab 内容 → 仅在总览页可见时刷新。 */
  store.on('sync_meta', renderRail);
  store.on('sync_meta', function () { if (isOverviewActive()) renderOverview(); });
  store.on('tab_active', function (tab) {
    if (tab === 'tab-overview') { renderRail(); renderOverview(); }
  });

  /* ── 5. 系统操作：release / reconcile / reload / reset ── */
  window.doSystemOps = async function (action) {
    var OPS = {
      release: {
        url: '/switch', body: { model: 'idle' },
        confirm: { title: '释放 GPU', body: '释放 GPU，停止当前独占模型。确定？', danger: true },
        ok: 'GPU 已释放',
      },
      reconcile: {
        url: '/reconcile', body: null,
        ok: '已重新校准状态',
      },
      reload: {
        url: '/reload-config', body: null,
        ok: '配置已重载',
      },
      reset: {
        url: '/reset', body: null,
        confirm: { title: '强制重置', body: '强制重置 GPU 状态。会 SIGKILL 所有模型进程。确定？', danger: true },
        ok: '已强制重置',
      },
    };
    var op = OPS[action];
    if (!op) { UI.toast('未知操作: ' + action, 'error'); return; }

    var run = async function () {
      try {
        var res = await fetch(op.url, {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: op.body ? JSON.stringify(op.body) : undefined,
        });
        if (!res.ok) {
          var msg = 'HTTP ' + res.status;
          try {
            var j = await res.json();
            if (j) msg = j.error || j.message || msg;
          } catch (_) {}
          throw new Error(msg);
        }
        UI.toast(op.ok, 'ok');
        store.forceRefresh();
      } catch (e) {
        UI.toast('操作失败: ' + (e && e.message ? e.message : e), 'error');
      }
    };

    if (op.confirm) {
      UI.confirm({
        title: op.confirm.title,
        body: op.confirm.body,
        danger: op.confirm.danger,
        onOk: run,
      });
    } else {
      run();
    }
  };

  /* ── 事件委托（无 inline onclick） ── */

  // 系统操作卡：release / reconcile / reload / reset
  var sysCard = $('sysOpsCard');
  if (sysCard) {
    sysCard.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-action]');
      if (!btn || !sysCard.contains(btn)) return;
      var act = btn.getAttribute('data-action');
      if (act && window.doSystemOps) window.doSystemOps(act);
    });
  }

  // 活跃模型卡快捷操作：stop / sleep / wake / goto-inference
  // 总览页保持 read-mostly：模型级操作引导至推理页（doModelAction 归 Task 6）
  var actCard = $('ovActiveCard');
  if (actCard) {
    actCard.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-action]');
      if (!btn || !actCard.contains(btn) || btn.disabled) return;
      var act = btn.getAttribute('data-action');
      if (act === 'goto-inference') {
        store.switchTab('tab-inference');
        return;
      }
      // stop / sleep / wake → 推理页执行
      store.switchTab('tab-inference');
      UI.toast('在推理页操作', 'info');
    });
  }

  // 异常卡"查看全部" → 异常 tab（移除原型 inline onclick，改委托）
  var anomCard = $('ovAnomTop');
  if (anomCard) {
    anomCard.addEventListener('click', function (ev) {
      var link = ev.target.closest('.if-card-link');
      if (!link || !anomCard.contains(link)) return;
      ev.preventDefault();
      store.switchTab('tab-anomaly');
    });
  }
})();
