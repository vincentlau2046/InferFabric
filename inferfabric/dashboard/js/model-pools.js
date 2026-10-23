/* InferFabric Console — 模型池 TAB（逻辑模型池，只读 + enabled 开关）
 * 面板内容：每池一张卡 — 逻辑名 / enabled 开关（服务端原子翻转）/ 候选表
 * （id、优先级、本地|云、目标、证据态）+ OpenAI 与 Anthropic 两套当前选择视图
 * （chosen 或错误码 + 每候选 included/排除原因，解释性展示非只给结果）。
 *
 * 数据源（API 冻结）：
 *   - GET  /admin/model-pools          → {revision, pools:{name:{enabled,…,candidates:[…]}}}
 *   - GET  /admin/model-pools/status   → {revision, pools:{name:{protocols:{openai,anthropic}}}}
 *   - POST /admin/model-pools/toggle   → {pool, enabled, expected_revision}（CAS）
 *       200 → {status:'updated', revision, pool, enabled}
 *       409 → {status:'revision_conflict', current_revision} — 提示冲突 + 重新拉取
 *
 * 开关契约：不做客户端读-改-写（GET 视图有损，经视图重建配置会丢数据），
 * 只发送 pool/enabled/expected_revision，由服务端基于原始配置原子翻转。
 *
 * 设计纪律（spec §7，零 emoji）：原生 checkbox（无自制伪控件）；无 inline onclick
 * （mpPools 上 change 事件委托）；数字等宽 tabular-nums。
 *
 * 暴露：
 *   - window.tabRenderers['tab-model-pools'] = renderModelPools
 *   - window.renderModelPools / window.doMpToggle(pool, enable)
 */
(function () {
  'use strict';

  var UI = window.UI;
  if (!UI) {
    console.warn('[model-pools] UI not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  var POOLS_URL = '/admin/model-pools';
  var STATUS_URL = '/admin/model-pools/status';
  var RUNTIME_URL = '/admin/model-pools/runtime';
  var TOGGLE_URL = '/admin/model-pools/toggle';

  var _revision = null;   // 最近渲染到的配置 revision（toggle CAS 用）
  var _loading = false;   // 在途渲染去重（避免连点刷新叠发请求）
  // v7.1.0 运营视图单一数据源：def（含 configured/load_error/raw）+ 双协议
  // 选择视图 + 候选运行态；编辑器（Task 6）以 _state.def.raw 为读-改-写基底
  var _state = { def: null, status: null, runtime: null };

  /* ── XSS-safe 转义（与 cloud.js / anomaly.js esc 一致） ── */
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── 运行态辅助（v7.1.0）── */

  // 每协议健康灯：chosen=首选候选 → ok；chosen 非空但非首选（回退中）→ warn；
  // chosen 空（无可用候选，请求将收 pool_unavailable）→ err
  function lampForProtocol(def, pv) {
    var cands = (def.candidates || []).slice()
      .sort(function (a, b) { return (a.priority || 0) - (b.priority || 0); });
    var first = cands.length ? cands[0].id : null;
    if (!pv || !pv.chosen) return 'mp-lamp-err';
    return (first && pv.chosen === first) ? 'mp-lamp-ok' : 'mp-lamp-warn';
  }

  // 秒 → m:ss（冷却/隔离倒计时）
  function fmtRemaining(s) {
    if (s == null || isNaN(s)) return '';
    var sec = Math.max(0, Math.round(s));
    return Math.floor(sec / 60) + ':' + ('0' + (sec % 60)).slice(-2);
  }

  // 候选行运行态：可用性徽标 + 冷却/隔离 + 最近失败
  function runtimeBadge(rt) {
    if (!rt) return '—';
    var parts = [];
    if (rt.cooling) {
      var label = rt.cooling.reason === 'quarantine_404' ? '隔离'
        : (rt.cooling.probing ? '探测中' : '冷却');
      parts.push(label + ' ' + fmtRemaining(rt.cooling.remaining_s)
        + (rt.cooling.reason ? ' (' + esc(rt.cooling.reason) + ')' : ''));
    }
    parts.push(rt.active
      ? (rt.kind === 'local' ? '服务中' : '已发现')
      : (rt.kind === 'local' ? '未启动' : '未发现'));
    if (rt.last_attempt && !rt.last_attempt.ok) {
      parts.push('HTTP ' + esc(rt.last_attempt.status));
    }
    return parts.join(' · ');
  }

  /* ── 池卡片 ── */
  function poolCard(name, def, st, rt) {
    var checked = def.enabled ? ' checked' : '';
    var html = '<div class="if-card mp-pool" data-mp-card="' + esc(name) + '">'
      + '<div class="if-card-hdr">'
      + '<span class="if-card-title mono">iff/' + esc(name) + '</span>'
      + '<span class="mp-lamps">'
      + ['openai', 'anthropic'].map(function (proto) {
          var cls = lampForProtocol(def, ((st || {}).protocols || {})[proto]);
          return '<span class="mp-lamp ' + cls + '" data-mp-lamp="' + esc(proto)
            + '" title="' + esc(proto) + '"></span>';
        }).join('')
      + '</span>'
      + '<span class="mp-toggle-wrap">'
      + '<label class="mp-toggle"><input type="checkbox" data-mp-pool="' + esc(name)
      + '"' + checked + '><span>启用</span></label>'
      + '</span>'
      + '</div>';

    // 候选链（id / 优先级 / 类型 / 目标 / 证据态 / 运行态）
    var cands = def.candidates || [];
    var rtById = {};
    (((rt || {}).candidates) || []).forEach(function (rc) { rtById[rc.id] = rc; });
    if (cands.length) {
      html += '<div class="cp-table-wrap"><table class="if-table">'
        + '<thead><tr><th>候选</th><th>优先级</th><th>类型</th><th>目标</th><th>证据</th><th>运行态</th></tr></thead>'
        + '<tbody>';
      cands.forEach(function (c) {
        var evKeys = Object.keys(c.evidence || {});
        var evTxt = evKeys.length
          ? evKeys.map(function (k) { return esc(k) + ':' + esc(c.evidence[k]); }).join(' · ')
          : '—';
        html += '<tr><td class="mono">' + esc(c.id) + '</td>'
          + '<td class="mono">' + esc(c.priority) + '</td>'
          + '<td>' + esc(c.kind === 'local' ? '本地' : '云') + '</td>'
          + '<td class="mono">' + esc(c.target) + '</td>'
          + '<td class="mono">' + evTxt + '</td>'
          + '<td class="mono">' + runtimeBadge(rtById[c.id]) + '</td></tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<p class="mp-empty">无可解析候选（目标悬空或未配置）</p>';
    }

    // 悬空目标 / 校验问题
    (def.issues || []).forEach(function (i) {
      html += '<p class="mp-issue">注意：' + esc(i) + '</p>';
    });

    // 双协议当前选择视图：解释性（chosen 或错误 + 每候选排除原因）
    var protocols = (st && st.protocols) || {};
    ['openai', 'anthropic'].forEach(function (proto) {
      var pv = protocols[proto] || {};
      html += '<div class="mp-proto" data-mp-proto="' + esc(proto) + '">'
        + '<span class="mp-proto-name mono">' + esc(proto) + '</span>';
      if (pv.chosen) {
        html += '<span class="mp-chosen mono">当前选择：' + esc(pv.chosen) + '</span>';
      } else {
        html += '<span class="mp-nochosen mono">' + esc(pv.error || '无可用候选') + '</span>';
      }
      var decisions = pv.decisions || [];
      if (decisions.length) {
        html += '<ul class="mp-decisions">';
        decisions.forEach(function (d) {
          html += '<li class="mono"><b>' + esc(d.id) + '</b> — '
            + (d.included ? '纳入' : '排除：' + esc(d.reason)) + '</li>';
        });
        html += '</ul>';
      }
      html += '</div>';
    });

    html += '</div>';
    return html;
  }

  /* ── 渲染（拉取：定义 + 双协议状态 + 运行态；三态空态分支） ── */
  async function renderModelPools() {
    var box = $('mpPools');
    if (!box || _loading) return;
    _loading = true;
    try {
      var defRes = await fetch(POOLS_URL, { headers: UI.adminHeaders() });
      var stRes = await fetch(STATUS_URL, { headers: UI.adminHeaders() });
      var rtRes = await fetch(RUNTIME_URL, { headers: UI.adminHeaders() });
      if (!defRes.ok || !stRes.ok) {
        box.innerHTML = '<p class="mp-empty">拉取失败 · HTTP '
          + esc(defRes.status) + '/' + esc(stRes.status) + '</p>';
        return;
      }
      var def = await defRes.json();
      var st = await stRes.json();
      var rt = null;
      if (rtRes.ok) {
        try { rt = await rtRes.json(); } catch (e) { rt = null; }
      }
      _state.def = def;
      _state.status = st;
      _state.runtime = rt;
      _revision = def.revision;
      var revEl = $('mpRevision');
      if (revEl) revEl.textContent = String(def.revision);

      // 三态分支（spec §4.3）：
      // 1) 文件损坏 → 错误横幅（registry 保留最后有效快照继续服务）
      if (def.load_error) {
        box.innerHTML = '<div class="mp-error-banner">'
          + '<p><b>model_pools.yaml 加载失败</b>（registry 保留最后有效快照）：</p>'
          + '<p class="mono">' + esc(def.load_error) + '</p>'
          + '<p>修复文件后点击「重载配置」，或直接编辑 YAML。</p>'
          + '</div>';
        return;
      }
      var names = Object.keys(def.pools || {});
      // 2) 从未配置（无文件）→ 初始化向导占位（Task 7 实装向导本体）
      if (def.configured === false && def.raw == null && !names.length) {
        box.innerHTML = '<p class="mp-empty">逻辑模型池尚未配置 — '
          + '使用初始化向导或创建 model_pools.yaml 后热加载</p>';
        return;
      }
      // 3) 配置了零个池 → 普通空态
      if (!names.length) {
        box.innerHTML = '<p class="mp-empty">暂无逻辑模型池 — 配置 model_pools.yaml 后热加载即可</p>';
        return;
      }
      var stPools = (st && st.pools) || {};
      var rtPools = (rt && rt.pools) || {};
      var html = '';
      names.forEach(function (n) {
        html += poolCard(n, def.pools[n], stPools[n], rtPools[n]);
      });
      box.innerHTML = html;
    } catch (e) {
      box.innerHTML = '<p class="mp-empty">拉取异常：' + esc(e && e.message) + '</p>';
    } finally {
      _loading = false;
    }
  }

  /* ── 单池 enabled 开关（服务端原子翻转 + CAS） ── */
  async function doMpToggle(pool, enable) {
    if (_revision == null) {
      UI.toast('配置尚未加载完成，请稍后重试', 'error');
      return;
    }
    try {
      var res = await fetch(TOGGLE_URL, {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({
          pool: pool,
          enabled: enable,
          expected_revision: _revision,
        }),
      });
      var d = null;
      try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      if (res.ok) {
        _revision = d && typeof d.revision === 'number' ? d.revision : _revision;
        UI.toast('已' + (enable ? '启用' : '停用') + ' iff/' + pool, 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        // revision 冲突：配置已被其他修改更新 — 提示 + 重新拉取让用户看到最新状态
        UI.toast('配置已被其他修改更新（冲突），已刷新，请重试', 'error');
        await renderModelPools();
        return;
      }
      UI.toast((d && d.error) || ('开关失败 · HTTP ' + res.status), 'error');
    } catch (e) {
      UI.toast('开关请求异常：' + (e && e.message), 'error');
    }
  }

  /* ── 事件委托：mpPools 上 checkbox change → doMpToggle（无 inline onclick） ── */
  function onBoxChange(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var pool = t.getAttribute('data-mp-pool');
    if (pool) doMpToggle(pool, !!t.checked);
  }

  var box = $('mpPools');
  if (box && typeof box.addEventListener === 'function') {
    box.addEventListener('change', onBoxChange);
  }

  /* ── 注册 ── */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-model-pools'] = renderModelPools;
  window.renderModelPools = renderModelPools;
  window.doMpToggle = doMpToggle;
})();
