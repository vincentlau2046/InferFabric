/* router.js — dashboard UX v3 A1：hash 路由（无依赖，~120 行）。
 *
 * 规格：docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md A1 +
 * specs/dashboard-ux-v3/interface-contract.md（A 区）。
 *
 * 职责：
 *   - URL `#/view[/sub][?k=v]` ⇄ 视图路由的唯一定义点（六区表 + 旧 tab 迁移）。
 *   - 启动解析优先级：location.hash > localStorage iff_view > 旧 iff_active_tab
 *     （含 tab-deploy→packs 前置迁移）> status。旧键迁移后删除。
 *   - navigate() 受切换锁约束（同 store.switchTab 现行为）；DOM 应用仍由
 *     store.applyTab 承担（唯一挂载/渲染路径），router 只管地址与恢复。
 *   - iff_view 只存 view[/sub]，永不存过滤参数（隐私与书签语义分离）。
 *
 * A1 过渡期：DOM tab id 仍是旧 tab-*，经 tabForRoute 映射；A2 换新壳后
 * 由装配层对齐为 view 命名，映射表收敛为恒等。
 */
(function () {
  'use strict';

  /* ── 路由表：view → 合法 sub 集合（null = 区默认页） ── */
  var VIEWS = ['status', 'requests', 'usage', 'routing', 'models', 'config'];
  var SUBS = {
    status: [null],
    requests: [null, 'switches', 'anomalies'],
    usage: [null],
    routing: [null],
    models: [null],
    config: [null, 'packs', 'auth', 'connect', 'sys', 'cloud'],
  };

  /* 旧 iff_active_tab → 路由（tab-deploy 先并 packs，再映射，2026-11-20 裁决沿用） */
  var LEGACY_TAB_ROUTE = {
    'tab-overview': { view: 'status', sub: null },
    'tab-monitor': { view: 'requests', sub: null },
    'tab-anomaly': { view: 'requests', sub: 'anomalies' },
    'tab-inference': { view: 'models', sub: null },
    'tab-model-pools': { view: 'routing', sub: null },
    'tab-packs': { view: 'config', sub: 'packs' },
    'tab-deploy': { view: 'config', sub: 'packs' },
    'tab-security': { view: 'config', sub: 'auth' },
    'tab-connect': { view: 'config', sub: 'connect' },
    'tab-settings': { view: 'config', sub: 'sys' },
    'tab-cloud': { view: 'config', sub: 'cloud' },
  };

  /* 路由 → 六区壳 DOM（A2：映射收敛为恒等；子页由 store.applyTab 的 data-sub 显隐承担） */
  var ROUTE_TAB = {
    'status': 'tab-status',
    'requests': 'tab-requests',
    'usage': 'tab-usage',
    'routing': 'tab-routing',
    'models': 'tab-models',
    'config': 'tab-config',
  };

  /* ── 纯函数：hash ⇄ route ── */

  function parseHash(hash) {
    if (typeof hash !== 'string' || hash.indexOf('#/') !== 0) return null;
    var body = hash.slice(2);
    var qIdx = body.indexOf('?');
    var path = qIdx >= 0 ? body.slice(0, qIdx) : body;
    var qs = qIdx >= 0 ? body.slice(qIdx + 1) : '';
    var parts = path.split('/');
    var view = parts[0] || '';
    var sub = parts.length > 1 ? (parts[1] || null) : null;
    if (VIEWS.indexOf(view) < 0) return null;          // 未知 view 交由 fallback
    if (sub !== null && SUBS[view].indexOf(sub) < 0) sub = '__invalid__';
    var params = {};
    if (qs) {
      qs.split('&').forEach(function (kv) {
        if (!kv) return;
        var eq = kv.indexOf('=');
        var k = eq >= 0 ? kv.slice(0, eq) : kv;
        var v = eq >= 0 ? kv.slice(eq + 1) : '';
        if (!k) return;
        try {
          params[decodeURIComponent(k)] = decodeURIComponent(v);
        } catch (e) { /* 畸形转义：丢该参数，不抛 */ }
      });
    }
    return { view: view, sub: sub, params: params };
  }

  function toHash(view, sub, params) {
    var h = '#/' + view + (sub ? '/' + sub : '');
    if (params) {
      var pairs = Object.keys(params).filter(Boolean).map(function (k) {
        return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
      });
      if (pairs.length) h += '?' + pairs.join('&');
    }
    return h;
  }

  function routeForTab(tabId) {
    // A2：六区壳 id（tab-<view>）直接映射；其余走旧 tab 迁移表
    if (typeof tabId === 'string' && tabId.indexOf('tab-') === 0) {
      var v = tabId.slice(4);
      if (VIEWS.indexOf(v) >= 0) return { view: v, sub: null };
    }
    var r = LEGACY_TAB_ROUTE[tabId];
    return r ? { view: r.view, sub: r.sub } : null;
  }

  function tabForRoute(view, sub) {
    return ROUTE_TAB[view] || null;
  }

  /* ── 存取（全程 try/catch：存储不可用不致命） ── */

  function readStoredRoute() {
    try {
      var v = localStorage.getItem('iff_view');
      if (v) {
        var r = parseHash('#/' + v);
        if (r) {
          if (r.sub === '__invalid__') r.sub = null;     // 非法子页回退区默认
          return r;
        }
      }
    } catch (e) { /* fall through to legacy */ }
    try {
      var tab = localStorage.getItem('iff_active_tab');
      if (tab) {
        if (tab === 'tab-deploy') {
          try { localStorage.setItem('iff_active_tab', 'tab-packs'); } catch (e2) {}
          tab = 'tab-packs';
        }
        var r2 = routeForTab(tab);
        if (r2) {
          try {
            localStorage.setItem('iff_view', r2.view + (r2.sub ? '/' + r2.sub : ''));
            localStorage.removeItem('iff_active_tab');   // 迁移成功即清旧键
          } catch (e3) { /* 写不进也不阻塞恢复 */ }
          return r2;
        }
      }
    } catch (e4) { /* fall through */ }
    return null;
  }

  function persistRoute(route) {
    try {
      localStorage.setItem('iff_view', route.view + (route.sub ? '/' + route.sub : ''));
    } catch (e) { /* 私有模式等：放弃持久化，不影响渲染 */ }
  }

  /* ── 锁与应用 ── */

  function isLocked() {
    try {
      return !!(window.store && typeof window.store.isSwitchLocked === 'function' &&
                window.store.isSwitchLocked());
    } catch (e) { return false; }
  }

  var _lastAppliedKey = null;

  function applyRoute(route) {
    if (!route) return false;
    // 幂等去重：navigate 显式应用后，浏览器随后派发的 hashchange（同 key）
    // 不再重复触发 renderer；真实多次应用只发生在 route 真正变化时。
    var key = route.view + '/' + (route.sub || '');
    if (key === _lastAppliedKey) return true;
    var tab = tabForRoute(route.view, route.sub);
    persistRoute(route);
    var applied = false;
    try {
      if (window.store && typeof window.store.setActiveView === 'function') {
        window.store.setActiveView(route.view, route.sub);      // 视图组合先于 DOM 应用
      }
      if (tab && window.store && typeof window.store.applyTab === 'function') {
        window.store.applyTab(tab);
        applied = true;
      }
    } catch (e) { /* store 未就绪/应用失败：下次 hashchange 再应用 */ }
    // 应用成功才记录键：applyTab 抛错时同路由必须可重试，不能被提前去重
    if (applied) _lastAppliedKey = key;
    return true;
  }

  function resolve(hash) {
    var r = parseHash(hash);
    if (r && r.sub === '__invalid__') r.sub = null;      // 非法子页回退区默认
    if (r) return r;
    // 旧锚点收藏（#tab-xxx，无 '#/'）：按旧 tab 迁移表解析（cross-review Important 2）
    if (typeof hash === 'string' && hash.indexOf('#tab-') === 0) {
      var lr = routeForTab(hash.slice(1));
      if (lr) return { view: lr.view, sub: lr.sub, params: {} };
    }
    return { view: 'status', sub: null, params: {} };
  }

  /** 把当前地址规范为 resolved 路由的规范形式（replace 不新增历史项）。
   *  hash→路由 的三个入口（boot / hashchange / reconcile）共用此规范化。 */
  function canonicalize(r) {
    var canonical = toHash(r.view, r.sub, r.params || {});
    if (location.hash !== canonical) {
      try { location.replace(canonical); } catch (e) { /* 非 DOM 环境忽略 */ }
    }
  }

  /* ── 公共 API ── */

  var router = {
    parseHash: parseHash,
    toHash: toHash,
    routeForTab: routeForTab,
    tabForRoute: tabForRoute,

    /** 程序化导航（click/代码共用入口）。锁定或未知 view → false。
     *  非法 sub 回退区默认（不进 hash/存储）；设 hash 后显式应用一次；
     *  浏览器随后派发的 hashchange 由幂等去重吞掉。 */
    navigate: function (view, sub, params) {
      if (VIEWS.indexOf(view) < 0) return false;
      if (isLocked()) return false;
      // 非法子页不得进入地址与持久化（cross-review Important 2）
      if (sub !== null && sub !== undefined &&
          (SUBS[view] || []).indexOf(sub) < 0) sub = null;
      var target = toHash(view, sub, params);
      if (location.hash !== target) location.hash = target;
      applyRoute({ view: view, sub: sub || null, params: params || {} });
      return true;
    },

    /** hashchange 入口：锁与兜底都在这一条路径上；畸形地址 replace 规范。 */
    applyFromHash: function () {
      if (isLocked()) return false;
      var r = resolve(location.hash);
      canonicalize(r);
      return applyRoute(r);
    },

    /** 解锁对账（store.setSwitchLocked(false) 调用）：锁内被丢弃的浏览器
     *  前进/后退 hash，在解锁时对当前地址补一次解析与应用。 */
    reconcile: function () {
      if (isLocked()) return false;
      var r = resolve(location.hash);
      var key = r.view + '/' + (r.sub || '');
      if (key === _lastAppliedKey) return true;
      canonicalize(r);
      return applyRoute(r);
    },

    /** 启动：hash 优先；否则按存储恢复（用 replace 不留多余历史项）。
     *  锁定时不应用也不持久化（与旧 restoreTab→switchTab 行为一致）；
     *  未知/畸形地址规范为合法 hash（replace，不产生历史项）。 */
    boot: function () {
      if (isLocked()) return false;
      if (location.hash) {
        var r = resolve(location.hash);
        canonicalize(r);
        return applyRoute(r);
      }
      var stored = readStoredRoute();
      var route = stored || { view: 'status', sub: null, params: {} };
      try { location.replace(toHash(route.view, route.sub, {})); } catch (e) { /* 忽略 */ }
      return applyRoute(route);
    },
  };

  window.ifRouter = router;
  try {
    window.addEventListener('hashchange', function () { router.applyFromHash(); });
  } catch (e) { /* 非 DOM 环境（单测）：跳过监听 */ }
  try { router.boot(); } catch (e2) { /* 启动失败不阻塞后续脚本 */ }
})();
