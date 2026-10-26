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
  var LATENCY_URL = '/admin/model-pools/latency';
  var EVENTS_URL = '/admin/model-pools/events';
  var CATALOG_URL = '/admin/model-pools/catalog';
  var TOGGLE_URL = '/admin/model-pools/toggle';
  var VALIDATE_URL = '/admin/model-pools/validate';
  var DRYRUN_URL = '/admin/model-pools/dry-run';
  var UPDATE_URL = '/admin/model-pools';

  var _revision = null;   // 最近渲染到的配置 revision（toggle CAS 用）
  var _loading = false;   // 在途渲染去重（避免连点刷新叠发请求）
  // v7.1.0 运营视图单一数据源：def（含 configured/load_error/raw）+ 双协议
  // 选择视图 + 候选运行态；编辑器（Task 6）以 _state.def.raw 为读-改-写基底
  var _state = { def: null, status: null, runtime: null, latency: null,
                 events: null };
  // C2：故障时间线展开态（pool → true；进 structSig — 开合是结构）
  var _timelineOpen = {};
  // C4：劣化提示关闭态（pool → true；进 structSig；劣化解除时清键重新武装）
  var _degradeDismissed = {};
  // C3：活体光带新鲜窗口 — last_attempt.ok 且 age_s < 此值 = 真实数据交换中
  var BEAM_FRESH_S = 30;

  /* ── 调用树 + 四组常驻卡（2026-10-26 大手术，v7.5.0）──────────────
   * 用户 2026-10-25 判定向导式初始化为失败设计：配置窗口用完即消失、
   * 缺失组无创建入口。重构为单一形态 — 顶部调用树（端口→角色→模型，
   * 链路态 = 有没有被真实调用）+ 四角色常驻配置卡（未配置即待建）。
   * 四角色固定序与描述表（原向导 _ROLE_DEFS 上移 — 树与卡共用）。 */
  var _ROLE_ORDER = ['fast', 'premium', 'small', 'classifier'];
  var _ROLE_DEFS = [
    { name: 'fast', desc: '快速响应 — 日常对话与轻任务' },
    { name: 'premium', desc: '旗舰质量 — 复杂推理与长文' },
    { name: 'small', desc: '小模型 — 分类/抽取/批量任务' },
    { name: 'classifier', desc: '分类判别 — 路由/打标/判别' },
  ];
  // 树链路流动窗：真实 ok 交换在此窗内 = flowing。比卡片光带窗
  // （BEAM_FRESH_S=30）宽 — 树答「近期有被调用」，光带答「此刻在交换」
  var FLOW_WINDOW_S = 60;

  // 链路五态纯函数（树渲染与后续卡渲染同源）：
  //   unconfigured 无 def（缺失组不消失，灰虚线）
  //   fail         issues 非空，或近窗真实尝试失败且窗内无成功交换
  //   flowing      任一候选近窗真实 ok 交换（降级服务中数据仍在流动 →
  //                flowing 优先于 cooldown，降级细节归 C4 提示）
  //   cooldown     无流动但有候选隔离/冷却
  //   idle         可路由但静默
  function linkState(def, rtPool) {
    if (!def) return { cls: 'unconfigured', label: '未配置' };
    if ((def.issues || []).length) {
      return { cls: 'fail', label: '故障' };
    }
    var flowing = false, recentFail = false, cooling = false;
    (((rtPool || {}).candidates) || []).forEach(function (rc) {
      var la = rc.last_attempt;
      if (la && typeof la.age_s === 'number' && la.age_s < FLOW_WINDOW_S) {
        if (la.ok) flowing = true; else recentFail = true;
      }
      if (rc.cooling) cooling = true;
    });
    if (flowing) return { cls: 'flowing', label: '调用中' };
    if (cooling) return { cls: 'cooldown', label: '冷却中' };
    if (recentFail) return { cls: 'fail', label: '故障' };
    return { cls: 'idle', label: '空闲' };
  }

  // 调用树渲染：每行完整链「:8999 ── iff/<角色> ── <模型叶>」+ 态徽标。
  // 四角色固定序 + 额外池（YAML 手工组）追加其后，不隐藏（R3）。
  function treeHtml(defs, rt) {
    var pools = (defs && defs.pools) || {};
    var rtPools = (rt && rt.pools) || {};
    var order = _ROLE_ORDER.slice();
    Object.keys(pools).forEach(function (n) {
      if (order.indexOf(n) < 0) order.push(n);
    });
    var rows = order.map(function (name) {
      var def = pools[name];
      var ls = linkState(def, rtPools[name]);
      var cands = (def && def.candidates) || [];
      var leaf = cands.length ? cands[0].target : '—';
      return '<div class="mp-tree-row" data-mp-tree-pool="' + esc(name) + '">'
        + '<span class="mp-tree-port">:8999</span>'
        // 链路段/态徽标带 id — applyDynamic by-id 类翻转（同 C3 光带，
        // 不进结构签名：轮询零闪烁）
        + '<span class="mp-link mp-link-' + ls.cls
        + '" id="mp-tlink-a-' + esc(name) + '"></span>'
        + '<span class="mp-tree-role">iff/' + esc(name) + '</span>'
        + '<span class="mp-link mp-link-' + ls.cls
        + '" id="mp-tlink-b-' + esc(name) + '"></span>'
        + '<span class="mp-tree-leaf">' + esc(leaf) + '</span>'
        + '<span class="mp-tree-state mp-tree-state-' + ls.cls
        + '" id="mp-tstate-' + esc(name) + '">'
        + esc(ls.label) + '</span>'
        + '</div>';
    });
    return '<div class="mp-tree" id="mpTree">' + rows.join('') + '</div>';
  }

  // 角色描述（四角色表查名；额外池无描述返回 null）
  function roleDesc(name) {
    for (var i = 0; i < _ROLE_DEFS.length; i++) {
      if (_ROLE_DEFS[i].name === name) return _ROLE_DEFS[i].desc;
    }
    return null;
  }

  // 未配置卡下拉默认值（原初始化引导的预填启发式按角色吸收；仅建议值）：
  // fast=活跃最小 / premium=最大上下文本地（无本地则首个云）/
  // small=最小 / classifier=首个活跃。目录条目需含 active/context_window
  // （fetchCatalog 完整形态）。额外池无启发式。
  function cardDefault(roleName, catalog) {
    var cat = catalog || {};
    var llms = cat.local || [];
    var act = llms.filter(function (m) { return !!m.active; });
    function pick(list, wantMax) {
      if (!list.length) {
        return (cat.cloudValues || []).length ? cat.cloudValues[0] : null;
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
    if (roleName === 'fast') return pick(act.length ? act : llms, false);
    if (roleName === 'premium') return pick(llms, true);
    if (roleName === 'small') return pick(llms, false);
    if (roleName === 'classifier') {
      return act.length ? 'local:' + act[0].name : pick(llms, false);
    }
    return null;
  }

  // 未配置卡下拉选项（与 A2 inlineOptions 同形 + defVal 预选）
  function configOptions(catalog, defVal) {
    var o = '';
    (catalog.localValues || []).forEach(function (v) {
      o += '<option value="' + esc(v) + '"'
        + (v === defVal ? ' selected' : '') + '>本地 · '
        + esc(v.slice('local:'.length)) + '</option>';
    });
    (catalog.cloudValues || []).forEach(function (v) {
      o += '<option value="' + esc(v) + '"'
        + (v === defVal ? ' selected' : '') + '>云 · '
        + esc(v.slice('cloud:'.length)) + '</option>';
    });
    return o;
  }

  // 未配置卡：同形态 + 「未配置」徽标 + 预填下拉（首次 apply 建池）+
  // 高级配置直通（openEditor 对缺失池走骨架）。无候选链/开关/接入片段
  // — 那些是「已配置」才有的动作。
  function unconfiguredCard(name, catalog) {
    var desc = roleDesc(name);
    var defVal = cardDefault(name, catalog);
    // 三态目录（与 A2 inline select 同语义）：object=就绪出选项（预填）；
    // null=未预热（占位可选，box mouseover/focusin 即拉取，轮询重建补上）；
    // false=拉取失败（禁用 + 明示去编辑器）
    var opts = catalog ? configOptions(catalog, defVal) : null;
    var selInner = catalog
      ? '<option value="">选择模型…</option>' + opts
      : (catalog === false
          ? '<option value="">目录不可用 — 请用编辑器</option>'
          : '<option value="">选择模型…（加载中）</option>');
    return '<div class="if-card mp-pool mp-unconfigured" id="mp-card-'
      + esc(name) + '" data-mp-card="' + esc(name) + '">'
      + '<div class="mp-beam mp-beam-dark" id="mp-beam-' + esc(name)
      + '"></div>'
      + '<div class="if-card-hdr">'
      + '<span class="if-card-title mono">iff/' + esc(name) + '</span>'
      + '<span class="mp-unconfigured-badge">未配置</span>'
      + '<span class="mp-toggle-wrap">'
      + '<button type="button" class="mp-edit-btn" data-mp-edit="'
      + esc(name) + '">高级配置</button>'
      + '</span>'
      + '</div>'
      + (desc ? '<p class="mp-role-desc">' + esc(desc) + '</p>' : '')
      + '<div class="mp-config-line">'
      + '<span class="mp-diff-old mono">（未配置）</span>'
      + '<span class="mp-diff-arrow">→</span>'
      + '<select class="mp-inline-sel mp-config-sel" data-mp-config-pool="'
      + esc(name) + '"' + (catalog === false ? ' disabled' : '') + '>'
      + selInner
      + '</select>'
      + '</div>'
      + '</div>';
  }

  // 四组常驻配置卡：_ROLE_ORDER 固定序四卡 + 额外池追加（R3）。
  // 已配置 → 现 poolCard 全部动作；未配置 → unconfiguredCard（待建）。
  // 面板只有这一种形态 — 不存在会消失的配置窗口。
  function cardsHtml(defs, st, rt, catalog) {
    var pools = (defs && defs.pools) || {};
    var stPools = (st && st.pools) || {};
    var rtPools = (rt && rt.pools) || {};
    var order = _ROLE_ORDER.slice();
    Object.keys(pools).forEach(function (n) {
      if (order.indexOf(n) < 0) order.push(n);
    });
    var cards = order.map(function (name) {
      var def = pools[name];
      return def
        ? poolCard(name, def, stPools[name], rtPools[name])
        : unconfiguredCard(name, catalog);
    });
    return '<div class="mp-cards" id="mpCards">' + cards.join('') + '</div>';
  }

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
      + esc(n.candId) + '"'
      + (n.validated ? '' : ' disabled title="需先通过预览"')
      + '>应用</button>'
      + '<button type="button" data-mp-inline-cancel="1">撤销</button>'
      + '</div>';
  }

  // 候选体检卡（UI/UX P1 Task 2：表格 → 横向卡片，状态一眼可读）
  /* ── C1 延迟心跳迷你图（P3 Task 8）── */
  // 60×24 内联 SVG：polyline + 末点圆；x 按序铺满、y 按 TTFT 归一反演
  //（越低越高位）。points = latency 端点该候选环（新在后）。
  function sparkSvg(points) {
    var W = 60, H = 24, PAD = 2;
    var pts = (points || []).filter(function (p) {
      return p && typeof p.ttft_ms === 'number' && isFinite(p.ttft_ms);
    });
    if (!pts.length) return '';
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) {
      if (p.ttft_ms < lo) lo = p.ttft_ms;
      if (p.ttft_ms > hi) hi = p.ttft_ms;
    });
    if (hi === lo) hi = lo + 1;   // 全平 → 置顶直线（不除零）
    var n = pts.length;
    var coords = pts.map(function (p, i) {
      var x = (n === 1) ? W / 2 : PAD + (W - 2 * PAD) * i / (n - 1);
      var y = PAD + (H - 2 * PAD) * (1 - (p.ttft_ms - lo) / (hi - lo));
      return [x, y];
    });
    var last = coords[n - 1];
    return '<svg class="mp-spark" width="' + W + '" height="' + H
      + '" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true">'
      + '<polyline points="' + coords.map(function (cd) {
          return cd[0].toFixed(1) + ',' + cd[1].toFixed(1);
        }).join(' ')
      + '" fill="none" style="stroke:var(--accent)" stroke-width="1.5"'
      + ' stroke-linecap="round" stroke-linejoin="round"/>'
      + '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1)
      + '" r="2" fill="var(--accent)"/></svg>';
  }

  // 候选延迟环（新在后，端点已按 20 封顶）；无数据 → []
  function latRing(pool, candId) {
    var lat = _state.latency;
    var ring = (lat && lat.pools && lat.pools[pool] && lat.pools[pool][candId]);
    return Array.isArray(ring) ? ring : [];
  }

  /* ── C2 失败时间线（P3 Task 9）── */
  // 池事件列表（新在后，端点已按 50 封顶）；无数据 → []
  function poolEvents(pool) {
    var ev = _state.events;
    var list = (ev && ev.pools && ev.pools[pool]);
    return Array.isArray(list) ? list : [];
  }

  // 轴内事件点（不含外壳）：最近 30min 窗口，新在右；
  // left% = (1 - age/1800)·100，越界夹紧；title 原生气泡（YAGNI）
  function timelineDots(pool) {
    return poolEvents(pool).map(function (e) {
      var age = (typeof e.age_s === 'number' && isFinite(e.age_s))
        ? e.age_s : 1800;
      var left = Math.max(0, Math.min(100, (1 - age / 1800) * 100));
      var title = (e.type || '') + ' · ' + (e.target || '')
        + (e.reason ? ' · ' + e.reason : '')
        + ' · ' + Math.round(age) + 's 前';
      return '<span class="mp-tl-dot mp-tl-' + esc(e.type || 'cooldown')
        + '" style="left:' + left.toFixed(1) + '%" title="' + esc(title)
        + '"></span>';
    }).join('');
  }

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
      + esc(c.id) + '">' + esc(runtimeBadgeText(rt)) + '</span>'
      // C1：延迟心跳迷你图容器常驻（by-id 更新锚点），有数据才出 SVG
      + '<span class="mp-spark-wrap" id="mp-spark-' + esc(pool) + '-'
      + esc(c.id) + '">' + sparkSvg(latRing(pool, c.id).slice(-20))
      + '</span>';
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

  /* ── C3 活体光带（P3 Task 10）：三态纯函数（渲染/applyDynamic 同源） ── */
  // flow：任一候选 last_attempt.ok 且新鲜（<BEAM_FRESH_S）— 真实数据交换中；
  // dark：全协议无 chosen（不可路由/全冷却）；idle：其余（可路由但静默）
  function beamClass(stP, rtP) {
    // 不可路由优先于新鲜交换（unroutable 是当前事实，交换是 30s 内历史）
    var protocols = ((stP || {}).protocols) || {};
    var anyChosen = ['openai', 'anthropic'].some(function (p) {
      return protocols[p] && protocols[p].chosen;
    });
    if (!anyChosen) return 'mp-beam-dark';
    var fresh = false;
    (((rtP || {}).candidates) || []).forEach(function (rc) {
      var la = rc.last_attempt;
      if (la && la.ok && typeof la.age_s === 'number'
          && la.age_s < BEAM_FRESH_S) fresh = true;
    });
    return fresh ? 'mp-beam-flow' : 'mp-beam-idle';
  }

  /* ── C4 劣化判定（纯函数）：首候选冷却/隔离 && 任一协议仍有 chosen
     （服务由回退候选提供 — 用户应知道首选已降级）── */
  function isDegraded(poolDef, stP, rtP) {
    var cands = (poolDef && poolDef.candidates) || [];
    if (!cands.length) return false;
    var rtById = {};
    (((rtP || {}).candidates) || []).forEach(function (rc) {
      rtById[rc.id] = rc;
    });
    var rtC = rtById[cands[0].id];
    if (!rtC || !rtC.cooling) return false;
    var protocols = ((stP || {}).protocols) || {};
    return ['openai', 'anthropic'].some(function (p) {
      return protocols[p] && protocols[p].chosen;
    });
  }

  /* ── 池卡片 ── */
  function poolCard(name, def, st, rt) {
    var checked = def.enabled ? ' checked' : '';
    var html = '<div class="if-card mp-pool" id="mp-card-' + esc(name)
      + '" data-mp-card="' + esc(name) + '">'
      // C3：池顶活体光带（三态由 beamClass 纯函数定，applyDynamic 同源更新）
      + '<div class="mp-beam ' + beamClass(st, rt) + '" id="mp-beam-'
      + esc(name) + '"></div>'
      + '<div class="if-card-hdr">'
      + '<span class="if-card-title mono">iff/' + esc(name) + '</span>'
      + (roleDesc(name)
          ? '<span class="mp-role-desc">' + esc(roleDesc(name)) + '</span>'
          : '')
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
    // C4：首候选降级而服务仍由回退候选提供 → 引导条（关闭后同态不重现）
    if (isDegraded(def, st, rt) && !_degradeDismissed[name]) {
      html += '<div class="mp-degrade"><span>首选已降级 — 当前服务经回退候选提供</span>'
        + '<button type="button" class="mp-edit-btn" data-mp-degrade-view="'
        + esc(name) + '">查看回退链</button>'
        + '<button type="button" class="mp-degrade-x" data-mp-degrade-dismiss="'
        + esc(name) + '" title="关闭">×</button></div>';
    }

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

    // C2：故障时间线（池有事件才出 toggle；展开态重建出轴）
    var evs = poolEvents(name);
    if (evs.length) {
      html += '<div class="mp-tl-row">'
        + '<button type="button" class="mp-edit-btn" id="mp-tl-toggle-'
        + esc(name) + '" data-mp-tl-toggle="' + esc(name)
        + '">故障时间线 · ' + evs.length + '</button>'
        + (_timelineOpen[name]
            ? '<div class="mp-tl" id="mp-tl-' + esc(name) + '">'
              + timelineDots(name) + '</div>'
            : '')
        + '</div>';
    }

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
        // C1：延迟数据不进签名（变化不重建）→ spark by-id 更新
        var spark = $('mp-spark-' + n + '-' + c.id);
        if (spark) spark.innerHTML = sparkSvg(latRing(n, c.id).slice(-20));
      });
      // C2：事件数据不进签名 → toggle 计数 + 展开轴 by-id 更新
      var evsN = poolEvents(n).length;
      var tgl = $('mp-tl-toggle-' + n);
      if (tgl && evsN) tgl.textContent = '故障时间线 · ' + evsN;
      if (_timelineOpen[n]) {
        var tl = $('mp-tl-' + n);
        if (tl) tl.innerHTML = timelineDots(n);
      }
      // C3：光带三态数据驱动 → by-id className 更新（不进 sig）
      var beam = $('mp-beam-' + n);
      if (beam) beam.className = 'mp-beam ' + beamClass(stPools[n], rtPools[n]);
      // 调用树：链路态 by-id（同哲学 — 类翻转是动态更新非结构变化，
      // 不进 sig；未配置行态恒定无需更新）
      var tls = linkState(poolDef, rtPools[n]);
      var tla = $('mp-tlink-a-' + n), tlb = $('mp-tlink-b-' + n);
      if (tla) tla.className = 'mp-link mp-link-' + tls.cls;
      if (tlb) tlb.className = 'mp-link mp-link-' + tls.cls;
      var tst = $('mp-tstate-' + n);
      if (tst) {
        tst.className = 'mp-tree-state mp-tree-state-' + tls.cls;
        tst.textContent = tls.label;
      }
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
      // C1：延迟环 best-effort（同 rt 模式 — 失败 → null 降级，不挡面板）
      var latRes = await fetch(LATENCY_URL, { headers: UI.adminHeaders() });
      // C2：事件时间线 best-effort（同上）
      var evRes = await fetch(EVENTS_URL, { headers: UI.adminHeaders() });
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
      var lat = null;
      if (latRes.ok) {
        try { lat = await latRes.json(); } catch (e) { lat = null; }
      }
      var evs = null;
      if (evRes.ok) {
        try { evs = await evRes.json(); } catch (e) { evs = null; }
      }
      _state.def = def;
      _state.status = st;
      _state.runtime = rt;
      _state.latency = lat;
      _state.events = evs;
      _revision = def.revision;
      var revEl = $('mpRevision');
      if (revEl) revEl.textContent = String(def.revision);

      // 单一形态（2026-10-26 大手术）：无空态分支 — 零池 = 四张未配置卡
      // （未配置即待建）；load_error → 横幅 + 同布局降级渲染（registry
      // 保留最后有效快照，树上照常显示现有池）
      // A2：目录不做渲染期拉取（Task 1 裁决 — 既有轮询 GET 计数是契约），
      // 手势惰性预热：box mouseover/focusin → primeInline()
      var stPools = (st && st.pools) || {};
      var rtPools = (rt && rt.pools) || {};
      cdSnapshot(rtPools);
      // C4 重新武装：劣化解除（首候选恢复健康/无 chosen）→ 清关闭键，
      // 下次劣化再现引导条
      Object.keys(def.pools || {}).forEach(function (n) {
        if (_degradeDismissed[n]
            && !isDegraded(def.pools[n], stPools[n], rtPools[n])) {
          delete _degradeDismissed[n];
        }
      });
      // B1 局部渲染（Task 4）：结构签名不变 → 只走 applyDynamic 定点更新，
      // 不重写 innerHTML（轮询零闪烁）；banner 消息/提示计入签名（评审 I6 —
      // 内容变了的横幅必须重建，否则旧原因配新行动闭包）；A2 目录在否与
      // 行内会话（池|候选|spec|rev）计入签名 — 下拉出现/diff 条/应用按钮
      // 启用都是结构变化
      var sig = structSig(def, stPools, rtPools)
        + '|cat:' + (_catalog ? '1' : _catalog === false ? '2' : '0')
        // C2：toggle 在否（池事件有无）+ 展开池列表都是结构 — 变化即重建；
        // 事件数据本身不进 sig（by-id 更新）
        + '|tl:' + Object.keys(def.pools || {}).filter(function (n) {
            return poolEvents(n).length > 0;
          }).join(',')
          + '/' + Object.keys(_timelineOpen).join(',')
        // C4：引导条在否（劣化在 + 未关闭）与关闭态都是结构 — 变化即重建
        + '|dg:' + Object.keys(def.pools || {}).filter(function (n) {
            return isDegraded(def.pools[n], stPools[n], rtPools[n]);
          }).join(',')
          + '/' + Object.keys(_degradeDismissed).join(',')
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
      if (def.load_error) {
        html += '<div class="mp-error-banner">'
          + '<p><b>model_pools.yaml 加载失败</b>（registry 保留最后有效快照）：</p>'
          + '<p class="mono">' + esc(def.load_error) + '</p>'
          + '<p>修复文件后点击「重载配置」，或直接编辑 YAML。</p>'
          + '</div>';
      }
      if (_actionError) html += bannerHtml();   // A3：行动横幅常驻
      html += treeHtml(def, rt);
      html += cardsHtml(def, st, rt, _catalog);
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
  /* ── B2 乐观 UI + 切换中骨架态（UI/UX P2 Task 2）──
     请求在途：池卡外壳 mp-busy（CSS 微光扫过）+ 协议灯直写乐观目标类；
     响应到达（fetch 段 finally）即清除 — 成败均由后续重渲染回服务端真值
     （真实浏览器全量重建自然覆盖；签名不变的局部渲染路径由清除兜底）。 */

  function markBusy(pool) {
    var c = $('mp-card-' + pool);
    if (c && (' ' + c.className + ' ').indexOf(' mp-busy ') < 0) {
      c.className = (c.className ? c.className + ' ' : '') + 'mp-busy';
    }
  }

  function clearBusy(pool) {
    var c = $('mp-card-' + pool);
    if (c) {
      c.className = (' ' + c.className + ' ')
        .split(' mp-busy ').join(' ').trim();
    }
  }

  // 乐观灯：与 lampForProtocol 同词表（启用→ok / 停用→warn）
  var _optLamps = null;

  function optimisticLamps(pool, enable) {
    var prev = [];
    ['openai', 'anthropic'].forEach(function (proto) {
      var id = 'mp-lamp-' + pool + '-' + proto;
      var e = $(id);
      if (!e) return;
      prev.push({ id: id, cls: e.className });
      e.className = 'mp-lamp ' + (enable ? 'mp-lamp-ok' : 'mp-lamp-warn');
    });
    _optLamps = prev.length ? prev : null;
  }

  function restoreLamps() {
    if (!_optLamps) return;
    _optLamps.forEach(function (o) {
      var e = $(o.id);
      if (e) e.className = o.cls;
    });
    _optLamps = null;
  }

  // C2：故障时间线开合 — 展开态变化走重建（轴出现/消失是结构变化）
  async function doMpTimelineToggle(pool) {
    if (_timelineOpen[pool]) delete _timelineOpen[pool];
    else _timelineOpen[pool] = true;
    await renderModelPools();
  }

  // C4：查看回退链 — 池卡加高亮类（定位滚动交给 CSS scroll-margin）
  function doMpDegradeView(pool) {
    var card = $('mp-card-' + pool);
    if (!card) return;
    card.className = (card.className + ' mp-highlight').trim();
  }

  // C4：关闭引导条 — 同态不重现；劣化解除后清键重新武装（见渲染主路径）
  async function doMpDegradeDismiss(pool) {
    if (!_degradeDismissed[pool]) _degradeDismissed[pool] = true;
    await renderModelPools();
  }

  async function doMpToggle(pool, enable) {
    if (_revision == null) {
      UI.toast('配置尚未加载完成，请稍后重试', 'error');
      return;
    }
    markBusy(pool);
    optimisticLamps(pool, enable);
    try {
      var res, d = null;
      try {
        res = await fetch(TOGGLE_URL, {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({
            pool: pool,
            enabled: enable,
            expected_revision: _revision,
          }),
        });
        try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      } finally {
        // B2：响应到达即清骨架 + 回滚乐观灯 — 成败分支的重渲染前完成
        clearBusy(pool);
        restoreLamps();
      }
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
        // null（fetchCatalog 层失败 — 404/解析失败等）与 false 同义：
        // 目录不可用，不得回落「未预热」占位态（真实浏览器 404 即此路径）
        .then(function (c) { _catalog = c || false; _catPromise = null; return _catalog; })
        .catch(function () {
          _catalog = false; _catPromise = null; return false;
        });
    }
    return _catPromise;
  }

  // 目录失败冷却（终审 I3）：目录不可用时 mouseover 高频触发不得逐次
  // 重放 fetch+render — 失败后 5s（= 轮询周期）内至多重试一次
  var PRIME_RETRY_MS = 5000;
  var _catFailTs = null;   // 失败时刻（null=未失败；0 是合法时钟值，不作哨兵）

  // 手势入口（box mouseover/focusin）：取目录 → 重渲染出选项
  async function primeInline() {
    if (_catalog) return;
    if (_catPromise) return;   // 在途不叠加（首个手势已发起）
    if (_catFailTs !== null
        && Date.now() - _catFailTs < PRIME_RETRY_MS) return;
    var ok = await ensureCatalog();
    if (!ok) _catFailTs = Date.now();
    await renderModelPools();   // 失败也当轮渲染禁用态（契约不变）
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
    markBusy(poolName);      // B2：应用在途骨架态
    try {
      var res, d = null;
      try {
        res = await fetch(UPDATE_URL, {
          method: 'POST', headers: UI.adminHeaders(),
          body: JSON.stringify(body),
        });
        try { d = await res.json(); } catch (e) { /* 非 JSON 响应体 */ }
      } finally {
        clearBusy(poolName);   // B2：响应到达即清（分支渲染前）
      }
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
      + '</b><span>编辑候选链（改完先预览，再应用）</span>'
      + '<button type="button" data-mp-act="close">关闭</button></div>';

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
      + (_edit.validated ? '' : ' disabled title="需先通过预览"')
      + '>应用</button>'
      + '<button type="button" data-mp-act="undo"' + (_undo ? '' : ' disabled') + '>回滚上次编辑</button>'
      + '</div></div>';
    modal.innerHTML = html;
  }

  // 关闭编辑器（B4：Esc 二级出口 + 头部关闭钮 — 此前模态无鼠标出口）
  function mpModalClose() {
    _edit = null;
    var modal = $('mpModal');
    if (modal) modal.innerHTML = '';
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
    else if (act === 'close') mpModalClose();
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
    // C2：故障时间线开合（展开态进 sig → 重建出轴/收起轴）
    var tlPool = t.getAttribute('data-mp-tl-toggle');
    if (tlPool) { doMpTimelineToggle(tlPool); return; }
    // C4：劣化引导条（查看回退链 / 关闭）
    var dgView = t.getAttribute('data-mp-degrade-view');
    if (dgView) { doMpDegradeView(dgView); return; }
    var dgDismiss = t.getAttribute('data-mp-degrade-dismiss');
    if (dgDismiss) { doMpDegradeDismiss(dgDismiss); return; }
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

  /* ── B4 键盘流（UI/UX P2 Task 4）：E 进编辑 / Esc 分级退出 ──
     仅本 tab 激活、焦点不在表单控件、无修饰键时生效；
     向导/成果卡在屏时忽略（那里没有池卡上下文）。 */
  function onKeyDown(ev) {
    if (!ev || !ev.key) return;
    if (window.store && typeof window.store.get === 'function'
        && window.store.get('tab_active') !== 'tab-model-pools') return;
    var t = ev.target;
    if (t && t.tagName
        && ['INPUT', 'SELECT', 'TEXTAREA']
            .indexOf(t.tagName.toUpperCase()) >= 0) return;
    if (ev.key === 'Escape') {
      if (_inline) return doMpInlineCancel();   // 一级：行内 diff 条
      if (_edit) { mpModalClose(); return; }    // 二级：编辑器模态
      return;
    }
    if ((ev.key === 'e' || ev.key === 'E')
        && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
      if (!_state.def) return;
      var card = (t && typeof t.closest === 'function')
        ? t.closest('[data-mp-card]') : null;
      var pool = (card && typeof card.getAttribute === 'function')
        ? card.getAttribute('data-mp-card') : null;
      if (pool) return doMpEdit(pool);
    }
  }

  if (typeof document !== 'undefined'
      && typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', onKeyDown);
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
  window.mpLinkState = linkState;
  window.mpTreeHtml = treeHtml;
  window.mpCardsHtml = cardsHtml;
  window.doMpToggle = doMpToggle;
  window.doMpEdit = doMpEdit;
  window.doMpUndo = doMpUndo;
  window.doMpBannerAction = doMpBannerAction;
  window.doMpBannerDismiss = doMpBannerDismiss;
  window.doMpInlineSelect = doMpInlineSelect;
  window.doMpInlineApply = doMpInlineApply;
  window.doMpInlineCancel = doMpInlineCancel;
  window.mpPrimeInline = primeInline;
  window.doMpTimelineToggle = doMpTimelineToggle;
  window.doMpDegradeView = doMpDegradeView;
  window.doMpDegradeDismiss = doMpDegradeDismiss;
  window.mpEditReplace = mpEditReplace;
  window.mpEditAddCandidate = mpEditAddCandidate;
  window.mpEditPreview = mpEditPreview;
  window.mpEditApply = mpEditApply;
  window.doMpSnippet = doMpSnippet;
  window.doMpSnippetCopy = doMpSnippetCopy;
})();
