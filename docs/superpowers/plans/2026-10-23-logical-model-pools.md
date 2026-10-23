# 逻辑模型池（P1+P2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 IFF 沙箱内实现逻辑模型池（`iff/fast` 等稳定逻辑名 → 优先级候选映射，OpenAI/Anthropic 双协议过滤，热更新，dry-run，P2 受控故障回退），并产出沙箱评估报告。

**Architecture:** 新建独立 `inferfabric/model_pool/` 模块（config/evidence/registry/selector 四件，纯函数内核，不碰 handler/forwarder）；两个既有入口（`handle_chat`、`_handle_messages`）在鉴权后识别 `iff/` 前缀，解析池快照，把请求改写为已选真实目标后走既有转发路径。P2 通过给 `forwarder.py` 增加**纯增量可选参数**（默认值保持旧行为）实现"错误未提交给客户端"的候选回退；合入生产前需用户对 PR-14 边界单独批准。

**Tech Stack:** Python 3.10+ stdlib（yaml、dataclasses、filelock-free 原子替换）、pytest（假上游，无 GPU/网络）、Node VM（dashboard 行为测试，同 `tests/unit/dashboard/test_inference_switch_result.py` 模式）。

**Spec:** `docs/superpowers/specs/2026-10-23-logical-model-pools-design.md`

## Global Constraints

- 全程 sandbox（`/home/vince/inferfabric/sandbox`），不修改生产代码、不重启生产服务、不操作真实 GPU、不发起真实云端付费生成。
- 不修改 sleep/wake 业务；不升级 vLLM/依赖。
- `forwarder.py` 只允许**增量可选参数**（默认值 = 现行为），任何转发核心既有行为不得改变；此文件改动合入生产前需用户单独批准 PR-14 边界。
- 本地候选 **ready_only**：仅当 `service_name in pm.mgr.active_services` 时可选，**绝不调用 `pm.ensure_service` / 任何启动/切换逻辑**（用户 2026-10-23 明确确认）。
- 跨本地/云回退默认关闭：`allowed_locations` 显式列出才启用（用户确认留显式开关）。
- 池请求跳过响应缓存（GET 与 PUT 两侧都不走 `response_cache`）；直连模型缓存行为不变。
- 池请求鉴权使用**完整逻辑名**（如 `iff/fast`），不做 `split("/")[-1]`；guest 白名单不含逻辑名即拒绝（不自动扩权）。
- 配置文件：`IFF_DATA_DIR / "model_pools.yaml"`；文件缺失/损坏 = 功能关闭（请求 `iff/*` 返回 404 unknown_pool），代理进程不崩溃。
- 池命名空间 `iff/`；禁止池引用池；候选 ID/优先级唯一。
- 每请求恰好一个入站协议（由请求路径决定），不转换协议；无兼容候选返回明确错误，不静默降级。

## Review Focus

1. **guest 越权**：guest key 白名单无 `iff/fast` 时请求池必须 401/403，不能因短名回退匹配到其它模型 → Task 5 测试。
2. **配置损坏不炸代理**：`model_pools.yaml` 非法 YAML / 悬空目标时，启动与热加载均保留最后有效快照并记录问题，`iff/*` 请求得到明确错误而非 500/崩溃 → Task 3 测试。
3. **CAS 并发编辑**：`expected_revision` 过期的 PUT 返回 409 且不丢另一方的更新 → Task 3 测试。
4. **协议正确的错误信封**：OpenAI 入站无兼容候选时错误是 OpenAI 形状（`{"error": ...}`），Anthropic 入站是 Anthropic 形状（`{"type":"error","error":{...}}`），且 stream 请求的错误在 headers 提交前发出 → Task 5/6 测试。
5. **绝不自动启动本地模型**：池选择路径对未激活本地候选只跳过（排除原因 `local_not_ready`），断言 mock pm 的 `ensure_service` 零调用 → Task 5/6 测试。
6. **依赖指纹失效证据**：Provider base URL / 模型 ID 变化后旧证据转 stale，不参与选择 → Task 2 测试。

---

### Task 1: 池配置数据模型与静态校验

**Files:**
- Create: `inferfabric/model_pool/__init__.py`（导出公共名）
- Create: `inferfabric/model_pool/config.py`
- Test: `tests/unit/model_pool/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) CandidateConfig`: `id: str`, `priority: int`, `kind: str`（"local"|"cloud"）, `model_name: str`（local 的 served_name）, `provider: str`, `model_id: str`（cloud）, `declared_protocols: tuple[str, ...]`（默认 `()`）, `features: tuple[str, ...]`（可选 "tools"/"stream"/"vision"/"structured"，默认全空=仅纯文本）
  - `@dataclass(frozen=True) PoolConfig`: `name: str`, `enabled: bool = False`, `strategy: str = "priority_failover"`, `allowed_locations: tuple[str, ...] = ()`（空=不启用任何位置）, `accept_declared: bool = False`, `allow_cross_location: bool = False`, `max_attempts: int = 2`, `deadline_ms: int = 60000`, `candidates: tuple[CandidateConfig, ...] = ()`
  - `@dataclass(frozen=True) PoolsFile`: `schema_version: int`, `revision: int`, `pools: dict[str, PoolConfig]`
  - `load_pools_dict(raw: dict, current_revision: int) -> tuple[PoolsFile, list[str]]` — 解析+静态校验，返回（配置, 问题列表）；问题非空时调用方不得发布
  - `load_pools_file(path: Path) -> tuple[PoolsFile | None, list[str]]` — 文件缺失 → `(None, [])`；损坏 → `(None, [问题])`
- 校验规则（静态，不查运行时）：schema_version==1；pool 名不含 `/`；候选 id 在池内唯一；priority 在池内唯一；kind ∈ {local, cloud}；local 有 model_name、cloud 有 provider+model_id；候选目标名不得以 `iff/` 开头（禁池引用池）；`allow_cross_location=True` 时 `allowed_locations` 必须同时含 "local" 和 "cloud"。

- [ ] **Step 1: Write the failing tests**

覆盖：合法解析（默认值）、缺文件、损坏 YAML、schema_version≠1、重复 candidate id、重复 priority、pool 名含 `/`、local 缺 model_name、cloud 缺 provider、候选目标是 `iff/xxx`、空候选列表池默认 disabled、`allow_cross_location` 与 `allowed_locations` 矛盾。每条断言问题列表内容或解析结果字段。

- [ ] **Step 2: Run to verify they fail** — `python3 -m pytest tests/unit/model_pool/test_config.py -v`，预期 ModuleNotFoundError

- [ ] **Step 3: Implement config.py** — 纯 dataclass + yaml.safe_load + 逐条校验；无 I/O 副作用

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit** — `git add inferfabric/model_pool/ tests/unit/model_pool/test_config.py && git commit -m "feat(model-pool): 池配置数据模型与静态校验"`

---

### Task 2: 证据存储（协议/功能五态 + 依赖指纹）

**Files:**
- Create: `inferfabric/model_pool/evidence.py`
- Test: `tests/unit/model_pool/test_evidence.py`

**Interfaces:**
- Consumes: 无（独立）
- Produces:
  - `EvidenceState`（str Enum）：`UNKNOWN / DECLARED / PROBED / FAILED / STALE`
  - `@dataclass(frozen=True) Evidence`: `state: EvidenceState`, `protocol: str`, `features: tuple[str, ...]`, `fingerprint: str`, `recorded_at: str`（ISO8601）, `source: str`（"admin-probe"/"admin-declare"）
  - `endpoint_fingerprint(kind, provider_or_service, base_url, model_id) -> str`
  - `class EvidenceStore(path: Path)`：JSON 持久化（原子替换）；`get(key: str) -> Evidence | None`；`record(key, evidence)`；`invalidate_by_fingerprint(old_fp) -> int`（依赖变化时把匹配证据转 STALE）；`load()` 失败保留内存旧态
  - 键：`f"{kind}:{provider_or_service}:{model_id}:{protocol}"`
- 选择参与规则（供 Task 4 消费，本任务只存取）：PROBED 恒可参与；DECLARED 仅当池 `accept_declared=True`；UNKNOWN/FAILED/STALE 恒不参与。

- [ ] **Step 1: Write the failing tests** — record/get 往返、持久化重载、损坏 JSON 保留旧态、`invalidate_by_fingerprint` 只命中匹配指纹并把 PROBED→STALE、fingerprint 对 base_url 变化敏感。
- [ ] **Step 2: Verify fail** — 预期 ModuleNotFoundError
- [ ] **Step 3: Implement evidence.py**
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): 协议/功能证据存储与依赖指纹"`

---

### Task 3: Registry — 不可变快照、revision、CAS 热更新

**Files:**
- Create: `inferfabric/model_pool/registry.py`
- Test: `tests/unit/model_pool/test_registry.py`

**Interfaces:**
- Consumes: Task 1 的 `load_pools_dict/load_pools_file/PoolsFile`
- Produces:
  - `@dataclass(frozen=True) ResolvedCandidate`: `candidate: CandidateConfig`, `endpoint_fingerprint: str`, `evidence: dict[str, Evidence]`（protocol → evidence，来自 store 或 declared 推导）
  - `@dataclass(frozen=True) PoolSnapshot`: `revision: int`, `pool: PoolConfig`, `candidates: tuple[ResolvedCandidate, ...]`, `issues: tuple[str, ...]`
  - `@dataclass(frozen=True) RegistrySnapshot`: `revision: int`, `pools: dict[str, PoolSnapshot]`
  - `class PoolRegistry(config_path: Path, evidence_path: Path)`：
    - `load() -> None` — 启动加载；失败保留空快照（revision 0，pools={}）并记 log
    - `snapshot() -> RegistrySnapshot` — 返回当前不可变快照（线程安全：内部 lock + 整体替换引用）
    - `update(raw_dict: dict, expected_revision: int) -> RegistrySnapshot` — 静态校验失败 → `ValueError(issues)`；revision 不匹配 → `RevisionConflict`（自定义异常）；成功 → 临时文件+`os.replace` 原子写盘、revision+1、发布新快照
    - `reload_from_disk() -> bool` — 供 SIGHUP/`/reload-config` 复用
  - 候选解析：local 目标在当前 models.d 中不存在 → 该候选进 `issues` 但不进 candidates；cloud provider 不在已配置 provider 列表 → 同上（校验器注入 `known_local: set[str]`, `known_providers: set[str]`，registry 构造时由调用方传入 getter）

- [ ] **Step 1: Write the failing tests** — 加载→快照、CAS 成功、CAS 409（RevisionConflict）且磁盘未变、非法新配置 ValueError 且旧快照保留、原子性（写中途不存在半文件，用 tmp_path 模拟）、悬空目标进 issues、evidence 由 store 注入 ResolvedCandidate、reload_from_disk 拾取外部修改。
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement registry.py**
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): registry 快照/CAS 热更新"`

---

### Task 4: Selector — 协议过滤 + 优先级排序 + dry-run 解释

**Files:**
- Create: `inferfabric/model_pool/selector.py`
- Test: `tests/unit/model_pool/test_selector.py`

**Interfaces:**
- Consumes: Task 3 的 `PoolSnapshot/ResolvedCandidate`，Task 2 参与规则
- Produces:
  - `@dataclass(frozen=True) RequestRequirements`: `protocol: str`（"openai"|"anthropic"）, `stream: bool`, `tools: bool`, `vision: bool`, `structured: bool`
  - `@dataclass(frozen=True) CandidateDecision`: `candidate_id: str`, `included: bool`, `reason: str`（included 时为 "ok"；排除时为 `disabled_pool/location_not_allowed/no_evidence/feature_unsupported/local_not_ready/cloud_model_missing` 之一）
  - `@dataclass(frozen=True) Selection`: `pool: str`, `decisions: tuple[CandidateDecision, ...]`, `chosen: ResolvedCandidate | None`, `error: str | None`（`pool_disabled/no_ready_target/no_compatible_target/unknown_pool` 之一，chosen 非空时为 None）
  - `select(pool_snap: PoolSnapshot, req: RequestRequirements, runtime: RuntimeView) -> Selection`
  - `@dataclass RuntimeView`: `active_services: set[str]`, `cloud_models: dict[str, object]`（键 `provider/model_id`，来自 pm.cloud.cloud_models）
- 规则（按序）：池 disabled → error=pool_disabled；按 priority 升序遍历；位置过滤（`allowed_locations` 含 "local"/"cloud"；候选位置不在列表 → location_not_allowed）；证据过滤（协议无 PROBED、或 DECLARED 但池未 accept_declared → no_evidence）；功能过滤（请求要求 features ⊄ 候选 features → feature_unsupported）；本地就绪（kind=local 且 service 不在 active_services → local_not_ready）；云存在（kind=cloud 且 `f"{provider}/{model_id}"` 不在 cloud_models → cloud_model_missing）。第一个全过的即 chosen；无 chosen → error=no_compatible_target（有候选被本地就绪排除时）或 no_ready_target。
  - 确定性：遍历顺序严格按 (priority)，无随机。

- [ ] **Step 1: Write the failing tests** — 双协议各选对端点、优先级顺序、协议不兼容排除、DECLARED 需 accept_declared、STALE 不参与、vision 请求排除无 vision 候选、local 未激活排除且不报错、cloud 未发现排除、全排除时 error 分类、dry-run 场景 decisions 完整可解释。
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement selector.py**
- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): 协议过滤/优先级选择/dry-run 解释内核"`

---

### Task 5: OpenAI 入口接入（`handle_chat`）

**Files:**
- Create: `inferfabric/model_pool/dispatch.py`
- Modify: `inferfabric/proxy/chat_handlers.py:195-268`（handle_chat 开头插入池分支）
- Modify: `inferfabric/proxy_manager.py:57-130`（构造 `self.pool_registry`，文件缺失时不 crash；`known_local`/`known_providers` getter 接 models.d 与 cloud.providers）
- Test: `tests/unit/model_pool/test_dispatch_openai.py`

**Interfaces:**
- Consumes: Task 3 `PoolRegistry.snapshot()`、Task 4 `select()`
- Produces:
  - `handle_pool_chat(handler, pm, data, model: str) -> bool` — 返回 True 表示已完整处理（含错误响应已发），False 表示不是池请求（前缀不是 `iff/`），调用方继续原路径
  - 流程：`iff/` 前缀检查 → `pm.auth.check(auth_header, model)`（**完整名，不 strip**）→ 取 snapshot → `select()` → 无 chosen → 按错误契约 `_send_json`（unknown_pool 404 / pool_disabled、no_ready_target 503+Retry-After / no_compatible_target 400，信封 `{"error": msg, "status": iff_code}`）→ chosen 为 local：**不调 ensure_service**，`data["model"] = served_name`，复用 `handle_chat` 的本地转发段（提取为可复用内部函数或直接内联调用 `_forward_request` 一次，rate gate 用逻辑名 `iff/fast` 作为 dual_gate 键）→ chosen 为 cloud：查 `pm.cloud.get_provider_config(provider)` 与 `cloud_models`，`data["model"] = model_id`，调 `forwarder.forward_to_cloud(..., protocol="openai", original_model=model)` 并记 RequestLog（route=`pool:{pool}/cloud:{provider}`）。
  - 池请求**不查/不写 response_cache**（跳过 Task 5 修改点之前的缓存段——池分支放在缓存段之前）。
  - RequestLog 的 model 字段记逻辑名 `iff/fast`，cloud_provider/usage 照常。

- [ ] **Step 1: Write the failing tests**（fake pm：内存 registry + mock forwarder，不打网络）
  - `iff/fast` 正常解析到 local 激活候选并转发（断言 data["model"] 被改写、`ensure_service` **零调用**）
  - local 候选未激活 → 跳到下一 cloud 候选
  - 无兼容候选 → 400 no_compatible_target，OpenAI 错误形状
  - guest key 白名单无 `iff/fast` → 401（**不因 strip 短名通过**）
  - 非 `iff/` 前缀 → 返回 False，原路径不受影响
  - 未配置池（registry 空）→ 404 unknown_pool
  - 池请求不触碰 response_cache（mock 断言 get/put 零调用）
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement dispatch.py + 接线**（handle_chat 开头：`if model.startswith("iff/") and handle_pool_chat(...): return`，放在 auth 之后缓存之前——鉴权在池分支内部做，原 auth 段对非池请求保持不变）
- [ ] **Step 4: Verify pass + 既有 proxy 测试全绿** — `python3 -m pytest tests/unit/proxy/ tests/unit/model_pool/ -v`
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): OpenAI 入口 iff/ 池调度（ready_only、缓存旁路、完整名鉴权）"`

---

### Task 6: Anthropic 入口接入（`_handle_messages`）

**Files:**
- Modify: `inferfabric/model_pool/dispatch.py`（新增 `handle_pool_messages(handler, pm, data, model) -> bool`）
- Modify: `inferfabric/proxy/handler.py:431-456`（`_handle_messages` 在 auth 段前插入池分支）
- Test: `tests/unit/model_pool/test_dispatch_anthropic.py`

**Interfaces:**
- Consumes: Task 5 同款结构；Anthropic 转发用既有 `forwarder.forward_anthropic_local(self, pm, data, auth_header, model_obj, original_model)`（local）与 `forward_to_cloud(..., protocol="anthropic")`（cloud）
- Produces: `handle_pool_messages(...) -> bool` — 语义同 Task 5；错误信封用 Anthropic 形状 `{"type":"error","error":{"type":"...","message":...}}`（404→`not_found_error`、400→`invalid_request_error`、503→`overloaded_error` + Retry-After header）；SWITCHING guard 对池请求不适用（池不切换本地模型，跳过 503 switching 段）

- [ ] **Step 1: Write the failing tests** — 同 Task 5 的镜像断言 + Anthropic 错误形状 + SWITCHING 状态下池请求仍正常（选择只看 ready 候选）
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement**（`_handle_messages` 在 `original_model` 解析后立即插池分支，位于既有 auth/缓存/SWITCHING 段之前）
- [ ] **Step 4: Verify pass + `tests/unit/proxy/` 全绿**
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): Anthropic 入口 iff/ 池调度"`

---

### Task 7: 管理接口（GET/PUT/validate/status/dry-run）

**Files:**
- Create: `inferfabric/model_pool/admin.py`
- Modify: `inferfabric/proxy/handler.py:215-256`（路由表注册，走 `_admin` 装饰器）
- Test: `tests/unit/model_pool/test_admin.py`

**Interfaces:**
- Consumes: Task 3 `PoolRegistry`，Task 4 `select()` + `RequestRequirements`
- Produces（handler 方法放 admin.py，handler.py 只注册）：
  - `GET /admin/model-pools` → `{revision, pools: {name: {enabled, strategy, allowed_locations, accept_declared, candidates: [{id, priority, kind, target, per-protocol evidence state}]}}}`
  - `PUT /admin/model-pools`（body: 完整配置 dict + `expected_revision`）→ 成功 `{revision}`；revision 不符 → 409 `{"error": "revision conflict", "current_revision": N}`；校验失败 → 400 + issues
  - `POST /admin/model-pools/validate`（body: 配置 dict）→ `{valid, issues}`；**不发探测、不出网**
  - `GET /admin/model-pools/status` → 每池当前选择视图（OpenAI/Anthropic 各自的有效候选与排除原因，用 dry-run 逻辑对两协议各跑一次）
  - `POST /admin/model-pools/dry-run`（body: `{pool, protocol, stream, tools, vision, structured}`）→ `{decisions, chosen, error}`；零副作用
  - 真实探测（PROBED 证据）本期**不实现自动执行**：evidence store 支持 admin 手动注入接口仅供测试；管理面板显示 declared/probed 区分。运行时探测需管理员显式操作+出网授权，推迟到独立任务（spec §5.1 允许）。

- [ ] **Step 1: Write the failing tests** — GET 快照、PUT 成功递增 revision、PUT 409、PUT 400 issues、validate 零出网（mock 断言无网络调用）、dry-run 双协议解释、status 汇总。
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement admin.py + 注册路由**
- [ ] **Step 4: Verify pass**（含 async shim：`async_server` 转发未知 admin 路径的既有机制确认——admin 路由经 handler 路由表分发，`_handle_forward` 覆盖；跑一个 aiohttp TestServer 冒烟确认 `/admin/model-pools` 200）
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): 管理接口 GET/PUT/validate/status/dry-run"`

---

### Task 8: P2 — 受控优先级故障回退（预算内、提交后不重放）

**Files:**
- Create: `inferfabric/model_pool/pool_forward.py`
- Modify: `inferfabric/forwarder.py:201`（`forward_to_cloud` 增加可选参 `suppress_client_errors: bool = False`，True 时不向客户端发错误响应、不内层重试，把失败装进返回的 `CloudResult.committed=False`；默认 False 时逐字节保持现行为）
- Modify: `inferfabric/model_pool/dispatch.py`（chosen 单发改为 attempt 循环）
- Test: `tests/unit/model_pool/test_pool_forward.py`

**Interfaces:**
- Consumes: `PoolSnapshot.max_attempts/deadline_ms`、`_forward_request`（OpenAI local，单次语义：headers 未发送返回 False）、改参后的 `forward_to_cloud`
- Produces:
  - `run_attempts(pool_snap, req, chosen_iter, transport_openai, transport_anthropic, deadline) -> AttemptOutcome` — 按选择序（已含排除）依次尝试，预算 = min(max_attempts, 剩余 deadline)；每次真实上游调用计 1 attempt
  - 错误动作表（spec §6.2 固定表）：连接失败/未提交 5xx → 下一候选；429 → 尊重 Retry-After 但受 deadline 约束，超预算切候选；上游 401/403 → 该候选端点冷却（内存 map，键=endpoint_fingerprint）+ 下一候选；404/400/422/内容安全/已提交任何字节 → **终止**，按入站协议返回既有错误；**响应已提交（headers 或任何 body 已发）→ 永不重试/切换**
  - `class CooldownStore`：内存 dict + 过期时间；`is_cooling(fp)`；selector 集成（冷却中的候选排除，reason=`cooldown`）
- Anthropic local 路径 P2 同样通过 `forward_anthropic_local` 增加同名可选参（默认现行为）获得"未提交失败"信号。

- [ ] **Step 1: Write the failing tests** — 主候选连接失败→备用成功且总 POST 数=2、预算耗尽不再尝试、429 等待受 deadline、401 触发冷却且后续请求跳过该候选、已提交后上游中断→不切换（断言第二候选零调用）、冷却过期恢复、`suppress_client_errors=False` 默认路径旧行为不变（既有 forwarder 测试全绿即证）。
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement**（forwarder 改动保持纯增量；pool_forward 不 import handler 内部状态，transport 以回调注入）
- [ ] **Step 4: Verify pass + `tests/unit/ tests/integration/` 全绿**（forwarder 既有测试证明默认行为未变）
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): P2 优先级故障回退（统一预算/提交后不重放/端点冷却）"`

---

### Task 9: Dashboard 逻辑模型池面板（只读 + 开关）

**Files:**
- Create: `inferfabric/dashboard/fragments/model-pools.html`（侧栏新分区）
- Create: `inferfabric/dashboard/js/model-pools.js`
- Modify: dashboard 既有侧栏/页签注册文件（按 `dashboard/` 现状定位）
- Test: `tests/unit/dashboard/test_model_pools_panel.py`（Node VM 模式，同 inference.js 测试法）

**Interfaces:**
- Consumes: Task 7 的 `GET /admin/model-pools` 与 `GET /admin/model-pools/status`
- Produces: 只读面板：每池显示 enabled、revision、OpenAI/Anthropic 有效候选及各候选排除原因；每池一个启用/禁用开关（走 PUT，读-改-写 expected_revision，409 时提示刷新重试）。**不做**候选编辑器（YAML/PUT API 编辑，面板后续迭代）。

- [ ] **Step 1: Write the failing tests** — 渲染池列表、双协议候选解释展示、开关 409 冲突提示、开关成功刷新。
- [ ] **Step 2: Verify fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Verify pass + `node --check`**
- [ ] **Step 5: Commit** — `git commit -m "feat(dashboard): 逻辑模型池只读面板与启用开关"`

---

### Task 10: `/v1/models` 列出启用逻辑名

**Files:**
- Modify: `inferfabric/proxy/handler.py:716-787`（`_handle_v1_models`）
- Test: `tests/unit/model_pool/test_v1_models.py`

**Interfaces:**
- Consumes: Task 3 snapshot
- Produces: 启用且有候选的池以 `id: "iff/fast", owned_by: "iff-pool"` 出现在 `/v1/models`；**不**合并候选能力；未配置池不出现；直连模型条目不变。

- [ ] **Step 1: Write the failing test** — 有池时列表含逻辑名、registry 空时列表与现状完全一致。
- [ ] **Step 2: Verify fail → Step 3: Implement → Step 4: Verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(model-pool): /v1/models 列出启用逻辑名"`

---

### Task 11: 沙箱评估报告 + 全量回归收口

**Files:**
- Create: `specs/model-pools/spec.md` + `specs/model-pools/STATUS.md`
- Create: `docs/reviews/2026-10-23-model-pools-evaluation.md`

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 全量回归** — `python3 -m pytest tests/unit/ tests/integration/ -v --tb=short`，记录通过数；`python3 -c "import inferfabric"` 冒烟。
- [ ] **Step 2: 隔离 HTTP 冒烟** — aiohttp TestServer + 假 lifecycle/假上游：`/v1/chat/completions`（OpenAI 假上游）与 `/v1/messages`（Anthropic 假上游）各请求 `iff/fast` → 200 且来自选定候选；无候选 → 正确错误信封；`/admin/model-pools` 全链路 GET/PUT/dry-run。
- [ ] **Step 3: 撰写评估报告** — 内容：功能覆盖 vs spec 验收清单逐条对照（§14 核心验收 1/2/3/5/6/7/8/9/10/11 中 P1+P2 范围内各项的通过证据与留白项）、已知限制（P3 会话/上下文估算/真实探测/预算硬顶未做）、配置示例、生产合入需单独批准事项（forwarder.py 增量参数的 PR-14 边界）、部署步骤草案（不执行）。
- [ ] **Step 4: 独立代码评审** — dispatching code reviewer（只审 model_pool/ 新文件 + chat_handlers/handler/forwarder 的 diff），修复 Critical/Important。
- [ ] **Step 5: Commit** — `git commit -m "docs(model-pool): 沙箱评估报告与验收对照"`

---

## Self-Review 结论

- **Spec 覆盖**：§2-6/8-13 的 P1+P2 范围逐条有任务；P3（会话粘性）、P4（协议转换）、真实探测执行、预算硬顶为显式留白，写入评估报告。
- **类型一致**：CandidateConfig/PoolConfig/PoolSnapshot/ResolvedCandidate/RequestRequirements/Selection 跨任务签名已核对。
- **Review Focus**：6 项均有归属任务测试。
- **比例**：spec 300+ 行，计划约同量级，无实现转写。
