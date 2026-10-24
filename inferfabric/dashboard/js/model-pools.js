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
  var CATALOG_URL = '/admin/model-pools/catalog';
  var TOGGLE_URL = '/admin/model-pools/toggle';
  var VALIDATE_URL = '/admin/model-pools/validate';
  var DRYRUN_URL = '/admin/model-pools/dry-run';
  var UPDATE_URL = '/admin/model-pools';

  var _revision = null;   // 最近渲染到的配置 revision（toggle CAS 用）
  var _loading = false;   // 在途渲染去重（避免连点刷新叠发请求）
  // v7.1.0 运营视图单一数据源：def（含 configured/load_error/raw）+ 双协议
  // 选择视图 + 候选运行态；编辑器（Task 6）以 _state.def.raw 为读-改-写基底
  var _state = { def: null, status: null, runtime: null };
  // v7.1.0 三段式编辑器（Task 6）：
  //   _edit = {pool, base, work, catalog} — base=raw 深拷贝（编辑前），
  //   work=编辑中配置；_undo=最近一次成功 apply 的 base（会话内回滚）
  var _edit = null;
  var _undo = null;
  // A3 错误即导航（UI/UX P1 Task 3）：常驻行动横幅 — toast 即时反馈，
  // banner 承担「常驻 + 去哪修」；action = {label, run} 闭包（重试原操作）
  var _actionError = null;
  // B1 局部渲染（UI/UX P1 Task 4）：结构签名 + 定点更新 + 本地倒计时
  var _lastSig = null;    // 最近一次整块重建时的结构签名
  var _cd = {};           // 'pool|candId' → {pool, candId, total, base, rt}
  var _rtAt = 0;          // runtime 拉取时刻（Date.now()，倒计时锚点，评审 I7）
  var _cdTimer = null;
  // A2 行内 diff 编辑（UI/UX P2 Task 1）：目录缓存（null=未取/不可用，
  // 每次渲染 best-effort 重试；应用成功后置 null 触发重取）+ 行内会话
  var _catalog = null;
  //   _inline = {pool, candId, spec, base, work, base_revision, validated,
  //              previewHtml, rev} — spec/rev 进结构签名：换目标或预览
  //   完成 完成（启用应用按钮）都必须重建
  var _inline = null;

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

  // 候选行运行态（纯文本 — innerHTML 路径 esc 包裹，局部更新走 textContent）
  function runtimeBadgeText(rt) {
    if (!rt) return '—';
    var parts = [];
    if (rt.cooling) {
      var label = rt.cooling.reason === 'quarantine_404' ? '隔离'
        : (rt.cooling.probing ? '探测中' : '冷却');
      parts.push(label + ' ' + fmtRemaining(rt.cooling.remaining_s)
        + (rt.cooling.reason ? ' (' + rt.cooling.reason + ')' : ''));
    }
    parts.push(rt.active
      ? (rt.kind === 'local' ? '服务中' : '已发现')
      : (rt.kind === 'local' ? '未启动' : '未发现'));
    if (rt.last_attempt && !rt.last_attempt.ok) {
      parts.push('HTTP ' + rt.last_attempt.status);
    }
    return parts.join(' · ');
  }

  // 状态灯五态（UI/UX P1，用户拍板 #3）：
  // probe（半开探测）> cool（冷却/隔离）> flow（真实数据交换：last_attempt
  // 成功且 <30s）> ok（routable 空闲）> off（未启动/未发现，unroutable）
  function candDotClass(rt) {
    if (!rt) return 'mp-dot-off';
    if (rt.cooling) {
      return rt.cooling.probing ? 'mp-dot-probe' : 'mp-dot-cool';
    }
    var la = rt.last_attempt;
    if (la && la.ok && typeof la.age_s === 'number' && la.age_s < 30) {
      return 'mp-dot-flow';
    }
    return rt.active ? 'mp-dot-ok' : 'mp-dot-off';
  }

  // 协议徽章来源：声明协议优先，回退 evidence 键（GET 视图无 declared 字段时）
  function candProtocols(c) {
    if (Array.isArray(c.declared_protocols) && c.declared_protocols.length) {
      return c.declared_protocols;
    }
    return Object.keys(c.evidence || {});
  }

  // A2 行内下拉选项（与编辑器 opts 同源同格式 — 目录是唯一元数据总线）
  function inlineOptions() {
    var o = '';
    (_catalog.localValues || []).forEach(function (v) {
      o += '<option value="' + esc(v) + '">本地 · '
        + esc(v.slice('local:'.length)) + '</option>';
    });
    (_catalog.cloudValues || []).forEach(function (v) {
      o += '<option value="' + esc(v) + '">云 · '
        + esc(v.slice('cloud:'.length)) + '</option>';
    });
    return o;
  }

  // A2 行内 diff 预览条：旧→新（红/绿）+ 自动预览结果 + 应用/撤销。
  // validated 经 _inline.rev 进结构签名 — 预览完成 → 重建以启用应用按钮
  function inlineDiffBar(pool, oldCand) {
    var n = _inline;
    var newC = candFromCatalogValue(n.spec, n.candId, _catalog);
    var newTarget = newC.kind === 'local' ? newC.model_name
      : (newC.provider + '/' + newC.model_id);
    // 旧目标：视图候选带 target；raw 候选（防御）按 kind 展开
    var oldTarget = oldCand.target
      ? oldCand.target
      : (oldCand.kind === 'local' ? oldCand.model_name
         : (oldCand.provider + '/' + oldCand.model_id));
    return '<div class="mp-inline-diff" data-mp-inline-bar="' + esc(pool)
      + '|' + esc(n.candId) + '">'
      + '<span class="mp-diff-old mono">' + esc(oldTarget) + '</span>'
      + '<span class="mp-diff-arrow">→</span>'
      + '<span class="mp-diff-new mono">' + esc(newTarget) + '</span>'
      + '<span class="mp-inline-preview" id="mp-inline-preview-'
      + esc(pool) + '-' + esc(n.candId) + '">'
      + (n.previewHtml || '预览中…') + '</span>'
      + '<button type="button" data-mp-inline-apply="' + esc(pool) + '|'
      + esc(n.candId) + '"' + (n.validated ? '' : ' disabled')
      + ' title="需先通过预览">应用</button>'
      + '<button type="button" data-mp-inline-cancel="1">撤销</button>'
      + '</div>';
  }

  // 候选体检卡（UI/UX P1 Task 2：表格 → 横向卡片，状态一眼可读）
  function candCard(pool, c, rt) {
    var dot = candDotClass(rt);
    var ev = c.evidence || {};
    var html = '<div class="mp-cand-card'
      + ((rt && rt.cooling) ? ' mp-cand-degraded' : '')
      + '" data-mp-cand-card="' + esc(c.id) + '">'
      + '<span class="mp-dot ' + dot + '" id="mp-dot-'
      + esc(pool) + '-' + esc(c.id) + '"></span>'
      + '<span class="mp-cand-main">'
      + '<span class="mono mp-cand-target">' + esc(c.target) + '</span>'
      + '<span class="mp-cand-kind">'
      + esc(c.kind === 'local' ? '本地' : '云') + '</span>'
      + '<span class="mono mp-cand-pri">P' + esc(c.priority) + '</span>'
      + candProtocols(c).map(function (p) {
          return '<span class="mp-proto-badge" title="'
            + (ev[p] ? esc(ev[p]) : '') + '">' + esc(p) + '</span>';
        }).join('')
      + (c.features || []).map(function (f) {
          return '<span class="mp-feat">' + esc(f) + '</span>';
        }).join('')
      + '</span>'
      + '<span class="mp-cand-rt mono" id="mp-rt-' + esc(pool) + '-'
      + esc(c.id) + '">' + esc(runtimeBadgeText(rt)) + '</span>';
    // 环是 mp-rt 的兄弟节点（评审 I1）：textContent 定点更新不得删除环
    if (rt && rt.cooling) {
      html += '<span class="mp-ring" id="mp-ring-' + esc(pool) + '-'
        + esc(c.id) + '" data-mp-ring="' + esc(pool) + '|'
        + esc(c.id) + '"></span>';
    }
    // A2 行内编辑入口：卡内「替换为…」下拉。目录手势惰性预热（Task 1
    // 裁决）：null=未预热（占位可选，mouseover/focusin 即拉取）；
    // false=拉取失败（禁用 + 明示）；object=就绪出选项
    html += '<select class="mp-inline-sel" data-mp-inline-replace="'
      + esc(pool) + '|' + esc(c.id) + '"'
      + (_catalog === false ? ' disabled' : '') + '>'
      + (_catalog
          ? '<option value="">替换为…</option>' + inlineOptions()
          : _catalog === false
            ? '<option value="">目录不可用 — 请用编辑器</option>'
            : '<option value="">替换为…</option>')
      + '</select>';
    html += '</div>';
    return html;
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
          return '<span class="mp-lamp ' + cls + '" id="mp-lamp-' + esc(name)
            + '-' + esc(proto) + '" data-mp-lamp="' + esc(proto)
            + '" title="' + esc(proto) + '"></span>';
        }).join('')
      + '</span>'
      + '<span class="mp-toggle-wrap">'
      + '<button type="button" class="mp-edit-btn" data-mp-edit="' + esc(name)
      + '">编辑</button>'
      + '<button type="button" class="mp-edit-btn" data-mp-snippet="' + esc(name)
      + '">接入片段</button>'
      + '<label class="mp-toggle"><input type="checkbox" data-mp-pool="' + esc(name)
      + '"' + checked + '><span>启用</span></label>'
      + '</span>'
      + '</div>';

    // 候选链（id / 优先级 / 类型 / 目标 / 证据态 / 运行态）
    var cands = def.candidates || [];
    var rtById = {};
    (((rt || {}).candidates) || []).forEach(function (rc) { rtById[rc.id] = rc; });
    if (cands.length) {
      html += '<div class="mp-cands">';
      cands.forEach(function (c) {
        html += candCard(name, c, rtById[c.id]);
        // A2：命中行内编辑会话 → 卡后渲染 diff 预览条
        if (_inline && _inline.pool === name && _inline.candId === c.id) {
          html += inlineDiffBar(name, c);
        }
      });
      html += '</div>';
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
        html += '<span class="mp-chosen mono" id="mp-chosen-' + esc(name)
          + '-' + esc(proto) + '">当前选择：' + esc(pv.chosen) + '</span>';
      } else {
        html += '<span class="mp-nochosen mono" id="mp-chosen-' + esc(name)
          + '-' + esc(proto) + '">' + esc(pv.error || '无可用候选') + '</span>';
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

  /* ── B1 局部渲染（Task 4）：结构签名 + applyDynamic 定点更新 + 本地倒计时 ── */

  // 结构签名（评审 I2/I3/I4 扩展）：池名 + enabled + issues 全文 +
  // 每候选（id/kind/优先级/目标/声明协议/能力/冷却在否）+ 每协议
  // （chosen + decisions 全文）+ 冷却在否。运行态展示字段 applyDynamic
  // 不全更新的（issues/target/decisions/环/降级）一律进签名 → 变化即重建。
  // 签名不变 → 不重写 mpPools.innerHTML（轮询零闪烁），只走 applyDynamic。
  function structSig(def, stPools, rtPools) {
    return Object.keys(def.pools || {}).map(function (n) {
      var p = def.pools[n];
      var stP = (stPools || {})[n] || {};
      var rtP = (rtPools || {})[n] || {};
      var rtById = {};
      (rtP.candidates || []).forEach(function (rc) { rtById[rc.id] = rc; });
      return n + ':' + (p.enabled ? 1 : 0)
        + ':' + (p.issues || []).join(';')
        + ':' + (p.candidates || []).map(function (c) {
            var rt = rtById[c.id];
            var tgt = c.target
              || (c.kind === 'local' ? (c.model_name || '')
                  : ((c.provider || '') + '/' + (c.model_id || '')));
            return [c.id, c.kind || '', c.priority || 0, tgt,
                    (c.declared_protocols || []).join(','),
                    (c.features || []).join(','),
                    (rt && rt.cooling) ? 'cool' : 'live'].join('~');
          }).join(',')
        + ':' + ['openai', 'anthropic'].map(function (proto) {
            var pv = (stP.protocols || {})[proto] || {};
            // chosen 不进签名（applyDynamic 定点更新）；decisions 进签名
            //（评审 I4 — 纳入/排除原因变化必须重建，否则解释停留首屏快照）
            return proto + '>' + (pv.decisions || []).map(function (d) {
                  return d.id + (d.included ? '+' : '-') + (d.reason || '');
                }).join(',');
          }).join('|');
    }).join('#');
  }

  // 冷却快照：首见锚定总时长 total；runtime 重取 → base 重锚（评审 I8a：
  // 更长剩余 = 新一轮冷却 → 重锚 total，环形百分比不超 100%）+ 锚点时刻刷新；
  // 冷却结束的条目清除。
  function cdSnapshot(rtPools) {
    var live = {};
    Object.keys(rtPools || {}).forEach(function (n) {
      ((rtPools[n] || {}).candidates || []).forEach(function (rc) {
        if (!rc.cooling || typeof rc.cooling.remaining_s !== 'number') return;
        var key = n + '|' + rc.id;
        live[key] = true;
        if (!_cd[key]) {
          _cd[key] = {pool: n, candId: rc.id, total: rc.cooling.remaining_s,
                      base: rc.cooling.remaining_s, rt: rc};
        } else {
          if (rc.cooling.remaining_s > _cd[key].base) {
            _cd[key].total = rc.cooling.remaining_s;
          }
          _cd[key].base = rc.cooling.remaining_s;
          _cd[key].rt = rc;
        }
      });
    });
    Object.keys(_cd).forEach(function (k) { if (!live[k]) delete _cd[k]; });
    _rtAt = Date.now();   // 评审 I7：以 runtime 拉取时刻为锚
  }

  // 按 id 定点更新（签名不变的轮询路径）：灯 className / 状态灯 / 运行态文本 /
  // 当前选择 / revision — 零 innerHTML 重写
  function applyDynamic(def, stPools, rtPools) {
    Object.keys(def.pools || {}).forEach(function (n) {
      var poolDef = def.pools[n];
      var protocols = (stPools[n] && stPools[n].protocols) || {};
      ['openai', 'anthropic'].forEach(function (proto) {
        var lamp = $('mp-lamp-' + n + '-' + proto);
        if (lamp) {
          lamp.className = 'mp-lamp '
            + lampForProtocol(poolDef, protocols[proto]);
        }
        var pv = protocols[proto] || {};
        var chosen = $('mp-chosen-' + n + '-' + proto);
        if (chosen) {
          chosen.className = pv.chosen ? 'mp-chosen mono' : 'mp-nochosen mono';
          chosen.textContent = pv.chosen ? ('当前选择：' + pv.chosen)
            : (pv.error || '无可用候选');
        }
      });
      var rtById = {};
      ((rtPools[n] || {}).candidates || []).forEach(function (rc) {
        rtById[rc.id] = rc;
      });
      (poolDef.candidates || []).forEach(function (c) {
        var rt = rtById[c.id];
        var dot = $('mp-dot-' + n + '-' + c.id);
        if (dot) dot.className = 'mp-dot ' + candDotClass(rt);
        var rtEl = $('mp-rt-' + n + '-' + c.id);
        if (rtEl) rtEl.textContent = runtimeBadgeText(rt);
      });
    });
    var revEl = $('mpRevision');
    if (revEl && typeof def.revision !== 'undefined') {
      revEl.textContent = String(def.revision);
    }
  }

  // 本地倒计时（1s interval）：以 runtime 拉取时刻为锚按流逝秒数递减
  // remaining_s，更新 mp-rt-* 文本与环形 --p，不发出任何 fetch
  function applyCountdown(elapsed) {
    Object.keys(_cd).forEach(function (key) {
      var e = _cd[key];
      if (!e.rt) return;
      var remaining = Math.max(0, e.base - elapsed);
      var rt2 = Object.assign({}, e.rt, {
        cooling: Object.assign({}, e.rt.cooling, {remaining_s: remaining}),
      });
      var rtEl = $('mp-rt-' + e.pool + '-' + e.candId);
      if (rtEl) rtEl.textContent = runtimeBadgeText(rt2);
      var ring = $('mp-ring-' + e.pool + '-' + e.candId);
      if (ring && ring.style && ring.style.setProperty) {
        // (e.total || 1)：remaining_s=0 首见时 total=0，除零防护（评审 I8b）
        ring.style.setProperty('--p',
          String(Math.round(remaining / (e.total || 1) * 100)));
      }
    });
  }

  function cdTick() {
    try {
      if (typeof document !== 'undefined' && document.hidden) return;
      if (window.store && typeof window.store.get === 'function'
          && window.store.get('tab_active') !== 'tab-model-pools') return;
      if (_wizard) return;
      // 评审 I7：按真实流逝时间递减（非执行次数计数——隐藏/切 tab 期间
      // 计时器被节流，锚点差值仍反映真实时间）
      var elapsed = Math.floor((Date.now() - _rtAt) / 1000);
      if (elapsed <= 0) return;
      applyCountdown(elapsed);
    } catch (e) { /* 倒计时失败静默 — 下个滴答重试 */ }
  }

  // 懒启动：首次整块重建后才开始（无池渲染不空转）
  function startCountdown() {
    if (_cdTimer || typeof setInterval !== 'function') return;
    _cdTimer = setInterval(cdTick, 1000);
  }

  /* ── A3 行动横幅（Task 3）：常驻 + 去哪修；样式复用 mp-error-banner ── */
  function setActionError(message, hint, action) {
    _actionError = {message: message, hint: hint || null, action: action || null};
    return renderModelPools();   // 立即可见（不等 5s 轮询）
  }

  function clearActionError() {
    _actionError = null;
  }

  function bannerHtml() {
    var a = _actionError;
    var html = '<div class="mp-error-banner mp-action-banner" id="mp-action-banner">'
      + '<p><span class="mp-action-msg">' + esc(a.message) + '</span>'
      + (a.hint ? ' <span class="mp-action-hint">' + esc(a.hint) + '</span>' : '')
      + '</p>';
    if (a.action) {
      html += '<button type="button" data-mp-banner-action="1">'
        + esc(a.action.label || '重试') + '</button>';
    }
    html += '<button type="button" data-mp-banner-dismiss="1">关闭</button>';
    return html + '</div>';
  }

  async function doMpBannerAction() {
    if (!_actionError || !_actionError.action) return;
    var run = _actionError.action.run;
    _actionError = null;   // 先清（重试成功保持清；失败会重新置）
    await run();
  }

  function doMpBannerDismiss() {
    _actionError = null;
    return renderModelPools();
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
        _lastSig = null;   // 评审 I5：错误页替换了面板 → 失效签名，恢复后必须重建
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
      // 离开向导态（配置出现/文件损坏）时清除陈旧向导状态 — 否则
      // 残留 _wizard 既挡轮询又让「创建这些池」提交陈旧选择（终审修复）
      _wizard = null;   // 向导分支内 renderWizard() 会重新建立
      if (def.load_error) {
        _lastSig = null;   // 评审 I5：错误横幅替换面板 → 失效签名
        box.innerHTML = '<div class="mp-error-banner">'
          + '<p><b>model_pools.yaml 加载失败</b>（registry 保留最后有效快照）：</p>'
          + '<p class="mono">' + esc(def.load_error) + '</p>'
          + '<p>修复文件后点击「重载配置」，或直接编辑 YAML。</p>'
          + '</div>';
        return;
      }
      var names = Object.keys(def.pools || {});
      // 2) 从未配置（无文件）→ 初始化向导（Task 7）
      if (def.configured === false && def.raw == null && !names.length) {
        _lastSig = null;   // 评审 I5：向导页替换面板 → 失效签名，恢复后必须重建
        await renderWizard();
        return;
      }
      // 3) 配置了零个池 → 普通空态
      if (!names.length) {
        _lastSig = null;
        box.innerHTML = '<p class="mp-empty">暂无逻辑模型池 — 配置 model_pools.yaml 后热加载即可</p>';
        return;
      }
      // A2：目录不做渲染期拉取（Task 1 裁决 — 既有轮询 GET 计数是契约），
      // 手势惰性预热：box mouseover/focusin → primeInline()
      var stPools = (st && st.pools) || {};
      var rtPools = (rt && rt.pools) || {};
      cdSnapshot(rtPools);
      // B1 局部渲染（Task 4）：结构签名不变 → 只走 applyDynamic 定点更新，
      // 不重写 innerHTML（轮询零闪烁）；banner 消息/提示计入签名（评审 I6 —
      // 内容变了的横幅必须重建，否则旧原因配新行动闭包）；A2 目录在否与
      // 行内会话（池|候选|spec|rev）计入签名 — 下拉出现/diff 条/应用按钮
      // 启用都是结构变化
      var sig = structSig(def, stPools, rtPools)
        + '|cat:' + (_catalog ? '1' : _catalog === false ? '2' : '0')
        + '|inline:' + (_inline
            ? _inline.pool + '|' + _inline.candId + '|' + _inline.spec
              + '|' + _inline.rev
            : '')
        + '|'
        + (_actionError
            ? 'banner:' + _actionError.message + '|' + (_actionError.hint || '')
            : '');
      if (sig === _lastSig) {
        applyDynamic(def, stPools, rtPools);
        return;
      }
      _lastSig = sig;
      var html = '';
      if (_actionError) html += bannerHtml();   // A3：渲染池列表前输出行动横幅
      names.forEach(function (n) {
        html += poolCard(n, def.pools[n], stPools[n], rtPools[n]);
      });
      box.innerHTML = html;
      startCountdown();
    } catch (e) {
      _lastSig = null;   // 评审 I5：异常页替换面板 → 失效签名
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
        clearActionError();
        _revision = d && typeof d.revision === 'number' ? d.revision : _revision;
        UI.toast('已' + (enable ? '启用' : '停用') + ' iff/' + pool, 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        // revision 冲突：配置已被其他修改更新 — 常驻横幅（重试原操作）+ 重新拉取
        await setActionError('配置已被其他修改更新（冲突）',
          '已刷新，基于最新配置重试',
          {label: '重试', run: function () { return doMpToggle(pool, enable); }});
        UI.toast('配置已被其他修改更新（冲突），已刷新，请重试', 'error');
        return;
      }
      var msg = (d && d.error) || ('开关失败 · HTTP ' + res.status);
      UI.toast(msg, 'error');
      await setActionError(msg, null,
        {label: '重试', run: function () { return doMpToggle(pool, enable); }});
    } catch (e) {
      UI.toast('开关请求异常：' + (e && e.message), 'error');
      await setActionError('开关请求异常：' + (e && e.message), null,
        {label: '重试', run: function () { return doMpToggle(pool, enable); }});
    }
  }

  /* ── A2 行内 diff 编辑（UI/UX P2 Task 1）：两步换模，不离开列表 ── */

  // 目录惰性预热（Task 1 裁决）：并发共享一个在途 promise；
  // 成功 → object，失败 → false（下次手势可重试）
  var _catPromise = null;

  function ensureCatalog() {
    if (_catalog) return Promise.resolve(_catalog);
    if (!_catPromise) {
      _catPromise = fetchCandidateCatalog()
        .then(function (c) { _catalog = c; _catPromise = null; return c; })
        .catch(function () {
          _catalog = false; _catPromise = null; return false;
        });
    }
    return _catPromise;
  }

  // 手势入口（box mouseover/focusin）：取目录 → 重渲染出选项
  async function primeInline() {
    if (_catalog) return;
    await ensureCatalog();
    await renderModelPools();
  }

  async function doMpInlineSelect(pool, candId, spec) {
    if (!_state.def) {
      UI.toast('配置尚未加载完成，请稍后重试', 'error');
      return;
    }
    var cat = await ensureCatalog();
    if (!cat) {
      UI.toast('目录不可用 — 请使用编辑器高级模式', 'error');
      return;
    }
    var base = _state.def.raw ? deepCopy(_state.def.raw)
      : { schema_version: 1, pools: {} };
    if (!base.pools || typeof base.pools !== 'object') base.pools = {};
    var cands = (base.pools[pool] || {}).candidates;
    if (!Array.isArray(cands)) {
      UI.toast('候选不存在：' + candId, 'error');
      return;
    }
    var i = cands.findIndex(function (c) { return c.id === candId; });
    if (i < 0) {
      UI.toast('候选不存在：' + candId, 'error');
      return;
    }
    var nc = candFromCatalogValue(spec, candId, _catalog);
    nc.priority = cands[i].priority;   // D4 一键换模：继承原优先级
    var work = deepCopy(base);
    work.pools[pool].candidates[i] = deepCopy(nc);
    // CAS 基点在选中时捕获（与编辑器打开时固定基点同语义 — 后台刷新
    // 推进 revision 不得让陈旧 work 借新基点覆盖他人修改）
    _inline = { pool: pool, candId: candId, spec: spec, base: base,
                work: work, base_revision: _state.def.revision,
                validated: false, previewHtml: '', rev: 0 };
    await renderModelPools();          // 重建出 diff 条
    var session = _inline;
    var out = await previewWork(work, pool);   // 自动预览（三段式门禁）
    if (_inline !== session) return;
    _inline.previewHtml = out.code === 'error'
      ? '<span class="mp-issue">预览请求异常：' + esc(out.message) + '</span>'
      : out.html;
    _inline.validated = out.validated;
    _inline.rev += 1;                  // 进签名 → 重建（启用/禁用应用按钮）
    await renderModelPools();
  }

  async function doMpInlineApply() {
    if (!_inline) return;
    if (!_inline.validated) {
      UI.toast('请先通过预览，再应用', 'error');
      return;
    }
    var session = _inline;   // 会话令牌：在途时可能被取消/重选
    var poolName = session.pool;
    var body = deepCopy(session.work);
    body.expected_revision = session.base_revision;
    try {
      var res = await fetch(UPDATE_URL, {
        method: 'POST', headers: UI.adminHeaders(),
        body: JSON.stringify(body),
      });
      var d = null;
      try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      if (_inline !== session) {
        // 陈旧完成：请求已发出并生效，但行内会话已换 — 只提示 + 刷新
        if (res.ok) {
          UI.toast('已更新 iff/' + poolName, 'ok');
          await renderModelPools();
        }
        return;
      }
      if (res.ok) {
        _undo = session.base;
        _inline = null;
        _catalog = null;   // 配置已变 → 目录缓存失效，下次手势重取
        clearActionError();
        UI.toast('已更新 iff/' + poolName, 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        // 基点过期：本 work 不可再应用（重试仍 409）— 横幅引导基于最新
        // 配置重新选择；diff 条保留供对照
        await setActionError('配置已被其他修改更新（冲突）',
          '已刷新，请重新选择替换目标',
          {label: '重新选择', run: function () {
             _inline = null;
             return renderModelPools();
           }});
        UI.toast('配置已被其他修改更新（冲突），请重新选择', 'error');
        await renderModelPools();
        return;
      }
      var msg = (d && d.error) || ('更新失败 · HTTP ' + res.status);
      UI.toast(msg, 'error');
      await setActionError(msg, null,
        {label: '重试', run: function () { return doMpInlineApply(); }});
    } catch (e) {
      UI.toast('更新请求异常：' + (e && e.message), 'error');
      await setActionError('更新请求异常：' + (e && e.message), null,
        {label: '重试', run: function () { return doMpInlineApply(); }});
    }
  }

  async function doMpInlineCancel() {
    if (!_inline) return;
    _inline = null;
    await renderModelPools();
  }

  /* ── 三段式编辑器（Task 6）：编辑 → 预览 → 应用/回滚 ── */

  function deepCopy(o) { return JSON.parse(JSON.stringify(o)); }

  // 默认池骨架（编辑器新建池 / 向导创建共用）
  function newPoolSkeleton() {
    return {
      enabled: true, strategy: 'priority',
      allowed_locations: ['local', 'cloud'],
      accept_declared: true, allow_cross_location: false,
      max_attempts: 2, deadline_ms: 60000, candidates: [],
    };
  }

  /* ── 空态初始化向导（Task 7）：从未配置 → 四角色引导创建 ── */

  var _wizard = null;   // {roles:{name:{desc,selected}}, removed:{}, catalog}

  var _ROLE_DEFS = [
    { name: 'fast', desc: '快速响应 — 日常对话与轻任务' },
    { name: 'premium', desc: '旗舰质量 — 复杂推理与长文' },
    { name: 'small', desc: '小模型 — 分类/抽取/批量任务' },
    { name: 'classifier', desc: '分类判别 — 路由/打标/判别' },
  ];

  // 模型目录（/models 全量条目 + 云 provider 模型，best-effort）
  // 向导消费形态：本地条目对象（预填启发式用 active/context_window）+ 云选择键
  async function fetchWizardModels() {
    var cat = null;
    try { cat = await fetchCatalog(); } catch (e) { cat = null; }
    if (!cat) {
      return { local: [], cloud: [], cloudValues: [], index: {} };
    }
    return { local: cat.local, cloud: cat.cloud, cloudValues: cat.cloudValues,
             index: cat.index };
  }

  // 预填启发式（可改；仅建议值）：fast=活跃小模型 / premium=大上下文本地，
  // 无本地则首个云 / small=最小 / classifier=首个活跃
  function _prefill(catalog) {
    var llms = catalog.local;
    var act = llms.filter(function (m) { return !!m.active; });
    function pick(list, wantMax) {
      if (!list.length) {
        return catalog.cloudValues.length ? catalog.cloudValues[0] : null;
      }
      var sorted = list.slice();
      var allSized = sorted.every(function (m) {
        return typeof m.context_window === 'number';
      });
      if (allSized) {
        sorted.sort(function (a, b) {
          return wantMax ? b.context_window - a.context_window
            : a.context_window - b.context_window;
        });
      } else if (!wantMax) {
        sorted.reverse();   // 无尺寸信息：表尾视作小模型（models.d 惯例）
      }
      return 'local:' + sorted[0].name;
    }
    return {
      fast: pick(act.length ? act : llms, false),
      premium: pick(llms, true),
      small: pick(llms, false),
      classifier: act.length ? 'local:' + act[0].name : pick(llms, false),
    };
  }

  async function renderWizard() {
    var catalog = await fetchWizardModels();
    var pre = _prefill(catalog);
    var roles = {};
    _ROLE_DEFS.forEach(function (rd) {
      roles[rd.name] = { desc: rd.desc, selected: pre[rd.name] };
    });
    _wizard = { roles: roles, removed: {}, catalog: catalog };
    renderWizardHtml();
  }

  function renderWizardHtml() {
    var box = $('mpPools');
    if (!box || !_wizard) return;
    var html = _actionError ? bannerHtml() : '';   // A3：向导模式同样常驻横幅
    html += '<div class="mp-wizard">'
      + '<p class="mp-wizard-intro">逻辑模型池尚未配置。为常用角色创建池后，'
      + 'agent 产品只需配置一次 <span class="mono">iff/&lt;角色&gt;</span>，'
      + '日后换模型在这里改，客户端零改动。</p>';
    _ROLE_DEFS.forEach(function (rd) {
      var st = _wizard.roles[rd.name];
      if (!st) return;   // 已移除
      var opts = '<option value="">（不创建）</option>';
      _wizard.catalog.local.forEach(function (m) {
        var v = 'local:' + m.name;
        opts += '<option value="' + esc(v) + '"'
          + (st.selected === v ? ' selected' : '')
          + '>本地 · ' + esc(m.name) + (m.active ? '（服务中）' : '')
          + '</option>';
      });
      _wizard.catalog.cloudValues.forEach(function (v) {
        var n = v.slice('cloud:'.length);
        opts += '<option value="' + esc(v) + '"'
          + (st.selected === v ? ' selected' : '')
          + '>云 · ' + esc(n) + '</option>';
      });
      html += '<div class="mp-wizard-role" data-mp-role="' + esc(rd.name) + '">'
        + '<span class="mono"><b>iff/' + esc(rd.name) + '</b></span>'
        + '<span class="mp-wizard-desc">' + esc(st.desc) + '</span>'
        + '<select data-mp-wizard-sel="' + esc(rd.name) + '">' + opts + '</select>'
        + '<button type="button" data-mp-wizard-del="' + esc(rd.name) + '">移除</button>'
        + '</div>';
    });
    var removedNames = Object.keys(_wizard.removed);
    if (removedNames.length) {
      html += '<div class="mp-wizard-removed">已移除的角色：';
      removedNames.forEach(function (n) {
        html += '<button type="button" data-mp-wizard-add="' + esc(n) + '">恢复 '
          + esc(n) + '</button>';
      });
      html += '</div>';
    }
    html += '<div class="mp-edit-actions">'
      + '<button type="button" data-mp-wizard-submit="1">创建这些池</button>'
      + '</div></div>';
    box.innerHTML = html;
  }

  // 角色移除/恢复（提交只 POST 仍在 roles 里的）
  function doMpWizardToggle(role) {
    if (!_wizard) return;
    if (_wizard.roles[role]) {
      _wizard.removed[role] = _wizard.roles[role];
      delete _wizard.roles[role];
    } else if (_wizard.removed[role]) {
      _wizard.roles[role] = _wizard.removed[role];
      delete _wizard.removed[role];
    }
    renderWizardHtml();
  }

  async function doMpWizardSubmit() {
    if (!_wizard) {
      UI.toast('向导未初始化（配置文件损坏时不可用向导）', 'error');
      return;
    }
    var session = _wizard;   // 会话令牌：在途期间向导态可能被清除/重建
    var pools = {};
    Object.keys(_wizard.roles).forEach(function (role) {
      var sel = _wizard.roles[role].selected;
      if (!sel) return;   // 无候选的角色跳过
      var c = candFromCatalogValue(sel, role + '-1', _wizard.catalog);
      c.priority = 1;
      var sk = newPoolSkeleton();
      sk.candidates = [c];
      pools[role] = sk;
    });
    if (!Object.keys(pools).length) {
      UI.toast('请至少为一个角色选择候选模型', 'error');
      return;
    }
    // 创建语义 = 从零建立：固定 expected_revision=0（终审修复 — 即便快照
    // revision 异常残留也不带病提交；错则 409 可重试，不会覆盖他人配置）
    var body = {
      schema_version: 1,
      pools: pools,
      expected_revision: 0,
    };
    try {
      var res = await fetch(UPDATE_URL, {
        method: 'POST', headers: UI.adminHeaders(),
        body: JSON.stringify(body),
      });
      var d = null;
      try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      if (_wizard !== session) {
        // 向导态已变（被重渲染清除/重建）— 只提示 + 刷新，不碰当前状态
        if (res.ok) {
          clearActionError();
          UI.toast('已创建 ' + Object.keys(pools).length + ' 个逻辑模型池', 'ok');
          await renderModelPools();
        }
        return;
      }
      if (res.ok) {
        _wizard = null;
        clearActionError();
        UI.toast('已创建 ' + Object.keys(pools).length + ' 个逻辑模型池', 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        await setActionError('配置已被其他修改更新（冲突）',
          '已刷新，基于最新配置重试',
          {label: '重试', run: function () { return doMpWizardSubmit(); }});
        UI.toast('配置已被其他修改更新（冲突），已刷新，请重试', 'error');
        return;
      }
      var msg = (d && d.error) || ('创建失败 · HTTP ' + res.status);
      UI.toast(msg, 'error');
      await setActionError(msg, null,
        {label: '重试', run: function () { return doMpWizardSubmit(); }});
    } catch (e) {
      UI.toast('创建请求异常：' + (e && e.message), 'error');
      await setActionError('创建请求异常：' + (e && e.message), null,
        {label: '重试', run: function () { return doMpWizardSubmit(); }});
    }
  }

  // 候选目录（best-effort：拉取失败 → 手填降级，编辑器不因此不可用）
  /* 候选目录（v7.1.2）：唯一数据源 = GET /admin/model-pools/catalog。
     旧版前端自行刮 /models 与 /admin/cloud/providers 逆向猜 schema，
     云目录曾因字段不符静默断掉 — 目录是池域概念，由后端权威构建。 */
  async function fetchCatalog() {
    var r = await fetch(CATALOG_URL, { headers: UI.adminHeaders() });
    if (!r.ok) return null;
    var d = await r.json();
    if (!d || !Array.isArray(d.local) || !Array.isArray(d.cloud)) return null;
    // 派生选择键（HTML select value）：'local:<served_name>' /
    // 'cloud:<provider>/<model_id>'；index 供候选生成查协议/能力
    var index = {};
    d.local.forEach(function (e) {
      e._value = 'local:' + e.name;
      index[e._value] = e;
    });
    d.cloud.forEach(function (e) {
      e._value = 'cloud:' + e.provider + '/' + e.model_id;
      index[e._value] = e;
    });
    d.localValues = d.local.map(function (e) { return e._value; });
    d.cloudValues = d.cloud.map(function (e) { return e._value; });
    d.index = index;
    return d;
  }

  // 编辑器消费形态：字符串数组（下拉）+ index（候选生成）
  async function fetchCandidateCatalog() {
    var cat = await fetchCatalog();
    if (!cat) return null;
    return { localValues: cat.localValues, cloudValues: cat.cloudValues,
             index: cat.index };
  }

  // 目录项 → 候选对象（'local:qwen-small' / 'cloud:prov/model-id'）
  function candFromCatalogValue(spec, id, catalog) {
    var entry = (catalog && catalog.index) ? catalog.index[spec] : null;
    // 协议/能力来自目录（云=发现结果，本地=引擎映射）；目录缺失时保守
    // 只声明 openai（declared 态，证据系统可探实）
    var protocols = (entry && entry.protocols) || ['openai'];
    var features = (entry && entry.features) || [];
    var i = spec.indexOf(':');
    var kind = spec.slice(0, i);
    var target = spec.slice(i + 1);
    if (kind === 'cloud') {
      var j = target.indexOf('/');
      return { id: id, kind: 'cloud', provider: target.slice(0, j),
               model_id: target.slice(j + 1),
               declared_protocols: protocols, features: features };
    }
    return { id: id, kind: 'local', model_name: target,
             declared_protocols: protocols, features: features };
  }

  // 编辑器渲染：#mpModal（候选链操作 + 池参数 + 预览/应用/回滚）
  function renderEditor() {
    var modal = $('mpModal');
    if (!modal || !_edit) return;
    var wp = _edit.work.pools[_edit.pool];
    var rtById = {};
    ((((_state.runtime || {}).pools || {})[_edit.pool] || {}).candidates || [])
      .forEach(function (rc) { rtById[rc.id] = rc; });

    var html = '<div class="mp-editor" data-mp-editor="' + esc(_edit.pool) + '">'
      + '<div class="mp-editor-hdr"><b class="mono">iff/' + esc(_edit.pool)
      + '</b><span>编辑候选链（改完先预览，再应用）</span></div>';

    // 候选链：id / 优先级 / 目标 / 运行态 / 操作（替换=行内下拉选中即换）
    var cands = wp.candidates || [];
    var opts = function (sel) {
      var catalog = _edit.catalog || {};
      var o = '';
      (catalog.localValues || []).forEach(function (v) {
        var n = v.slice('local:'.length);
        o += '<option value="' + esc(v) + '"' + (sel === v ? ' selected' : '')
          + '>本地 · ' + esc(n) + '</option>';
      });
      (catalog.cloudValues || []).forEach(function (v) {
        var n = v.slice('cloud:'.length);
        o += '<option value="' + esc(v) + '"' + (sel === v ? ' selected' : '')
          + '>云 · ' + esc(n) + '</option>';
      });
      if (!o) o = '<option value="">（目录不可用 — 请用下方新增候选）</option>';
      return o;
    };
    html += '<div class="cp-table-wrap"><table class="if-table">'
      + '<thead><tr><th>候选</th><th>优先级</th><th>类型</th><th>目标</th>'
      + '<th>运行态</th><th>操作</th></tr></thead><tbody>';
    cands.forEach(function (c, idx) {
      var target = c.kind === 'local' ? c.model_name
        : (c.provider + '/' + c.model_id);
      var sel = c.kind + ':' + target;
      html += '<tr><td class="mono">' + esc(c.id) + '</td>'
        + '<td class="mono">' + esc(c.priority) + '</td>'
        + '<td>' + esc(c.kind === 'local' ? '本地' : '云') + '</td>'
        + '<td class="mono">' + esc(target) + '</td>'
        + '<td class="mono">' + esc(runtimeBadgeText(rtById[c.id])) + '</td>'
        + '<td class="mp-edit-ops">'
        + '<select data-mp-replace="' + esc(c.id) + '">'
        + '<option value="">替换为…</option>' + opts(sel) + '</select>'
        + '<button type="button" data-mp-act="up" data-mp-cand="' + esc(c.id) + '">上移</button>'
        + '<button type="button" data-mp-act="down" data-mp-cand="' + esc(c.id) + '">下移</button>'
        + '<button type="button" data-mp-act="del" data-mp-cand="' + esc(c.id) + '">删除</button>'
        + '</td></tr>';
    });
    html += '</tbody></table></div>';

    // 新增候选（目录下拉；空目录时提示手填 YAML）
    html += '<div class="mp-edit-add"><select data-mp-add="1">'
      + '<option value="">新增候选…</option>' + opts('') + '</select>'
      + '<span class="mp-hint">新增的候选排在链尾（优先级最低）</span></div>';

    // 池参数
    html += '<div class="mp-edit-params">'
      + '<label>max_attempts <input type="number" data-mp-param="max_attempts" value="'
      + esc(wp.max_attempts) + '"></label>'
      + '<label>deadline_ms <input type="number" data-mp-param="deadline_ms" value="'
      + esc(wp.deadline_ms) + '"></label>'
      + '<label>accept_declared <input type="checkbox" data-mp-param="accept_declared"'
      + (wp.accept_declared ? ' checked' : '') + '></label>'
      + '</div>';

    // 预览区 + 动作按钮
    html += '<div id="mpPreview" class="mp-preview"></div>'
      + '<div class="mp-edit-actions">'
      + '<button type="button" data-mp-act="preview">预览</button>'
      + '<button type="button" data-mp-act="apply"'
      + (_edit.validated ? '' : ' disabled') + '>应用</button>'
      + '<button type="button" data-mp-act="undo"' + (_undo ? '' : ' disabled') + '>回滚上次编辑</button>'
      + '</div></div>';
    modal.innerHTML = html;
  }

  // 打开编辑器：基底 = raw（视图有损，防丢数据 — spec §4.3）；池不存在 → 新建骨架
  async function doMpEdit(pool) {
    if (!_state.def) {
      UI.toast('配置尚未加载完成，请稍后重试', 'error');
      return;
    }
    var base = _state.def.raw ? deepCopy(_state.def.raw)
      : { schema_version: 1, pools: {} };
    if (!base.pools || typeof base.pools !== 'object') base.pools = {};
    if (!base.pools[pool]) {
      base.pools[pool] = newPoolSkeleton();   // 池不存在（含从未配置）→ 新建骨架
    }
    if (!Array.isArray(base.pools[pool].candidates)) {
      base.pools[pool].candidates = [];
    }
    // CAS 基点在打开时捕获：后台刷新推进 _revision 不得让陈旧工作副本
    // 借新 revision 通过 CAS 覆盖他人修改（安全评审 — 固定基点，
    // 他人改动后 apply 只会 409，需重开编辑器取新基底）
    _edit = { pool: pool, base: base, work: deepCopy(base), catalog: null,
              base_revision: _state.def.revision, validated: false };
    renderEditor();
    try { _edit.catalog = await fetchCandidateCatalog(); }
    catch (e) { _edit.catalog = null; }
    renderEditor();
  }

  // 候选操作（无 inline onclick；模态内事件委托调用这些函数）

  function mpEditReplace(candidateId, newCand) {
    if (!_edit) return;
    var cands = _edit.work.pools[_edit.pool].candidates;
    var i = cands.findIndex(function (c) { return c.id === candidateId; });
    if (i < 0) {
      UI.toast('候选不存在：' + candidateId, 'error');
      return;
    }
    var c = deepCopy(newCand);
    // D4 一键换模语义：直接替换候选，继承原优先级（不做优先级重排）
    c.priority = cands[i].priority;
    cands[i] = c;
    _edit.validated = false;   // 任何变更使上次预览失效（终审 I1）
    renderEditor();
  }

  function mpEditAddCandidate(newCand) {
    if (!_edit) return;
    var cands = _edit.work.pools[_edit.pool].candidates;
    var maxP = cands.reduce(function (m, c) { return Math.max(m, c.priority || 0); }, 0);
    var c = deepCopy(newCand);
    c.priority = maxP + 1;   // 链尾 = 最低优先级
    cands.push(c);
    _edit.validated = false;
    renderEditor();
  }

  function mpEditRemove(candidateId) {
    if (!_edit) return;
    var cands = _edit.work.pools[_edit.pool].candidates;
    var i = cands.findIndex(function (c) { return c.id === candidateId; });
    if (i >= 0) {
      cands.splice(i, 1);
      _edit.validated = false;
    }
    renderEditor();
  }

  function mpEditMove(candidateId, dir) {
    if (!_edit) return;
    var cands = _edit.work.pools[_edit.pool].candidates;
    var i = cands.findIndex(function (c) { return c.id === candidateId; });
    var j = dir === 'up' ? i - 1 : i + 1;
    if (i < 0 || j < 0 || j >= cands.length) return;
    var tmp = cands[i]; cands[i] = cands[j]; cands[j] = tmp;
    // 交换优先级，保持链序与优先级一致
    var p = cands[i].priority; cands[i].priority = cands[j].priority; cands[j].priority = p;
    _edit.validated = false;
    renderEditor();
  }

  function mpEditPoolParam(key, value) {
    if (!_edit) return;
    _edit.work.pools[_edit.pool][key] = value;
    _edit.validated = false;
  }

  // 预览核心（模态编辑器与 A2 行内编辑共用）：validate（静态）→ 不通过
  // 阻止一切；通过 → dry-run×2 携带编辑态 config。
  // 返回 {validated, code, html, message}；code ∈ ok | invalid | http_fail |
  // none_chosen | error（调用方决定 toast 文案与容器前缀）。
  // spec §3.2 门：HTTP 失败或两协议均无可用候选 → 阻止应用
  // （单协议无候选 = 黄灯，属合法状态，不阻止）
  async function previewWork(work, pool) {
    try {
      var res = await fetch(VALIDATE_URL, {
        method: 'POST', headers: UI.adminHeaders(),
        body: JSON.stringify(work),
      });
      var d = await res.json();
      if (!d.valid) {
        return {
          validated: false, code: 'invalid',
          html: '<p class="mp-issue">校验未通过（已阻止预览与应用）：</p>'
            + '<ul class="mp-decisions">'
            + (d.issues || []).map(function (i) {
                return '<li class="mono">' + esc(i) + '</li>';
              }).join('')
            + '</ul>',
        };
      }
      var outs = [];
      for (var k = 0; k < ['openai', 'anthropic'].length; k++) {
        var proto = ['openai', 'anthropic'][k];
        var r = await fetch(DRYRUN_URL, {
          method: 'POST', headers: UI.adminHeaders(),
          body: JSON.stringify({ pool: pool, protocol: proto,
                                 config: work }),
        });
        var b = null;
        try { b = await r.json(); } catch (e) { /* 非 JSON 响应体 */ }
        outs.push({ proto: proto, ok: r.ok, body: b || {} });
      }
      var html = outs.map(function (o) {
        var b = o.body || {};
        return '<p class="mono">' + esc(o.proto) + ' → '
          + (b.chosen ? ('选择 <b>' + esc(b.chosen) + '</b>')
             : ('<span class="mp-nochosen">' + esc(b.error || '无可用候选')
                + '</span>'))
          + '</p>';
      }).join('');
      var failed = outs.filter(function (o) { return !o.ok; });
      if (failed.length) {
        return { validated: false, code: 'http_fail',
                 html: html + '<p class="mp-issue">预览请求失败 · HTTP '
                   + esc(failed.map(function (o) { return o.proto; })
                         .join('/'))
                   + ' — 已阻止应用</p>' };
      }
      var noneChosen = outs.filter(function (o) { return !o.body.chosen; });
      if (noneChosen.length === outs.length) {
        return { validated: false, code: 'none_chosen',
                 html: html + '<p class="mp-issue">两个协议均无可用候选'
                   + ' — 已阻止应用</p>' };
      }
      return { validated: true, code: 'ok', html: html };
    } catch (e) {
      return { validated: false, code: 'error', html: '',
               message: (e && e.message) || String(e) };
    }
  }

  // 预览（模态）：结果写入 #mpPreview，toast 按 code 映射。
  // 会话令牌：await 之后编辑器可能已被关闭/重开 — 陈旧续体一律放弃。
  async function mpEditPreview() {
    if (!_edit) return;
    var session = _edit;
    var pv = $('mpPreview');
    _edit.validated = false;   // 每次预览重置；通过才置 true（apply 的前置）
    var out = await previewWork(_edit.work, _edit.pool);
    if (_edit !== session) return;
    if (out.code === 'error') {
      UI.toast('预览请求异常：' + out.message, 'error');
      return;
    }
    if (pv) pv.innerHTML = '<p>预览（编辑态，未应用）：</p>' + out.html;
    if (out.code === 'invalid') {
      UI.toast('校验未通过 — 请按问题列表修正', 'error');
      return;
    }
    if (out.code === 'http_fail') {
      UI.toast('预览请求失败 — 已阻止应用', 'error');
      return;
    }
    if (out.code === 'none_chosen') {
      UI.toast('两个协议均无可用候选 — 已阻止应用', 'error');
      return;
    }
    _edit.validated = true;
    UI.toast('预览完成 — 确认无误后点「应用」', 'ok');
  }

  // 应用：CAS POST；成功 → 记录 _undo；409 → 冲突 toast + 重新拉取。
  // 三段式纪律：必须先通过预览（validate+dry-run）才能应用。
  async function mpEditApply() {
    if (!_edit) return;
    if (!_edit.validated) {
      UI.toast('请先点「预览」并通过校验，再应用', 'error');
      return;
    }
    var session = _edit;   // 会话令牌：请求在途时编辑器可能被关闭/重开
    var poolName = session.pool;
    var body = deepCopy(session.work);
    body.expected_revision = session.base_revision;  // 打开时的基点，不被后台刷新推进
    try {
      var res = await fetch(UPDATE_URL, {
        method: 'POST', headers: UI.adminHeaders(),
        body: JSON.stringify(body),
      });
      var d = null;
      try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      if (_edit !== session) {
        // 陈旧完成：请求已发出并生效，但当前编辑器已是另一会话 —
        // 只提示 + 刷新，绝不触碰 _edit/_undo（终审 I3）
        if (res.ok) {
          UI.toast('已更新 iff/' + poolName, 'ok');
          await renderModelPools();
        }
        return;
      }
      if (res.ok) {
        _undo = session.base;
        _edit = null;
        var modal = $('mpModal');
        if (modal) modal.innerHTML = '';
        UI.toast('已更新 iff/' + poolName, 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        // 基点已过期：本工作副本不可再应用（重试仍会 409）— 引导重开编辑器
        _edit.validated = false;
        UI.toast('配置已被其他修改更新（冲突）。请关闭编辑器后重新打开，基于最新配置再编辑', 'error');
        await renderModelPools();
        return;
      }
      UI.toast((d && d.error) || ('更新失败 · HTTP ' + res.status), 'error');
    } catch (e) {
      UI.toast('更新请求异常：' + (e && e.message), 'error');
    }
  }

  // 回滚：以重新拉取到的最新 revision 重新 POST 编辑前配置
  async function doMpUndo() {
    if (!_undo) {
      UI.toast('没有可回滚的编辑', 'error');
      return;
    }
    var snapshot = _undo;   // 会话令牌：在途期间 _undo 可能已被新编辑替换
    try {
      await renderModelPools();   // 先取最新 revision（CAS 基点）
      if (_undo !== snapshot) return;   // 回滚对象已变 — 放弃本次
      var body = deepCopy(snapshot);
      body.expected_revision = _revision;
      var res = await fetch(UPDATE_URL, {
        method: 'POST', headers: UI.adminHeaders(),
        body: JSON.stringify(body),
      });
      var d = null;
      try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      if (_undo !== snapshot) {
        if (res.ok) {
          UI.toast('已回滚到上次编辑前的配置', 'ok');
          await renderModelPools();
        }
        return;
      }
      if (res.ok) {
        _undo = null;
        clearActionError();
        UI.toast('已回滚到上次编辑前的配置', 'ok');
        await renderModelPools();
        return;
      }
      if (res.status === 409) {
        await setActionError('回滚遇到冲突（配置又有新修改）',
          '已刷新，请重试',
          {label: '重试', run: function () { return doMpUndo(); }});
        UI.toast('回滚遇到冲突（配置又有新修改），已刷新，请重试', 'error');
        return;
      }
      var msg = (d && d.error) || ('回滚失败 · HTTP ' + res.status);
      UI.toast(msg, 'error');
      await setActionError(msg, null,
        {label: '重试', run: function () { return doMpUndo(); }});
    } catch (e) {
      UI.toast('回滚请求异常：' + (e && e.message), 'error');
      await setActionError('回滚请求异常：' + (e && e.message), null,
        {label: '重试', run: function () { return doMpUndo(); }});
    }
  }

  // 模态内事件委托：下拉选中即换/即增；按钮 → 操作函数
  function onModalChange(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var repId = t.getAttribute('data-mp-replace');
    if (repId && t.value) {
      mpEditReplace(repId, candFromCatalogValue(t.value, repId, _edit.catalog));
      return;
    }
    if (t.getAttribute('data-mp-add') && t.value) {
      var id = 'cand-' + Date.now().toString(36);
      mpEditAddCandidate(candFromCatalogValue(t.value, id, _edit.catalog));
      return;
    }
    var param = t.getAttribute('data-mp-param');
    if (param) {
      var v = t.type === 'checkbox' ? !!t.checked : t.value;
      if (t.type === 'number') v = Number(v);
      mpEditPoolParam(param, v);
    }
  }

  function onModalClick(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var cp = t.getAttribute('data-mp-copy');
    if (cp) { doMpSnippetCopy(cp); return; }
    var act = t.getAttribute('data-mp-act');
    if (!act) return;
    var cand = t.getAttribute('data-mp-cand');
    if (act === 'preview') mpEditPreview();
    else if (act === 'apply') mpEditApply();
    else if (act === 'undo') doMpUndo();
    else if (act === 'del' && cand) mpEditRemove(cand);
    else if (act === 'up' && cand) mpEditMove(cand, 'up');
    else if (act === 'down' && cand) mpEditMove(cand, 'down');
  }

  var modal = $('mpModal');
  if (modal && typeof modal.addEventListener === 'function') {
    modal.addEventListener('change', onModalChange);
    modal.addEventListener('click', onModalClick);
  }

  /* ── 事件委托：mpPools 上 checkbox change → doMpToggle（无 inline onclick） ── */
  function onBoxChange(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    var pool = t.getAttribute('data-mp-pool');
    if (pool) { doMpToggle(pool, !!t.checked); return; }
    var inl = t.getAttribute('data-mp-inline-replace');
    if (inl && t.value) {
      var bar = inl.split('|');   // 'pool|candId'
      doMpInlineSelect(bar[0], bar[1], t.value);
      return;
    }
    var wsel = t.getAttribute('data-mp-wizard-sel');
    if (wsel && _wizard && _wizard.roles[wsel]) {
      _wizard.roles[wsel].selected = t.value || null;
    }
  }

  /* ── 接入片段（Task 8）：客户端复制即用代码 ── */

  var _snippet = null;   // {pool, openai, anthropic} — copy 按钮的文本源

  function snippetText(pool, proto) {
    var host = '127.0.0.1';
    var scheme = 'http:';
    try {
      if (typeof location !== 'undefined' && location) {
        if (location.hostname) host = location.hostname;
        // 协议跟随 dashboard 自身来源：反代 TLS 终结时片段保持 https，
        // 不把已加密的管理面降级成明文 http
        if (location.protocol) scheme = location.protocol;
      }
    } catch (e) { /* 非浏览器环境 → 缺省本机 http */ }
    var base = scheme + '//' + host + ':8999';
    // model 字面量用 JSON.stringify 生成（Python 兼容转义）：池名含引号/
    // 反斜杠/换行也只是一个完整字符串参数，无法注入到复制的代码（安全评审）
    var modelLit = JSON.stringify('iff/' + pool);
    if (proto === 'anthropic') {
      return 'from anthropic import Anthropic\n\n'
        + 'client = Anthropic(\n'
        + '    base_url=' + JSON.stringify(base) + ',   # 裸端口，不要加 /v1\n'
        + '    api_key="<你的 IFF API key>",\n'
        + ')\n'
        + 'resp = client.messages.create(\n'
        + '    model=' + modelLit + ',\n'
        + '    max_tokens=1024,\n'
        + '    messages=[{"role": "user", "content": "你好"}],\n'
        + ')\n'
        + 'print(resp.content[0].text)';
    }
    return 'from openai import OpenAI\n\n'
      + 'client = OpenAI(\n'
      + '    base_url=' + JSON.stringify(base + '/v1') + ',\n'
      + '    api_key="<你的 IFF API key>",\n'
      + ')\n'
      + 'resp = client.chat.completions.create(\n'
      + '    model=' + modelLit + ',\n'
      + '    messages=[{"role": "user", "content": "你好"}],\n'
      + ')\n'
      + 'print(resp.choices[0].message.content)';
  }

  function doMpSnippet(pool) {
    var modal = $('mpModal');
    if (!modal) return;
    var openai = snippetText(pool, 'openai');
    var anthropic = snippetText(pool, 'anthropic');
    _snippet = { pool: pool, openai: openai, anthropic: anthropic };
    modal.innerHTML = '<div class="mp-editor">'
      + '<div class="mp-editor-hdr"><b class="mono">iff/' + esc(pool)
      + '</b><span>接入片段 — 复制即用，之后换模型无需改客户端</span></div>'
      + '<p class="mp-hint">注意：若 API key 配置了 models ACL，需包含'
      + '<span class="mono">iff/' + esc(pool) + '</span>，否则会 401。</p>'
      + '<p class="mp-snippet-title">OpenAI 协议</p>'
      + '<pre class="mp-snippet">' + esc(openai) + '</pre>'
      + '<button type="button" data-mp-copy="openai">复制 OpenAI 片段</button>'
      + '<p class="mp-snippet-title">Anthropic 协议</p>'
      + '<pre class="mp-snippet">' + esc(anthropic) + '</pre>'
      + '<button type="button" data-mp-copy="anthropic">复制 Anthropic 片段</button>'
      + '<p class="mp-hint">OpenAI SDK 的 base_url 要带 /v1；Anthropic SDK 用裸端口'
      + ' — 再加会叠成 /v1/v1。</p>'
      + '</div>';
  }

  async function doMpSnippetCopy(proto) {
    if (!_snippet || !_snippet[proto]) {
      UI.toast('请先打开接入片段', 'error');
      return;
    }
    try {
      if (typeof navigator !== 'undefined' && navigator
          && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(_snippet[proto]);
        UI.toast('片段已复制到剪贴板', 'ok');
      } else {
        UI.toast('当前环境不支持剪贴板 — 请手动选择文本复制', 'error');
      }
    } catch (e) {
      UI.toast('复制失败：' + (e && e.message), 'error');
    }
  }

  function onBoxClick(ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) return;
    // A3：行动横幅（重试原操作 / 关闭）
    if (t.getAttribute('data-mp-banner-action')) { doMpBannerAction(); return; }
    if (t.getAttribute('data-mp-banner-dismiss')) { doMpBannerDismiss(); return; }
    // A2：行内 diff 条（应用 / 撤销）
    if (t.getAttribute('data-mp-inline-apply')) { doMpInlineApply(); return; }
    if (t.getAttribute('data-mp-inline-cancel')) { doMpInlineCancel(); return; }
    var edit = t.getAttribute('data-mp-edit');
    if (edit) { doMpEdit(edit); return; }
    var snip = t.getAttribute('data-mp-snippet');
    if (snip) { doMpSnippet(snip); return; }
    var del = t.getAttribute('data-mp-wizard-del');
    if (del) { doMpWizardToggle(del); return; }
    var add = t.getAttribute('data-mp-wizard-add');
    if (add) { doMpWizardToggle(add); return; }
    if (t.getAttribute('data-mp-wizard-submit')) { doMpWizardSubmit(); }
  }

  var box = $('mpPools');
  if (box && typeof box.addEventListener === 'function') {
    box.addEventListener('change', onBoxChange);
    box.addEventListener('click', onBoxClick);
    // A2 目录惰性预热（Task 1 裁决）：鼠标掠过/键盘聚焦面板即拉取，
    // 用户点开下拉前选项已就绪 — 普通渲染/轮询不产生目录请求
    box.addEventListener('mouseover', function () { primeInline(); });
    box.addEventListener('focusin', function () { primeInline(); });
  }

  /* ── 5s 轮询（spec §6）：与首屏同一刷新函数（定义+状态+运行态合并拉取）。
     store.js 的周期快照不触发 tabRenderers，故本模块自持定时器；
     页面隐藏 / 非本 TAB / 向导开启（避免清掉选择框）时跳过。 ── */
  var _pollStarted = false;

  function pollTick() {
    try {
      if (typeof document !== 'undefined' && document.hidden) return;
      if (window.store && typeof window.store.get === 'function'
          && window.store.get('tab_active') !== 'tab-model-pools') return;
      if (_wizard) return;
      return renderModelPools();   // 返回 promise 供测试/调用方等待
    } catch (e) { /* 轮询失败静默 — 下个滴答重试 */ }
  }

  function startRuntimePolling() {
    if (_pollStarted || typeof setInterval !== 'function') return;
    _pollStarted = true;
    setInterval(pollTick, 5000);
  }

  startRuntimePolling();

  /* ── 注册 ── */
  window.tabRenderers = window.tabRenderers || {};
  window.tabRenderers['tab-model-pools'] = renderModelPools;
  window.renderModelPools = renderModelPools;
  window.doMpToggle = doMpToggle;
  window.doMpEdit = doMpEdit;
  window.doMpUndo = doMpUndo;
  window.doMpBannerAction = doMpBannerAction;
  window.doMpBannerDismiss = doMpBannerDismiss;
  window.doMpInlineSelect = doMpInlineSelect;
  window.doMpInlineApply = doMpInlineApply;
  window.doMpInlineCancel = doMpInlineCancel;
  window.mpPrimeInline = primeInline;
  window.mpEditReplace = mpEditReplace;
  window.mpEditAddCandidate = mpEditAddCandidate;
  window.mpEditPreview = mpEditPreview;
  window.mpEditApply = mpEditApply;
  window.doMpWizardToggle = doMpWizardToggle;
  window.doMpWizardSubmit = doMpWizardSubmit;
  window.doMpSnippet = doMpSnippet;
  window.doMpSnippetCopy = doMpSnippetCopy;
})();
