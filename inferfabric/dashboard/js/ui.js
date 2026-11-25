/* InferFabric Console — UI helpers (v2)
 * window.UI: toast / confirm / skeleton / 格式化 / icon / adminHeaders
 * 零 emoji（SVG 图标）、数字等宽 tabular-nums。
 * 依赖：base.html 提供 #toast、#confirmModal 容器与 SVG symbol defs。
 */
(function () {
  'use strict';

  var TOAST_KEY = 'toast';
  var _toastTimer = null;

  /* ── Toast（DOM 与旧 showToast 一致：#toast，2.8s 自动消失） ── */
  function toast(msg, kind) {
    var el = document.getElementById(TOAST_KEY);
    if (!el) return;
    el.textContent = msg;
    var cls = kind || 'info';
    if (cls === 'success') cls = 'ok';   // 别名归一
    el.className = 'toast-banner ' + cls + ' show';
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () {
      el.classList.remove('show');
    }, 2800);
  }

  /* ── 确认弹窗（渲染进 #confirmModal，danger → 红色主按钮） ── */
  function confirm(opts) {
    opts = opts || {};
    var holder = document.getElementById('confirmModal');
    if (!holder) { // 无容器兜底：原生确认
      var ok = window.confirm(opts.title + (opts.body ? '\n' + opts.body : ''));
      if (ok && typeof opts.onOk === 'function') opts.onOk();
      return;
    }
    var danger = !!opts.danger;
    holder.innerHTML =
      '<div class="if-modal-backdrop">' +
        '<div class="if-modal" role="dialog" aria-modal="true">' +
          '<div class="if-modal-title"></div>' +
          '<div class="if-modal-body"></div>' +
          '<div class="if-modal-actions">' +
            '<button class="btn btn-sec" data-act="cancel">取消</button>' +
            '<button class="btn ' + (danger ? 'btn-danger' : 'btn-pri') + '" data-act="ok">' +
              (danger ? '确认执行' : '确认') +
            '</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    holder.style.display = '';
    holder.querySelector('.if-modal-title').textContent = opts.title || '确认操作';
    holder.querySelector('.if-modal-body').textContent = opts.body || '';
    if (danger) holder.querySelector('.if-modal-title').style.color = 'var(--crit)';

    function close() {
      holder.style.display = 'none';
      holder.innerHTML = '';
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) {
      if (e.key === 'Escape') close();
      if (e.key === 'Enter') { close(); if (typeof opts.onOk === 'function') opts.onOk(); }
    }
    // Click listener attaches to the backdrop child (destroyed by holder.innerHTML=''
    // in close()), NOT the persistent holder — otherwise each confirm() accumulates
    // a listener on #confirmModal and stale onOk closures re-fire on later confirms.
    var backdrop = holder.querySelector('.if-modal-backdrop');
    function onBackdropClick(e) {
      var act = e.target.getAttribute && e.target.getAttribute('data-act');
      if (act === 'ok') { close(); if (typeof opts.onOk === 'function') opts.onOk(); }
      else if (act === 'cancel') { close(); }
      else if (e.target === backdrop) { close(); }
    }
    if (backdrop) backdrop.addEventListener('click', onBackdropClick);
    document.addEventListener('keydown', onKey);
    var okBtn = holder.querySelector('[data-act="ok"]');
    if (okBtn) okBtn.focus();
  }

  /* ── Skeleton（shimmer 占位：rows 行） ── */
  function skeleton(el, rows) {
    if (!el) return;
    var n = rows || 3;
    var html = '<div class="skeleton">';
    for (var i = 0; i < n; i++) html += '<div class="skeleton-row"></div>';
    html += '</div>';
    el.innerHTML = html;
  }

  /* ── 格式化 helper ── */
  function fmtGB(mb) {
    if (mb == null || isNaN(mb)) return '—';
    return (Number(mb) / 1024).toFixed(1);
  }

  // = 旧 ovFormatUptime（行为不变）
  function fmtDur(sec) {
    if (sec == null || sec === undefined) return '—';
    var d = Math.floor(sec / 86400),
        h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60),
        s = Math.floor(sec % 60);
    var out = '';
    if (d) out += d + 'd ';
    if (h) out += h + 'h ';
    out += m + 'm ' + s + 's';
    return out;
  }

  function fmtNum(n) {
    if (n == null || n === '' || isNaN(n)) return '—';
    return Number(n).toLocaleString();
  }

  /* ── 内联 SVG 图标（<svg><use href="#s-...">） ── */
  function icon(name, size) {
    size = size || 16;
    return '<svg width="' + size + '" height="' + size + '"><use href="#s-' + name + '"/></svg>';
  }

  /* ── adminHeaders（issue #47：控制面零凭证 — 只带 Content-Type；
   * 凭证头/#凭证输入框概念已删除，所有面板统一经此取请求头） ── */
  function adminHeaders() {
    return { 'Content-Type': 'application/json' };
  }

  window.UI = {
    toast: toast,
    confirm: confirm,
    skeleton: skeleton,
    fmtGB: fmtGB,
    fmtDur: fmtDur,
    fmtNum: fmtNum,
    icon: icon,
    adminHeaders: adminHeaders,
  };

  // 兼容别名：旧 showToast 调用点（legacy 调用方兼容）
  window.showToast = toast;
})();
