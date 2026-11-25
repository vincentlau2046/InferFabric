/* InferFabric Console — Connect TAB（M3-T3 本机客户端接入 / 回退）
 *
 * 数据源（合同由 Task 2 test_connect_admin.py 钉住）：
 *   - GET  /admin/connect/status   → {clients:[{client,state,target,revision},...]}
 *   - GET  /admin/security/status  → {keys:[{id,name,...}]}（仅取名/ID，绝不发 key）
 *   - POST /admin/connect/apply    {client,expected_revision,confirmed:true,key_id,api_key}
 *   - POST /admin/connect/rollback {client,expected_revision,confirmed:true}
 *
 * 行为契约（计划 Task 3 / Review Focus）：
 *   - 进入页面只 GET，不改配置；页面状态保留最后一次预览 revision（CAS 用），
 *     不展示原配置正文或旧 URL。
 *   - 托管/回退都点击显式确认（confirmed:true）才 POST；409 → 提示重读状态
 *     重新确认，绝不自动重试；401 专用提示不被通用错误覆盖。
 *   - api_key 走一次性密码输入：弹窗关闭 / 请求结束即清空，不进浏览器存储。
 *   - 写完提示需重启客户端；未提供的模型设置由现有客户端配置保留。
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) { console.warn('[connect] UI missing — deferring'); return; }
  var store = window.store;

  var URL_STATUS = '/admin/connect/status';
  var URL_APPLY = '/admin/connect/apply';
  var URL_ROLLBACK = '/admin/connect/rollback';
  var URL_SEC_STATUS = '/admin/security/status';

  var _status = null;        // 最后一次 /admin/connect/status（revision CAS 源）
  var _keys = null;          // /admin/security/status 的 keys 元数据
  var _modal = null;         // {action:'apply'|'rollback', client, revision}

  function $(id) { return document.getElementById(id); }
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var CLIENT_LABEL = { claude: 'Claude Code', codex: 'Codex' };
  var STATE_COPY = {
    managed: '已托管',
    unmanaged: '未托管（现有配置将保留）',
    missing_file: '未创建配置文件',
    missing_directory: '客户端目录不存在（首次接入将创建）',
    conflict: '状态冲突 — 配置被外部修改，需人工处理后重试'
  };
  var TARGET_COPY = {
    this_gateway: '本机网关',
    other: '指向其他服务',
    unset: '未配置',
    unknown: '未知'
  };

  /* ── 请求（统一 401/错误形态，与 security.js 同式） ── */
  function _fetch(url, opts, cb) {
    opts = opts || {};
    opts.headers = UI.adminHeaders();
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    fetch(url, opts).then(function (res) {
      var unauthorized = res.status === 401;
      res.json().then(function (j) {
        if (unauthorized) {
          cb({ unauthorized: true, message: (j && j.error) || 'unauthorized' }, j, res.status);
        } else if (!res.ok) {
          cb({ message: (j && (j.error || j.message)) || ('HTTP ' + res.status),
               code: j && j.code }, j, res.status);
        } else {
          cb(null, j, res.status);
        }
      }, function () {
        cb({ message: 'HTTP ' + res.status + '（非 JSON 响应）' }, null, res.status);
      });
    }, function (e) {
      cb({ message: '网络错误：' + e });
    });
  }

  function loadStatus() {
    _fetch(URL_STATUS, {}, function (err, j) {
      if (err) {
        if (!err.unauthorized) UI.toast('读取接入状态失败：' + err.message, 'error');
        $('connStatus').innerHTML =
          '<p class="if-field-hint">状态读取失败 — ' + escHtml(err.message) + '</p>';
        return;
      }
      _status = j && j.clients ? j.clients : [];
      render();
    });
    _fetch(URL_SEC_STATUS, {}, function (err, j) {
      if (!err && j && j.keys) _keys = j.keys;   // 只取元数据（id/name）
    });
  }

  function rowFor(c) {
    var state = STATE_COPY[c.state] || c.state;
    var target = TARGET_COPY[c.target] || c.target;
    var actions = '';
    if (c.state === 'managed') {
      actions = '<button type="button" class="btn btn-sec btn-sm" data-action="conn-rollback"' +
        ' data-client="' + escHtml(c.client) + '">回退</button>';
    } else if (c.state !== 'conflict') {
      actions = '<button type="button" class="btn btn-pri btn-sm" data-action="conn-apply"' +
        ' data-client="' + escHtml(c.client) + '">托管到本网关</button>';
    }
    return '<div class="if-grid" style="align-items:center;justify-content:space-between;' +
      'border-top:1px solid var(--if-border,#e5e5e5);padding-top:8px;margin-top:8px">' +
      '<div><b>' + escHtml(CLIENT_LABEL[c.client] || c.client) + '</b>' +
      '<span class="if-field-hint" style="margin-left:8px">' + escHtml(state) +
      ' · 指向：' + escHtml(target) + '</span></div>' +
      '<div>' + actions + '</div></div>';
  }

  function render() {
    var host = $('connStatus');
    if (!host) return;
    if (!_status || !_status.length) {
      host.innerHTML = '<p class="if-field-hint">暂无客户端状态</p>';
      return;
    }
    var html = '<p class="if-field-hint">托管会把客户端配置指向本网关（保留其余设置；' +
      '写入前自动备份，可一键回退）。配置写入后需重启对应客户端生效。</p>';
    for (var i = 0; i < _status.length; i++) html += rowFor(_status[i]);
    host.innerHTML = html;
  }

  /* ── 确认弹窗（显式确认才 POST；一次性 key 输入） ── */
  function closeModal() {
    var holder = $('connModalHolder');
    if (holder) holder.innerHTML = '';
    var inp = $('connKeyInput');
    if (inp) inp.value = '';              // 一次性输入：关闭即清空
    _modal = null;
  }

  function openModal(action, client) {
    var cur = null;
    for (var i = 0; i < (_status || []).length; i++) {
      if (_status[i].client === client) { cur = _status[i]; break; }
    }
    if (!cur) return;
    _modal = { action: action, client: client, revision: cur.revision };
    var isApply = action === 'apply';
    var title = isApply ? '托管 ' + (CLIENT_LABEL[client] || client) + ' 到本网关'
                        : '回退 ' + (CLIENT_LABEL[client] || client) + ' 配置';
    var keyRow = '';
    if (isApply) {
      var opts = '';
      (_keys || []).forEach(function (k) {
        opts += '<option value="' + escHtml(k.id) + '">' + escHtml(k.name) + '</option>';
      });
      keyRow = '<div style="margin-top:10px">' +
        '<label class="if-field-hint">使用已有 API 密钥（仅名称可选；明文不回显）</label><br>' +
        '<select id="connKeyId" class="if-input" style="max-width:280px">' + opts + '</select></div>' +
        '<div style="margin-top:10px">' +
        '<label class="if-field-hint">粘贴该密钥的明文（仅本次写入使用，关闭即清空）</label><br>' +
        '<input type="password" id="connKeyInput" class="if-input" style="max-width:280px"' +
        ' autocomplete="off" placeholder="sk-…"></div>' +
        '<p class="if-field-hint" style="margin-top:6px">遗失明文请在「鉴权」页重建密钥；' +
        '接入不生成新密钥、不找回明文。</p>';
    }
    var holder = $('connModalHolder');
    holder.innerHTML =
      '<div class="if-card" id="connModal" style="border:1px solid var(--if-border,#e5e5e5)">' +
      '<div class="if-card-hdr"><span class="if-card-title">' + escHtml(title) + '</span>' +
      '<span class="if-card-sub">点击确认才写入；写入前自动备份原配置</span></div>' +
      '<div class="if-card-body">' + keyRow +
      '<p class="if-field-hint" style="margin-top:8px">确认后需要重启客户端加载新配置。</p>' +
      '<div class="if-grid" style="margin-top:10px">' +
      '<button type="button" class="btn ' + (isApply ? 'btn-pri' : 'btn-sec') +
      ' btn-sm" data-action="conn-confirm">确认' + (isApply ? '写入' : '回退') + '</button>' +
      '<button type="button" class="btn btn-sec btn-sm" data-action="conn-cancel">取消</button>' +
      '</div><p class="if-field-hint" id="connModalMsg" style="margin-top:8px"></p>' +
      '</div></div>';
  }

  function submit() {
    if (!_modal) return;
    var m = _modal;
    var body = { client: m.client, expected_revision: m.revision, confirmed: true };
    var url;
    if (m.action === 'apply') {
      var kid = $('connKeyId') ? $('connKeyId').value : '';
      var key = $('connKeyInput') ? $('connKeyInput').value : '';
      if (!kid || !key) {
        $('connModalMsg').textContent = '请选择密钥并粘贴明文';
        return;
      }
      body.key_id = kid;
      body.api_key = key;
      url = URL_APPLY;
    } else {
      url = URL_ROLLBACK;
    }
    _fetch(url, { method: 'POST', body: body }, function (err, j, status) {
      if (err) {
        if (err.unauthorized) {
          // 401 专用提示，不被通用错误覆盖
          UI.toast('401：请求被拒绝 — 请刷新页面后重试', 'error');
          return;
        }
        if (status === 409 || (j && j.code === 'conflict')) {
          // 409：状态已变 — 要求重新确认，不自动重试
          UI.toast('配置已在外部变化（409）— 已重读状态，请重新确认', 'error');
          closeModal();
          loadStatus();
          return;
        }
        UI.toast('操作失败：' + (err.message || '未知错误'), 'error');
        var msg = $('connModalMsg');
        if (msg) msg.textContent = '失败：' + (err.message || '未知错误');
        if (m.action === 'apply') {
          var inp = $('connKeyInput');
          if (inp) inp.value = '';        // 请求结束即清空明文
        }
        return;
      }
      var verb = m.action === 'apply' ? '已写入' : '已回退';
      UI.toast(verb + ' — 需重启 ' + (CLIENT_LABEL[m.client] || m.client) + ' 生效', 'ok');
      closeModal();
      loadStatus();
    });
    if (m.action === 'apply') {
      var inp2 = $('connKeyInput');
      if (inp2) inp2.value = '';          // 请求发出即清空
    }
  }

  /* ── 事件分派（无 inline onclick） ── */
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-action]');
    if (!btn) return;
    var act = btn.getAttribute('data-action');
    if (act === 'conn-apply') { openModal('apply', btn.getAttribute('data-client')); return; }
    if (act === 'conn-rollback') { openModal('rollback', btn.getAttribute('data-client')); return; }
    if (act === 'conn-confirm') { submit(); return; }
    if (act === 'conn-cancel') { closeModal(); return; }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && _modal) closeModal();
  });

  if (store && store.on) {
    store.on('tab_active', function (tab) {
      if (tab === 'tab-connect') loadStatus();   // 进入页面只读状态
      else closeModal();                          // 切页不留一次性输入
    });
  }
})();
