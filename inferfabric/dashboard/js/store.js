/* InferFabric Console — State Store (v2)
 * 自 state.js 全量移植：StateStore + /api/snapshot 轮询 + ETag + 乱序守卫
 *   + 切换 overlay 订阅 + sync 指示器 + 主题初始化。
 * 变更点：
 *   - forceRefresh 调用 window.tabRenderers[tab_active]?.(snap) 替代 window.refreshPanels
 *   - 顶栏绑定改为订阅式（gpu_mode→#modeBadge、sync_meta→#syncMeta、version→#navVer、
 *     api_error→#apiErrorBanner、switch_target→UI.toast）；旧指标卡绑定作废（遥测带由后续任务接管）
 *   - 主题：dark 为 :root 默认，[data-theme="light"] 覆盖；updateThemeIcon 改双 SVG
 *   - 自举：旧巨石模块已删除，store 自行 restoreTab + startPolling
 * 暴露为 window.store。
 */
(function () {
  'use strict';

class StateStore {
  constructor(initial = {}) {
    this._state = { ...initial };
    this._subs = {};
    this._timer = null;
    this._tabActive = null;
    this._switchLocked = false;
    // Snapshot sync metadata
    this._etag = null;   // last etag received from /api/snapshot
    this._lastTs = 0;    // last (monotonic) snapshot timestamp
  }

  /* ── Core pub/sub ── */
  get(key) { return key ? this._state[key] : this._state; }

  set(key, value) {
    const old = this._state[key];
    if (old === value) return;  // skip no-op
    this._state[key] = value;
    this._notify(key, value, old);
  }

  update(obj, prefix = '') {
    if (obj == null || typeof obj !== 'object') return;
    for (const [k, v] of Object.entries(obj)) {
      const fullKey = prefix ? `${prefix}.${k}` : k;
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
        // Recurse into nested objects
        const existing = this._state[fullKey] || {};
        if (existing !== v) {
          this._state[fullKey] = { ...existing, ...v };
          this._notify(fullKey, this._state[fullKey], existing);
        }
      } else {
        const oldVal = this._state[fullKey];
        if (oldVal !== v) {
          this._state[fullKey] = v;
          this._notify(fullKey, v, oldVal);
        }
      }
    }
  }

  on(key, callback) {
    if (!this._subs[key]) this._subs[key] = new Set();
    this._subs[key].add(callback);
    const v = this._state[key];
    if (v !== undefined) callback(v, undefined);
    return () => this._subs[key].delete(callback);
  }

  _notify(key, value, old) {
    const subs = this._subs[key];
    if (subs) subs.forEach(cb => { try { cb(value, old); } catch(e) { console.warn('[state] sub error:', key, e); } });
  }

  /* ── API polling (single source of truth: /api/snapshot) ── */
  async fetchSnapshot(force = false) {
    try {
      const headers = {};
      if (this._etag && !force) headers['If-None-Match'] = this._etag;
      const res = await fetch('/api/snapshot', { headers, cache: 'no-store' });

      if (res.status === 304) {
        // Control plane unchanged — keep last state, update sync meta only.
        // lastOk = 最近一次成功响应（304 也算"在线"）；ts 仍是最近一次 200 数据时间。
        // 两者分离：稳态下连续 304 不再触发"已断线"（C1 附带：store.js:353 误报）。
        this.set('api_error', null);
        this.set('sync_meta', {
          etag: this._etag,
          rev: (this._etag || '').slice(1, 9) || '',
          ts: this._lastTs,
          lastOk: Date.now() / 1000,
          changed: false,
          stale: false,
        });
        return null;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const snap = await res.json();

      // Out-of-order guard: drop stale responses (ts regressed).
      const stale = !force && snap.ts != null && this._lastTs > 0 && snap.ts <= this._lastTs;
      this._etag = snap.etag || null;
      this._lastTs = Math.max(this._lastTs, snap.ts || 0);
      this.set('api_error', null);

      if (stale) {
        console.warn('[state] out-of-order snapshot dropped (ts', snap.ts, '<= last', this._lastTs);
        this.set('sync_meta', {
          etag: this._etag,
          rev: (this._etag || '').slice(1, 9) || '',
          ts: this._lastTs,
          changed: false,
          stale: true,
        });
        return snap;
      }

      const status = snap.status || {};
      const sys = snap.system || {};

      // ── Normalize into binding-friendly shape (merged via update → merge)
      const gpuUsed = status.gpu_used_mb || 0;
      const gpuTotal = status.gpu_total_mb || 32607;
      const ramTotal = sys.ram_total_gb || 1;
      const ramUsed = sys.ram_used_gb || 0;

      const merged = {
        gpu_mode: status.gpu_mode || 'idle',
        gpu: {
          used:  gpuUsed,
          total: gpuTotal,
          pct:   (gpuUsed / gpuTotal * 100),
        },
        gpu_util: {
          pct:   sys.gpu_util_pct || 0,
          clock: sys.gpu_clock_mhz || '—',
          power: sys.gpu_power_w || '—',
          temp:  sys.gpu_temp_c || null,
        },
        mem: {
          used:  ramUsed,
          total: ramTotal,
          pct:   (ramUsed / ramTotal * 100),
        },
        cpu: {
          pct:    sys.cpu_percent || 0,
          cores:  sys.cpu_cores || '—',
          uptime: sys.uptime_seconds || 0,
        },
        version: status.version || sys.version || '',
        switch_target: status.switch_target || null,
        // Raw passthrough for legacy consumers
        active_services: status.active_services || [],
        services_info:   status.services_info || {},
        services_health: status.services_health || {},
        sleep_states:    status.sleep_states || {},
      };
      this.update(merged);

      // ── Replace semantics for collections/maps: drop stale entries ──
      this.set('status', status);
      this.set('active_services', merged.active_services);
      this.set('services_info', merged.services_info);
      this.set('services_health', merged.services_health);
      this.set('sleep_states', merged.sleep_states);
      this.set('models', snap.models || []);
      this.set('history', snap.history || []);
      this.set('token_stats', snap.token_stats || {});
      this.set('request_log', snap.request_log || []);
      this.set('metrics_24h', snap.metrics_24h || {});
      this.set('local_models', snap.local_models || { discovered: [], configured: [] });
      this.set('sync_meta', {
        etag: this._etag,
        rev: (this._etag || '').slice(1, 9) || '',
        ts: snap.ts || this._lastTs,
        lastOk: Date.now() / 1000,
        changed: true,
        stale: false,
      });

      // Trigger render via batched macrotask
      this._scheduleRender();
      return snap;
    } catch (e) {
      console.warn('[state] snapshot fetch error:', e);
      this.set('api_error', e.message);
      this.set('sync_meta', {
        etag: this._etag,
        rev: (this._etag || '').slice(1, 9) || '',
        ts: this._lastTs,
        changed: false,
        stale: true,
      });
      return null;
    }
  }

  /* Force a fresh snapshot (manual refresh / post-action refresh).
   * v2: 按 TAB 注册的渲染器替代 window.refreshPanels。 */
  async forceRefresh() {
    const snap = await this.fetchSnapshot(true);
    if (snap) {
      const fn = window.tabRenderers && window.tabRenderers[this.get('tab_active')];
      if (typeof fn === 'function') {
        try { fn(snap); } catch (e) { console.warn('[store] tabRenderer error:', e); }
      }
    }
    return snap;
  }

  _scheduleRender() {
    if (this._renderScheduled) return;
    this._renderScheduled = true;
    // Use macrotask to batch multiple updates into one render
    setTimeout(() => {
      this._renderScheduled = false;
      if (window.render) window.render(this._state);
    }, 0);
  }

  startPolling(intervalMs = 3000) {
    // Immediate fetch
    this.fetchSnapshot();
    // Clear any existing timer
    if (this._timer) clearInterval(this._timer);
    // Start new timer
    this._timer = setInterval(() => this.fetchSnapshot(), intervalMs);
  }

  stopPolling() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  /* ── Tab Management ── */
  switchTab(tabId) {
    if (this._switchLocked) return;
    // Deactivate all tabs
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.if-nav-item').forEach(t => t.classList.remove('active'));
    // Activate target
    const target = document.getElementById(tabId);
    if (target) target.classList.add('active');
    const btn = document.querySelector(`.if-nav-item[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');
    // Store + notify
    this._tabActive = tabId;
    this.set('tab_active', tabId);
    // Save to localStorage
    try { localStorage.setItem('iff_active_tab', tabId); } catch(e) {}
  }

  restoreTab() {
    try {
      const saved = localStorage.getItem('iff_active_tab');
      // Restore last active tab (anomaly is a regular nav item in v2, no longer hidden)
      if (saved) this.switchTab(saved);
    } catch(e) {}
  }

  /* ── Switch Lock ── */
  setSwitchLocked(locked) {
    this._switchLocked = locked;
  }
  isSwitchLocked() {
    return this._switchLocked;
  }
}

/* ── Bootstrap ─────────────────────────────── */
const store = new StateStore();
window.store = store;
window.startPolling = (ms) => store.startPolling(ms);
window.restoreTab = () => store.restoreTab();

// TAB 渲染器注册表（后续任务填充：tabRenderers['tab-overview'] = fn(snap)）
window.tabRenderers = {};

// Render on state changes (batched)
store.on('gpu_mode', () => store._scheduleRender());

// Expose switchTab globally (replaces legacy monolith version)
window.switchTab = window.switchTab || ((tabId) => store.switchTab(tabId));

/* ── 顶栏 / 外壳绑定（订阅式，R2） ──────────────────────────── */

// GPU mode → #modeBadge（状态配色 + 文本）
store.on('gpu_mode', (mode) => {
  const el = document.getElementById('modeBadge');
  if (!el) return;
  const m = mode || 'idle';
  el.className = 'if-mode ' + m;
  const txt = el.querySelector('.mode-txt');
  if (txt) txt.textContent = m;
});

// sync_meta → #syncMeta（顶栏同步文本）
store.on('sync_meta', (m) => {
  const el = document.getElementById('syncMeta');
  if (!el || !m) return;
  const t = m.ts ? new Date(m.ts * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
  el.textContent = m.stale ? '已断线' : '已同步 ' + t + ' · rev ' + (m.rev || '—');
  el.title = m.stale ? '数据已断线' : '最近同步 ' + t;
});

// version → #navVer
store.on('version', (v) => {
  const el = document.getElementById('navVer');
  if (el) el.textContent = v ? 'v' + v : '';
});

// api_error → #apiErrorBanner 可见性 + "最后数据更新于 X"（spec §5：断连 banner 带最后同步时间）
// sync_meta.ts 为最近一次成功同步的 epoch 秒（断连时 store 仍持有 _lastTs）；
// 从未成功同步过则 ts=0 → 显示 "—"。
store.on('api_error', (err) => {
  const el = document.getElementById('apiErrorBanner');
  if (!el) return;
  const txt = el.querySelector('span');
  if (err) {
    const meta = store.get('sync_meta') || {};
    const t = meta.ts
      ? new Date(meta.ts * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit'})
      : '—';
    if (txt) txt.textContent = 'API 连接异常 · 最后数据更新于 ' + t;
    el.style.display = '';
  } else {
    if (txt) txt.textContent = 'API 连接异常';
    el.style.display = 'none';
  }
});

// switch_target → UI.toast（切换通知）
store.on('switch_target', (val) => {
  if (val !== null && val !== '' && val !== undefined && window.UI) {
    window.UI.toast('正在切换到 ' + val + '...', 'info');
  }
});

/* ── Switch Overlay（P5.4，自 state.js 移植） ── */
store.on('switch_target', (val) => {
  const overlay = document.getElementById('switchOverlay');
  if (!overlay) return;
  if (val) {
    overlay.style.display = '';
    const msg = document.getElementById('switchOverlayMsg');
    if (msg) msg.textContent = '正在切换到 ' + val + '…';
    // Lock sidebar nav
    document.querySelectorAll('.if-nav-item').forEach(el => el.classList.add('locked'));
    store.setSwitchLocked(true);
  } else {
    overlay.style.display = 'none';
    document.querySelectorAll('.if-nav-item').forEach(el => el.classList.remove('locked'));
    store.setSwitchLocked(false);
  }
});

/* ── Sync / Freshness Indicator（侧栏底部状态点，自 state.js 移植） ── */
function updateSyncIndicator(meta) {
  if (!meta) return;
  const dot = document.getElementById('sidebarStatusDot');
  const txt = document.getElementById('sidebarSyncTxt');
  // lastOk（最近一次成功响应 200/304）驱动"在线"判定；ts（最近一次 200 数据时间）仅用于显示。
  // 稳态连续 304 时 lastOk 持续刷新 → 不误报"已断线"（C1 附带：store.js:353 误报）。
  const stale = meta.stale || (!meta.changed && ((Date.now() / 1000) - (meta.lastOk || meta.ts || 0) > 6));
  if (dot) {
    dot.className = 'sidebar-status-dot ' + (stale ? 'err' : 'ok');
  }
  if (txt) {
    txt.textContent = stale ? '已断线' : (meta.rev ? 'rev ' + meta.rev : '运行中');
  }
}
store.on('sync_meta', updateSyncIndicator);

/* ── 顶栏手动刷新（store 自带，旧巨石已删除） ── */
window.refreshNow = async function () {
  await store.forceRefresh();
  if (window.UI) window.UI.toast('已刷新', 'ok');
};

/* ── Theme Toggle（dark 为 :root 默认，[data-theme="light"] 覆盖） ── */
function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.getAttribute('data-theme') === 'light';
  if (isLight) {
    html.removeAttribute('data-theme');          // → dark (:root 默认)
    localStorage.setItem('iff_theme', 'dark');
    store.set('theme', 'dark');                  // 通知图表等订阅者（charts.js）
  } else {
    html.setAttribute('data-theme', 'light');
    localStorage.setItem('iff_theme', 'light');
    store.set('theme', 'light');
  }
  updateThemeIcon();
}

function updateThemeIcon() {
  const btn = document.getElementById('themeToggle');
  if (!btn) return;
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  // light 模式显示月亮（切回深色）；dark 模式显示太阳（切到浅色）
  btn.innerHTML = isLight
    ? '<svg width="18" height="18"><use href="#s-moon"/></svg>'
    : '<svg width="18" height="18"><use href="#s-sun"/></svg>';
  btn.title = isLight ? '切换到深色主题' : '切换到浅色主题';
}
window.toggleTheme = toggleTheme;

// Initialize theme from localStorage. 用户偏好浅色（2026-11-03 拍板）：
// light 为默认。显式 saved==='dark' 仍尊重；无显式选择时不再跟随 OS，
// 直接 light。:root 语义色仍按 dark 定义（[data-theme] 覆盖），default 只是
// 首屏落点。
(function initTheme() {
  const saved = localStorage.getItem('iff_theme');
  var th = 'light';
  if (saved === 'dark') {
    document.documentElement.removeAttribute('data-theme');   // → dark (:root 默认)
    th = 'dark';
  } else {
    document.documentElement.setAttribute('data-theme', 'light');
    th = 'light';
  }
  store.set('theme', th);                       // 初值：charts.js 订阅时即时回放
  updateThemeIcon();
})();

/* ── 自举：store 启动轮询 + 恢复 TAB（旧巨石已删除） ── */
function boot() {
  try { store.restoreTab(); } catch (e) { console.warn('[store] restoreTab:', e); }
  try { store.startPolling(); } catch (e) { console.warn('[store] startPolling:', e); }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
})();
