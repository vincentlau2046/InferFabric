/* InferFabric Console — Cloud TAB (v2.1, 模型策展)
 * 预设厂商网格（9 项，点击展开内联 API Key 表单）+ Provider 表
 * （行内 测试/发现/模型/删除，模型 = 行下内联策展面板）
 * + 手动配置表单 + 可路由模型列表。
 *
 * 策展语义（v6.1 Phase 1 白名单制）：
 *   - 发现候选 = 上游 /models 快照，默认不勾、不路由 → 注销不路由
 *   - 勾选 = 可路由；取消 = 隐藏；手填 = 新增（恒可路由）
 *   - 操作即生效：勾选/移除/添加即时 POST，无「保存」按钮
 *
 * 数据源（全部 /admin/cloud/*，UI.adminHeaders()）：
 *   - GET    /admin/cloud/presets    — {presets:[…]}
 *   - GET    /admin/cloud/providers  — {providers:[{name,enabled,openai_base,anthropic_base,
 *            discovery_enabled,discovery_interval,enabled_models,model_specs:[{id,manual}],
 *            candidates,routable_count,key_env_var,key_env_set,preset_id}], models:[…],
 *            total_cloud_models, last_discovery}
 *   - POST   /admin/cloud/providers  — 预设 {preset, api_key}；手动 {name, api_key, openai_base, anthropic_base}
 *   - POST   /admin/cloud/provider-models — {provider, action: enable|disable|add|remove, model}
 *   - DELETE /admin/cloud/providers  — {name}
 *   - POST   /admin/cloud/test       — {url(base 去尾斜杠, 不拼 /models), api_key, protocol, probe_model?}
 *                                       → {status, pass, message, ...}（pass = 可添加门禁）
 *   - POST   /admin/cloud/provider-test — {provider, probe_model?}（用服务端已存 key 测已注册 provider）
 *   - POST   /admin/cloud/discover   — 全部 {}；单个 {provider}
 *   - POST   /admin/cloud/reload     — {}
 *
 * 设计纪律（R13 / spec §4.5 §7 / v2 token 延伸）：
 *   - 零 emoji；状态全部 SVG 图标 + 文本标签双编码，绝不单独用颜色
 *   - 数字等宽 tabular-nums；无 inline onclick；无自制伪 checkbox（原生控件）
 *   - 面板为行下内联展开（非模态）：curate 对象是高亮的 provider 行，上下文不丢
 *   - 空态给动作指路，不空悲叹
 *
 * 暴露：
 *   - window.tabRenderers['tab-cloud'] = renderCloud；window.renderCloud
 *   - window.doCloudAdd() / doCloudAddPreset() / doCloudTest(opts) / doCloudDiscover() / doCloudDelete() / doCloudReload()
 *       doCloudTest(opts)：opts={base,key,protocol,probeModel}，返回 Promise<{ok,pass,data,error}>
 *   - window.doCpmToggle() / doCpmAdd() / doCpmRemove() / doCpmRefresh()
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[cloud] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  var _presets = [];          // 预设缓存（GET /admin/cloud/presets）
  var _selectedPreset = null; // 当前选中预设
  var _providers = [];        // provider 缓存（行内 测试 需查 openai_base）
  var _openProvider = null;   // 当前展开策展面板的 provider 名（一次只开一个）
  var _manualTestOk = false;  // 手动表单测试通过门禁（R1/R2：通过才放行"添加"）
  var _presetTestOk = false;  // 预设表单测试通过门禁
  // R3：发现后自动展开策展面板。'first' = 首个有候选的 provider；provider 名 = 该行有候选才展开。
  // 由刷新后的候选数据判定（发现响应的 cloud_models 是可路由注册表计数，≠ 候选数，不能作门控）。
  var _autoOpen = null;
  // 行内"测试"列状态（R4）：provider 名 → {pass, warn, ts, msg}；sessionStorage 持久化（刷新不丢）
  var _testStatus = (function () {
    try { var s = sessionStorage.getItem('iff.cloud.teststatus'); return s ? JSON.parse(s) : {}; }
    catch (e) { return {}; }
  })();
  function saveTestStatus() {
    try { sessionStorage.setItem('iff.cloud.teststatus', JSON.stringify(_testStatus)); } catch (e) {}
  }
  function fmtTime(ts) {
    var d = new Date(ts), p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }

  /* ── XSS-safe 转义（与 inference.js escHtml 一致） ── */
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── 预设 monogram（R13：icon 字段是 emoji 不渲染；
   *    拉丁名取前 2 字符大写，CJK 名取首字符） ── */
  function monogram(displayName) {
    var n = String(displayName || '').trim();
    if (!n) return '·';
    if (n.charCodeAt(0) < 128) return n.slice(0, 2).toUpperCase();
    return n.charAt(0);
  }

  function icon(name) { return UI.icon(name, 12); }

  /* ── 按钮 busy 态（provider 行按钮按 data-provider 精确定位） ──
   *   provider 为用户输入（手动 provider 名），不得拼进 CSS 选择器——
   *   名字含 " / [ / \ 会使 querySelectorAll 抛错（review fix#1）。
   *   改为按 data-action 取候选（action 为代码内固定字面量，安全），
   *   再 getAttribute 比较 data-provider，彻底避免注入。 */
  function setBtnBusy(action, busy, provider) {
    var els = document.querySelectorAll('[data-action="' + action + '"]');
    for (var i = 0; i < els.length; i++) {
      if (provider && els[i].getAttribute('data-provider') !== provider) continue;
      els[i].disabled = busy;
    }
  }

  /* ══ 预设：GET /admin/cloud/presets → #presetGrid ══ */
  async function cloudLoadPresets() {
    var grid = $('presetGrid');
    if (!grid) return;
    // 首次加载（无缓存）→ skeleton（spec §5 全 TAB 骨架；重取不闪）
    if (!_presets.length) UI.skeleton(grid, 3);
    try {
      var res = await fetch('/admin/cloud/presets', { headers: UI.adminHeaders() });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        grid.innerHTML = '<div class="if-empty">加载失败 · ' +
          esc((d && d.error) || ('HTTP ' + res.status)) + '</div>';
        return;
      }
      _presets = d.presets || [];
      if (!_presets.length) {
        grid.innerHTML = '<div class="if-empty">无预设 — 用下方手动配置添加 Provider</div>';
        return;
      }
      var html = '';
      _presets.forEach(function (p) {
        // R13：预设的 icon 字段是 emoji，不渲染 → 文本 monogram 徽章
        var proto = [];
        if (p.openai_base) proto.push('OpenAI');
        if (p.anthropic_base) proto.push('Anthropic');
        var meta = proto.join(' · ');
        if (p.model_count != null) meta += (meta ? ' · ' : '') + p.model_count + ' 模型';
        var sel = _selectedPreset && _selectedPreset.id === p.id;
        html +=
          '<div class="cp-preset' + (sel ? ' selected' : '') + '" role="button" tabindex="0" ' +
            'aria-pressed="' + (sel ? 'true' : 'false') + '" ' +
            'data-action="preset-select" data-preset="' + esc(p.id) + '">' +
            '<span class="cp-mono" aria-hidden="true">' + esc(monogram(p.display_name)) + '</span>' +
            '<span class="cp-preset-body">' +
              '<span class="cp-preset-name">' + esc(p.display_name) + '</span>' +
              '<span class="cp-preset-meta">' + esc(meta) + '</span>' +
            '</span>' +
          '</div>';
      });
      grid.innerHTML = html;
    } catch (e) {
      grid.innerHTML = '<div class="if-empty">加载失败 · ' + esc(e.message || e) + '</div>';
    }
  }

  function cloudSelectPreset(id) {
    var p = null;
    for (var i = 0; i < _presets.length; i++) {
      if (_presets[i].id === id) { p = _presets[i]; break; }
    }
    if (!p) return;
    _selectedPreset = p;
    var grid = $('presetGrid');
    if (grid) {
      grid.querySelectorAll('.cp-preset').forEach(function (c) {
        var isSel = c.getAttribute('data-preset') === id;
        c.classList.toggle('selected', isSel);
        c.setAttribute('aria-pressed', isSel ? 'true' : 'false');
      });
    }
    var form = $('presetForm');
    if (form) form.style.display = '';
    var label = $('presetSelected');
    if (label) label.textContent = p.display_name || p.id;
    var key = $('cpPresetApiKey');
    if (key) { key.value = ''; key.focus(); }
    var hint = $('presetEnvHint');
    if (hint) {
      var env = p.env_var || ('IFF_' + String(p.id).toUpperCase().replace(/-/g, '_') + '_KEY');
      hint.textContent = 'Key 将存为 ' + env + '，写入 ~/.inferfabric/secrets.env';
    }
    // 换预设 = 未测试：重置门禁 + 结果条
    _presetTestOk = false;
    setBtnBusy('preset-add', true);
    resetTestResult('cpPresetTestResult', '先点「测试」验证 Key 与连接，通过后方可添加');
  }

  function cloudDeselectPreset() {
    _selectedPreset = null;
    var grid = $('presetGrid');
    if (grid) {
      grid.querySelectorAll('.cp-preset').forEach(function (c) {
        c.classList.remove('selected');
        c.setAttribute('aria-pressed', 'false');
      });
    }
    var form = $('presetForm');
    if (form) form.style.display = 'none';
    _presetTestOk = false;
    setBtnBusy('preset-add', true);
    resetTestResult('cpPresetTestResult', '先点「测试」验证 Key 与连接，通过后方可添加');
  }

  /* ══ 预设添加：POST /admin/cloud/providers {preset, api_key} ══ */
  window.doCloudAddPreset = async function () {
    if (!_selectedPreset) { UI.toast('请先选择预设', 'error'); return; }
    var keyEl = $('cpPresetApiKey');
    var apiKey = keyEl ? keyEl.value.trim() : '';
    if (!apiKey) {
      UI.toast('请填写 API Key', 'error');
      if (keyEl) keyEl.focus();
      return;
    }
    if (!_presetTestOk) {
      UI.toast('请先点「测试」验证连接与 Key，通过后方可添加', 'error');
      return;
    }
    var preset = _selectedPreset;
    try {
      var res = await fetch('/admin/cloud/providers', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({ preset: preset.id, api_key: apiKey }),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('添加失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('Provider ' + (preset.display_name || preset.id) + ' 已添加', 'success');
      if (preset.discovery) {
        // doCloudDiscover 内部 finally 会刷新 Provider 表（无论发现成败，review fix#2）
        await window.doCloudDiscover(preset.id);
      } else {
        await cloudLoadProviders();
      }
      cloudDeselectPreset();
    } catch (e) {
      UI.toast('添加失败: ' + (e.message || e), 'error');
    }
  };

  /* ── Provider 表加载骨架（UI.skeleton 同款 .skeleton/.skeleton-row，逐行占位；
   *    与 anomaly.js showSkeleton 同模式：detached div 生成后取 innerHTML 入 <tr>） ── */
  function providerSkeleton() {
    var tbody = $('provTbody');
    if (!tbody) return;
    var html = '';
    for (var i = 0; i < 3; i++) {
      var cell = document.createElement('div');
      UI.skeleton(cell, 1);
      html += '<tr class="cp-skel"><td colspan="7">' + cell.innerHTML + '</td></tr>';
    }
    tbody.innerHTML = html;
  }

  /* ══ Provider 表：GET /admin/cloud/providers → #provTable + #cloudModels ══ */
  function providerRow(p) {
    var stOn = !!p.enabled;
    // 状态：SVG 图标 + 文本标签双编码（spec §7：不得仅用颜色）
    var stHtml = stOn
      ? '<span class="cp-st on">' + icon('check') + '已启用</span>'
      : '<span class="cp-st off">' + icon('xmark') + '已禁用</span>';
    var oi = !!p.openai_base;
    var ai = !!p.anthropic_base;
    var protoHtml =
      '<span class="cp-chips">' +
        '<span class="cp-chip ' + (oi ? 'on' : 'off') + '">' + icon(oi ? 'check' : 'xmark') + 'OpenAI</span>' +
        '<span class="cp-chip ' + (ai ? 'on' : 'off') + '">' + icon(ai ? 'check' : 'xmark') + 'Anthropic</span>' +
      '</span>';
    // 可路由数 + 候选/发现状态（数字等宽 tabular-nums）
    var sub = p.discovery_enabled
      ? '<span class="cp-sub">候选 ' + UI.fmtNum((p.candidates || []).length) + '</span>'
      : '<span class="cp-sub">发现关闭</span>';
    var countHtml =
      '<div class="cp-cell"><span class="cp-num mono">' + UI.fmtNum(p.routable_count || 0) + '</span>' +
      sub + '</div>';
    // Key 状态：icon + 已设置/未设置 + 变量名
    var keyOn = !!p.key_env_set;
    var keyHtml =
      '<div class="cp-cell">' +
        '<span class="cp-key ' + (keyOn ? 'on' : 'off') + '">' + icon('key') + (keyOn ? '已设置' : '未设置') + '</span>' +
        (p.key_env_var ? '<span class="cp-sub">' + esc(p.key_env_var) + '</span>' : '') +
      '</div>';
    var nameAttr = esc(p.name);
    // "测试"列（R4）：读 _testStatus；未测试(灰点) / 通过(check+时间) / 警告(alert) / 失败(xmark+短msg)
    var tst = _testStatus[p.name];
    var testHtml;
    if (!tst) {
      testHtml = '<div class="cp-cell"><span class="cp-test untested">· 未测试</span></div>';
    } else if (tst.warn) {
      testHtml = '<div class="cp-cell"><span class="cp-test warn">' + icon('alert') + '警告</span>' +
        (tst.msg ? '<span class="cp-sub">' + esc(tst.msg) + '</span>' : '') +
        (tst.ts ? '<span class="cp-sub">' + fmtTime(tst.ts) + '</span>' : '') + '</div>';
    } else if (tst.pass) {
      testHtml = '<div class="cp-cell"><span class="cp-test ok">' + icon('check') + '通过</span>' +
        (tst.ts ? '<span class="cp-sub">' + fmtTime(tst.ts) + '</span>' : '') + '</div>';
    } else {
      testHtml = '<div class="cp-cell"><span class="cp-test fail">' + icon('xmark') + '失败</span>' +
        (tst.msg ? '<span class="cp-sub">' + esc(tst.msg) + '</span>' : '') +
        (tst.ts ? '<span class="cp-sub">' + fmtTime(tst.ts) + '</span>' : '') + '</div>';
    }
    var canTest = !!(p.openai_base || p.anthropic_base);
    var isOpen = _openProvider === p.name;
    var modelsBtn =
      '<button type="button" class="btn btn-sec btn-sm" data-action="provider-models" data-provider="' + nameAttr + '"' +
        ' aria-expanded="' + (isOpen ? 'true' : 'false') + '">模型</button>';
    var opsHtml =
      '<div class="cp-ops">' +
        '<button type="button" class="btn btn-sec btn-sm" data-action="provider-test" data-provider="' + nameAttr + '"' +
          (canTest ? '' : ' disabled title="该 Provider 无 Base URL"') + '>测试</button>' +
        '<button type="button" class="btn btn-sec btn-sm" data-action="provider-discover" data-provider="' + nameAttr + '">发现</button>' +
        modelsBtn +
        '<button type="button" class="btn btn-danger btn-sm" data-action="provider-delete" data-provider="' + nameAttr + '">删除</button>' +
      '</div>';
    return '<tr>' +
      '<td><div class="cp-cell">' +
        '<span class="cp-name-txt">' + esc(p.name) + '</span>' +
        (p.preset_id ? '<span class="cp-sub">预设 ' + esc(p.preset_id) + '</span>' : '') +
      '</div></td>' +
      '<td>' + stHtml + '</td>' +
      '<td>' + protoHtml + '</td>' +
      '<td class="num">' + countHtml + '</td>' +
      '<td>' + keyHtml + '</td>' +
      '<td>' + testHtml + '</td>' +
      '<td>' + opsHtml + '</td>' +
      '</tr>';
  }

  async function cloudLoadProviders() {
    var tbody = $('provTbody');
    if (!tbody) return;
    function fail(msg) {
      tbody.innerHTML = '<tr class="cp-row-loading"><td colspan="7">' +
        '<div class="if-empty">加载失败 · ' + esc(msg) + '</div></td></tr>';
    }
    // 首次加载（无缓存）→ skeleton（spec §5 全 TAB 骨架；重取不闪）
    if (!_providers.length) providerSkeleton();
    try {
      var res = await fetch('/admin/cloud/providers', { headers: UI.adminHeaders() });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        fail((d && d.error) || ('HTTP ' + res.status));
        return;
      }
      _providers = d.providers || [];
      // R3：发现后自动展开策展面板——按刷新后的候选数据判定（"发现全部"不再是死胡同）
      if (_autoOpen) {
        var _openTarget = null;
        if (_autoOpen === 'first') {
          for (var i = 0; i < _providers.length; i++) {
            if ((_providers[i].candidates || []).length > 0) { _openTarget = _providers[i].name; break; }
          }
        } else {
          for (var j = 0; j < _providers.length; j++) {
            if (_providers[j].name === _autoOpen && (_providers[j].candidates || []).length > 0) {
              _openTarget = _providers[j].name; break;
            }
          }
        }
        if (_openTarget) _openProvider = _openTarget;
        _autoOpen = null;
      }
      ensureOpenProviderAlive();
      if (!_providers.length) {
        tbody.innerHTML = '<tr class="cp-row-loading"><td colspan="7">' +
          '<div class="if-empty">暂无 Provider — 使用预设或手动配置添加</div></td></tr>';
        _openProvider = null;
      } else {
        // 行序重排后一次渲染：provider 行 + （若打开）紧邻其后的策展面板行
        var rows = '';
        _providers.forEach(function (p) {
          rows += providerRow(p);
          if (_openProvider === p.name) rows += renderModelPanelRow(p.name);
        });
        tbody.innerHTML = rows;
      }
      cloudRenderModels(d);
    } catch (e) {
      fail(e.message || e);
    }
  }

  /* [data-provider] 命中的 provider 不存在（已删除）时复位打开态 */
  function ensureOpenProviderAlive() {
    if (_openProvider &&
        !_providers.some(function (p) { return p.name === _openProvider; })) {
      _openProvider = null;
    }
  }

  function renderModelPanelRow(providerName) {
    var p = null;
    for (var i = 0; i < _providers.length; i++) {
      if (_providers[i].name === providerName) { p = _providers[i]; break; }
    }
    if (!p) return '';
    var panel = renderModelPanel(p);
    return '<tr class="cpm-tr"><td colspan="7">' + panel + '</td></tr>';
  }

  /* ══ 模型策展面板（行下内联展开，一次一个） ══
   * 三段：发现候选（checkbox 勾选=可路由）/ 已声明（spec 可移除）/ 添加模型。
   * footer 诚实注脚：未勾选的候选 → 显式 404 不兜底。 */
  function renderModelPanel(p) {
    var nameAttr = esc(p.name);
    var cands = p.candidates || [];
    var enabled = (p.enabled_models || []).slice();
    var enableSet = {};
    enabled.forEach(function (m) { enableSet[m] = true; });
    var specs = p.model_specs || [];

    // ── 发现候选段 ──
    var candHtml = '';
    if (!p.discovery_enabled) {
      candHtml = '<div class="if-empty">该 Provider 发现已关闭 — 在上方「发现」手动拉取候选，或直接用右栏手填模型名</div>';
    } else if (!cands.length) {
      // 千帆类厂商无 /models 列表端点（404）→ 发现永远拉不到候选，不再让用户重复点「发现」
      candHtml = '<div class="if-empty">该厂商未提供 /models 列表（或上游未返回模型）— 在下方「已声明」添加模型名（恒可路由）</div>';
    } else {
      candHtml = cands.map(function (mid) {
        var checked = !!enableSet[mid];
        var chips = availabilityChips(p);
        return '<label class="cpm-item" data-action="cpm-toggle" data-provider="' + nameAttr + '" data-model="' + esc(mid) + '">' +
          '<input type="checkbox" class="cpm-check" ' + (checked ? 'checked' : '') + ' aria-label="' + esc(mid) + '">' +
          '<span class="cpm-id mono">' + esc(mid) + '</span>' +
          '<span class="cpm-src">发现</span>' +
          chips +
        '</label>';
      }).join('');
    }

    // ── 已声明段（spec：配置声明 / 手填） ──
    var specHtml = '';
    if (!specs.length) {
      specHtml = '<div class="if-empty">没有已声明模型 — 手动添加的模型名在这里列出，恒可路由</div>';
    } else {
      specHtml = specs.map(function (s) {
        var label = s.manual ? '手填' : '配置声明';
        return '<div class="cpm-item">' +
          '<span class="cpm-check-ph" aria-hidden="true"></span>' +
          '<span class="cpm-id mono">' + esc(s.id) + '</span>' +
          '<span class="cpm-src">' + label + '</span>' +
          '<span class="cpm-ops">' +
            '<button type="button" class="btn btn-danger btn-xs" data-action="cpm-remove" data-provider="' + nameAttr + '" data-model="' + esc(s.id) + '">移除</button>' +
          '</span>' +
        '</div>';
      }).join('');
    }

    var refreshBtn = p.discovery_enabled
      ? '<button type="button" class="btn btn-sec btn-sm" data-action="cpm-refresh" data-provider="' + nameAttr + '">刷新发现</button>'
      : '';

    return '' +
      '<div class="cpm" data-provider="' + nameAttr + '">' +
        '<div class="cpm-hdr">' +
          '<span class="cpm-title">模型管理</span>' +
          '<span class="cpm-status">可路由 <b class="num">' + UI.fmtNum(p.routable_count || 0) + '</b>' +
            ' · 候选 <b class="num">' + UI.fmtNum(cands.length) + '</b></span>' +
          refreshBtn +
        '</div>' +
        '<div class="cpm-cols">' +
          '<section class="cpm-sec">' +
            '<div class="cpm-sec-hdr">发现候选 <span class="cpm-hint">勾选 = 可路由</span></div>' +
            '<div class="cpm-list">' + candHtml + '</div>' +
          '</section>' +
          '<section class="cpm-sec">' +
            '<div class="cpm-sec-hdr">已声明 <span class="cpm-hint">手动添加 / 预设模型，恒可路由</span></div>' +
            '<div class="cpm-list">' + specHtml + '</div>' +
            '<div class="cpm-add">' +
              '<input type="text" class="if-field-input mono cpm-add-input" placeholder="模型名，如 my-glm" ' +
                'autocomplete="off" spellcheck="false" data-action="cpm-add" data-provider="' + nameAttr + '" aria-label="添加模型名">' +
              '<button type="button" class="btn btn-pri btn-sm" data-action="cpm-add" data-provider="' + nameAttr + '">添加</button>' +
            '</div>' +
          '</section>' +
        '</div>' +
        '<div class="cpm-foot">路由只认「已启用」 — 未勾选的候选按未知模型返回 404，不静默兜底</div>' +
      '</div>';
  }

  // 协议可用徽标（面板行内用，OpenAI/Anthropic 双编码）
  function availabilityChips(p) {
    var oi = !!p.openai_base;
    var ai = !!p.anthropic_base;
    var chips = '';
    if (oi) chips += '<span class="cp-chip on">' + icon('check') + 'OpenAI</span>';
    if (ai) chips += '<span class="cp-chip on">' + icon('check') + 'Anthropic</span>';
    return chips ? '<span class="cp-chips cpm-chips">' + chips + '</span>' : '';
  }

  /* ══ 面板开关：provider-models 按钮 ══ */
  function cloudToggleModels(provider) {
    if (!provider) return;
    _openProvider = (_openProvider === provider) ? null : provider;
    cloudLoadProviders();
  }

  /* ══ 策展动作：POST /admin/cloud/provider-models ══ */
  async function callProviderModels(provider, action, model) {
    var res = await fetch('/admin/cloud/provider-models', {
      method: 'POST',
      headers: UI.adminHeaders(),
      body: JSON.stringify({ provider: provider, action: action, model: model }),
    });
    var d = null;
    try { d = await res.json(); } catch (_) {}
    return { ok: res.ok, data: d, status: res.status };
  }

  window.doCpmToggle = async function (provider, model, checked) {
    var action = checked ? 'enable' : 'disable';
    try {
      var r = await callProviderModels(provider, action, model);
      if (!r.ok || !r.data || r.data.error) {
        UI.toast((r.data && r.data.error) || ('操作失败 · HTTP ' + r.status), 'error');
        return;
      }
      UI.toast(checked ? model + ' 已加入可路由' : model + ' 已隐藏', 'success');
      await cloudLoadProviders();
    } catch (e) {
      UI.toast('操作失败: ' + (e.message || e), 'error');
    }
  };

  window.doCpmAdd = async function (provider, inputEl) {
    var model = (inputEl ? inputEl.value : '').trim();
    if (!model) { UI.toast('请填写模型名', 'error'); return; }
    try {
      var r = await callProviderModels(provider, 'add', model);
      if (!r.ok || !r.data || r.data.error) {
        UI.toast((r.data && r.data.error) || ('添加失败 · HTTP ' + r.status), 'error');
        return;
      }
      UI.toast(model + ' 已添加（恒可路由）', 'success');
      if (inputEl) inputEl.value = '';
      await cloudLoadProviders();
    } catch (e) {
      UI.toast('添加失败: ' + (e.message || e), 'error');
    }
  };

  window.doCpmRemove = function (provider, model) {
    if (!provider || !model) { UI.toast('缺少模型名称', 'error'); return; }
    UI.confirm({
      title: '移除模型',
      body: '从 "已声明" 移除 "' + model + '"？它将不再可路由。',
      danger: true,
      onOk: async function () {
        try {
          var r = await callProviderModels(provider, 'remove', model);
          if (!r.ok || !r.data || r.data.error) {
            UI.toast((r.data && r.data.error) || ('移除失败 · HTTP ' + r.status), 'error');
            return;
          }
          UI.toast(model + ' 已移除', 'success');
          await cloudLoadProviders();
        } catch (e) {
          UI.toast('移除失败: ' + (e.message || e), 'error');
        }
      },
    });
  };

  window.doCpmRefresh = function (provider) {
    if (!provider) return;
    window.doCloudDiscover(provider);
  };

  /* ── 可路由模型列表（策展后 = 注册表；行内来源徽标） ── */
  function cloudRenderModels(data) {
    var models = (data && data.models) || [];
    var count = $('cloudModelCount');
    if (count) count.textContent = UI.fmtNum(models.length) + ' 个可路由';
    var list = $('cloudModelsList');
    if (!list) return;
    if (!models.length) {
      list.innerHTML = '<div class="if-empty">暂无可路由模型 — 某 Provider 行点「模型」勾选候选或手填模型名</div>';
      return;
    }
    // 来源索引：{provider: {mid: '发现'|'手填'|'配置声明'}}
    var srcIndex = {};
    _providers.forEach(function (p) {
      var map = (srcIndex[p.name] = {});
      (p.enabled_models || []).forEach(function (m) { map[m] = '发现'; });
      (p.model_specs || []).forEach(function (s) { map[s.id] = s.manual ? '手填' : '配置声明'; });
    });
    var sorted = models.slice().sort(function (a, b) {
      var pa = (a.provider || '') < (b.provider || '') ? -1 : (a.provider === b.provider ? 0 : 1);
      if (pa !== 0) return pa;
      return ((b.discovered_at || 0) - (a.discovered_at || 0));
    });
    var html = '';
    sorted.forEach(function (m) {
      var src = ((srcIndex[m.provider] || {})[m.model_id || m.id]) || '发现';
      var chips = '';
      if (m.openai_available) chips += '<span class="cp-chip on">' + icon('check') + 'OpenAI</span>';
      if (m.anthropic_available) chips += '<span class="cp-chip on">' + icon('check') + 'Anthropic</span>';
      html +=
        '<div class="cp-model-row">' +
          '<span class="cpm-src">' + esc(src) + '</span>' +
          '<span class="cp-model-id">' + esc(m.model_id || m.id) + '</span>' +
          chips +
          '<span class="cp-model-prov">' + esc(m.provider) + '</span>' +
        '</div>';
    });
    list.innerHTML = html;
  }

  /* ══ 手动添加：POST /admin/cloud/providers {name, api_key, openai_base, anthropic_base} ══ */
  window.doCloudAdd = async function () {
    var name = ($('cpName') || {}).value ? $('cpName').value.trim() : '';
    var apiKey = ($('cpApiKey') || {}).value ? $('cpApiKey').value.trim() : '';
    var openaiBase = ($('cpOpenaiBase') || {}).value ? $('cpOpenaiBase').value.trim() : '';
    var anthropicBase = ($('cpAnthropicBase') || {}).value ? $('cpAnthropicBase').value.trim() : '';
    if (!name) { UI.toast('请填写 Provider 名称', 'error'); return; }
    if (!openaiBase && !anthropicBase) { UI.toast('至少填写一个 Base URL', 'error'); return; }
    if (!_manualTestOk) {
      UI.toast('请先测试连接并通过', 'error');
      return;
    }
    setBtnBusy('manual-add', true);
    try {
      var res = await fetch('/admin/cloud/providers', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({
          name: name,
          api_key: apiKey,
          openai_base: openaiBase,
          anthropic_base: anthropicBase,
        }),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('添加失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('Provider ' + name + ' 已添加', 'success');
      await window.doCloudDiscover(name);  // 发现单个 + 刷新 Provider 表
      ['cpName', 'cpApiKey', 'cpOpenaiBase', 'cpAnthropicBase', 'cpProbeModel'].forEach(function (id) {
        var el = $(id);
        if (el) el.value = '';
      });
      _manualTestOk = false;
      setBtnBusy('manual-add', true);
      resetTestResult('cpManualTestResult', '先点「测试连接」，通过后方可添加');
    } catch (e) {
      UI.toast('添加失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('manual-add', false);
    }
  };

  /* ══ 测试连接：POST /admin/cloud/test（v6.6 重做，允许未注册）
   * doCloudTest(opts)：opts={base, key, protocol, probeModel}。
   *   base 传去尾斜杠的 base（后端不再拼 /models）；probeModel 有则发 1-token 真实生成。
   * 返回 Promise<{ok, pass, data, error}>：ok=请求成功，pass=可添加门禁（后端 CLOUD_TEST_PASS_STATUSES）。
   * key 留空时后端不带鉴权头（公开端点可测）。 */
  window.doCloudTest = async function (opts) {
    opts = opts || {};
    var base = String(opts.base || '').replace(/\/+$/, '');
    if (!base) {
      return { ok: false, pass: false, error: '请填写 Base URL' };
    }
    var body = { url: base, api_key: opts.key ? String(opts.key) : '', protocol: opts.protocol || 'openai' };
    if (opts.probeModel) body.probe_model = String(opts.probeModel);
    try {
      var res = await fetch('/admin/cloud/test', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify(body),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        return { ok: false, pass: false, error: (d && d.error) || ('HTTP ' + res.status), data: d };
      }
      return { ok: true, pass: !!d.pass, data: d };
    } catch (e) {
      return { ok: false, pass: false, error: e.message || String(e) };
    }
  };

  /* ── 测试结果条（ok/warn/fail：图标 + 文本双编码）。
   * r = doCloudTest 返回值；model_unavailable 归为"警告"（连接与鉴权正常，仅该模型不在套餐）。 ── */
  function renderTestResult(containerId, r) {
    var el = $(containerId);
    if (!el) return;
    el.style.display = '';
    if (!r || !r.ok) {
      el.className = 'cp-test-result fail';
      el.innerHTML = icon('xmark') + esc((r && r.error) || '连接失败');
      return;
    }
    var st = (r.data && r.data.status) || '';
    if (r.pass) {
      el.className = 'cp-test-result ok';
      el.innerHTML = icon('check') + esc((r.data && r.data.message) || '连接正常');
    } else if (st === 'model_unavailable') {
      el.className = 'cp-test-result warn';
      el.innerHTML = icon('alert') + esc((r.data && r.data.message) || '探测模型不在套餐内（连接与鉴权正常）');
    } else {
      el.className = 'cp-test-result fail';
      el.innerHTML = icon('xmark') + esc((r.data && r.data.message) || '连接失败');
    }
  }

  function resetTestResult(containerId, msg) {
    var el = $(containerId);
    if (!el) return;
    el.className = 'cp-test-result stale';
    el.innerHTML = esc(msg || '');
    el.style.display = '';
  }

  /* ══ 模型发现：POST /admin/cloud/discover {} | {provider} ══ */
  window.doCloudDiscover = async function (name) {
    var provider = name && String(name).trim() ? String(name).trim() : null;
    var opts = { method: 'POST', headers: UI.adminHeaders() };
    if (provider) opts.body = JSON.stringify({ provider: provider });
    setBtnBusy('provider-discover', true, provider);
    try {
      var res = await fetch('/admin/cloud/discover', opts);
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('发现失败 · HTTP ' + res.status), 'error');
        return;
      }
      // R3：自动打开策展面板让用户勾选，不再是"toast 一下完事"的死胡同。
      // 注意：响应里的 cloud_models 是可路由注册表计数（≠ 候选数），不能据此判断候选；
      // 面板开不开由刷新后的候选数据判定（cloudLoadProviders 里的 _autoOpen 扫描）。
      var n = d.cloud_models || 0;
      if (provider) {
        _autoOpen = provider;
        UI.toast('Provider ' + provider + ' 发现完成' + (n ? ' · 可路由 ' + n + ' 个模型' : '') + ' · 有候选则自动打开勾选面板', 'success');
      } else {
        _autoOpen = 'first';
        UI.toast('模型发现完成' + (n ? ' · 可路由 ' + n + ' 个模型' : '') + ' · 有候选则自动打开勾选面板', 'success');
      }
    } catch (e) {
      UI.toast('发现失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('provider-discover', false, provider);
      await cloudLoadProviders();  // review fix#2：无论发现成败都刷新（provider 可能已添加但发现失败）
    }
  };

  /* ══ 删除：DELETE /admin/cloud/providers {name}（R13：UI.confirm danger） ══ */
  window.doCloudDelete = function (name) {
    if (!name) { UI.toast('缺少 Provider 名称', 'error'); return; }
    UI.confirm({
      title: '删除 Provider',
      body: '确定删除 Provider "' + name + '"？其发现的模型将一并移除。',
      danger: true,
      onOk: async function () {
        setBtnBusy('provider-delete', true, name);
        try {
          var res = await fetch('/admin/cloud/providers', {
            method: 'DELETE',
            headers: UI.adminHeaders(),
            body: JSON.stringify({ name: name }),
          });
          var d = null;
          try { d = await res.json(); } catch (_) {}
          if (!res.ok || !d || d.error) {
            UI.toast((d && d.error) || ('删除失败 · HTTP ' + res.status), 'error');
            return;
          }
          UI.toast('Provider ' + name + ' 已删除', 'success');
          if (_openProvider === name) _openProvider = null;
          await cloudLoadProviders();
        } catch (e) {
          UI.toast('删除失败: ' + (e.message || e), 'error');
        } finally {
          setBtnBusy('provider-delete', false, name);
        }
      },
    });
  };

  /* ══ 重载配置：POST /admin/cloud/reload ══ */
  window.doCloudReload = async function () {
    setBtnBusy('reload', true);
    try {
      var res = await fetch('/admin/cloud/reload', {
        method: 'POST',
        headers: UI.adminHeaders(),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('重载失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('已重新加载: ' + (d.providers || 0) + ' 个 provider, ' + (d.cloud_models || 0) + ' 个可路由模型', 'success');
      await cloudLoadProviders();
    } catch (e) {
      UI.toast('重载失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('reload', false);
    }
  };

  /* ── 渲染入口：tab_active 刷新（tabRenderers 契约，forceRefresh 亦复用） ── */
  function renderCloud() {
    cloudLoadPresets();
    cloudLoadProviders();
  }
  window.renderCloud = renderCloud;
  window.tabRenderers['tab-cloud'] = renderCloud;

  store.on('tab_active', function (tab) {
    if (tab === 'tab-cloud') renderCloud();
  });

  /* ── 事件委托（无 inline onclick） ──
   * #tab-cloud 点击：[data-action] + [data-provider]。
   * checkbox 的动作挂 label：点击文本/控件都能命中（closest 上溯到 label），
   * label 原生切换 checkbox → 读 checked 得新状态。
   * 预设卡 / 添加输入为 div/input → keydown Enter/Space 亦触发。 */
  async function handleAction(el) {
    var act = el.getAttribute('data-action');
    if (!act || el.disabled) return;
    var provider = el.getAttribute('data-provider');
    switch (act) {
      case 'preset-select': {
        var pid = el.getAttribute('data-preset');
        if (!pid) return;
        // 再次点击已选卡 → 取消（spec §4.5）
        if (_selectedPreset && _selectedPreset.id === pid) cloudDeselectPreset();
        else cloudSelectPreset(pid);
        break;
      }
      case 'preset-cancel':
        cloudDeselectPreset();
        break;
      case 'preset-add':
        window.doCloudAddPreset();
        break;
      case 'manual-test': {
        var ob = ($('cpOpenaiBase') ? $('cpOpenaiBase').value.trim() : '');
        var ab = ($('cpAnthropicBase') ? $('cpAnthropicBase').value.trim() : '');
        var key = ($('cpApiKey') ? $('cpApiKey').value.trim() : '');
        var pm = ($('cpProbeModel') ? $('cpProbeModel').value.trim() : '');
        // protocol 取填了 base 的那个；两个都填 → 默认 openai（primary signal）
        var protocol, base;
        if (ob && !ab) { protocol = 'openai'; base = ob; }
        else if (!ob && ab) { protocol = 'anthropic'; base = ab; }
        else if (ob && ab) { protocol = 'openai'; base = ob; }
        if (!base) { UI.toast('请填写至少一个 Base URL', 'error'); return; }
        setBtnBusy('manual-test', true);
        var r = await window.doCloudTest({ base: base, key: key, protocol: protocol, probeModel: pm });
        setBtnBusy('manual-test', false);
        renderTestResult('cpManualTestResult', r);
        if (r.pass) { _manualTestOk = true; setBtnBusy('manual-add', false); }
        else { _manualTestOk = false; setBtnBusy('manual-add', true); }
        break;
      }
      case 'manual-add':
        window.doCloudAdd();
        break;
      case 'preset-test': {
        if (!_selectedPreset) { UI.toast('请先选择预设', 'error'); return; }
        var pp = _selectedPreset;
        var pkey = ($('cpPresetApiKey') ? $('cpPresetApiKey').value.trim() : '');
        if (!pkey) { UI.toast('请填写 API Key', 'error'); return; }
        var pproto, pbase;
        if (pp.openai_base && !pp.anthropic_base) { pproto = 'openai'; pbase = pp.openai_base; }
        else if (!pp.openai_base && pp.anthropic_base) { pproto = 'anthropic'; pbase = pp.anthropic_base; }
        else if (pp.openai_base && pp.anthropic_base) { pproto = 'openai'; pbase = pp.openai_base; }
        if (!pbase) { UI.toast('该预设未配置 Base URL', 'error'); return; }
        setBtnBusy('preset-test', true);
        var pr = await window.doCloudTest({ base: pbase, key: pkey, protocol: pproto });
        setBtnBusy('preset-test', false);
        renderTestResult('cpPresetTestResult', pr);
        if (pr.pass) { _presetTestOk = true; setBtnBusy('preset-add', false); }
        else { _presetTestOk = false; setBtnBusy('preset-add', true); }
        break;
      }
      case 'reload':
        window.doCloudReload();
        break;
      case 'provider-discover':
        window.doCloudDiscover(provider || '');
        break;
      case 'provider-models':
        cloudToggleModels(provider);
        break;
      case 'cpm-refresh':
        window.doCpmRefresh(provider);
        break;
      case 'cpm-toggle': {
        if (!provider) return;
        var ck = el.querySelector ? el.querySelector('input') : null;
        if (!ck) return;
        window.doCpmToggle(provider, el.getAttribute('data-model'), !!ck.checked);
        break;
      }
      case 'cpm-add': {
        if (!provider) return;
        var wrap = el.closest ? el.closest('.cpm-add') : null;
        var input = wrap ? wrap.querySelector('.cpm-add-input') : null;
        if (!input) { UI.toast('找不到输入框', 'error'); return; }
        window.doCpmAdd(provider, input);
        break;
      }
      case 'cpm-remove':
        if (provider) window.doCpmRemove(provider, el.getAttribute('data-model'));
        break;
      case 'provider-test': {
        if (!provider) return;
        var p = null;
        for (var i = 0; i < _providers.length; i++) {
          if (_providers[i].name === provider) { p = _providers[i]; break; }
        }
        if (!p || (!p.openai_base && !p.anthropic_base)) {
          UI.toast('该 Provider 未配置 Base URL', 'error');
          return;
        }
        // R4：服务端用已存 key 测该 provider 全部 base（前端不传/不回显 key）
        setBtnBusy('provider-test', true, provider);
        try {
          var res = await fetch('/admin/cloud/provider-test', {
            method: 'POST',
            headers: UI.adminHeaders(),
            body: JSON.stringify({ provider: provider }),
          });
          var d = null;
          try { d = await res.json(); } catch (_) {}
          if (!res.ok || !d || d.error) {
            UI.toast((d && d.error) || ('测试失败 · HTTP ' + res.status), 'error');
          } else {
            var statuses = (d.results || []).map(function (r) { return r.status; });
            var anyFail = statuses.some(function (s) { return s === 'auth_failed' || s === 'error'; });
            var anyWarn = statuses.some(function (s) { return s === 'model_unavailable'; });
            var ok = !!d.ok;
            var firstBad = (d.results || []).filter(function (r) { return !r.pass; })[0];
            _testStatus[provider] = {
              pass: ok,
              warn: (!ok && anyWarn && !anyFail),
              ts: Date.now(),
              msg: ok ? (d.key_set ? '' : 'Key 未设置（未带鉴权）')
                   : (firstBad ? (firstBad.message || '') : ''),
            };
            saveTestStatus();
            if (!d.key_set) UI.toast('Key 未设置 — 本次测试未带鉴权', 'warn');
            UI.toast(ok ? ('Provider ' + provider + ' 测试通过')
                       : ('Provider ' + provider + ' 测试未通过'), ok ? 'success' : 'error');
            await cloudLoadProviders();  // 重渲染"测试"列（读 _testStatus）
          }
        } catch (e) {
          UI.toast('测试失败: ' + (e.message || e), 'error');
        } finally {
          setBtnBusy('provider-test', false, provider);
        }
        break;
      }
      case 'provider-delete':
        if (provider) window.doCloudDelete(provider);
        break;
    }
  }

  var root = $('tab-cloud');
  if (root) {
    root.addEventListener('click', function (ev) {
      var el = ev.target.closest ? ev.target.closest('[data-action]') : null;
      if (!el || !root.contains(el)) return;
      handleAction(el);
    });
    root.addEventListener('keydown', function (ev) {
      var t = ev.target;
      var typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA');
      // 输入框内：空格正常输入；仅 Enter 提交
      if (typing && ev.key !== 'Enter') return;
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var el = t.closest ? t.closest('[data-action]') : null;
      if (!el || !root.contains(el) || el.tagName === 'BUTTON') return;
      ev.preventDefault();
      handleAction(el);
    });

    // R1/R2 失效规则：已通过的测试，任一表单字段 change → 重新禁用"添加" + 结果条提示
    ['cpName', 'cpApiKey', 'cpOpenaiBase', 'cpAnthropicBase', 'cpProbeModel'].forEach(function (id) {
      var el = $(id);
      if (!el) return;
      el.addEventListener('change', function () {
        if (_manualTestOk) {
          _manualTestOk = false;
          setBtnBusy('manual-add', true);
          resetTestResult('cpManualTestResult', '配置已变更，请重新测试');
        }
      });
    });
    var pkeyEl = $('cpPresetApiKey');
    if (pkeyEl) pkeyEl.addEventListener('change', function () {
      if (_presetTestOk) {
        _presetTestOk = false;
        setBtnBusy('preset-add', true);
        resetTestResult('cpPresetTestResult', '配置已变更，请重新测试');
      }
    });
  }
})();