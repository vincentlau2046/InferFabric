/* InferFabric Console — Settings TAB（PR3 WS4 授权设置）
 *
 * 数据源：
 *   - GET  /admin/security/status           → {active,policy,enforced,key_count,keys[],password_set}
 *   - POST /admin/security/enable           {enforce}（成功响应可能含一次性明文 key）
 *   - POST /admin/security/console-password {new_password}（首次设密 — 需 X-Admin-Token）
 *   - POST /admin/keys/login                {password}（改密第一跳 — 当前密码换会话 + CSRF）
 *   - POST /admin/keys/password             {current_password,new_password}（改密第二跳 — X-CSRF-Token）
 *
 * 行为契约（PR3 WS4 / Review Focus 4）：
 *   - 本机零门槛边界：设置页不设登录；开关只影响推理 API 客户端与 Key 敏感操作。
 *   - 推理鉴权开/关写前 UI.confirm（开=客户端将 401；关=仅迁移宽限，key 全保留）。
 *   - Key 保护密码只保护 API Key 敏感操作（PR4 reveal 等），不是控制台登录。
 *   - 首设走 console-password（管理员凭证恒校验）；改密走会话两跳（login → password）。
 *   - 密码值只进 login/console-password/keys-password 三个请求体 — 绝不进 GET、
 *     innerHTML、toast 文案、浏览器存储；一次性 key 明文只经 textContent + 代次守卫。
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) { console.warn('[settings] UI missing — deferring'); return; }
  var store = window.store;

  var URL_STATUS = '/admin/security/status';
  var URL_ENABLE = '/admin/security/enable';
  var URL_CONSOLE_PW = '/admin/security/console-password';
  var URL_LOGIN = '/admin/keys/login';
  var URL_PW_CHANGE = '/admin/keys/password';

  var _status = null;
  var _gen = 0;          // 回显代次：清空/切页即自增，迟到响应按代次丢弃

  function $(id) { return document.getElementById(id); }

  /* ── 请求（统一错误形态；额外头部与 adminHeaders 合并 — CSRF/首设凭证） ── */
  function _fetch(url, opts, cb) {
    opts = opts || {};
    opts.headers = Object.assign(UI.adminHeaders(), opts.headers || {});
    if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    fetch(url, opts).then(function (res) {
      var unauthorized = res.status === 401;
      res.json().then(function (j) {
        if (!res.ok) {
          cb({ unauthorized: unauthorized,
               message: (j && (j.error || j.message)) || ('HTTP ' + res.status) }, j, res.status);
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

  /* ── 一次性明文回显（textContent；代次守卫丢弃迟到响应） ── */
  function showReveal(token, gen) {
    if (gen !== _gen) return;   // 迟到响应：期间发生过清空/切页 → 直接丢弃
    var box = $('stReveal'), el = $('stRevealToken');
    if (!box || !el) return;
    el.textContent = token;     // 明文只经 textContent，不进 innerHTML/全局状态
    box.style.display = '';
    el.focus();
  }
  function clearReveal() {
    _gen++;
    var box = $('stReveal'), el = $('stRevealToken');
    if (el) el.textContent = '';
    if (box) box.style.display = 'none';
  }
  // 回显可见期间 Esc 也清空（关闭路径之一；迟到响应由代次守卫拦下）
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') clearReveal();
  });

  /* ── 推理 API 鉴权卡（三态分立：active / policy / enforced） ── */
  function renderInferenceAuth() {
    var el = $('setBody');
    if (!el) return;
    if (!_status) { el.innerHTML = '<p class="if-field-hint">加载中…</p>'; return; }
    var s = _status;
    // 本机零门槛边界（每次渲染重申 — 设置页不是登录页）
    var html = '<p class="if-field-hint">本机控制台不设登录 — 能力包安装、部署、切换模型、'
             + '打开本页均不需要密码。以下开关只影响推理 API 客户端与 Key 敏感操作，'
             + '不改变本机零门槛。</p>';
    var enforcedBadge = s.enforced
      ? '<span class="badge ok">实际鉴权：生效</span>'
      : '<span class="badge warn">实际鉴权：未生效</span>';
    if (!s.active) {
      html += '<p>密钥存储尚未初始化。开启鉴权将初始化密钥库并生成首个 API key'
           +  '（明文仅回显一次）。开启后所有客户端请求需携带 API key，未携带将 401。</p>'
           +  enforcedBadge
           +  '<div class="if-grid" style="margin-top:12px">'
           +  '<button type="button" class="btn btn-pri btn-sm" data-action="st-enable">开启鉴权</button></div>';
    } else {
      var policyBadge = s.policy === 'all'
        ? '<span class="badge ok">all — 新策略拦截全部请求</span>'
        : s.policy === 'off'
          ? '<span class="badge warn">off — 迁移宽限（不等于免鉴权）</span>'
          : '<span class="badge info">pools — 仅池化模型要求 key</span>';
      html += policyBadge + ' ' + enforcedBadge
           +  '<p class="if-field-hint">未吊销密钥：' + s.key_count + ' 把 — 在「鉴权」页管理</p>'
           +  '<div class="if-grid" style="margin-top:12px">'
           +  '<button type="button" class="btn btn-sec btn-sm" data-action="st-disable">关闭新策略</button></div>'
           +  '<p class="if-field-hint">关闭仅回到迁移宽限，保留全部已建 key；可随时再开启。</p>';
    }
    el.innerHTML = html;
  }

  /* ── Key 保护密码卡（未设 → 首设表单；已设 → 会话改密表单） ── */
  function renderPasswordCard() {
    var el = $('pwBody');
    if (!el) return;
    if (!_status) { el.innerHTML = '<p class="if-field-hint">加载中…</p>'; return; }
    var html = '';
    if (!_status.password_set) {
      html += '<p class="if-field-hint">首次设置需管理员凭证（右上角 admin token）。'
           +  '无凭证可在网关本机运行 "iff keys init"。</p>'
           +  '<div class="if-grid" style="margin-top:8px">'
           +  '<input type="password" class="if-input" id="stToken" placeholder="admin token（首次设置必填）"'
           +  ' autocomplete="off" spellcheck="false" style="max-width:320px"></div>'
           +  '<div class="if-grid" style="margin-top:8px">'
           +  '<input type="password" class="if-input" id="stNewPw" placeholder="新密码（至少 12 字符）"'
           +  ' autocomplete="new-password" style="max-width:320px">'
           +  '<input type="password" class="if-input" id="stNewPw2" placeholder="再输入一次"'
           +  ' autocomplete="new-password" style="max-width:320px">'
           +  '<button type="button" class="btn btn-pri btn-sm" data-action="st-set-pw">设置密码</button></div>';
    } else {
      html += '<p class="if-field-hint">修改需控制台会话 — 提交时先用当前密码换取会话，再携 CSRF 提交。'
           +  '忘记密码：网关本机运行 "iff keys set-password"（保留全部 API key）。</p>'
           +  '<div class="if-grid" style="margin-top:8px">'
           +  '<input type="password" class="if-input" id="pwCurrent" placeholder="当前密码"'
           +  ' autocomplete="current-password" style="max-width:320px"></div>'
           +  '<div class="if-grid" style="margin-top:8px">'
           +  '<input type="password" class="if-input" id="pwNew" placeholder="新密码（至少 12 字符）"'
           +  ' autocomplete="new-password" style="max-width:320px">'
           +  '<input type="password" class="if-input" id="pwNew2" placeholder="再输入一次"'
           +  ' autocomplete="new-password" style="max-width:320px">'
           +  '<button type="button" class="btn btn-pri btn-sm" data-action="st-pw-change">修改密码</button></div>';
    }
    el.innerHTML = html;
  }

  function loadStatus() {
    _fetch(URL_STATUS, {}, function (err, j) {
      if (err) { if (!err.unauthorized) UI.toast('状态加载失败：' + err.message, 'error'); return; }
      _status = j;
      renderInferenceAuth();
      renderPasswordCard();
    });
  }

  /* ── 动作（window 导出：事件委托与面板测试共用同一入口） ── */
  function doEnable(enforce) {
    var gen = _gen;
    UI.confirm({
      title: enforce ? '开启推理 API 鉴权' : '关闭推理 API 鉴权',
      body: enforce
        ? '开启后所有客户端请求需携带 API key，未携带将 401。首次开启会自动生成一个 key（明文仅回显一次）。'
        : '关闭后新策略进入迁移宽限（不等于免鉴权）；已建 key 全部保留，可随时再开启。',
      danger: false,
      onOk: function () {
        _fetch(URL_ENABLE, { method: 'POST', body: { enforce: enforce } }, function (err, j) {
          if (err) { if (!err.unauthorized) UI.toast('操作失败：' + err.message, 'error'); return; }
          if (j && j.key) showReveal(j.key, gen);   // 首建 key 明文仅本次回显
          loadStatus();                             // 实际鉴权以重读的 status 为准
        });
      },
    });
  }

  function doConsoleSet() {
    var tokEl = $('stToken');
    var p1El = $('stNewPw'), p2El = $('stNewPw2');
    var tok = tokEl ? tokEl.value : '';
    var p1 = p1El ? p1El.value : '';
    var p2 = p2El ? p2El.value : '';
    if (p1 !== p2) { UI.toast('两次输入的密码不一致', 'error'); return; }
    if (p1.length < 12) { UI.toast('密码至少 12 个字符', 'error'); return; }
    // 首设：管理员凭证恒校验在服务端 — token 只进本请求头，不落任何存储
    _fetch(URL_CONSOLE_PW, { method: 'POST', body: { new_password: p1 },
                             headers: { 'X-Admin-Token': tok } }, function (err) {
      if (err) { UI.toast('设置失败：' + err.message, 'error'); return; }
      UI.toast('Key 保护密码已设置', 'ok');
      loadStatus();
    });
  }

  function doPasswordChange() {
    var curEl = $('pwCurrent');
    var p1El = $('pwNew'), p2El = $('pwNew2');
    var cur = curEl ? curEl.value : '';
    var p1 = p1El ? p1El.value : '';
    var p2 = p2El ? p2El.value : '';
    if (p1 !== p2) { UI.toast('两次输入的密码不一致', 'error'); return; }
    if (p1.length < 12) { UI.toast('密码至少 12 个字符', 'error'); return; }
    // 两跳：login（当前密码换会话 + CSRF）→ password（X-CSRF-Token + 新旧密码）
    _fetch(URL_LOGIN, { method: 'POST', body: { password: cur } }, function (err, j) {
      if (err) { UI.toast('当前密码验证失败：' + err.message, 'error'); return; }
      var csrf = j && j.csrf;
      _fetch(URL_PW_CHANGE, { method: 'POST',
                              body: { current_password: cur, new_password: p1 },
                              headers: { 'X-CSRF-Token': csrf } }, function (err2) {
        if (err2) { UI.toast('修改失败：' + err2.message, 'error'); return; }
        UI.toast('Key 保护密码已修改', 'ok');
        loadStatus();
      });
    });
  }

  /* ── 事件委托（无 inline onclick） ── */
  function onClick(ev) {
    var btn = ev.target.closest('[data-action]');
    if (!btn || btn.disabled) return;
    var act = btn.getAttribute('data-action');
    if (act === 'st-enable') doEnable(true);
    else if (act === 'st-disable') doEnable(false);
    else if (act === 'st-set-pw') doConsoleSet();
    else if (act === 'st-pw-change') doPasswordChange();
    else if (act === 'st-copy-key') {
      var el = $('stRevealToken');
      if (el && el.textContent) {
        navigator.clipboard.writeText(el.textContent).then(function () {
          UI.toast('已复制到剪贴板（明文仍在页面上，保存后请关闭）', 'ok');
        }, function () {
          UI.toast('复制失败 — 请手动选择文本复制', 'error');
        });
      }
    }
    else if (act === 'st-dismiss-key') clearReveal();
  }

  /* ── 注册 ── */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-settings'] = function () {
    clearReveal();          // 每次进入先清空（迟到响应由代次守卫丢弃）
    loadStatus();
  };
  if (store && store.on) {
    store.on('tab_active', function (tab) {
      if (tab === 'tab-settings') {
        clearReveal();
        loadStatus();
      } else {
        clearReveal();   // 切页不留明文
      }
    });
  }
  document.addEventListener('click', function (ev) {
    if (ev.target && ev.target.closest && ev.target.closest('#tab-settings')) onClick(ev);
  });
  window.doEnable = doEnable;
  window.doConsoleSet = doConsoleSet;
  window.doPasswordChange = doPasswordChange;
})();
