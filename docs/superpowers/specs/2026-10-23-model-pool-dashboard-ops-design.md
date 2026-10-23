# IFF 模型池 Dashboard 运营视图设计（Phase 1）

日期：2026-10-23
状态：用户已确认方向与四项关键决策，授权全自动完成（沙箱实施 → 门禁 → 合入 → 部署）。
基础：`docs/superpowers/specs/2026-10-23-logical-model-pools-design.md`（已实现并合入 v7.0.0）。

## 1. 目标与动机

用户场景链条：多个 agent 产品各自配模型太麻烦 → 固定角色（`iff/fast` 等逻辑名）让 agent 只配一次；
模型经常变更、性能劣化 → 需要在 Dashboard 里**按角色快速换底层模型**，所有 agent 零改动跟随。

底座（池调度、CAS 管理 API、热加载、冷却/隔离/半开探测）已在 v7.0.0 完成。本设计补齐**运营层**：
让「换一个模型」成为 < 30 秒、零 YAML、零重启、agent 零改动的面板操作。

### 成功标准（验收场景）

| # | 场景 | 验收 |
|---|---|---|
| S1 | 首次接入 agent 产品 | 面板复制接入片段 → 粘贴进 agent 配置，1 分钟内完成 |
| S2 | 换模型（最高频） | 选中角色 → 替换候选 → 预览 → 应用，< 30 秒，零 YAML/零重启/agent 零改动 |
| S4 | 故障实时可见 | 冷却剩余时间/隔离到期/最近失败原因/半开探测态在面板实时可见 |
| S5 | 改错能回来 | validate+dry-run 拦截错误配置；CAS 409 冲突可见；会话级撤销可回退本次修改 |

S3（性能劣化发现）与失败时间线、持久化历史回滚属 Phase 2；劣化自动提示、重新探测属 Phase 3。

## 2. 用户已确认的四项决策

1. **角色模板**：`models_pools.yaml` 不存在（registry 空快照 revision=0）时，面板提供
   fast / premium / small / classifier 四角色引导式初始化；用户从候选目录中为每角色挑选候选，
   一次 CAS POST（`expected_revision: 0`）生成完整配置。模板只是**预填建议**，不是硬编码——
   四个角色名/槽位语义允许用户改名、增删（沿用 v7.0.0 "不写死只能四个" 的原则）。
2. **延迟迷你图内嵌候选行**（Phase 2 实现数据，Phase 1 预留行内槽位）。
3. **配置历史/回滚归 Phase 2**；Phase 1 提供会话级撤销：编辑器保留本次应用前的完整定义，
   「撤销」= 用**最新 revision** 重新 POST 旧内容（CAS 语义正确：撤销内容，不撤销版本号）。
4. **一键换模 = 直接替换候选**：新候选占原优先级位，旧候选从链上移除（不保留兜底）。
   用户已知悉失去兜底的代价并明确选择此语义。

## 3. 界面设计

### 3.1 布局三层次

```
① 角色卡片区（每池一卡）
   角色名 · 健康灯 · 双协议徽标 · 每协议当前实际选中物理模型 · enabled 开关
② 候选链（选中角色展开，优先级序，运行态实时）
   每候选行: 本地/云徽标 · 协议证据 · 运行态(服务中/未启动/冷却 mm:ss/隔离/探测中)
             · 最近失败(状态码+时长) · [替换][上移/下移][禁用] · (P2: 延迟迷你图槽位)
③ 操作区
   [编辑候选] [接入片段📋] [撤销本次修改]  — 编辑器三段式模态
```

**健康灯语义**：🟢 首选候选服务中；🟡 回退链工作中（首选被冷却/隔离，后续候选接管）；
🔴 全候选不可用（请求将收到 `pool_unavailable`）。灯是**每协议**的（池对 OpenAI 绿、对
Anthropic 红是合法状态），卡片上显示主导色 + hover 展开每协议明细。

### 3.2 编辑器三段式（复用既有后端 API，零新增写路径）

1. **编辑**：候选行内增删改（kind/model/协议声明/features）、拖拽或按钮调序、池参数
   （max_attempts/deadline_ms/allowed_locations）。候选选择器数据源复用现有端点：
   本地 = models.d 全量 + 启动状态；云端 = providers + cloud_models。
2. **预览**：`POST /admin/model-pools/validate`（静态校验问题列表）+
   `POST /admin/model-pools/dry-run`（携带编辑中 `config`，OpenAI 与 Anthropic 两个协议
   各跑一次，展示每候选被选/被排除及原因——预览的是**编辑后**配置，见 §4.4）。
   校验问题非空或两协议均无可用候选时**阻止应用**（MVP：issues 非空一律阻止，
   与 registry 解析语义一致）。
3. **应用**：`POST /admin/model-pools`（CAS，`expected_revision` = 编辑开始时的 GET revision）。
   409 → 展示冲突（他方已修改，当前 revision 是 N'）→ 提供「重新加载并重做」；不提供
   自动覆盖。成功 → 保留改前定义为会话撤销快照。

**替换候选（决策 4）**：编辑器中每候选行的「替换」打开候选选择器 → 选定后**原位替换**
（继承 priority），旧候选移除。卡片上不设独立的"一键换模"大按钮——替换入口就在候选行上，
避免同一语义两个入口。

### 3.3 角色模板初始化（空态向导）

触发条件：`GET /admin/model-pools` 返回 `pools` 为空 **且** 数据目录无 `model_pools.yaml`
（区分「未配置」与「配置了零个池」：后者显示普通空态文案，不出向导）。
区分依据：GET 响应新增字段 `configured: bool`（registry 知道文件是否存在）。

向导：四张角色卡（fast=快速小模型 / premium=最强模型 / small=最省 / classifier=便宜分类），
每张卡内从候选目录选 1–N 个候选（默认 1 个，可加兜底）；用户可删掉不要的角色。
提交 = 一次 CAS POST `expected_revision: 0`。模板建议规则（仅预填，可改）：
fast→本地已启动的小参数模型；premium→本地大模型或云旗舰；small→最小本地模型；
classifier→任一便宜快速候选。

### 3.4 接入片段

每池「接入片段」按钮生成两份可复制文本（OpenAI / Anthropic）：

```
# OpenAI 兼容客户端
base_url: http://<主机>:8999/v1
model: iff/fast
api_key: <IFF API key>

# Anthropic 客户端
base_url: http://<主机>:8999
model: iff/fast
api_key: <IFF API key>   # header: x-api-key
```

主机名取 `location.hostname`（Dashboard 同源）。警告 /v1 重复拼接问题
（沿用池设计文档 §2 的注意事项文案）。

**密钥提示（builder check #3 裁决）**：片段密钥为占位符，附一行提示——
「若 API key 配置了 models ACL，需包含所用 `iff/<pool>` 名，否则 401」
（`proxy/auth.py` 支持每 key 模型白名单，受限于物理模型名的 key 会被
`iff/` 逻辑名拒绝）。一期不做密钥 ACL introspection：单用户工作站场景
提示文案足够自查，避免池面板耦合 auth 面。

## 4. 后端新增（N1 —— 本设计唯一的新后端面）

### 4.1 `GET /admin/model-pools/runtime`

只读。返回每池每候选的**运行态**，供候选链实时视图（前端 5s 轮询，与 status 轮询合并节奏）：

```json
{"revision": 7,
 "pools": {"fast": {"candidates": [
   {"id": "qwen-small", "kind": "local", "target": "qwen-small",
    "fingerprint": "local:qwen-small:8007",
    "active": true,
    "cooling": {"remaining_s": 42.0, "reason": "cooldown_401", "probing": false},
    "last_attempt": {"ok": false, "status": 503, "age_s": 187.0}}]}}}
```

- `cooling` 为 null 表示不在冷却/隔离；`probing: true` 表示半开探测在途；
  `remaining_s <= 0` 且条目仍在 = 已到期待探测。
- `active` 语义按 kind（builder check minor 裁决）：**本地候选 = 在
  `active_services`（已启动）**；**云候选 = 在 `cloud_models`（已发现）**。
  不引入第三个"当前可选"字段——选择结果由 status 端点承载，runtime 只报目标可用性。
- `last_attempt` 为 null 表示进程内尚无该端点的尝试记录（重启后为空是预期）。
- `fingerprint` 来自 selector 对候选的既有解析（`endpoint_fingerprint`），保证与调度期
  排除用的是同一身份。

### 4.2 CooldownStore 扩展（additive，不改既有语义）

1. **原因记录**：`add()` 增加可选 `reason` 参数；`quarantine()` 传
   `reason="quarantine_404"`；401/403 冷却传 `reason="cooldown_401"/"cooldown_403"`。
   `finish_probe` 失败重冷却保留原 reason。默认 reason 为 None（既有调用零变化）。
2. **只读视图**：新方法 `entries(now=None) -> dict[fingerprint, {"until", "remaining_s",
   "reason", "probing"}]`——含已到期未探测条目（半开语义要求条目保留），全部在锁内快照。
3. **最近尝试记录**：新方法 `record_attempt(fingerprint, ok, status=None, now=None)`
   （每 fingerprint 只存最近一次：`(ok, status, mono)`）；`run_attempts` 在每次传输回调
   返回后调用（`cooldowns is not None` 时）。429/5xx/连接失败虽不写冷却，但会出现在
   last_attempt——S4 的"最近失败原因"由它承接。

### 4.3 registry 快照 `configured` 字段与 `raw` 配置基底

`RegistrySnapshot` 暴露 `configured: bool`（上次 reload 是否读到真实文件）；
registry 另保留 `last_load_error: str | None`（最近一次加载失败原因，加载成功清空）。
`GET /admin/model-pools` 响应增加：

- `configured: bool` + `load_error: str | null` — **三态**（builder check #4 裁决）：
  - `configured=true`：文件存在且解析成功（含「配置了零个池」→ 普通空态文案）；
  - `configured=false, load_error=null`：从未配置 → **出初始化向导**；
  - `configured=false, load_error≠null`：文件损坏/不可读（registry 保留最后有效快照）→
    显示错误横幅 + 错误内容 + 「重载配置」按钮，**绝不出向导**——坏文件时 revision
    停在快照值（或 0），向导的 `expected_revision: 0` CAS POST 会静默覆盖坏文件。
- `raw: dict | null` — 服务端 `_raw`（最后成功加载/写入的原始配置）的深拷贝。

**为什么必须有 raw**：GET 视图是有损的（悬空候选被剔除、`declared_protocols` 等字段
不回传）——toggle 端点的既有注释明确警告「经视图重建配置会丢数据」。编辑器是全量
读-改-写（复用 CAS POST），必须以 raw 为基底，否则一次编辑就会丢失悬空候选和
未回传字段。`raw` 为 null（未配置）时编辑器以空骨架 `{"schema_version":1,"pools":{}}`
为基底——与初始化向导是同一条路径。

### 4.4 dry-run 支持「编辑中」配置

既有 `POST /admin/model-pools/dry-run` 只对**当前已发布快照**跑选择——编辑器预览的
却是**编辑后**配置。扩展：body 增加可选 `config` 键（完整池配置 dict，形如
`{"schema_version":1,"pools":{...}}`）；提供时先 `load_pools_dict` 静态校验（校验失败
→ 400 + issues，不落盘），再对 `config.pools[pool]` 跑 select；**不提供时行为逐字节
不变**（向后兼容，既有调用方/测试零影响）。编辑器预览阶段 = `validate(config)` +
`dry-run(config, openai)` + `dry-run(config, anthropic)` 三连，全部零副作用。

### 4.5 不做的事

- 不新增写路径（编辑器完全复用 validate/dry-run/CAS POST/toggle；§4.3/§4.4 是**读路径**扩展）。
- 不自动启动本地模型（候选行「未启动」只显示状态 + 跳推理页的链接，**不在池面板放启动按钮**——
  比 §3.1 草案更保守：启动操作留在推理页单一入口，避免双入口状态竞争）。
- 不做自动出网探测、不做延迟聚合（P2）、不做持久化历史（P2）。
- 不修改转发核心路径（PR-14）：改动面 = `model_pool/pool_forward.py`（CooldownStore 只读扩展）、
  `model_pool/dispatch.py`（record_attempt 一行）、`model_pool/admin.py`（runtime GET handler +
  raw/configured/dry-run 扩展）、`model_pool/registry.py`（configured + raw 访问器）、
  `proxy/handler.py`（一行路由注册）、`dashboard/`（前端）。

## 5. 前端结构（与既有 dashboard 约定一致）

- 文件：`dashboard/js/model-pools.js`（重构扩展）+ `dashboard/fragments/model-pools.html`（扩展现有单卡）。
- 约定：无框架原生 JS、事件委托、无 inline onclick、暗色优先、与既有 if-card 体系同视觉。
- 轮询：`GET /admin/model-pools`（定义+revision）+ `GET /admin/model-pools/status`（每协议选择）
  + `GET /admin/model-pools/runtime`（运行态）合并为一次刷新函数，5s 间隔，页面隐藏时暂停
  （沿用既有 TAB 可见性处理）。CAS 冲突后强制刷新。
- 编辑器模态复用推理页「⚙ 场景」三阶段模态的交互范式（选 → 预览 → 应用），视觉一致。

## 6. 验收与门禁

- 全量 pytest 绿（含新增：CooldownStore reason/entries/record_attempt 单测、runtime 端点
  handler 测试、configured 字段测试、dry-run 阻止逻辑为前端行为——后端已覆盖）。
- 前端无 JS 语法错误（node --check）；隔离 HTTP 冒烟（假 pm）走通三段式与 runtime 轮询。
- 交叉评审（独立上下文）+ 单一修复 pass。
- 版本 7.0.0 → **7.1.0**（用户授权 phase 小版本）。
- 部署：合入生产 main → push → 重启 `inferfabric-proxy.service` → 冒烟
  （`/health`、`/admin/model-pools`、`/admin/model-pools/runtime`）。

## 7. Phase 2 / 3 概要（本设计只锁定接口不实现）

- **Phase 2（7.2.0）**：候选行内嵌延迟迷你图（RequestLog 按物理模型聚合，需小后端聚合参数）；
  失败时间线（attempt 级记录聚合）；配置历史/回滚（registry 内存 ring 保留最近 N 份快照 + 恢复端点）。
- **Phase 3（7.3.0）**：延迟阈值劣化提示（建议换序）；重新探测端点（spec §5.1 出网授权流程）。
