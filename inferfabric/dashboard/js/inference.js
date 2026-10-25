/* InferFabric Console — Inference tab (v2, Task 6)
 * 模型卡片网格（独占 / 共享 / 空闲 三组）+ 网关控制卡（LRU 缓存开关迁移至此）。
 *
 * 数据源：
 *   - window.store  /api/snapshot 轮询结果
 *       models         — 全量模型清单（name/mode/type/active/quantization/context_window）
 *       sleep_states   — { name: sleep_label }（L2 休眠中的模型）
 *       services_info  — { name: { mode, type, port } }（活跃模型端口等）
 *       services_health— { name: health_str }
 *       local_models   — { cache_enabled, cache_stats: {hits,size,max}|null,
 *                          auto_switch: {enabled, source},
 *                          rate_limit: {mode, server_rpm, model_rpm_default,
 *                                       max_concurrent, global_max_concurrent}|null,
 *                          configured, discovered }（缓存开关 + LRU 生效计数
 *                          + 自动切换 R-AS + 速率限制只读指示 R11b）
 *   - POST /switch | /stop | /sleep | /wake  — 模型生命周期操作（admin header）
 *   - POST /admin/cache/toggle               — LRU 缓存开关（admin header）
 *   - POST /admin/auto-switch/toggle         — 自动切换开关（admin header，立即生效）
 *   - GET  /admin/tune/scenarios?model=      — 场景选择集 + 当前生效（D5：后端直读应用层文件）
 *   - GET  /admin/tune/preview?model=&preset=— 场景差异预览（diff + 超卖/MTP 告警）
 *   - POST /admin/tune {model,preset,restart}— 应用场景并重启（失败自动回滚）
 *       场景徽标（卡内「场景」stat）数据源 = snapshot.status.scenario_active
 *       （manager.status() 直读应用层文件，所有模型卡可见，≡ 容器实际值）；
 *       应用后本地立即刷新该字段，服务端 15s snapshot TTL 内同步且值一致
 *
 * 设计纪律：
 *   - 事件委托（无 inline onclick）；数字等宽 tabular-nums；零 emoji（SVG 图标）
 *   - 破坏性操作（stop）UI.confirm danger；switch/sleep/wake UI.confirm 写明后果
 *   - switch 依赖 store 既有的 switch_target → #switchOverlay 机制（overlay 自动出现）
 *   - cacheHits 展示 LRU 生效（方案 A）：开 → 命中 N 次 · 在用 size/max（snapshot
 *     cache_stats = ResponseCache.stats() 真计数器）；关 → 状态 + 上限 500
 *     （R10 旧契约「后端不暴露命中数」已作废 —— 计数器由后端真实提供，
 *      且缓存命中不再落 RequestLog，命中数成为唯一可见通道）
 *   - rlMeta/rlBadge 速率限制只读指示器（R11b，取代 R11 静态标签）：
 *     数据源 = snapshot local_models.rate_limit（DualGateLimiter.describe()）；
 *     徽章 observe=warn「观察中」/ reject=ok「生效中」；副标题 = 实际生效的
 *     并发排队上限 + RPM 门状态（server_rpm=0 且 model_rpm_default=0 → 「关」）。
 *     刻意不做开关：无按钮/无 action/无 admin 端点——配置在 iff.yaml rate_limit
 *     段，改动需重启 proxy（无热加载），徽章 title 提示这一点
 *   - autoSwitchMeta 展示自动切换来源（R-AS）：env 锁定 / iff.yaml / 默认；
 *     切换经 POST /admin/auto-switch/toggle 立即生效（无需重启 proxy），
 *     env 锁定时 toast 附 hint 提示重启后回到 env 值
 *
 * 暴露：
 *   - window.tabRenderers['tab-inference'] = renderInference
 *   - window.doModelAction(name, action)   — action ∈ switch/stop/sleep/wake
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[inference] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  /* ── 客户端会话级 uptime（同 overview.js：后端未暴露进程启动时间，
   *    此值为"本次页面会话观察到的运行时长"，刷新后归零）。── */
  var _startedAt = {};

  var ENGINE_LABEL = {
    vllm: 'vLLM', sglang: 'SGLang', ninfer: 'NInfer',
    ollama: 'Ollama', ollama_cpp: 'OllamaCpp', comfyui: 'ComfyUI',
    tts: 'TTS', asr: 'ASR',
  };

  function engineLabel(type) {
    return ENGINE_LABEL[type] || (type ? type.charAt(0).toUpperCase() + type.slice(1) : '—');
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function escAttr(s) {
    return escHtml(s);
  }

  function isVllm(type) { return type === 'vllm'; }

  /* 缓存上限（response_cache.py:24 默认 maxsize=500）—— 常量，后端不暴露实时值 */
  var CACHE_MAXSIZE = 500;

  /* ── 分组 ──
   * mode: 'exclusive' → 独占 / 'shared' → 共享 / 'none' → 空闲（CPU-only） */
  function groupModels(models) {
    var excl = [], shrd = [], free = [];
    if (!models) return { exclusive: excl, shared: shrd, none: free };
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      if (!m) continue;
      var mode = m.mode || 'exclusive';
      if (mode === 'shared') shrd.push(m);
      else if (mode === 'none') free.push(m);
      else excl.push(m);
    }
    return { exclusive: excl, shared: shrd, none: free };
  }

  /* ── 单张模型卡 ── */
  function modelCard(m, info, sleepState, scenarioActive) {
    var name = m.name || '';
    var type = (info && info.type) || m.type || '';
    var port = info && info.port ? info.port : null;
    var sleeping = !!sleepState;
    var active = !!m.active && !sleeping;

    // 场景徽标（D5：数据源 = snapshot.status.scenario_active，直读应用层文件 ≡ 容器值）
    // default（= 模型 YAML 当前值）灰显；非 default 场景琥珀色
    var scn = scenarioActive || 'default';
    var scnCls = scn === 'default' ? '' : ' warn';

    // 客户端 uptime
    var now = Date.now();
    if (!sleeping && active) {
      if (!_startedAt[name]) _startedAt[name] = now;
    } else {
      delete _startedAt[name];
    }
    var upSecs = _startedAt[name] ? Math.max(0, Math.floor((now - _startedAt[name]) / 1000)) : 0;

    // 状态点 + 文本
    var dotCls, stateTxt;
    if (active) { dotCls = 'ok'; stateTxt = 'running'; }
    else if (sleeping) { dotCls = 'warn'; stateTxt = 'sleeping ' + String(sleepState).toUpperCase(); }
    else { dotCls = ''; stateTxt = 'stopped'; }

    // 操作按钮（按状态可用性显隐）
    var actions = '';
    var nm = escAttr(name);
    if (active) {
      actions += '<button type="button" class="btn btn-danger btn-sm" data-action="stop" data-model="' + nm + '">' +
        UI.icon('stop', 14) + 'Stop</button>';
      if (isVllm(type)) {
        actions += '<button type="button" class="btn btn-sec btn-sm" data-action="sleep" data-model="' + nm + '">' +
          UI.icon('sleep', 14) + 'Sleep</button>';
      }
    } else if (sleeping) {
      actions += '<button type="button" class="btn btn-sec btn-sm" data-action="wake" data-model="' + nm + '">' +
        UI.icon('wake', 14) + 'Wake</button>';
      actions += '<button type="button" class="btn btn-danger btn-sm" data-action="stop" data-model="' + nm + '">' +
        UI.icon('stop', 14) + 'Stop</button>';
    } else {
      actions += '<button type="button" class="btn btn-pri btn-sm" data-action="switch" data-model="' + nm + '">' +
        UI.icon('play', 14) + '启动</button>';
    }
    // 场景控件：所有卡可见（default 也是合法目标——手改 YAML 后重启生效；
    // 无预设的模型打开弹窗只有 default 一项，引擎不支持时预览会明确报错）
    actions += '<button type="button" class="btn btn-sec btn-sm" data-action="scenario" data-model="' + nm + '">' +
      UI.icon('gear', 14) + '场景</button>';

    var quant = m.quantization ? escHtml(m.quantization) : '';
    var portHtml = (port != null && active)
      ? '<div class="inf-stat"><dt>端口</dt><dd class="mono">:' + escHtml(port) + '</dd></div>'
      : '';

    return '' +
      '<div class="if-card inf-card">' +
        '<div class="if-card-hdr">' +
          '<span class="status-dot ' + dotCls + '" title="' + escHtml(stateTxt) + '"></span>' +
          '<span class="if-card-title">' + escHtml(name) + '</span>' +
          '<span class="badge">' + escHtml(engineLabel(type)) + '</span>' +
        '</div>' +
        '<div class="if-card-body">' +
          '<dl class="inf-stats">' +
            portHtml +
            '<div class="inf-stat"><dt>uptime</dt><dd class="mono">' + (active ? UI.fmtDur(upSecs) : '—') + '</dd></div>' +
            (quant ? '<div class="inf-stat"><dt>精度</dt><dd class="mono">' + quant + '</dd></div>' : '') +
            '<div class="inf-stat"><dt>场景</dt><dd class="mono inf-scenario' + scnCls + '" title="' +
              escHtml(scn === 'default' ? 'default（= 模型 YAML 当前值）' : '已应用场景 ' + scn) + '">' +
              escHtml(scn) + '</dd></div>' +
          '</dl>' +
        '</div>' +
        '<div class="if-card-actions">' + actions + '</div>' +
      '</div>';
  }

  /* ── 渲染一个分组 ── */
  function renderGroup(groupEl, countEl, listEl, models, sleepStates, servicesInfo, scenarioActive) {
    if (!groupEl || !listEl || !countEl) return;

    if (models === undefined) {
      // 快照未到达 → skeleton
      countEl.textContent = '—';
      listEl.innerHTML = '<div class="skeleton"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';
      groupEl.style.display = '';
      return;
    }

    countEl.textContent = String(models.length);

    if (!models.length) {
      // 空态：隐藏整组（DOM 保留供测试 / 后续填充）
      groupEl.style.display = 'none';
      listEl.innerHTML = '';
      return;
    }

    groupEl.style.display = '';
    var html = '';
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      var info = (servicesInfo && servicesInfo[m.name]) || null;
      var sleep = (sleepStates && sleepStates[m.name]) || null;
      var scn = scenarioActive && scenarioActive[m.name];
      html += modelCard(m, info, sleep, scn || 'default');
    }
    listEl.innerHTML = html;
  }

  /* ── 网关控制卡：缓存状态 + 自动切换 + 速率限制 ── */
  function renderGateway() {
    var lm = store.get('local_models') || {};
    var cacheOn = !!lm.cache_enabled;

    // #cacheHits — LRU 生效可见性（方案 A）：真计数器来自 snapshot
    // local_models.cache_stats（ResponseCache.stats()：命中次数 / 在用条目
    // / 上限）。缓存关闭或字段缺失时回退「状态 + 上限」信息性标签。
    var stats = (lm.cache_stats && typeof lm.cache_stats === 'object') ? lm.cache_stats : null;
    var cacheLabel = $('cacheHits');
    if (cacheLabel) {
      if (cacheOn && stats) {
        cacheLabel.textContent = 'LRU 缓存 · 开 · 命中 ' + UI.fmtNum(stats.hits || 0) +
          ' 次 · 在用 ' + (stats.size || 0) + '/' + (stats.max || CACHE_MAXSIZE) + ' 条';
      } else {
        cacheLabel.textContent = 'LRU 缓存 · ' + (cacheOn ? '开' : '关') + ' · 上限 ' + CACHE_MAXSIZE + ' 条';
      }
    }

    // #cacheToggle — 按钮文本随状态翻转
    var tog = $('cacheToggle');
    if (tog) {
      tog.textContent = cacheOn ? '关闭' : '开启';
      tog.setAttribute('aria-pressed', String(cacheOn));
      tog.classList.toggle('btn-pri', !cacheOn);
      tog.classList.toggle('btn-sec', cacheOn);
    }

    // #autoSwitchMeta / #autoSwitchToggle — R-AS 自动切换（snapshot local_models.auto_switch）
    // source: env=环境变量锁定（重启后回到 env 值）/ file=iff.yaml / default=默认开
    var as = (lm.auto_switch && typeof lm.auto_switch === 'object') ? lm.auto_switch : null;
    var asOn = !!(as && as.enabled);
    var asSrcLabel = { env: 'env 锁定', file: 'iff.yaml', default: '默认' }[as ? as.source : ''] || '默认';
    var asMeta = $('autoSwitchMeta');
    if (asMeta) {
      asMeta.textContent = '自动切换 · ' + (asOn ? '开' : '关') + ' · 来源 ' + asSrcLabel;
    }
    var asTog = $('autoSwitchToggle');
    if (asTog) {
      asTog.textContent = asOn ? '关闭' : '开启';
      asTog.setAttribute('aria-pressed', String(asOn));
      asTog.classList.toggle('btn-pri', !asOn);
      asTog.classList.toggle('btn-sec', asOn);
    }

    // #rlMeta/#rlBadge — R11b：速率限制只读指示器（snapshot local_models.rate_limit）
    // 徽章：observe=warn「观察中」（超限只记日志）/ reject=ok「生效中」（超限 429）；
    // 副标题：实际生效的并发排队上限 + RPM 门状态。字段缺失（旧 proxy/异常）→ 回退
    // 静态标签「配置见 iff.yaml」。刻意无按钮/action：配置改动走 iff.yaml + 重启。
    var rlData = (lm.rate_limit && typeof lm.rate_limit === 'object') ? lm.rate_limit : null;
    var rlMeta = $('rlMeta');
    var rlBadge = $('rlBadge');
    var rlBadgeText = $('rlBadgeText');
    if (!rlData) {
      // 字段缺失（旧 proxy / 异常）→ 回退静态标签（不白屏）
      if (rlMeta) rlMeta.textContent = '配置见 iff.yaml';
      if (rlBadge) rlBadge.className = 'badge';
      if (rlBadgeText) rlBadgeText.textContent = '—';
    } else {
      // RPM 门：server_rpm 优先（整站门）；=0 时看 model_rpm_default（每模型门）；都 0 → 关
      // （global_max_concurrent 为 describe() 预留契约字段，当前 UI 不渲染，勿误删）
      var rpmText = rlData.server_rpm > 0 ? '每分钟限流 ≤ ' + rlData.server_rpm + ' 次'
        : (rlData.model_rpm_default > 0 ? '每分钟限流 ≤ ' + rlData.model_rpm_default + ' 次（每模型）'
                                        : '每分钟限流 关');
      var concText = (rlData.max_concurrent > 0) ? '并发排队 ' + rlData.max_concurrent + ' 路' : '并发排队 关';
      if (rlMeta) rlMeta.textContent = concText + ' · ' + rpmText;
      var isReject = rlData.mode === 'reject';
      if (rlBadge) rlBadge.className = 'badge ' + (isReject ? 'ok' : 'warn');
      if (rlBadgeText) rlBadgeText.textContent = isReject ? '生效中' : '观察中';
    }
  }

  /* ── 清理已不活跃模型的 uptime 记录 ── */
  function pruneUptime(models) {
    if (!models) return;
    var live = {};
    for (var i = 0; i < models.length; i++) {
      if (models[i]) live[models[i].name] = true;
    }
    for (var k in _startedAt) {
      if (!live[k]) delete _startedAt[k];
    }
  }

  /* ── 渲染入口 ── */
  function renderInference() {
    var models = store.get('models');
    var sleepStates = store.get('sleep_states') || {};
    var servicesInfo = store.get('services_info') || {};
    // D5：场景徽标数据源 = snapshot.status.scenario_active（后端直读应用层文件）
    var status = store.get('status') || {};
    var scenarioActive = status.scenario_active || {};

    pruneUptime(models);

    var g = groupModels(models);

    renderGroup($('infExclGroup'), $('infExclCount'), $('infExclList'),
                g.exclusive, sleepStates, servicesInfo, scenarioActive);
    renderGroup($('infShrdGroup'), $('infShrdCount'), $('infShrdList'),
                g.shared, sleepStates, servicesInfo, scenarioActive);
    renderGroup($('infFreeGroup'), $('infFreeCount'), $('infFreeList'),
                g.none, sleepStates, servicesInfo, scenarioActive);

    renderGateway();
  }

  window.renderInference = renderInference;
  window.tabRenderers['tab-inference'] = renderInference;

  /* ── 订阅：snapshot 到达时刷新（仅推理页可见时） ── */
  function isInferenceActive() {
    var el = $('tab-inference');
    return !!(el && el.classList.contains('active'));
  }

  store.on('sync_meta', function () { if (isInferenceActive()) renderInference(); });
  store.on('tab_active', function (tab) {
    if (tab === 'tab-inference') renderInference();
  });

  /* ── 模型操作：switch / stop / sleep / wake ── */
  window.doModelAction = async function (name, action) {
    if (!name) { UI.toast('缺少模型名', 'error'); return; }

    var OPS = {
      switch: {
        url: '/switch',
        confirm: {
          title: '切换模型',
          body: '将切换到 ' + name + '。这是一项重型操作，可能需要数十秒加载权重。确定？',
        },
        ok: name + ' 已切换',
      },
      stop: {
        url: '/stop',
        confirm: {
          title: '停止模型',
          body: '将停止 ' + name + '，进程立即终止。确定？',
          danger: true,
        },
        ok: name + ' 已停止',
      },
      sleep: {
        url: '/sleep',
        confirm: {
          title: '休眠模型',
          body: '将休眠 ' + name + '（L2：丢弃 vLLM 权重释放显存，唤醒需 3-6s 重新加载）。确定？',
        },
        ok: name + ' 休眠中',
      },
      wake: {
        url: '/wake',
        confirm: {
          title: '唤醒模型',
          body: '将唤醒 ' + name + '（需 3-6s 重新加载权重）。确定？',
        },
        ok: name + ' 唤醒中',
      },
    };

    var op = OPS[action];
    if (!op) { UI.toast('未知操作: ' + action, 'error'); return; }

    var run = async function () {
      try {
        var res = await fetch(op.url, {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({ model: name }),
        });
        if (!res.ok) {
          var msg = 'HTTP ' + res.status;
          try {
            var j = await res.json();
            if (j) msg = j.error || j.message || msg;
          } catch (_) {}
          throw new Error(msg);
        }
        var message = op.ok;
        if (action === 'switch') {
          var result = await res.json();
          var status = result && result.status;
          if (status !== 'switched' && status !== 'already_active') {
            throw new Error((result && (result.error || result.message)) || '切换结果无效，请刷新状态确认');
          }
          if (result.rollback === 'succeeded') {
            store.forceRefresh();
            throw new Error('切换到 ' + name + ' 失败，已恢复 ' + (result.model || '原模型'));
          }
          if (result.model && result.model !== name) {
            store.forceRefresh();
            throw new Error('切换未完成：当前模型为 ' + result.model);
          }
          if (status === 'already_active') message = name + ' 已在运行';
        }
        UI.toast(message, 'ok');
        store.forceRefresh();
      } catch (e) {
        UI.toast('操作失败: ' + (e && e.message ? e.message : e), 'error');
      }
    };

    if (op.confirm) {
      UI.confirm({
        title: op.confirm.title,
        body: op.confirm.body,
        danger: !!op.confirm.danger,
        onOk: run,
      });
    } else {
      run();
    }
  };

  /* ── 缓存开关切换 ── */
  async function toggleCache() {
    try {
      var res = await fetch('/admin/cache/toggle', {
        method: 'POST',
        headers: UI.adminHeaders(),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var j = await res.json();
      UI.toast(j && j.cache_enabled ? '缓存已开启' : '缓存已关闭', 'ok');
      store.forceRefresh();
    } catch (e) {
      UI.toast('缓存切换失败: ' + (e && e.message ? e.message : e), 'error');
    }
  }

  /* ── 自动切换开关（R-AS：立即生效，无需重启 proxy） ── */
  async function toggleAutoSwitch() {
    try {
      var res = await fetch('/admin/auto-switch/toggle', {
        method: 'POST',
        headers: UI.adminHeaders(),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var j = await res.json();
      var msg = j && j.auto_switch ? '自动切换已开启' : '自动切换已关闭';
      if (j && j.hint) UI.toast(msg + '（' + j.hint + '）', 'info');
      else UI.toast(msg, 'ok');
      store.forceRefresh();
    } catch (e) {
      UI.toast('自动切换切换失败: ' + (e && e.message ? e.message : e), 'error');
    }
  }

  /* ── 场景控件：选择 → 差异预览 → 应用并重启（POST /admin/tune） ──
   * D4：场景变更走进程内 API（写应用层 + 触发重启由 proxy 编排），CLI 侧
   * 写同一应用层文件、经文件锁互斥。应用后直接刷新本地 scenario_active
   *（服务端 snapshot 15s TTL 内同步，值一致无闪烁）。
   * D5：选择集/当前值来自 /admin/tune/scenarios（后端直读应用层文件）。
   */
  function fmtVal(v) {
    if (v === true) return 'on';
    if (v === false) return 'off';
    return v == null ? '—' : String(v);
  }

  function openScenarioModal(name) {
    var host = document.createElement('div');
    host.className = 'scn-modal-host';
    host.innerHTML =
      '<div class="if-modal-backdrop">' +
        '<div class="if-modal scn-modal" role="dialog" aria-modal="true">' +
          '<div class="if-modal-title"></div>' +
          '<div class="scn-select-row">' +
            '<select class="if-field-select scn-select" disabled></select>' +
            '<button type="button" class="btn btn-pri btn-sm scn-preview-btn" disabled>预览差异</button>' +
          '</div>' +
          '<div class="scn-preview" hidden></div>' +
          '<div class="if-modal-actions">' +
            '<button type="button" class="btn btn-sec btn-sm scn-cancel">取消</button>' +
            '<button type="button" class="btn btn-pri btn-sm scn-apply" hidden>应用并重启</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(host);

    var sel = host.querySelector('.scn-select');
    var previewBtn = host.querySelector('.scn-preview-btn');
    var previewBox = host.querySelector('.scn-preview');
    var applyBtn = host.querySelector('.scn-apply');
    host.querySelector('.if-modal-title').textContent = '场景预设 · ' + name;

    function close() {
      document.removeEventListener('keydown', onKey);
      host.remove();
    }
    function onKey(ev) { if (ev.key === 'Escape') close(); }

    function adminGet(url) {
      return fetch(url, { headers: UI.adminHeaders() })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); });
    }

    /* 阶段 1：场景选择集 + 当前生效（D5：后端直读应用层文件） */
    adminGet('/admin/tune/scenarios?model=' + encodeURIComponent(name))
      .then(function (res) {
        if (!res.ok) {
          UI.toast('获取场景列表失败: ' + (res.j && res.j.error ? res.j.error : 'HTTP ' + res.status), 'error');
          close();
          return;
        }
        var j = res.j || {};
        var choices = j.choices || [];
        sel.innerHTML = choices.map(function (c) {
          var label = c === 'default' ? 'default (= model.yaml)' : c;
          return '<option value="' + escAttr(c) + '"' + (c === j.active ? ' selected' : '') + '>' +
            escHtml(label) + '</option>';
        }).join('');
        sel.disabled = false;
        previewBtn.disabled = false;
      })
      .catch(function (e) {
        UI.toast('获取场景列表失败: ' + (e && e.message ? e.message : e), 'error');
        close();
      });

    /* 阶段 2：差异预览（diff + 超卖/MTP 告警） */
    function showPreview() {
      var preset = sel.value;
      if (!preset) return;
      previewBox.innerHTML = '<div class="scn-issue">加载预览中…</div>';
      previewBox.hidden = false;
      applyBtn.hidden = true;
      adminGet('/admin/tune/preview?model=' + encodeURIComponent(name) +
               '&preset=' + encodeURIComponent(preset))
        .then(function (res) {
          if (!res.ok) {
            previewBox.innerHTML = '<div class="scn-issue">预览失败: ' +
              escHtml(res.j && res.j.error ? res.j.error : 'HTTP ' + res.status) + '</div>';
            return;
          }
          var p = res.j || {};
          var html = '';
          var after = p.after || {};
          var before = p.before || {};
          for (var k in after) {
            var b = before[k], a = after[k];
            var changed = String(b) !== String(a);
            html += '<div class="scn-diff-row' + (changed ? '' : ' unchanged') + '">' +
              '<span class="scn-diff-k">' + escHtml(k) + '</span>' +
              '<span>' + escHtml(fmtVal(b)) + (changed ? ' → ' + escHtml(fmtVal(a)) : '') + '</span>' +
            '</div>';
          }
          (p.issues || []).forEach(function (i) {
            html += '<div class="scn-issue">' + escHtml(i) + '</div>';
          });
          (p.clamp_notes || []).forEach(function (n) {
            html += '<div class="scn-issue">⚙ ' + escHtml(n) + '</div>';
          });
          html += '<div class="scn-warn">应用将重启 ' + escHtml(name) + '（NInfer 约 3-6s），' +
            '在途请求短暂 503；启动失败自动回滚到上一状态。</div>';
          previewBox.innerHTML = html;
          applyBtn.hidden = false;
        })
        .catch(function (e) {
          previewBox.innerHTML = '<div class="scn-issue">预览失败: ' + escHtml(e && e.message ? e.message : e) + '</div>';
        });
    }

    /* 阶段 3：应用并重启（POST /admin/tune，失败自动回滚由后端完成） */
    function applyScenario() {
      var preset = sel.value;
      if (!preset) return;
      applyBtn.disabled = true;
      fetch('/admin/tune', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({ model: name, preset: preset, restart: true }),
      })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
        .then(function (res) {
          var j = res.j || {};
          if (res.ok) {
            // 本地徽标立即刷新（响应含最终 active_preset；服务端 15s 内同步且值一致）
            var st = store.get('status') || {};
            st.scenario_active = st.scenario_active || {};
            st.scenario_active[name] = j.active_preset || 'default';
            store.set('status', st);
            var msgs = {
              applied: name + ' 场景 ' + preset + ' 已应用并重启',
              restarted: name + ' 已重启到 default（= 模型 YAML 当前值）',
              rolled_back: name + ' 已回滚到 default（= 模型 YAML 值）',
              already_default: '当前已在 default（= 模型 YAML 值），无需操作',
              applied_restart_pending: '场景 ' + preset + ' 已写入应用层，重启后生效',
              rolled_back_pending: '已回滚到模型 YAML 值，重启后生效',
              failed_rolled_back: '重启失败，已回滚到上一状态' + (j.error ? '：' + j.error : ''),
              failed_rollback_failed: '重启失败且回滚也失败，请人工检查' + (j.error ? '：' + j.error : ''),
              failed_rollback_error: '回滚出错' + (j.error ? '：' + j.error : ''),
            };
            var bad = /^failed/.test(j.status);
            UI.toast(msgs[j.status] || ('未知状态: ' + j.status), bad ? 'error' : (j.status === 'already_default' ? 'info' : 'ok'));
            close();
            store.forceRefresh();
          } else {
            UI.toast('应用失败: ' + (j.error ? j.error : 'HTTP ' + res.status), 'error');
            applyBtn.disabled = false;
          }
        })
        .catch(function (e) {
          UI.toast('应用失败: ' + (e && e.message ? e.message : e), 'error');
          applyBtn.disabled = false;
        });
    }

    previewBtn.addEventListener('click', showPreview);
    applyBtn.addEventListener('click', applyScenario);
    host.querySelector('.scn-cancel').addEventListener('click', close);
    host.querySelector('.if-modal-backdrop').addEventListener('click', function (ev) {
      if (ev.target === ev.currentTarget) close();
    });
    document.addEventListener('keydown', onKey);
  }

  /* ── 事件委托（无 inline onclick） ── */
  var root = $('tab-inference');
  if (root) {
    root.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-action]');
      if (!btn || !root.contains(btn) || btn.disabled) return;
      var act = btn.getAttribute('data-action');

      if (act === 'cache-toggle') {
        toggleCache();
        return;
      }
      if (act === 'auto-switch-toggle') {
        toggleAutoSwitch();
        return;
      }
      if (act === 'goto-deploy') {
        store.switchTab('tab-deploy');
        return;
      }
      // 场景控件：所有模型卡可见（default 也是合法目标）
      if (act === 'scenario') {
        var scnModel = btn.getAttribute('data-model');
        if (scnModel) openScenarioModal(scnModel);
        return;
      }
      // 模型操作：switch / stop / sleep / wake
      var model = btn.getAttribute('data-model');
      if (model && window.doModelAction) {
        window.doModelAction(model, act);
      }
    });
  }
})();
