# 模型池 API KEY 安全方案（设计稿，待定夺）

日期：2026-10-29 · 状态：**设计阶段，未实施** — 按指示「别急着动手，想清楚整体安全方案，再定夺」

需求原文：「对模型池的模型优先增加 API KEY 功能，没有 API KEY 的配置逐渐退役后，再默认必须有 API KEY，增加安全性。API KEY 应该在哪个界面进行生成，并且通过密码进行控制访问，或者密码提供可见和复制服务。」

---

## 一、现状盘点（事实，已核实代码）

| 面 | 现状 | 位置 |
|---|---|---|
| 推理鉴权 | `AuthManager`：`~/.inferfabric/api_keys.yaml`，`primary`（全模型长期）+ `guests[]`（名字/模型白名单/过期时间） | `proxy/auth.py` |
| **总开关语义** | **文件不存在/无 primary = 鉴权完全关闭（全开放）**。生产现状：文件不存在 → :8999 所有推理端点匿名可用 | `auth.py:122-140` |
| 鉴权覆盖面 | 开启后全覆盖：`/v1/chat/completions`、`/v1/messages`、池双协议（`iff/<pool>` 按完整逻辑名校验，不 strip、不自动扩权）、embeddings/rerank | `chat_handlers.py:235`、`handler.py:468/2293`、`model_pool/dispatch.py:238/353` |
| 协议头 | `Authorization: Bearer <k>` 或 `x-api-key: <k>` | 同上 |
| 请求日志 | 只记 key **名字**（`key_name`），永不记原始 key；匿名 = `anonymous` | `request_logger` |
| 热加载 | `iff reload` / SIGHUP → fail-closed | `config_reloader.py` |
| 控制面 | `/admin/*` 用 `X-Admin-Token` 比对 `IFF_ADMIN_TOKEN`（env，空 = 本地开放）；**生产未设**（secrets.env 只有云厂商 key） | `handler.py:1448` |
| 面板 | 7 个 tab，无登录概念；admin token 经 `#adminToken` 输入框或 `?token=` 注入 | `dashboard/js/ui.js:122` |
| 密钥存储先例 | `secrets.env`（600 权限，SecretsManager 管理） | `cloud_discovery.py:137` |

**关键结论：**
1. 鉴权机制本身已完整（含按 key 的模型白名单 → **天然支持按池发 key**），缺的只是**分阶段强制范围**（现在是一刀切全局开/关）与**管理界面**。
2. 生产的「退役终点」（默认必须有 KEY）**已经是现成行为**——放上 api_keys.yaml 即全强制，零代码改动。
3. 模型池接入片段已有 `api_key="<你的 IFF API key>"` 占位（`model-pools.js:1833`），key 管理页可直接喂它。

## 二、核心设计

### 2.1 分阶段强制 —— 两层开关，零触碰 PR-14 保护路径

PR-14 冻结：`auth.py`、`handler.py`（转发核心）、`chat_handlers.py` 等。因此**不新增全局 mode 概念、不改 AuthManager**，用「两个文件 = 两层开关」实现渐进：

```
阶段 1（池优先）        阶段 2（逐客户端迁移）      阶段 3（默认必须）
pool_keys.yaml 放上      每个客户端配上 key          api_keys.yaml 放上
─────────────────      ─────────────────         ─────────────────
iff/<pool> 请求 → 强制   匿名直连请求逐个消灭        所有推理端点 → 强制
直连模型名请求 → 开放    （面板可视化进度）          （现有代码路径，零改动）

新代码：model_pool 池侧强制层     ← 全部在新文件/池文件里 →   无新代码
```

- **池侧强制层**（新文件 `model_pool/key_auth.py` + 改 `model_pool/dispatch.py` 两处鉴权点）：`pool_keys.yaml` 存在 → `iff/*` 请求必须带有效 key。**直接复用 `AuthManager` 类**（`AuthManager(pool_keys.yaml)` 第二实例——primary=全池通行，guests=按池白名单），解析/过期/校验零新代码。`model_pool/` 不在 PR-14 冻结名单（PR-21 同区改动先例）。
- **直连强制层**：就是现有 `pm.auth`（api_keys.yaml）。阶段 3 放上文件即全局强制——**终点零代码**。
- 一把 key 两边通用：key 格式沿用现有约定（`sk-iff-<yyyymm>-v<n>` / `sk-<agent>-<yyyymm>-v<n>`）；「全模型」scope 的 key 由管理页同时写入两个文件，用户体验是一份 key 列表。

### 2.2 密钥管理界面 —— 新增「密钥」tab（第 8 个侧栏页）

**推荐独立 tab**（而非塞进「部署」或「模型池」）：key 既服务池也服务直连模型（阶段 3），且页面要承载退役进度/审计，长期是安全中心。

```
┌ 密钥 ──────────────────────────────────────────────┐
│ 强制状态卡：池强制 [ON/OFF] · 直连强制 [ON/OFF]      │
│            近 24h 匿名直连请求：N 笔（退役进度）← 请求日志聚合 │
├────────────────────────────────────────────────────┤
│ key 列表：名字 | 范围(全模型/池名) | 建立于 | 过期 |  │
│          最近使用(请求日志 key_name 聚合) | [查看][吊销] │
│ [＋ 生成密钥]                                       │
└────────────────────────────────────────────────────┘
生成向导：名字 → 范围(全模型 | 勾选池) → 有效期(永久/30d/90d/自定义)
          → 输入控制台密码 → 生成 → 明文一次性展示 + 复制钮
```

### 2.3 控制台密码 —— 明文 key 永远只在密码验证后出服务器

**铁律：原始 key 值离开服务器的唯一通道 = 密码验证成功的响应。**（这同时封死无 token 的恶意本地网页经 `/admin` 建 key 偷 key 的路径——建了也看不到值。）

- 密码：首次使用时设置，**只存哈希**（stdlib `hashlib.scrypt` 加盐，`~/.inferfabric/console.hash`，600 权限）；CLI 亦可 `iff keys set-password`。
- 生成与查看均需密码；验证走常数时间比较；**限速 5 次/分钟**（超限 429 + AnomalyEvent 记录）；每次验证写 journal 审计行。
- 浏览器侧：密码验证成功后存 `sessionStorage`（tab 生命周期，关 tab 即失效），期间免重复输入；每次「查看」弹遮罩式明文（默认打码、点击显示、30s 自动隐藏）+ 复制钮。
- 列表接口永不返回明文（只回 `sk-iff-…-v1` 式名字与元数据）。

### 2.4 管理端点（走既有路由表先例，转发核心零改动）

`GET /admin/keys`（列表）、`POST /admin/keys`（生成，带密码）、`POST /admin/keys/reveal`（查看，带密码）、`POST /admin/keys/revoke`。实现在新模块 `key_admin.py`；handler.py 仅在既有 admin 路由表**追加 4 行**（与 `/admin/model-pools/*` 完全同构的先例，不触碰任何转发/鉴权逻辑）。

### 2.5 迁移执行顺序（退役路线）

1. **上线即阶段 1**：部署 PR 后建 `pool_keys.yaml` + 为现有客户端（Claude Code、agents.d 各工具）发 key → 池请求立即强制。
2. **阶段 2 逐客户端**：面板「匿名直连请求」计数归零过程 = 退役进度条。**内部消费者要点名排查**：分类器链路（经 :8999 请求 TXT，今日不带 key）、bench 脚本、任何硬编码直连配置——阶段 3 前必须全部配 key。
3. **阶段 3 人工触发**：匿名计数为 0 后放上 api_keys.yaml（面板状态卡实时显示两层开关状态，误切可即时删除回滚）。
4. 回滚任何阶段 = 删对应文件（或吊销单个 key），`iff reload` 生效，无代码回滚。

## 三、待定夺点

| # | 决策 | 推荐 |
|---|---|---|
| 1 | 两文件两层开关（零 PR-14 触碰）vs 单文件加 mode（需 auth.py PR-14 例外） | **两文件**——终点本就是现有机制 |
| 2 | 界面位置：新「密钥」tab vs 部署页内嵌节 | **新 tab** |
| 3 | 密码输入频度：每次输 vs sessionStorage 免重复 | **sessionStorage（tab 级）** |
| 4 | 顺手设置 `IFF_ADMIN_TOKEN`（生产现为空，/admin 本地开放；面板 UX 已支持 token 注入） | **建议一并设置**（keys 端点有密码内层，不设也不泄漏 key，但整体控制面更稳） |
| 5 | 切分：PR-23a 后端（store+强制+端点+密码+CLI）→ PR-23b 面板 tab + 浏览器硬门禁 | **两刀**，后端先行 |

## 四、质量门（实施时）

- 单测：开关矩阵（文件在/不在、guest 过期、池白名单）、密码哈希+限速、端点鉴权。
- 集成：dispatch.py 各 401 路径 × 两阶段；阶段 3 双文件全强制。
- **浏览器硬门禁**（per 2026-10-25 教训）：真实交互测试——设密码→生成（明文可见）→列表打码→密码查看→复制→吊销，全链路。
- 版本：PR-23a/23b 各自补丁位升级。
