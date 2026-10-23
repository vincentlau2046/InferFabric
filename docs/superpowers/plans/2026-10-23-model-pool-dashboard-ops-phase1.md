# 模型池 Dashboard 运营视图 Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「换一个模型」成为 < 30 秒、零 YAML、零重启、agent 零改动的面板操作：角色卡片 + 候选链运行态 + 三段式编辑器 + 角色模板向导 + 接入片段。

**Architecture:** 后端只加**读路径**（runtime 端点、GET 三态+raw、dry-run 编辑态预览、CooldownStore 只读扩展）；前端重构 model-pools.js 为运营视图（轮询三端点、健康灯、编辑器复用既有 validate/dry-run/CAS API）。

**Tech Stack:** Python 3.10+（stdlib only，无新依赖）；dashboard 原生 JS（无框架、事件委托、无 inline onclick、零 emoji）；前端测试用既有 Node VM harness 模式（tests/unit/dashboard/test_model_pools_panel.py）。

**Spec:** `docs/superpowers/specs/2026-10-23-model-pool-dashboard-ops-design.md`（含 builder check 四项裁决，全部已定案）

## Global Constraints

- PR-14：**不修改转发核心**——`forwarder.py` / `proxy/chat_handlers.py` / `handler.py` 既有函数零改动；`handler.py` 仅允许在 `_GET_ROUTES` 表加一行路由注册。
- 池调度**绝不自动启动本地模型**；候选行「未启动」只显示状态（无启动按钮）。
- 沙箱工作区带 cloud-UX 任务 WIP：`handler.py`、`fragments/style.css`、`js/cloud.js`、`js/inference.js`、`fragments/cloud.html`、`cloud_discovery.py`、`forwarder.py`、若干 test 文件。本任务与 WIP 同文件叠加的只有 `handler.py` 与 `style.css`——**提交时用 /tmp 剥离法**（cp 全量备份 → checkout HEAD → 仅重放本任务增量 → add+commit → 恢复备份）。其余文件提交用精确文件清单，禁止 `git add -A`。
- 版本：Phase 1 = **7.1.0**（用户已授权 phase 小版本；`inferfabric/__init__.py` 唯一版本源）。
- 每次改 JS 后跑 `node --check`；前端行为测试跑真实 JS（Node VM harness），不测 mock。
- dashboard 视觉约定：沿用 if-card/if-table 体系；新 CSS 进 `fragments/style.css` 末尾独立注释段（`/* ── 模型池运营视图 v7.1.0 ── */`），方便提交剥离。
- 测试时钟：所有注入 `monotonic` 的测试不得混用真实时钟（终审 Ruling 教训）。

## Review Focus（本计划测试未覆盖、最可能咬人的五类）

1. **runtime 端点在 registry 从未配置时**（空快照 revision=0）必须返回 `{"revision":0,"pools":{}}` 而不是 500 — Task 5 加测试。
2. **CooldownStore.entries() 半开探测在途快照一致性**：claim_probe 后 finish_probe 前，entries 必须报 `probing=true` 且 `remaining_s<=0`；finish_probe(ok) 后条目消失 — Task 1 加测试。
3. **dry-run 编辑态与已发布快照同池不同候选**：预览结果必须来自传入 config 而非快照 — Task 5 加测试（同池名，config 中候选与快照不同，断言 decisions 来自 config）。
4. **坏文件三态**：`configured=false + load_error≠null` 时 GET 不 500、前端不出向导 — Task 5 + Task 8 加测试。
5. **record_attempt 混时钟**：run_attempts 注入假时钟时 record_attempt 也必须用同一时钟（否则 age_s 错乱）— Task 2 测试全部注入 `_FakeClock`。

---

### Task 1: CooldownStore 运行态扩展（reason / entries / record_attempt / last_attempt）

**Files:**
- Modify: `inferfabric/model_pool/pool_forward.py`（CooldownStore，约 47–115 行区域）
- Test: `tests/unit/model_pool/test_cooldown_runtime.py`（新建）

**Interfaces:**
- Produces（Task 5 的 runtime 端点依赖）:
  - `CooldownStore.add(fingerprint, now=None, ttl=None, reason=None)` — 新增第 4 参，既有调用零变化
  - `CooldownStore.quarantine(fingerprint, now=None)` — 内部以 `reason="quarantine_404"` 调 add
  - `CooldownStore.entries(now=None) -> dict[str, dict]` — `{fp: {"until": float, "remaining_s": float, "reason": str|None, "probing": bool}}`；含已到期待探测条目；锁内快照
  - `CooldownStore.record_attempt(fingerprint, ok, status=None, now=None) -> None`
  - `CooldownStore.last_attempts(now=None) -> dict[str, dict]` — `{fp: {"ok": bool, "status": int|None, "age_s": float}}`
- 语义：`finish_probe(ok=False)` 重新冷却**保留原 reason**；`finish_probe(ok=True)` 删条目（连 reason 一起）；`add()` 覆盖 reason（新故障取代旧原因）。

- [ ] **Step 1: 写失败测试**（新文件，全部注入 `_FakeClock`，不碰真实时钟）

```python
class _FakeClock:
    def __init__(self, start=0.0): self.t = start
    def __call__(self): return self.t
    def sleep(self, s): self.t += s
```

测试用例（每个一 test）：
- `test_add_records_reason_in_entries` — add(fp, reason="cooldown_401") → entries()[fp]["reason"] == "cooldown_401"
- `test_quarantine_reason_is_404` — quarantine(fp) → entries()[fp]["reason"] == "quarantine_404" 且 remaining_s ≈ 1800
0
- `test_add_without_reason_keeps_none`（默认向后兼容）
- `test_entries_reports_probing_during_half_open` — add → 时钟走过 TTL → claim_probe True → entries 报 remaining_s <= 0 且 probing True → finish_probe(False) → probing False 且重新冷却（reason 保留）
- `test_entries_omits_recovered` — finish_probe(True) 后 fp 不在 entries
- `test_record_attempt_roundtrip` — record_attempt(fp, ok=False, status=503) → last_attempts()[fp] == {"ok": False, "status": 503, "age_s": 0}；时钟前进 180 → age_s == 180
- `test_record_attempt_overwrites` — 第二次 record 覆盖第一次
- `test_last_attempts_empty_initially`

- [ ] **Step 2: 跑测试确认失败** — `python3 -m pytest tests/unit/model_pool/test_cooldown_runtime.py -v` → Expected: FAIL（entries/record_attempt 不存在）
- [ ] **Step 3: 实现** — CooldownStore 增加 `self._reason: dict[str, str|None]`、`self._last: dict[str, tuple[bool, int|None, float]]`；add 存 reason + `_last` 不动；entries/record_attempt/last_attempts 全部锁内；finish_probe 失败路径保留 `_reason`。
- [ ] **Step 4: 跑测试确认通过** — Expected: 9/9 PASS
- [ ] **Step 5: 既有池测试回归** — `python3 -m pytest tests/unit/model_pool/ -q` → Expected: 全绿（无既有调用被破坏）
- [ ] **Step 6: Commit** — `git add inferfabric/model_pool/pool_forward.py tests/unit/model_pool/test_cooldown_runtime.py` → `feat(model-pool): CooldownStore 运行态只读扩展 — reason/entries/record_attempt`

### Task 2: run_attempts 记录最近尝试 + 冷却写入原因

**Files:**
- Modify: `inferfabric/model_pool/pool_forward.py`（run_attempts）
- Test: `tests/unit/model_pool/test_cooldown_runtime.py`（追加类 TestRunAttemptsRecording）

**Interfaces:**
- Consumes: Task 1 的 record_attempt；既有 `run_attempts(pool, candidates, transport, cooldowns=None, *, monotonic=None, sleep=None, deadline_start=None)`
- Produces: 每次真实传输回调返回后（admission 跳过不算），`cooldowns.record_attempt(cand.endpoint_fingerprint, res.ok, res.status, now=mono())`；401/403 路径 `cooldowns.add(..., reason="cooldown_401"/"cooldown_403")`；404 已由 quarantine 带 reason。

- [ ] **Step 1: 失败测试**（注入 `_FakeClock`；transport stub 返回 503）：
- `test_failed_attempt_recorded_in_last_attempts` — 跑一次 run_attempts（候选失败）→ cooldowns.last_attempts() 有该 fp，status==503，ok False
- `test_admission_skip_not_recorded` — 候选在冷却中（预先 add）→ last_attempts 不含该 fp（claim_probe 跳过不记录）
- `test_401_sets_cooldown_reason` — transport 返回 401 → entries()[fp]["reason"] == "cooldown_401"
- `test_404_sets_quarantine_reason` — transport 返回 404 → entries()[fp]["reason"] == "quarantine_404"
- [ ] **Step 2: 确认失败** — Expected: 4 个 FAIL（reason 未传/未记录）
- [ ] **Step 3: 实现**（record_attempt 在 transport 返回后、动作表分支前调用一次）
- [ ] **Step 4: 确认通过 + 池回归** — `python3 -m pytest tests/unit/model_pool/ -q` 全绿
- [ ] **Step 5: Commit** — `feat(model-pool): run_attempts 记录最近尝试 + 401/403/404 冷却原因`

### Task 3: registry 运行状态（configured / last_load_error / raw_config / compile_pool）

**Files:**
- Modify: `inferfabric/model_pool/registry.py`
- Test: `tests/unit/model_pool/test_registry_runtime_state.py`（新建）

**Interfaces:**
- Produces（Task 5 依赖）:
  - `PoolRegistry.configured -> bool` — True=曾成功加载/写入过有效配置
  - `PoolRegistry.last_load_error -> str | None` — 最近一次加载失败原因；成功后清 None
  - `PoolRegistry.raw_config() -> dict | None` — `self._raw` 深拷贝（无则 None）
  - `PoolRegistry.compile_pool(config: dict, pool_name: str) -> PoolSnapshot` — `load_pools_dict(config, current_revision=<当前 revision>)` → `_compile(parsed)` → 返回 `pools[pool_name]`；非法 `ValueError`（issues join）、池不存在 `KeyError`。**只读不发布**。
- 状态更新点：`_load_locked` 文件缺失 → configured 保持；不可读/非法 → `last_load_error=消息`；成功 → `configured=True, last_load_error=None`。`update()`/`flip_pool_enabled()` 成功后同置。初始（未 load）= False/None。
- 注意：`_load_locked` 现有返回三态语义**零变化**（config_reloader 依赖）。

- [ ] **Step 1: 失败测试** —
  - `test_initial_state_unconfigured` — 新 registry（未 load）→ configured False、last_load_error None、raw_config() None
  - `test_load_valid_file_sets_configured` — 写合法 YAML → load() → configured True、raw_config() 含 pools
  - `test_missing_file_stays_unconfigured` — load()（无文件）→ configured False、error None
  - `test_invalid_file_sets_load_error` — 写坏 YAML → load() → configured False、last_load_error 非空；随后写入好文件 reload_from_disk() → error 清空、configured True
  - `test_update_sets_configured` — update() 成功 → configured True、raw_config 回读一致
  - `test_compile_pool_returns_snapshot_for_config` — compile_pool({"schema_version":1,"pools":{...}}, name) 返回 PoolSnapshot，candidates 已解析（含 endpoint_fingerprint）
  - `test_compile_pool_invalid_raises` — 非法 config → ValueError 含 issues
  - `test_compile_pool_unknown_pool_raises` — KeyError
  - `test_compile_pool_does_not_publish` — compile 后 snapshot() 不变
- [ ] **Step 2: 确认失败** — Expected: FAIL（属性不存在）
- [ ] **Step 3: 实现**
- [ ] **Step 4: 确认通过 + registry 既有测试回归**
- [ ] **Step 5: Commit** — `feat(model-pool): registry 运行状态三态 + raw_config + compile_pool（dry-run 编辑态用）`

### Task 4: admin 端点扩展 + 路由注册

**Files:**
- Modify: `inferfabric/model_pool/admin.py`、`inferfabric/proxy/handler.py`（仅 `_GET_ROUTES` 一行）
- Test: `tests/unit/model_pool/test_admin.py`（扩展）+ `tests/unit/proxy/test_routes.py`（若存在路由表测试则加一行断言；否则并入 test_admin）

**Interfaces:**
- Consumes: Task 1 `entries()/last_attempts()`；Task 3 `configured/last_load_error/raw_config()/compile_pool()`；既有 `runtime_view(pm)`、`select()`、`RequestRequirements`。
- Produces:
  - `GET /admin/model-pools` 响应新增 `"configured"`, `"load_error"`, `"raw"` 三键
  - `POST /admin/model-pools/dry-run` body 可选 `"config"`（完整配置 dict）→ compile_pool → 400 invalid_config（issues）/ 404 unknown_pool；不传时行为逐字节不变
  - `GET /admin/model-pools/runtime`（新）→ `{"revision", "pools": {name: {"candidates": [{id, kind, target, fingerprint, active, cooling, last_attempt}]}}}`；`active`: 本地=在 active_services，云=在 cloud_models；`cooling`: entries() 命中该 fp 时 `{"remaining_s", "reason", "probing"}` 否则 null；`last_attempt`: last_attempts() 命中时 `{"ok","status","age_s"}` 否则 null
  - handler.py `_GET_ROUTES` 增加 `"/admin/model-pools/runtime": _admin(lambda h, pm: mp_admin.handle_get_model_pools_runtime(h, pm)),`

- [ ] **Step 1: 失败测试**（沿用 test_admin.py 既有 fake handler/pm 模式）—
  - `test_get_includes_configured_raw_load_error`（三键存在、raw 与写入配置一致）
  - `test_get_unconfigured_returns_null_raw`（configured False、raw None、load_error None）
  - `test_dry_run_with_config_previews_edited_candidates`（**Review Focus #3**：同池名，config 候选 ≠ 快照候选，decisions 来自 config）
  - `test_dry_run_with_config_invalid_400`
  - `test_dry_run_with_config_unknown_pool_404`
  - `test_dry_run_without_config_unchanged`（不传 config → 走快照，响应与既有契约一致）
  - `test_runtime_endpoint_shape`（配置 + 冷却注入后：active/cooling/last_attempt 三态各出现一次）
  - `test_runtime_endpoint_empty_registry`（**Review Focus #1**：从未配置 → `{"revision":0,"pools":{}}` 200）
  - `test_runtime_endpoint_registered_in_get_routes`（handler._GET_ROUTES 含该路径）
- [ ] **Step 2: 确认失败** — Expected: FAIL
- [ ] **Step 3: 实现 admin.py 三个 handler 改动 + handler.py 一行路由**
- [ ] **Step 4: 确认通过 + `python3 -m pytest tests/unit/model_pool/ tests/unit/proxy/ -q` 全绿**
- [ ] **Step 5: Commit（/tmp 剥离法，见 Global Constraints）** — handler.py 只提交路由行；`feat(model-pool): runtime 端点 + GET 三态/raw + dry-run 编辑态预览`

### Task 5: 前端数据层与角色卡片重构（健康灯 + 候选链运行态）

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`（重构扩展）、`inferfabric/dashboard/fragments/model-pools.html`（sub 文案 + 操作按钮锚点）、`inferfabric/dashboard/fragments/style.css`（末尾新注释段）
- Test: `tests/unit/dashboard/test_model_pools_panel.py`（扩展）

**Interfaces:**
- Consumes: `GET /admin/model-pools`（含 configured/load_error/raw）、`/status`、`/runtime`（Task 4 契约）；既有 `UI.toast`、`UI.adminHeaders`、`tabRenderers` 注册。
- Produces（Task 6/7/8 依赖的 JS 结构）:
  - `window.renderModelPools()` — 渲染主入口（既有，扩展为三端点合并渲染）
  - `window.doMpToggle(pool, enable)` — 既有，零变化
  - 内部 `_state = {pools, status, runtime, revision, raw}` — 单一数据源
  - 候选行 DOM 锚点：`data-mp-cand="<pool>:<id>"`；操作按钮 `data-mp-act="edit|snippet|undo"`（事件委托）
  - 健康灯：`<span class="mp-lamp mp-lamp-ok|warn|err">` — 每协议计算：status.protocols[proto].chosen 是优先级 1 候选 → ok；chosen 非 null 但非首选 → warn；chosen null → err
  - 运行态徽标：active→「服务中/已发现」；!active→「未启动/未发现」；cooling→「冷却 mm:ss (reason)」或「隔离 mm:ss」或「探测中」；last_attempt.ok false→「最近: HTTP <status>」
- 渲染规则：`configured=false && load_error=null` → 渲染向导占位（Task 7 实装，本任务先渲染「未配置」空态文案）；`load_error≠null` → 错误横幅；polling 5s 且 document.hidden 时跳过（沿用既有 tabRenderer 触发模式，无独立 interval——按既有 renderModelPools 被调用节奏，本任务不加新 timer，刷新由既有 UI 轮询触发；若既有无轮询则本任务加 5s interval + visibilitychange 暂停）。

- [ ] **Step 1: 失败测试**（Node VM harness，routes 增 `/admin/model-pools/runtime`）—
  - `test_runtime_states_rendered` — 候选行含「冷却」+ reason + 「HTTP 503」最近失败文案
  - `test_health_lamp_warn_on_fallback` — openai chosen=第2候选 → mp-lamp-warn；两协议全 chosen=null → mp-lamp-err；首选 chosen → mp-lamp-ok
  - `test_unconfigured_state_message` — configured=false, raw=null → 显示未配置文案（不出候选表）
  - `test_load_error_banner` — load_error 非空 → 横幅含错误文本，无候选表
  - `test_active_badge_local_vs_cloud` — 本地 active=true「服务中」/false「未启动」；云 active=true「已发现」
- [ ] **Step 2: 确认失败** — `python3 -m pytest tests/unit/dashboard/test_model_pools_panel.py -v` → FAIL
- [ ] **Step 3: 实现 JS/HTML/CSS**（CSS 进 style.css 末尾 `/* ── 模型池运营视图 v7.1.0 ── */` 段）
- [ ] **Step 4: `node --check inferfabric/dashboard/js/model-pools.js` + 测试通过 + 既有面板测试不回归**
- [ ] **Step 5: Commit（style.css 用 /tmp 剥离法）** — `feat(dashboard): 模型池角色卡片 — 健康灯/候选链运行态/三态空态`

### Task 6: 三段式编辑器（含替换候选 + 会话撤销）

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`、`inferfabric/dashboard/fragments/model-pools.html`（模态容器）、`fragments/style.css`（模态样式段）
- Test: `tests/unit/dashboard/test_model_pools_panel.py`（扩展）

**Interfaces:**
- Consumes: `_state.raw`（编辑基底；null → `{"schema_version":1,"pools":{}}`）；`POST /admin/model-pools/validate`、`/dry-run`（带 config）、`/admin/model-pools`（CAS）；候选目录 `GET /models`（本地，取 name/served_name）+ `GET /status`（active_services）+ `GET /admin/cloud/providers`（云端 candidates）。
- Produces:
  - `window.doMpEdit(pool)` — 打开编辑器（事件委托 data-mp-act="edit"）
  - 编辑器内部状态：`_edit = {base: raw副本, work: 深拷贝, pool, revision}`；所有修改作用于 work
  - 候选操作（全部改 work.pools[pool].candidates）：**替换**（决策 4：新候选继承原 priority，旧候选移除）/ 上移 / 下移 / 删除 / 新增（priority=当前最大+1）；池参数：max_attempts、deadline_ms、allowed_locations、accept_declared
  - 预览阶段（点「预览」）：validate(work) → issues 非空 → 阻止并展示；否则 dry-run(work, openai) + dry-run(work, anthropic) 并排展示 decisions
  - 应用阶段（点「应用」）：POST `{...work, expected_revision: _state.revision}`；200 → toast + 刷新 + `_undo = base`（会话撤销快照）；409 → 冲突提示 + 重新拉取（不自动覆盖）
  - 撤销（卡片上 data-mp-act="undo"，仅 `_undo` 非空时显示）：POST `{..._undo, expected_revision: 最新拉取的 revision}`；成功 → 清 `_undo`
  - 模态内嵌于 `#mpModal` 容器（HTML fragment 增加），无 inline onclick

- [ ] **Step 1: 失败测试** —
  - `test_editor_replace_candidate_sends_new_config` — 替换后预览+应用：validate 调用体、dry-run config、POST body 均含新候选且不含旧候选；新候选 priority == 旧候选 priority
  - `test_editor_preview_blocked_on_issues` — validate 返回 issues 非空 → 不发 dry-run/POST，显示 issues
  - `test_editor_apply_409_prompts_reload` — POST 409 → toast 冲突 + 重新 GET
  - `test_editor_undo_reposts_previous_config` — 应用成功后点撤销 → POST body == 改前配置且 expected_revision == 最新 revision
  - `test_editor_base_is_raw_not_view` — GET raw 含悬空候选（视图无）→ 编辑器 work 含悬空候选 → POST body 保留之（**防丢数据核心断言**）
  - `test_editor_unconfigured_empty_base` — raw=null → work 为空骨架 → 可加候选并 POST
- [ ] **Step 2: 确认失败** — Expected: FAIL
- [ ] **Step 3: 实现**（模态三段 DOM 状态机：edit → preview → result；候选选择器数据从 /models + /status + /admin/cloud/providers 拉取合并）
- [ ] **Step 4: `node --check` + 全部面板测试通过**
- [ ] **Step 5: Commit** — `feat(dashboard): 模型池三段式编辑器 — 替换候选/预览/CAS/会话撤销`

### Task 7: 空态向导（四角色模板）+ 坏文件横幅行为闭环

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`、`fragments/model-pools.html`、`fragments/style.css`
- Test: `tests/unit/dashboard/test_model_pools_panel.py`（扩展）

**Interfaces:**
- Consumes: Task 5 三态分支；`POST /admin/model-pools`（`expected_revision: 0`）；候选目录端点同 Task 6。
- Produces:
  - `configured=false && load_error=null` 时渲染向导：fast/premium/small/classifier 四张角色卡（可删），每卡 0..N 候选（默认 1 槽，可加兜底槽）；提交 → 一次 CAS POST 完整配置（revision 0）
  - 模板建议（预填，可改）：fast → 本地已启动的小模型（按 /models 中 type 与 active 排序，active 优先）；premium → 未启动的大模型或云端首个候选；small → 最小本地模型；classifier → 任一 active 候选。目录为空时留空槽让用户手选。
  - `load_error≠null` 时仅横幅（Task 5 已渲染），**断言无 POST 发生**（Review Focus #4）

- [ ] **Step 1: 失败测试** —
  - `test_wizard_renders_four_roles_when_unconfigured`
  - `test_wizard_submit_posts_expected_revision_zero` — POST body 含四池 + expected_revision 0
  - `test_wizard_removed_role_not_posted` — 删掉 classifier → POST body 无 classifier
  - `test_broken_file_shows_banner_no_wizard_no_post` — load_error≠null → 横幅、无角色卡、零 POST
- [ ] **Step 2: 确认失败** — Expected: FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: `node --check` + 面板测试全绿**
- [ ] **Step 5: Commit** — `feat(dashboard): 模型池初始化向导 — 四角色模板 + 坏文件横幅`

### Task 8: 接入片段

**Files:**
- Modify: `inferfabric/dashboard/js/model-pools.js`、`fragments/model-pools.html`、`fragments/style.css`
- Test: `tests/unit/dashboard/test_model_pools_panel.py`（扩展）

**Interfaces:**
- Produces: `window.doMpSnippet(pool)` — 模态展示 OpenAI / Anthropic 两段可复制文本；内容含 `location.hostname` 拼 base_url、`iff/<pool>`、占位密钥、ACL 提示行（设计 §3.4 文案）、/v1 重复拼接警告；复制走 `navigator.clipboard.writeText`（harness mock），降级为选中文本提示。

- [ ] **Step 1: 失败测试** —
  - `test_snippet_contains_endpoints_and_model` — 两协议段各含 base_url 与 iff/<pool>；含「models ACL」提示文案
  - `test_snippet_copy_uses_clipboard` — navigator.clipboard.writeText 被调用且参数含 iff/<pool>
- [ ] **Step 2: 确认失败** — Expected: FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: `node --check` + 面板测试全绿**
- [ ] **Step 5: Commit** — `feat(dashboard): 模型池接入片段 — 双协议复制 + ACL 提示`

### Task 9: 版本 7.1.0 + 文档 + 全量门禁 + 提交树复验

**Files:**
- Modify: `inferfabric/__init__.py`（7.1.0）、`README.md`（Dashboard features 两行 + Model Pools 端点表 +runtime 行 + 逻辑模型池章节管理段更新）、`specs/model-pool-dashboard/STATUS.md`（新建，含提交链与验收记录）
- Test: 全量回归

- [ ] **Step 1: 版本号 7.1.0** — `inferfabric/__init__.py`
- [ ] **Step 2: README 更新** — Dashboard features 补「角色卡片/健康灯/三段式编辑器/初始化向导/接入片段」；Model Pools (admin) 表加 `GET /admin/model-pools/runtime` 行；逻辑模型池章节「管理」段补编辑器与三态说明
- [ ] **Step 3: specs/model-pool-dashboard/STATUS.md** — 目标/边界/提交链/验收（S1/S2/S4/S5 对照）/deferred（P2/P3 项）
- [ ] **Step 4: 全量回归** — `python3 -m pytest tests/unit/ tests/integration/ -q` → 全绿
- [ ] **Step 5: import 冒烟** — `python3 -c "import inferfabric; print(inferfabric.__version__)"` → 7.1.0
- [ ] **Step 6: 隔离 HTTP 冒烟**（假 pm 起 handler，走 GET /admin/model-pools、/runtime、dry-run 带 config）— 脚本随跑随弃
- [ ] **Step 7: 提交树复验**（committed-tree-verification 记忆）— `git worktree add /tmp/iff-p1-verify HEAD && cd /tmp/iff-p1-verify && python3 -m pytest tests/unit/ tests/integration/ -q` → 全绿（工作区绿 ≠ 提交树绿）
- [ ] **Step 8: Commit** — `chore(version): 7.1.0 — 模型池 Dashboard 运营视图 Phase 1` + docs 一并（精确清单）

---

## 完成后（executing-plans 流程）

- 终审（独立上下文 code review，Review Focus 逐项核对）→ Critical/Important 单一修复 pass（RED→GREEN）
- 合入生产 main（stash 生产 WIP → merge → stash pop → unstage）；push（BatchMode SSH）
- 重启 `inferfabric-proxy.service` + 生产冒烟（/health、/admin/model-pools、/admin/model-pools/runtime）
- 最终报告（Rulings + deferred minors + 验收对照）
