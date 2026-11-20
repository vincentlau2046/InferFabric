/* InferFabric Console — 五步部署向导（Task 4；挂模型池 tab 内）
 *
 * 数据源（Task 2/3/3b 后端契约）：
 *   - GET  /admin/deploy/options          — 能力×引擎×平台收窄（Python 侧收窄，不可绕过）
 *   - GET  /admin/deploy/search?q=&page=  — ModelScope 搜索代理（归一化/错误归一）
 *   - POST /admin/deploy/download         — {model_id, path?} 启动 GGUF 下载；多变体
 *                                           → 400 {code:"select_file", choices}
 *   - GET  /admin/deploy/download/status  — ?download_id= 轮询（切出即停）
 *   - POST /admin/packs/install           — {name, type} legacy 流=install_model：
 *                                           装载不启动服务（Mac 语义）。**不走 /deploy**
 *                                           （auto_deploy 装载即 switch，违反本向导约束）
 *   - POST /admin/models/set-autostart    — {name, enabled}（显式勾选项，默认关）
 *
 * 行为契约（评审纪律沿用 packs.js）：
 *   - 五步：能力→框架→部署方式→搜索→参数一键；写操作 _busy 防双击；
 *   - 切出 tab / Esc → 停轮询并重置在途态（Review Focus 5）；
 *   - 错误路径全部 UI.toast，页面不因半截响应崩。
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) { console.warn('[wizard] UI missing — deferring'); return; }
  var store = window.store;

  var URL_OPTIONS = '/admin/deploy/options';
  var URL_SEARCH = '/admin/deploy/search';
  var URL_DOWNLOAD = '/admin/deploy/download';
  var URL_DL_STATUS = '/admin/deploy/download/status';
  var URL_INSTALL = '/admin/packs/install';
  var URL_AUTOSTART = '/admin/models/set-autostart';
  var POLL_MS = 1500;
  var SEARCH_DEBOUNCE = 300;

  var CAP_LABELS = { llm: '对话 / 文本', embedding: '向量 / 嵌入',
                     tts: '语音合成', asr: '语音识别', ocr: '视觉 / 图像' };
  var MODE_LABELS = { 'local-gguf': '本地 GGUF（下载权重 → 装载）',
                      'packs-catalog': '能力包目录（packs 一键装机）',
                      'local-dir': '本地目录（服务框架，vLLM/SGLang）' };
  var STEP_LABELS = ['能力', '框架', '部署方式', '搜索', '参数'];
  var NAME_RE = /^(?!.*\.\.)[\w.\-:/]+$/;   // pull_model 白名单同形态

  /* 五步状态（Task 4 Interfaces 契约） */
  var wiz = { step: 1, capability: null, engine: null, mode: null,
              model: null, params: { name: '', port: '', quant: '',
                                     autostart: false } };
  var _options = null;
  var _open = false;
  var _busy = false;              // 一键序列在途（防双击/防重复提交）
  var _pollTimer = null;
  var _searchTimer = null;
  var _searchQ = '', _searchPage = 1, _searchTotal = 0, _searchItems = [];
  var _searchState = '';          // '' | 'empty' | 'failed'
  var _choices = null;            // select_file 变体清单（400 → picker）
  var _result = null;             // 完成结果卡

  function $(id) { return document.getElementById(id); }
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function _fetch(url, opts, cb) {
    opts = opts || {};
    opts.headers = UI.adminHeaders();
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    fetch(url, opts).then(function (res) {
      var unauthorized = res.status === 401;
      res.json().then(function (j) {
        if (unauthorized) {
          UI.toast('401：需要 admin token — 请在右上角输入后重试', 'error');
          cb({ message: 'unauthorized' }, j, res.status);
        } else if (!res.ok) {
          cb({ message: (j && (j.error || j.message)) || ('HTTP ' + res.status),
               code: j && j.code, choices: j && j.choices }, j, res.status);
        } else {
          cb(null, j, res.status);
        }
      }, function () {
        cb({ message: 'HTTP ' + res.status + '（非 JSON 响应）' }, null, res.status);
      });
    }, function (e) {
      cb({ message: '网络错误：' + e.message });
    });
  }

  /* ── 渲染 ── */
  function stepBar() {
    return '<div style="display:flex;gap:6px;margin-bottom:12px">' +
      STEP_LABELS.map(function (label, i) {
        var n = i + 1;
        var cls = n === wiz.step ? 'badge info'
          : (n < wiz.step ? 'badge ok' : 'badge');
        return '<span class="' + cls + '">' + n + '. ' + escHtml(label) + '</span>';
      }).join('<span class="if-field-hint">→</span>') + '</div>';
  }

  function btn(action, label, extra, disabled) {
    return '<button type="button" class="btn btn-pri btn-sm"' +
      ' data-action="' + action + '"' + (extra || '') +
      (disabled ? ' disabled' : '') + '>' + label + '</button>';
  }

  function renderStep() {
    var el = $('wizBody');
    if (!el) return;
    if (_result) { el.innerHTML = stepBar() + _result; return; }
    var html = stepBar();
    if (wiz.step === 1) {
      html += '<p class="if-field-hint">这个模型用来做什么？</p>';
      (_options && _options.capabilities || []).forEach(function (cap) {
        html += btn('pick-cap', escHtml(CAP_LABELS[cap] || cap),
                    ' data-cap="' + escHtml(cap) + '"', false) + ' ';
      });
    } else if (wiz.step === 2) {
      html += '<p class="if-field-hint">推理框架（已按本机平台收窄）：</p>';
      (_options && _options.engines || []).forEach(function (e) {
        var dis = e.available ? '' : ' disabled';
        html += btn('pick-engine', escHtml(e.label) +
                    (e.available ? '' : '（未安装 — 去「能力包」安装）'),
                    ' data-engine="' + escHtml(e.id) + '"' + dis,
                    false) + ' ';
      });
      html += '<p class="if-field-hint">未安装的引擎可在「能力包」tab 一键安装。</p>';
    } else if (wiz.step === 3) {
      html += '<p class="if-field-hint">部署方式：</p>';
      var modes = (_options && _options.deployment_modes &&
                   _options.deployment_modes[wiz.engine]) || [];
      modes.forEach(function (m) {
        html += btn('pick-mode', escHtml(MODE_LABELS[m] || m),
                    ' data-mode="' + escHtml(m) + '"', false) + ' ';
      });
    } else if (wiz.step === 4) {
      html += '<input type="text" id="wizSearchInput" class="if-input"' +
        ' placeholder="在 ModelScope 搜索 GGUF 模型…" value="' +
        escHtml(_searchQ) + '" style="width:60%">' +
        '<div id="wizSearchResults" style="margin-top:10px"></div>';
    } else if (wiz.step === 5) {
      html += _renderParams();
    }
    el.innerHTML = html;
    if (wiz.step === 4) { renderSearch(); var inp = $('wizSearchInput');
      if (inp) inp.focus(); }
  }

  function _renderParams() {
    var m = wiz.model || {};
    var p = wiz.params;
    var engineLabel = wiz.engine === 'llama_cpp' ? 'llama.cpp' : wiz.engine;
    var html = '<p class="if-field-hint">模型：' + escHtml(m.id || '') +
      ' · 引擎：' + escHtml(engineLabel) + '</p>';
    if (_choices) {
      html += '<p class="if-field-hint">选择量化变体：</p><select id="wizQuant" class="if-input">';
      _choices.forEach(function (c) {
        html += '<option value="' + escHtml(c) + '"' +
          (p.quant === c ? ' selected' : '') + '>' + escHtml(c) + '</option>';
      });
      html += '</select>';
    }
    html += '<p style="margin-top:10px">装载名称：<input type="text" id="wizName"' +
      ' class="if-input" value="' + escHtml(p.name) + '" placeholder="models.d 中的名称">' +
      '　端口：<input type="text" id="wizPort" class="if-input" value="' +
      escHtml(p.port) + '" placeholder="留空自动分配" style="width:110px"></p>' +
      '<p><label><input type="checkbox" id="wizAutostart"' +
      (p.autostart ? ' checked' : '') + '> 开机自启（显式勾选；默认关）</label></p>' +
      '<p class="if-field-hint">装载不启动服务（Mac 语义）：启动留给首请求唤醒或手动切换。</p>' +
      btn('wiz-submit', _busy ? '处理中…' : '一键部署', '', _busy);
    return html;
  }

  function renderSearch() {
    var el = $('wizSearchResults');
    if (!el) return;
    if (_searchState === 'failed') {
      el.innerHTML = '<p class="if-field-hint">搜索失败（网络或服务不可用），可稍后重试。</p>';
      return;
    }
    if (!_searchItems.length) {
      el.innerHTML = '<p class="if-field-hint">' +
        (_searchQ ? '无结果。' : '输入关键词开始搜索。') + '</p>';
      return;
    }
    var rows = _searchItems.map(function (m) {
      return '<div style="margin:4px 0">' +
        btn('pick-model', escHtml(m.name),
            ' data-model="' + escHtml(m.id) + '" data-name="' + escHtml(m.name) + '"',
            false) +
        ' <span class="badge">' + escHtml(String(m.downloads)) + ' 下载</span>' +
        (m.capability_guess ? '<span class="badge info">' +
          escHtml(m.capability_guess) + '</span>' : '') + '</div>';
    }).join('');
    var more = (_searchItems.length < _searchTotal)
      ? '<div style="margin-top:8px">' + btn('wiz-more', '加载更多', '', false) + '</div>' : '';
    el.innerHTML = rows + more;
  }

  /* ── 数据 ── */
  function loadOptions(cb) {
    _fetch(URL_OPTIONS, {}, function (err, j) {
      if (err) { UI.toast('向导选项加载失败：' + err.message, 'error'); return; }
      _options = j;
      if (cb) cb();
    });
  }

  function doSearch(q, page) {
    _fetch(URL_SEARCH + '?q=' + encodeURIComponent(q) + '&page=' + page, {},
           function (err, j) {
      if (err) { _searchState = 'failed'; renderSearch();
        UI.toast('搜索失败：' + err.message, 'error'); return; }
      _searchTotal = (j && j.total) || 0;
      var models = (j && j.models) || [];
      _searchItems = page > 1 ? _searchItems.concat(models) : models;
      _searchState = _searchItems.length ? '' : 'empty';
      renderSearch();
    });
  }

  function stopPoll() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  }

  function pollStatus(downloadId) {
    stopPoll();
    _pollTimer = setTimeout(function () {
      _fetch(URL_DL_STATUS + '?download_id=' + encodeURIComponent(downloadId),
             {}, function (err, j) {
        if (err) { _finishOneClick(false, '下载状态查询失败：' + err.message); return; }
        if (j.stage === 'done') { _installModel(); return; }
        if (j.stage === 'error') { _finishOneClick(false, '下载失败：' + (j.error || '')); return; }
        var el = $('wizBody');
        if (el) {
          var pct = j.files_total ? (j.files_done + '/' + j.files_total) : '';
          el.innerHTML = stepBar() +
            '<p>下载中 ' + escHtml(j.current_file || '') + ' ' + escHtml(pct) +
            '（切换页面不会中断下载，但本页不再跟踪进度）</p>';
        }
        pollStatus(downloadId);
      });
    }, POLL_MS);
  }

  function startDownload(path) {
    var body = { model_id: wiz.model.id };
    if (path) body.path = path;
    _fetch(URL_DOWNLOAD, { method: 'POST', body: body }, function (err, j) {
      if (err) {
        if (err.code === 'select_file') {
          _choices = err.choices || [];
          renderStep();
          return;
        }
        _finishOneClick(false, '下载启动失败：' + err.message); return;
      }
      _choices = null;
      var el = $('wizBody');
      if (el) el.innerHTML = stepBar() + '<p>开始下载 ' +
        escHtml(String(j.total_bytes)) + ' 字节…</p>';
      pollStatus(j.download_id);
    });
  }

  function _installModel() {
    _fetch(URL_INSTALL, { method: 'POST',
                          body: { name: wiz.params.name, type: wiz.engine } },
           function (err, j) {
      if (err) { _finishOneClick(false, '装载失败：' + err.message); return; }
      if (j.status === 'already_configured') {
        // Review Focus 3：已配置可继续（提示 + 允许 switch/自启），不是失败
        if (wiz.params.autostart) { _setAutostart('已配置（可直接切换）'); return; }
        _finishOneClick(true, '已配置，可直接切换（未重复装载）');
        return;
      }
      if (j.status !== 'installed') {
        _finishOneClick(false, '装载失败：' + (j.message || j.status)); return;
      }
      if (wiz.params.autostart) { _setAutostart('已装载'); return; }
      _finishOneClick(true, '已装载（未启动服务 — 首请求唤醒或手动切换）');
    });
  }

  function _setAutostart(prefix) {
    _fetch(URL_AUTOSTART, { method: 'POST',
                            body: { name: wiz.params.name, enabled: true } },
           function (err) {
      if (err) { _finishOneClick(true, prefix + '；自启设置失败：' + err.message); return; }
      _finishOneClick(true, prefix + '；已设为自启');
    });
  }

  function _finishOneClick(ok, msg) {
    _busy = false;
    stopPoll();
    if (!ok) { renderStep(); UI.toast(msg, 'error'); return; }
    _result = '<p>✅ ' + escHtml(msg) + '</p>' +
      btn('wiz-done', '去模型池查看', '', false) + ' ' +
      btn('wiz-again', '再部署一个', '', false);
    renderStep();
    UI.toast(msg, 'ok');
  }

  function submitOneClick() {
    if (_busy) return;                    // 防双击
    var name = (wiz.params.name || '').trim();
    if (!name || !NAME_RE.test(name)) {
      UI.toast('装载名称需为字母/数字/._-:/（不含 ..）', 'error'); return;
    }
    _busy = true;
    renderStep();
    startDownload(_choices ? (_choices.indexOf(wiz.params.quant) >= 0
                              ? wiz.params.quant : _choices[0]) : null);
  }

  function resetWizard() {
    stopPoll();
    if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null; }
    _busy = false;
    _result = null;
    _choices = null;
    _searchQ = ''; _searchPage = 1; _searchItems = []; _searchTotal = 0;
    _searchState = '';
    wiz = { step: 1, capability: null, engine: null, mode: null, model: null,
            params: { name: '', port: '', quant: '', autostart: false } };
    var card = $('tab-deploy-wizard');
    if (card) card.style.display = 'none';
  }

  /* ── 事件（委托，无 inline onclick） ── */
  document.addEventListener('click', function (ev) {
    if (!ev.target || !ev.target.closest ||
        !ev.target.closest('#tab-deploy-wizard')) return;
    var t = ev.target.closest('[data-action]');
    if (!t) return;
    var act = t.getAttribute('data-action');
    if (act === 'wiz-cancel' || act === 'wiz-done') {
      resetWizard();
      if (window.tabRenderers && window.tabRenderers['tab-model-pools']) {
        try { window.tabRenderers['tab-model-pools'](); } catch (e) { /* 渲染失败不阻断 */ }
      }
    } else if (act === 'wiz-again') {
      _result = null; wiz.step = 1; renderStep();
    } else if (act === 'pick-cap') {
      wiz.capability = t.getAttribute('data-cap'); wiz.step = 2; renderStep();
    } else if (act === 'pick-engine') {
      wiz.engine = t.getAttribute('data-engine'); wiz.step = 3; renderStep();
    } else if (act === 'pick-mode') {
      wiz.mode = t.getAttribute('data-mode');
      if (wiz.mode === 'packs-catalog') {
        // packs 目录装机：跳本向导，deep-link 能力包 tab
        resetWizard();
        if (store && store.set) store.set('tab_active', 'tab-packs');
        return;
      }
      wiz.step = 4; renderStep();
    } else if (act === 'wiz-more') {
      _searchPage += 1; doSearch(_searchQ, _searchPage);
    } else if (act === 'pick-model') {
      var id = t.getAttribute('data-model');
      var name = t.getAttribute('data-name') || '';
      wiz.model = { id: id, name: name };
      wiz.params.name = name.replace(/\.gguf$/i, '');
      wiz.step = 5; renderStep();
    } else if (act === 'wiz-submit') {
      var nameEl = $('wizName'), portEl = $('wizPort'), autoEl = $('wizAutostart'),
          quantEl = $('wizQuant');
      if (nameEl) wiz.params.name = nameEl.value;
      if (portEl) wiz.params.port = portEl.value;
      if (autoEl) wiz.params.autostart = autoEl.checked;
      if (quantEl) wiz.params.quant = quantEl.value;
      submitOneClick();
    }
  });

  document.addEventListener('input', function (ev) {
    if (!ev.target || ev.target.id !== 'wizSearchInput') return;
    var q = ev.target.value.trim();
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function () {
      _searchQ = q; _searchPage = 1; _searchItems = []; _searchTotal = 0;
      if (q) doSearch(q, 1); else { _searchState = ''; renderSearch(); }
    }, SEARCH_DEBOUNCE);
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape' || !_open) return;
    var card = $('tab-deploy-wizard');
    if (card && card.style.display !== 'none') resetWizard();   // Esc 关闭重置
  });

  /* 切出模型池 tab：停轮询 + 重置在途态（Review Focus 5） */
  if (store && store.on) {
    store.on('tab_active', function (tab) {
      if (tab !== 'tab-model-pools') {
        stopPoll();
        if (_open) resetWizard();
        _open = false;
      }
    });
  }

  window.Wizard = {
    open: function (capability) {
      _open = true;
      var card = $('tab-deploy-wizard');
      if (card) card.style.display = '';
      wiz.step = 1;
      if (capability) wiz.capability = capability;
      renderStep();
      loadOptions(renderStep);
    }
  };
})();
