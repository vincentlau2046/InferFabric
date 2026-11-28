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
    // A2：视图组合判断（状态页 = 本内容组）；store 无该能力时退回旧 DOM 判断
    if (store && typeof store.isGroupActive === 'function') {
      return store.isGroupActive('tab-overview');
    }
    var el = $('tab-overview');
    return !!(el && el.classList.contains('active'));
  }

  /* ── 1. GPU 遥测带（全局 shell 元素，每个快照都刷新，不限定 tab） ── */
  function renderRail() {
    var gpu = store.get('gpu') || {};
    var util = store.get('gpu_util') || {};
    // DeviceTelemetry T5: memory_kind 驱动标签与语义；null = 不可用 → '—'
    // 或不填充，绝不画 0。量级条无固定设备额定上限（已改为 no-scale 空态）。
    var kind = store.get('memory_kind') || 'unknown';

    var used = gpu.used, total = gpu.total;
    var vramPct = (gpu.pct != null) ? gpu.pct : 0;
    var vram = $('trVram');
    if (vram) {
      vram.textContent = (used == null) ? '—' : UI.fmtGB(used);
      var seg = vram.closest ? vram.closest('.tr-seg') : null;
      if (seg) {
        var lbl = seg.querySelector('.tr-label');
        if (lbl) lbl.textContent = (kind === 'unified') ? '统一内存' : 'VRAM';
        var unit = seg.querySelector('.tr-unit');
        if (unit) unit.textContent = (total != null) ? ('/ ' + UI.fmtGB(total)) : '';
      }
    }
    setFill('trVramFill', vramPct);

    var utilPct = (util.pct == null) ? null : util.pct;
    var utilEl = $('trUtil');
    if (utilEl) utilEl.textContent = (utilPct == null) ? '—' : UI.fmtNum(Math.round(utilPct));
    setFill('trUtilFill', utilPct || 0);

    var pwr = $('trPwr');
    if (pwr) pwr.textContent = (util.power == null) ? '—' : UI.fmtNum(Math.round(util.power));
    // 无额定上限数据 → 不填充量级条（no-scale 空态），数值仍正常显示
    setFill('trPwrFill', 0);

    var temp = $('trTemp');
    if (temp) temp.textContent = (util.temp == null || util.temp === '') ? '—' : UI.fmtNum(Math.round(util.temp));
    setFill('trTempFill', numPct(util.temp, 90));   // 90°C 通用热上限，非设备额定值

    var clk = $('trClock');
    if (clk) clk.textContent = fmtClock(util.clock);
    setFill('trClockFill', 0);                      // 无额定上限数据 → no-scale
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

    // C 期契约 §C3：后端未暴露可信启动时刻，不把浏览器打开时间算作模型
    // 运行时长 —— 不显示 uptime，而非显示会话时长冒充。

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

    if (!active.length) {
      card.innerHTML =
        '<div class="if-card-hdr">' +
          '<span class="status-dot"></span>' +
          '<span class="if-card-title">无活跃模型</span>' +
          '<span class="if-card-sub">GPU 空闲</span>' +
        '</div>' +
        '<div class="if-card-body" id="ovActiveBody">' +
          '<p class="muted">当前无模型在运行。切换模型请前往模型页。</p>' +
        '</div>' +
        '<div class="if-card-actions">' +
          '<button class="btn btn-sec btn-sm" data-action="goto-inference">' + UI.icon('cube', 14) + '前往模型</button>' +
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

  /* ── C2: 设备遥测（能力门控，契约 §C1）──
   * 平台适配层供数、UI 按能力消费：memory_kind 决定标签，capabilities 门控
   * 行卡，真 0 照实显示，null「暂不可用」，不按 OS 名写死版式；未声明能力
   * 不占默认卡片。采样年龄徽章按 X-IFF-Device-Sample-Age（store 跟踪），
   * >30s 过期，无头「新鲜度未确认」，不由轮询频率反推。 */
  var NA_HTML = '<span class="muted">暂不可用</span>';

  function pressureLabel(lv) {
    if (lv === 1) return '正常';
    if (lv === 2) return '警告';
    if (lv === 3) return '严重';
    return null;
  }

  function teleRow(label, valueHtml) {
    return '<div class="ov-stat"><dt>' + label + '</dt><dd class="mono">' + valueHtml + '</dd></div>';
  }

  function renderTelemetry() {
    var card = $('ovTelemetry');
    if (!card) return;
    var body = $('ovTeleBody');
    var fresh = $('ovTeleFresh');
    if (fresh) {
      var age = store.deviceSampleAge ? store.deviceSampleAge() : null;
      if (age == null) fresh.textContent = '新鲜度未确认';
      else if (age > 30) fresh.textContent = '读数已过期 ' + Math.floor(age) + 's';
      else fresh.textContent = '采样 ' + Math.floor(age) + 's 前';
    }
    if (!body) return;

    var dev = store.get('device');
    var snap = dev && dev.snapshot;
    if (!dev || !snap || typeof snap !== 'object') {
      body.innerHTML = '<p class="muted">设备遥测不可用（旧接口或资料未知）。' +
        '连接与健康状态不受影响。</p>';
      return;
    }
    var caps = (dev && dev.capabilities) || store.get('capabilities') || {};
    var rows = '';
    if (caps.memory) {
      // memory_kind 决定标签；total 无效时只展示有证据的值，不画比例
      var kind = snap.memory_kind;
      var memLabel = (kind === 'unified') ? '统一内存'
        : (kind === 'vram' ? 'VRAM' : '设备内存');
      var used = snap.memory_used_mb, total = snap.memory_total_mb;
      var memVal = (used == null) ? NA_HTML
        : UI.fmtGB(used) + ((total != null && total > 0)
          ? ' <span class="muted">/ ' + UI.fmtGB(total) + ' GB</span>' : '');
      rows += teleRow(memLabel, memVal);
      // 推理预算单列：不是实时剩余量、物理总量或启动承诺
      var budget = snap.memory_budget_mb;
      rows += teleRow('推理预算',
        (budget == null) ? NA_HTML : UI.fmtGB(budget) + ' <span class="muted">GB</span>');
    }
    if (caps.pressure) {
      var pl = pressureLabel(snap.pressure_level);
      rows += teleRow('内存压力', (pl == null) ? NA_HTML : pl);
    }
    if (caps.utilization) {
      var u = snap.utilization_pct;
      rows += teleRow('利用率', (u == null) ? NA_HTML : UI.fmtNum(Math.round(u)) + '%');
    }
    if (caps.power) {
      var p = snap.power_w;
      rows += teleRow('功耗', (p == null) ? NA_HTML : UI.fmtNum(Math.round(p)) + ' <span class="muted">W</span>');
    }
    if (caps.temperature) {
      var t = snap.temperature_c;
      rows += teleRow('温度', (t == null) ? NA_HTML : UI.fmtNum(Math.round(t)) + ' <span class="muted">°C</span>');
    }
    if (caps.clock) {
      var c = snap.clock_mhz;
      rows += teleRow('频率', (c == null) ? NA_HTML : UI.fmtNum(Math.round(c)) + ' <span class="muted">MHz</span>');
    }
    if (!rows) {
      body.innerHTML = '<p class="muted">此设备后端未声明可采集的指标。' +
        '连接与健康状态不受影响。</p>';
      return;
    }
    body.innerHTML = '<dl class="ov-stats ov-tele">' + rows + '</dl>';
  }

  /* ── C2: 态势（契约 §C3）──
   * UTC 当天请求数（token_stats.local/cloud 日桶，桶缺失 = 真 0）、
   * 已保留警告（内存环形缓冲区，非完整日计数）、最近五条（只统计所显示
   * 条数，详情跳 #/requests）、空闲快捷项（history 成功到达的最近三个
   * 去重模型）、切换中指示。system.uptime_seconds 不挂「网关运行时长」。 */
  function utcToday() {
    return new Date().toISOString().slice(0, 10);
  }

  function fmtTimeOfDay(ts) {
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '—';
    return d.toISOString().slice(11, 19);
  }

  function renderSituation() {
    var card = $('ovSituation');
    if (!card) return;
    var body = $('ovSitBody');
    if (!body) return;

    body.innerHTML =
      '<dl class="ov-stats ov-sit">' +
        '<div class="ov-stat"><dt>今日请求 <span class="muted">UTC</span></dt>' +
          '<dd class="mono" id="ovReqToday">—</dd></div>' +
        '<div class="ov-stat"><dt>警告 <span class="muted">已保留</span></dt>' +
          '<dd class="mono" id="ovWarnCount">—</dd></div>' +
        '<div class="ov-stat"><dt>最近请求</dt>' +
          '<dd class="mono">最近 5 条</dd></div>' +
      '</dl>' +
      '<div id="ovSwitchLine" class="muted"></div>' +
      '<div id="ovIdleShortcuts" class="ov-shortcuts"></div>' +
      '<ul class="ov-req5" id="ovRecent5"></ul>';

    // 切换中（switch_target 在途）
    var target = store.get('switch_target');
    var swLine = $('ovSwitchLine');
    if (swLine) swLine.textContent = target ? ('切换中 → ' + target) : '';

    // UTC 当天请求数
    var today = utcToday();
    var stats = store.get('token_stats') || {};
    var n = 0;
    for (var scope in stats) {
      var day = stats[scope] && stats[scope][today];
      if (day && typeof day === 'object') {
        for (var m in day) if (day[m] && day[m].requests) n += day[m].requests;
      }
    }
    var reqEl = $('ovReqToday');
    if (reqEl) reqEl.textContent = UI.fmtNum(n);

    // 已保留警告（severity=warning 环形缓冲）
    var warnEl = $('ovWarnCount');
    fetch('/api/anomalies?severity=warning&limit=100', { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (warnEl) warnEl.textContent = UI.fmtNum(
          (data && data.count != null) ? data.count
            : ((data && data.events && data.events.length) || 0));
      })
      .catch(function () { if (warnEl) warnEl.textContent = '—'; });

    // 空闲快捷项：history 中成功到达、仍可切换的最近三个去重模型
    var sc = $('ovIdleShortcuts');
    if (sc) {
      var active = store.get('active_services') || [];
      if (!active.length) {
        var hist = store.get('history') || [];
        var seen = {}, picks = [];
        for (var i = 0; i < hist.length && picks.length < 3; i++) {
          var h = hist[i] || {};
          var name = h.model || h.name || '';
          var st = String(h.status || '').toLowerCase();
          if (!name || seen[name]) continue;
          if (st && st !== 'ok' && st !== 'success' && st !== 'switched') continue;
          seen[name] = 1;
          picks.push(name);
        }
        sc.innerHTML = picks.length
          ? '<span class="muted">快速切换：</span>' + picks.map(function (p) {
              return '<button class="btn btn-sec btn-sm" data-action="goto-inference" data-model="' + escHtml(p) + '">' + escHtml(p) + '</button>';
            }).join('')
          : '';
      } else {
        sc.innerHTML = '';
      }
    }

    // 最近五条（只统计所显示条数）
    var r5 = $('ovRecent5');
    if (r5) {
      var logs = store.get('request_log') || [];
      var five = logs.slice(0, 5);
      var lis = '';
      for (var j = 0; j < five.length; j++) {
        var r = five[j] || {};
        var st = r.status;
        var cls = (st == null) ? '' : (st >= 500 ? 'crit' : (st >= 400 ? 'warn' : 'ok'));
        lis += '<li class="ov-req-row"><span class="mono">' + escHtml(fmtTimeOfDay(r.timestamp)) + '</span>' +
          ' <span>' + escHtml(r.model || '—') + '</span>' +
          ' <span class="badge' + (cls ? ' ' + cls : '') + '">' + (st == null ? '—' : st) + '</span></li>';
      }
      r5.innerHTML = lis || '<li class="muted">暂无请求记录</li>';
    }
  }

  /* ── 总览页渲染入口 ── */
  function renderOverview() {
    renderActive();
    renderSpark();
    renderTelemetry();
    renderSituation();
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
    var on = (store && typeof store.isGroupActive === 'function')
      ? store.isGroupActive('tab-overview') : (tab === 'tab-overview');
    if (on) { renderRail(); renderOverview(); }
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
            var j0 = await res.json();
            if (j0) msg = j0.error || j0.message || msg;
          } catch (_) {}
          throw new Error(msg);
        }
        // C 期契约 §C3 严格结果判定：200 业务失败不报成功
        var j = null;
        try { j = await res.json(); } catch (_) {}
        if (j && j.status === 'error') {
          throw new Error(j.message || j.error || '操作未成功，请刷新状态确认');
        }
        if (action === 'release' && j && j.status &&
            j.status !== 'switched' && j.status !== 'already_active') {
          // 释放 GPU = switch→idle：未到 idle 不报成功
          throw new Error(j.message || ('释放未完成（' + j.status + '）'));
        }
        if (action === 'reload' && j && j.failed && j.failed.length) {
          UI.toast('配置部分重载失败（' + j.failed.length + ' 项）', 'error');
          store.forceRefresh();
          return;
        }
        var okMsg = op.ok;
        if (action === 'reconcile' && j && j.status) okMsg = op.ok + '（' + j.status + '）';
        UI.toast(okMsg, 'ok');
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
