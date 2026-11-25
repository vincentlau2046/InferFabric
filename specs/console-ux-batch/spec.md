# 控制台易用性批次：向导迁移 / 组级回退开关 / key 重看与硬删 / 设置页

状态：2026-11-24 用户提出需求并逐条裁决（本文件即裁决落盘）；PR1 已实施（#43，评审 2 important + 4 nit 修入同分支），PR2 已实施（组级回退开关），PR3–PR4 未开始。

## 0. 背景与裁决

需求来自 Mac 端使用反馈（2026-11-24 会话），四项裁决全部采纳推荐方案：

| # | 需求 | 裁决 | 推翻的既有裁决（实施时在对应文档记录） |
|---|---|---|---|
| 1 | 部署向导不应放模型池页 | 迁至**能力包页** | 2026-11-19「向导挂模型池 tab」（docs/superpowers/plans/2026-11-21-deploy-wizard.md） |
| 2 | 组内模型路由开关 | **组级回退开关**（first_only / priority_failover，默认开） | 无（新语义；现有"启用"整池开关保持不变） |
| 3a | 重新查看 api-key | **方案 A：加密入库**（secrets.env 服务端密钥） | pr23「没有 reveal 接口 / 明文仅创建时展示一次」（specs/pr23-api-key-security/STATUS.md:76） |
| 3b | 已吊销 key 不留痕 | **吊销即硬删** | 软删留痕现状（key_store.py:104-109） |
| 4 | 设置按钮 + 密码门控 + 监控入总览组 | 新增设置页；重看 key 逐次验密；密码默认空=不可重看 | 无（控制台密码 CLI 已存在，仅补 UI） |

## 1. WS1 · 部署向导迁至能力包页

- 现状：五步向导（能力→框架→部署方式→ModelScope→参数）入口按钮与向导卡物理嵌在模型池 tab（`model-pools.html:7` 按钮、`:19` 占位符、`__init__.py:34-38` 拼装、`wizard.js:419-434` tab 守卫绑 `tab-model-pools`）。
- 改动：占位符迁至 `packs.html`（"扩展·部署"区旁，成为主安装入口）；`wiz-open` 按钮迁至能力包卡头；`__init__.py` 拼装注释与顺序同步；`wizard.js` 的 Escape/切页/轮询守卫从 `tab-model-pools` 改 `tab-packs`；`wizard.js:379-386` packs-catalog 深链逻辑保持自洽。
- 测试钉更新：`tests/unit/test_dashboard_wizard.py:26,31-32`。
- 入口层级（2026-11-24 UX 审视 ISSUE-03）：向导为能力包页**主装载入口**（卡头主按钮）；「扩展·部署」legacy 表单收进「高级」折叠并标注旧版，避免三入口（向导/装包/legacy 表单）并列无主次。
- 约束：IA v3 不加新 tab；`data-tab` 属性保留；fragments 拼装机制与 `get_html()` 不动。

## 2. WS2 · 组级回退路由开关

- 语义：`strategy: priority_failover`（**默认** = 现行为：按优先级候选链回退，7.11.3 预算语义）/ `first_only`（关闭回退：仅尝试 P1 候选，P1 失败/不可用即池错误，不向下路由）。
- 改动面：
  - `model_pool/config.py`：`_parse_pool` strict 枚举解析（沿用 enabled/accept_declared 的 strict-bool 风格，禁止字符串强转）；缺省 = `priority_failover`。
  - `model_pool/selector.py`：`first_only` 时候选链截断为首个就绪 P1（readiness/cooldown 过滤仍生效——被过滤不算回退）。
  - `model_pool/registry.py` + `admin.py`：CAS 原子翻转（仿 `flip_pool_enabled` / `POST /admin/model-pools/toggle`），带 expected_revision。
  - `dashboard/js/model-pools.js`：池卡在「启用」checkbox 旁加「回退路由」开关，乐观 UI + 409 冲突处理同既有 toggle。
- 可读性（UX 审视 ISSUE-02）：`first_only` 且 P1 不可用时的失败原因必须显式化——API reason / degrade banner / fault timeline 明示「回退路由已关闭，P1 不可用」，与 `no_ready_target`（回退开但仍无就绪候选）可区分；关闭开关时 UI 附一句后果提示（「关闭后仅使用第一优先级候选，其故障时该组不可用」）。
- 边界（不变项）：池级逻辑门（`fabric-` 名义 RPM/并发闸）打满仍直接 429，不属回退；跨池无 failover；直连（非 fabric-）路径不动；per-candidate 候选级禁用不在本期（裁决未选）。

## 3. WS3 · key 重看（加密入库）+ 吊销即硬删

- 存储：`security.db` api_keys 表加 `token_enc` 列；加密密钥放 `~/.inferfabric/secrets.env`（600，不入库），DB 单独拖库不可还原明文。创建流程同事务写入 `sha256` + `token_enc`。
- 重看：`POST /admin/keys/{id}/reveal`，要求 ①控制台会话（既有 console session + CSRF + Origin 白名单语义）②**逐次密码验证**（请求体带密码，与 settings 表口令哈希比对）；成功返回明文并写 AnomalyEvent（key_revealed 审计）。密码未设置（默认空）→ 端点拒绝 + UI 按钮禁用提示「先在设置中配置密码」。
- 存量旧 key：无 token_enc → 永不可重看，列表标记「旧版创建，请轮换」。
- 轮换（UX 审视 ISSUE-01，本期必做）：key 行新增「轮换」操作 = 吊销旧（硬删）→ **同名**新建 → 一次性展示新明文。这是存量旧 key 唯一的换发动线（旧 key 无法重看），也是常规换 key 的快捷路径；硬删释放 UNIQUE 名字正好支撑同名轮换。
- 吊销：`POST /admin/keys/{id}/revoke` 改为**同事务失效 + 删行**；列表不再出现 revoked 行；名字可复用（UNIQUE 约束随行删除释放）。一次性迁移：清理现存 revoked 行。确认弹窗文案定稿：「该 key 将立即失效并从列表删除，不可恢复」（UX 审视 ISSUE-05）。审计边界：request_log / 异常事件中的历史 key 名保留不动。
- 文档顺手项：CLAUDE.md 鉴权描述仍写 YAML 明文库（过时，实际 security.db KeyStore）→ 更新；`api-spec/openapi.yaml` 缺 `/admin/keys*`、`/admin/model-pools*` → 可选补录。

## 4. WS4 · 设置页 + 密码门控 + 导航重组

- 设置页：新 nav 项「设置」放**网关策略**组（鉴权旁）；`fragments/settings.html` + `js/settings.js`。v1 功能 = ①控制台密码设置/修改（修改需验旧密码；沿用 ≥12 字节规则 `_password_bytes`；CSRF + Origin 校验；口令哈希仍存 security.db settings 表）②reveal 门状态展示（已设置/未设置）。宪法约束：面板必须有真实功能，不留 placeholder。
- 重看验密：WS3 的逐次密码验证，密码是本页管理的那把控制台密码。
- 忘记密码（UX 审视 ISSUE-04）：设置页密码卡提供恢复指引——终端 `iff keys recover`（明示**会作废全部 API key** 并重建）；reveal 验密失败的错误提示同带此指引。
- 导航重组（`base.html:178-211`）：**监控移入「状态」组**（总览、监控）；异常并入「状态」组尾部（避免单条目组——此项为随附建议，用户可单独否决）；「可观测」组取消。tab id 与 `data-tab` 不变，`localStorage.iff_active_tab` 迁移逻辑不受影响。
- **PR3 实施修订（2026-11-25 方案分析获用户批准，代码核实后）**：
  - 首次设密（网页）：强制验证网关管理员凭证 — `X-Admin-Token` 对进程内 `_admin_token` **恒校验**（即便 admin auth 关闭；零摩擦回环默认不豁免设密这一步）；`security.db` 缺失时按 `iff keys init` 语义初始化（policy 'off'、**不建 key、不改策略**，marker 由 `build_auth` 落）；无凭证 → 走本机 `iff keys init`。端点 `POST /admin/security/console-password`（仅未设置时可用，已设置 409）。**不得借用** `/admin/security/enable` 路径（其会置 policy 'all' + 建首个 key，会让现有客户端突然 401）。
  - 修改密码：控制台会话 + CSRF + Origin（`SecurityAdmin` 新 action，`POST /admin/keys/password`，复用既有 `Path=/admin/keys` 会话 cookie）+ 请求体携带旧密码（逐次验密原语，PR4 reveal 同源）；成功后旧会话随口令记录更新自动失效（`ConsoleAuth.validate` 比对记录原文，无需额外清理）。
  - 忘记密码指引修正：**健康库优先 `iff keys set-password`**（保留全部 API key 与策略，`security/cli.py` 既有能力）；仅库损坏/确需全量重置才 `iff keys recover`（作废全部 key、新库强制 all）。原「recover 为默认指引」不成立。
  - 状态展示：`GET /admin/security/status` 增 `password_set` 字段（只加字段，鉴权页契约向后兼容）。
  - 导航：nav 9→10 项（设置入网关策略组，鉴权旁），既有 nav 钉测试同步更新。
  - **授权体系审计修订（2026-11-25 用户反馈「装能力包竟没权限/找不到开权限入口，软件变死进程」后全面梳理）**：设置页定位收敛为**授权设置**——
    - 本机控制台（回环）**零门槛是默认且不变的行为**：装包/部署/切模型/开设置永不过密码，不新增全站锁、不做登录页；页面卡体常驻边界说明（每次渲染重申）。
    - 三卡结构：①本机零门槛边界说明 ②推理 API 鉴权卡（客户端 401 与否——用户主动开启的独立授权，开/关写前 UI.confirm，关=迁移宽限不删 key）③Key 保护密码卡（仅保护 API Key 敏感操作如 PR4 reveal，**不是控制台登录**）；远程管理开关本期不做（留待后续 PR 另行裁决）。
    - 进入/退出/恢复路径显式化：首设表单带「无凭证 → 网关本机 `iff keys init`」；改密表单带「忘记密码 → `iff keys set-password`（保留全部 key）」；鉴权关闭不删除任何 key（可随时再开）。

## 5. 全局约束与非目标

- Python-only（不引入 Node 工具链）；fragments 拼装机制保留；破坏性操作走 `UI.confirm`；无 emoji；键盘可见 focus。
- 非目标：候选级禁用、跨池 failover、逻辑门 429 行为变更、CI 引入。

## 6. PR 拆分与顺序（建议）

| PR | 内容 | 依赖 |
|---|---|---|
| PR1 `feat(dashboard): IA — wizard to packs, monitor into status group` | WS1 + WS4 导航部分 | 无 |
| PR2 `feat(pools): per-pool fallback routing toggle` | WS2 | 无 |
| PR3 `feat(security): settings page + console password UI` | WS4 设置页 | 无 |
| PR4 `feat(security): key re-view + revoke hard-delete` | WS3 | PR3（密码 UI） |

版本号随首个实施 PR 定（本批为功能批次，建议 minor，如 7.12.0）。每 PR：RED→GREEN、全量 unit+integration、fresh-context cross-review。
