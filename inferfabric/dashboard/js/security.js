/* InferFabric Console — Security TAB（M2-T2 鉴权状态 / 密钥 / 一次性回显）
 *
 * 数据源（API 冻结，不新增路由）：
 *   - GET  /admin/security/status  → {active,policy,enforced,key_count,keys[]}
 *   - POST /admin/security/enable  {enforce,key_name?}（成功响应可能含一次性明文 key）
 *   - POST /admin/security/keys    {name?}（成功响应含一次性明文 key）
 *   - POST /admin/security/keys/revoke {id}
 *
 * 行为契约（评审 2026-11-19 / Review Focus 2）：
 *   - 三态分立：active=密钥存储可用；policy=新策略（all/off）；
 *     enforced=实际鉴权。policy 'off' 是迁移宽限，绝不渲染成“无需鉴权”。
 *   - 全吊销 ≠ 首次启用：已初始化且未吊销数为 0 时，开启前先引导创建替换 key；
 *     从未初始化走后端首次发 key 流程。最后一把未吊销 key 的吊销有警告。
 *   - 策略变更后重读 status，不凭 /enable 返回值推断实际鉴权。
 *   - 明文一次性回显：textContent 渲染、仅主动复制；不写浏览器存储/全局
 *     状态/toast/URL；关闭、Esc、切页、迟到响应均不留明文在 DOM。
 *   - 401（严格模式 GET/写均可）→ 统一提示，不自动重试写请求。
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) { console.warn('[security] UI missing — deferring'); return; }
  var store = window.store;

  var URL_STATUS = '/admin/security/status';
  var URL_ENABLE = '/admin/security/enable';
  var URL_KEYS = '/admin/security/keys';
  var URL_REVOKE = '/admin/security/keys/revoke';

  var _status = null;
  var _gen = 0;          // 回显代次：清空/切页即自增，迟到响应按代次丢弃

  function $(id) { return document.getElementById(id); }
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ── 请求（统一 401/错误形态，与 packs.js 同式） ── */
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

  /* ── 一次性明文回显（textContent；代次守卫丢弃迟到响应） ── */
  function showReveal(token, gen) {
    if (gen !== _gen) return;   // 迟到响应：期间发生过清空/切页 → 直接丢弃
    var box = $('secReveal'), el = $('secRevealToken');
    if (!box || !el) return;
    el.textContent = token;     // 明文只经 textContent，不进 innerHTML/全局状态
    box.style.display = '';
    el.focus();
  }
  function clearReveal() {
    _gen++;
    var box = $('secReveal'), el = $('secRevealToken');
    if (el) el.textContent = '';
    if (box) box.style.display = 'none';
  }
  // 回显可见期间 Esc 也清空（关闭路径之一；迟到响应由代次守卫拦下）
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') clearReveal();
  });

  /* ── 状态渲染（三态分立） ── */
  function policyLabel(p) {
    if (p === 'all') return '<span class="badge ok">all — 新策略拦截全部请求</span>';
    if (p === 'off') return '<span class="badge warn">off — 迁移宽限（legacy 鉴权可能仍在拦截）</span>';
    return '<span class="badge info">未设置</span>';
  }

  function renderStatus() {
    var el = $('secStatus');
    if (!el) return;
    if (!_status) { el.innerHTML = '<p class="if-field-hint">加载中…</p>'; return; }
    var s = _status;
    var enforcedBadge = s.enforced
      ? '<span class="badge ok">实际鉴权：生效</span>'
      : '<span class="badge warn">实际鉴权：未生效</span>';
    var html = '';
    if (!s.active) {
      html = '<p>密钥存储尚未初始化。开启鉴权将初始化密钥库并生成首个 API key' +
        '（明文仅回显一次）。</p>' + enforcedBadge;
    } else {
      html = policyLabel(s.policy) + ' ' + enforcedBadge +
        '<p class="if-field-hint">未吊销密钥：' + s.key_count + ' 把</p>';
      var rows = (s.keys || []).map(function (k) {
        var act = k.revoked ? '<span class="badge">已吊销</span>' :
          '<button type="button" class="btn btn-danger btn-sm" data-action="sec-revoke"' +
          ' data-kid="' + escHtml(k.id) + '"' +
          ' data-kname="' + escHtml(k.name || '') + '">' + UI.icon('trash', 14) + '吊销</button>';
        return '<tr><td class="mono">' + escHtml(k.id) + '</td>' +
          '<td>' + escHtml(k.name || '—') + '</td>' +
          '<td>' + escHtml(k.created_at || '—') + '</td>' +
          '<td>' + act + '</td></tr>';
      }).join('');
      if (rows) {
        html += '<table class="if-table"><thead><tr><th>ID</th><th>名称</th>' +
          '<th>创建时间</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>';
      }
    }
    el.innerHTML = html;
    renderEnableButtons();
  }

  /* 启用/关闭按钮的可用性与引导文案（全吊销 ≠ 首次启用） */
  function renderEnableButtons() {
    var hint = $('secEnableHint');
    if (!hint || !_status) return;
    if (_status.active && _status.key_count === 0) {
      hint.textContent = '密钥库已初始化但没有未吊销 key — 请先创建一个替换 key，再开启鉴权（否则客户端将全部被拒）。';
    } else if (!_status.active) {
      hint.textContent = '开启后所有客户端请求需携带 API key（明文仅首次回显一次）。';
    } else {
      hint.textContent = '';
    }
  }

  function loadStatus() {
    _fetch(URL_STATUS, {}, function (err, j) {
      if (err) { if (!err.unauthorized) UI.toast('状态加载失败：' + err.message, 'error'); return; }
      _status = j;
      renderStatus();
    });
  }

  /* ── 动作 ── */
  function doEnable(enforce) {
    var gen = _gen;
    var body = { enforce: enforce };
    var nameEl = $('secKeyName');
    if (enforce && nameEl && nameEl.value.trim()) body.key_name = nameEl.value.trim();
    _fetch(URL_ENABLE, { method: 'POST', body: body }, function (err, j) {
      if (err) {
        if (!err.unauthorized) UI.toast('操作失败：' + err.message, 'error');
        return;
      }
      if (j && j.key) showReveal(j.key, gen);   // 明文仅本次回显
      loadStatus();                             // 实际鉴权以重读的 status 为准
    });
  }

  function doCreateKey() {
    var nameEl = $('secKeyName');
    var gen = _gen;
    var body = {};
    if (nameEl && nameEl.value.trim()) body.name = nameEl.value.trim();
    _fetch(URL_KEYS, { method: 'POST', body: body }, function (err, j) {
      if (err) {
        if (!err.unauthorized) UI.toast('创建失败：' + err.message, 'error');
        return;
      }
      if (j && j.key) showReveal(j.key, gen);
      loadStatus();
    });
  }

  function doRevoke(kid, kname) {
    var lastActive = _status && _status.key_count === 1;
    UI.confirm({
      title: '吊销 API key' + (kname ? ' ' + kname : ''),
      body: lastActive
        ? '这是最后一把未吊销 key — 吊销后没有任何可用 key，所有客户端请求将被拒绝。请先创建替换 key 再吊销。'
        : '吊销后使用该 key 的客户端将立即被拒绝（policy off 下已注册凭证的吊销仍生效）。',
      danger: true,
      onOk: function () {
        _fetch(URL_REVOKE, { method: 'POST', body: { id: kid } }, function (err) {
          if (err) {
            if (!err.unauthorized) UI.toast('吊销失败：' + err.message, 'error');
            return;
          }
          UI.toast('已吊销', 'ok');
          loadStatus();
        });
      },
    });
  }

  function doCopyKey() {
    var el = $('secRevealToken');
    if (!el || !el.textContent) return;
    var gen = _gen;
    navigator.clipboard.writeText(el.textContent).then(function () {
      UI.toast('已复制到剪贴板（明文仍在页面上，保存后请关闭）', 'ok');
    }, function () {
      // 剪贴板失败：明文保持 textContent 显示，用户仍可手动选择复制
      if (gen === _gen) UI.toast('复制失败 — 请手动选择文本复制', 'error');
    });
  }

  /* ── 事件委托（无 inline onclick） ── */
  function onClick(ev) {
    var btn = ev.target.closest('[data-action]');
    if (!btn || btn.disabled) return;
    var act = btn.getAttribute('data-action');
    if (act === 'sec-enable') {
      if (_status && _status.active && _status.key_count === 0) {
        renderEnableButtons();   // 引导文案已就位，不发起注定拒绝全体的启用
        UI.toast('请先创建替换 key，再开启鉴权', 'error');
        return;
      }
      doEnable(true);
    } else if (act === 'sec-disable') doEnable(false);
    else if (act === 'sec-create-key') doCreateKey();
    else if (act === 'sec-revoke') doRevoke(btn.getAttribute('data-kid'),
                                            btn.getAttribute('data-kname'));
    else if (act === 'sec-copy-key') doCopyKey();
    else if (act === 'sec-dismiss-key') clearReveal();
  }

  /* ── 注册 ── */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-security'] = function () {
    clearReveal();          // 每次进入先清空（迟到响应由代次守卫丢弃）
    loadStatus();
  };
  if (store && store.on) {
    store.on('tab_active', function (tab) {
      if (tab === 'tab-security') {
        clearReveal();   // 每次进入先清空（迟到响应由代次守卫丢弃）
        loadStatus();
      } else {
        clearReveal();   // 切页不留明文
      }
    });
  }
  document.addEventListener('click', function (ev) {
    if (ev.target && ev.target.closest && ev.target.closest('#tab-security')) onClick(ev);
  });
})();
