/* InferFabric Console — Packs TAB（M2-T1 能力包生命周期）
 *
 * 数据源（API 冻结）：
 *   - GET  /admin/packs/catalog           — 目录（pack_id/display_name/summary/tags/capability/builtin/risk_level）
 *   - GET  /admin/packs/status            — 装机状态机（stage 实时覆盖；无下载字节数，不显示百分比）
 *   - POST /admin/packs/install           — {pack_id} → 202/409/404
 *   - POST /admin/packs/custom            — {name,url,capability,sha256?} → 202/400/409
 *   - GET  /admin/packs/uninstall-preview — ?pack_id= → {affected_pools}（失败不提交）
 *   - POST /admin/packs/uninstall         — {pack_id,confirmed} → 预览→显式确认→confirmed:true
 *   - GET|POST /admin/packs/policy        — {max_model_parameters_b, system_reserved_memory_mb}
 *
 * 行为契约（评审 2026-11-19）：
 *   - 阶段标签覆盖 packs/installer.py INSTALL_STAGES 全集 + failed（9+1 态）；
 *     未知阶段中性回退原文，不伪造下载百分比/字节数。
 *   - 卸载：预览失败 → toast 且不发 POST；确认框列出当前候选并明示
 *     「最终候选引用以服务端执行时重新计算为准」；执行后 409（引用变化）
 *     只报错不自动重试。提交期间防双击。
 *   - 401（严格模式缺/错 admin token）→ 明确提示，不自动重试写操作。
 *   - status 仅 tab 激活时 2s 轮询；切出即停。
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) { console.warn('[packs] UI missing — deferring'); return; }
  var store = window.store;

  var URL_CATALOG = '/admin/packs/catalog';
  var URL_STATUS = '/admin/packs/status';
  var URL_INSTALL = '/admin/packs/install';
  var URL_CUSTOM = '/admin/packs/custom';
  var URL_PREVIEW = '/admin/packs/uninstall-preview';
  var URL_UNINSTALL = '/admin/packs/uninstall';
  var URL_POLICY = '/admin/packs/policy';
  var POLL_MS = 2000;
  var RESERVE_MIN = 1024, RESERVE_MAX = 65536;   // POLICY_MEM_RANGE

  /* 阶段标签：与 packs/installer.py INSTALL_STAGES + failed 一一对应 */
  var STAGE_LABELS = {
    preflight: '预检',
    staging: '暂存',
    downloading: '下载中',
    verifying: '校验中',
    preparing_runtime: '准备运行时',
    writing_config: '写入配置',
    isolated_healthcheck: '隔离健康检查',
    gateway_reload: '网关重载',
    installed: '已安装',
    failed: '失败',
  };

  var _catalog = null;      // 目录缓存（惰性加载）
  var _catalogLoading = false;
  var _status = null;       // 最近一次 status payload
  var _policy = null;
  var _pollTimer = null;
  var _busy = {};           // pack_id → true（写操作在途，防双击）

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function stageLabel(stage) {
    if (stage == null || stage === '') return '—';
    return STAGE_LABELS[stage] || String(stage);   // 未知阶段中性回退
  }

  function $(id) { return document.getElementById(id); }

  /* ── 请求（统一 401/错误形态） ── */
  function _fetch(url, opts, cb) {
    opts = opts || {};
    opts.headers = UI.adminHeaders();
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    fetch(url, opts).then(function (res) {
      var unauthorized = res.status === 401;
      res.json().then(function (j) {
        if (unauthorized) {
          UI.toast('401：需要 admin token — 请在右上角输入后重试', 'error');
          cb({ unauthorized: true, message: (j && j.error) || 'unauthorized' }, j, res.status);
        } else if (!res.ok) {
          cb({ message: (j && (j.error || j.message)) || ('HTTP ' + res.status) }, j, res.status);
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

  /* ── 状态区 ── */
  function renderStatus() {
    var el = $('pkStatus');
    if (!el) return;
    var packs = (_status && _status.packs) || [];
    if (!packs.length) {
      el.innerHTML = '<p class="if-field-hint">暂无装机记录。</p>';
      return;
    }
    var rows = packs.map(function (p) {
      var failed = p.status === 'failed' || p.stage === 'failed';
      var inProgress = !failed && p.status !== 'installed' && p.stage !== 'installed' &&
                       p.status !== 'installed';
      var badgeCls = failed ? 'badge crit' : (inProgress ? 'badge warn' : 'badge ok');
      var errTxt = (failed && p.error) ? ' — ' + escHtml(p.error) : '';
      var pathTxt = p.install_path ? ' <span class="mono if-field-hint">' +
        escHtml(p.install_path) + '</span>' : '';
      return '<tr><td class="mono">' + escHtml(p.pack_id) + '</td>' +
        '<td>' + escHtml(p.pack_version || '—') + '</td>' +
        '<td><span class="' + badgeCls + '">' + stageLabel(p.stage || p.status) + '</span>' +
        errTxt + pathTxt + '</td></tr>';
    }).join('');
    el.innerHTML = '<h3 class="if-card-title">装机状态</h3>' +
      '<table class="if-table"><thead><tr><th>包</th><th>版本</th><th>阶段</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  /* ── 目录区 ── */
  function renderCatalog() {
    var el = $('pkCatalog');
    if (!el) return;
    var list = (_catalog && _catalog.packs) || [];
    if (!list.length) {
      el.innerHTML = '<p class="if-field-hint">目录为空。</p>';
      return;
    }
    var byId = {};
    ((_status && _status.packs) || []).forEach(function (p) { byId[p.pack_id] = p; });
    var rows = list.map(function (p) {
      var st = byId[p.pack_id];
      var installed = st && (st.status === 'installed' || st.stage === 'installed');
      var inFlight = !!_busy[p.pack_id];
      var act = installed
        ? '<button type="button" class="btn btn-danger btn-sm" data-action="uninstall"' +
          ' data-pack="' + escHtml(p.pack_id) + '"' + (inFlight ? ' disabled' : '') + '>' +
          UI.icon('trash', 14) + '卸载</button>'
        : '<button type="button" class="btn btn-pri btn-sm" data-action="install"' +
          ' data-pack="' + escHtml(p.pack_id) + '"' + (inFlight ? ' disabled' : '') + '>' +
          UI.icon('download', 14) + '安装</button>';
      var capBadge = p.capability ? '<span class="badge info">' + escHtml(p.capability) +
        '</span> ' : '';
      var tags = (p.tags || []).map(function (t) {
        return '<span class="badge">' + escHtml(t) + '</span>';
      }).join(' ');
      return '<tr><td class="mono">' + escHtml(p.pack_id) + '</td>' +
        '<td>' + escHtml(p.display_name || p.pack_id) +
        (p.summary ? '<br><span class="if-field-hint">' + escHtml(p.summary) + '</span>' : '') +
        '</td>' +
        '<td>' + capBadge + tags + '</td>' +
        '<td>' + act + '</td></tr>';
    }).join('');
    el.innerHTML = '<h3 class="if-card-title">目录</h3>' +
      '<table class="if-table"><thead><tr><th>包</th><th>说明</th><th>能力</th><th>操作</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  /* ── 策略区 ── */
  function renderPolicy() {
    if (!_policy) return;
    var maxEl = $('pkPolicyMaxB'), resEl = $('pkPolicyReserve');
    if (maxEl && document.activeElement !== maxEl) maxEl.value = _policy.max_model_parameters_b;
    if (resEl && document.activeElement !== resEl) resEl.value = _policy.system_reserved_memory_mb;
  }

  function renderPacks() {
    renderStatus();
    renderCatalog();
    renderPolicy();
  }

  /* ── 数据加载 ── */
  function loadCatalog() {
    if (_catalog || _catalogLoading) return;
    _catalogLoading = true;
    _fetch(URL_CATALOG, {}, function (err, j) {
      _catalogLoading = false;
      if (err) {
        if (!err.unauthorized) UI.toast('目录加载失败：' + err.message, 'error');
        return;
      }
      _catalog = j;
      renderCatalog();
    });
  }

  function loadStatus() {
    _fetch(URL_STATUS, {}, function (err, j) {
      if (err) return;   // 轮询失败静默（顶部 api_error 横幅统一报断连）
      _status = j;
      renderStatus();
      renderCatalog();
    });
  }

  function loadPolicy() {
    _fetch(URL_POLICY, {}, function (err, j) {
      if (err) return;
      _policy = j;
      renderPolicy();
    });
  }

  /* ── tab 激活轮询（仅激活时 2s，切出即停） ── */
  function startPoll() {
    loadStatus();
    if (_pollTimer) return;
    _pollTimer = setInterval(loadStatus, POLL_MS);
  }
  function stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  /* ── 动作 ── */
  function setBusy(packId, busy) {
    if (busy) _busy[packId] = true; else delete _busy[packId];
    renderCatalog();
  }

  function doInstall(packId) {
    setBusy(packId, true);
    _fetch(URL_INSTALL, { method: 'POST', body: { pack_id: packId } },
      function (err, j, status) {
        setBusy(packId, false);
        if (err) {
          if (!err.unauthorized) UI.toast('安装请求失败：' + err.message, 'error');
          return;   // 401 已由 _fetch 统一提示，回调不再覆盖
        }
        UI.toast('安装已开始：' + packId + '（进度见装机状态）', 'ok');
        loadStatus();
      });
  }

  /* 卸载：预览 → 显式确认 → confirmed:true（预览失败绝不提交） */
  function requestUninstall(packId) {
    setBusy(packId, true);
    _fetch(URL_PREVIEW + '?pack_id=' + encodeURIComponent(packId), {},
      function (err, j) {
        setBusy(packId, false);
        if (err) {
          if (!err.unauthorized) UI.toast('卸载预览失败，已取消：' + err.message, 'error');
          return;   // 401 已由 _fetch 统一提示，回调不再覆盖
        }
        var pools = (j && j.affected_pools) || [];
        var lines = pools.map(function (p) {
          return (p.pool || '?') + ' ← ' + (p.model_name || '?');
        }).join('；');
        UI.confirm({
          title: '卸载能力包 ' + packId,
          body: '将停止关联服务、删除受控配置与包目录（模型权重不删）。' +
            (pools.length ? '将清除模型池候选：' + lines + '。' : '当前无关联模型池候选。') +
            '最终候选引用以服务端执行时重新计算为准。',
          danger: true,
          onOk: function () { doUninstall(packId); },
        });
      });
  }

  function doUninstall(packId) {
    setBusy(packId, true);
    _fetch(URL_UNINSTALL, { method: 'POST', body: { pack_id: packId, confirmed: true } },
      function (err, j, status) {
        setBusy(packId, false);
        if (err) {
          if (err.unauthorized) return;   // 401 已由 _fetch 统一提示
          // 409 = 执行时服务端重算出候选引用（预览后新引用出现）——只报错不自动重试
          var pools = (j && j.affected_pools) || [];
          var detail = err.message + (pools.length ? '（涉及候选：' +
            pools.map(function (p) { return (p.pool || '?') + ' ← ' + (p.model_name || '?'); })
              .join('；') + '）' : '');
          UI.toast('卸载未执行：' + detail, 'error');
          return;
        }
        UI.toast('已卸载：' + packId, 'ok');
        loadStatus();
      });
  }

  function doCustomInstall() {
    var name = ($('pkCustomName') || {}).value || '';
    var url = ($('pkCustomUrl') || {}).value || '';
    var cap = ($('pkCustomCap') || {}).value || 'llm';
    var sha = (($('pkCustomSha') || {}).value || '').trim();
    if (!name.trim() || !url.trim()) {
      UI.toast('名称与下载 URL 必填', 'error');
      return;
    }
    var body = { name: name.trim(), url: url.trim(), capability: cap };
    if (sha) body.sha256 = sha;
    _fetch(URL_CUSTOM, { method: 'POST', body: body }, function (err, j, status) {
      if (err) {
        if (!err.unauthorized) UI.toast('自定义安装请求失败：' + err.message, 'error');
        return;   // 401 已由 _fetch 统一提示
      }
      UI.toast('自定义包安装已开始（进度见装机状态）', 'ok');
      loadStatus();
    });
  }

  function doSavePolicy() {
    var maxB = parseInt(($('pkPolicyMaxB') || {}).value, 10);
    var reserve = parseInt(($('pkPolicyReserve') || {}).value, 10);
    if (isNaN(maxB) || maxB < 1) { UI.toast('最大模型参数量必须为 ≥1 的整数', 'error'); return; }
    if (isNaN(reserve) || reserve < RESERVE_MIN || reserve > RESERVE_MAX) {
      UI.toast('系统保留内存必须在 [' + RESERVE_MIN + ', ' + RESERVE_MAX + '] MiB', 'error');
      return;
    }
    _fetch(URL_POLICY, { method: 'POST',
      body: { max_model_parameters_b: maxB, system_reserved_memory_mb: reserve } },
      function (err, j) {
        if (err) {
          if (!err.unauthorized) UI.toast('策略保存失败：' + err.message, 'error');
          return;   // 401 已由 _fetch 统一提示
        }
        _policy = j;
        renderPolicy();
        UI.toast('装机策略已保存', 'ok');
      });
  }

  /* ── 事件委托（无 inline onclick） ── */
  function onClick(ev) {
    var btn = ev.target.closest('[data-action]');
    if (!btn || btn.disabled) return;
    var act = btn.getAttribute('data-action');
    if (act === 'install') doInstall(btn.getAttribute('data-pack'));
    else if (act === 'uninstall') requestUninstall(btn.getAttribute('data-pack'));
    else if (act === 'custom-install') doCustomInstall();
    else if (act === 'save-policy') doSavePolicy();
  }

  /* ── 注册 ── */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-packs'] = function () {
    renderPacks();
    loadCatalog();
    loadPolicy();
    startPoll();
  };
  if (store && store.on) {
    store.on('tab_active', function (tab) {
      if (tab === 'tab-packs') {
        renderPacks(); loadCatalog(); loadPolicy(); startPoll();
      } else {
        stopPoll();
      }
    });
  }
  document.addEventListener('click', function (ev) {
    if (ev.target && ev.target.closest && ev.target.closest('#tab-packs')) onClick(ev);
  });
})();
