# API KEY Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一次性展示的随机 API KEY、密码保护的管理能力和可控的渐进鉴权，不在部署时切断现有调用。

**Architecture:** 单一摘要存储 + 兼容现有鉴权接口的 facade；管理面使用 CLI 初始化密码和短时 Cookie 会话。PR-23a 后端默认 off；PR-23b 独立面板另立实施计划，不与后端一并展开。

**Tech Stack:** Python 3.10+、stdlib secrets/hashlib/hmac/sqlite3、现有 HTTP 管理路由与 pytest。

**Spec:** `specs/pr23-api-key-security-spec.md`

## Global Constraints

- sandbox-first；vLLM 0.24 不升级。
- 不修改 `proxy/auth.py` 或推理转发核心；handler 仅允许沿既有先例追加 admin 路由项。
- KEY 只存摘要，密码不进入浏览器存储；原值仅一次创建响应返回。
- 生产密码/KEY 不写入对话；不改生产鉴权，不自动开启 pools/all。
- Python 3.10+；一次 fresh-context cross-review；补丁版本按实施时 main 递增。

## Review Focus

- 旧鉴权已开启时，新增策略 off 不得降低原有保护。
- store 丢失/损坏后重启不得把已保护服务变为匿名开放。
- 两个管理请求并发创建/吊销，不能丢更新或发布未提交状态。
- 恶意本地网页、跨源请求、Host 欺骗不能登录、建 KEY 或改变策略。
- 一个池的凭证不能调用其他池、直连候选或绕过另一种 API 协议。

---

### Task 1: 摘要存储与策略快照

**Files:** Create `inferfabric/security/__init__.py`, `inferfabric/security/key_store.py`; Test `tests/unit/security/test_key_store.py`。

**Interfaces:**
- `KeyStore(path: Path, *, provision: bool = False)`：仅显式 provision 初始化；运行时打开缺失/损坏文件抛配置错误。
- `create_key(name: str, models: list[str] | None, expires_at: datetime | None) -> tuple[dict, str]`：返回安全元数据和一次性原值。
- `list_keys() -> list[dict]`, `revoke_key(key_id: str) -> None`。
- `check(token: str, model: str) -> tuple[bool, str]`, `key_name(token: str) -> str`。
- `get_policy() -> str`, `set_policy(policy: str) -> None`，policy 仅 off/pools/all。
- SQLite 独立文件 `security.db`，0600；事务串行提交；不借用现有运行状态库。

- [ ] **Step 1 — RED:** 测试新 KEY 不同且随机部分解码 32 bytes；关闭再打开数据库仍能验证；数据库全部表中找不到原值；scope 精确匹配；过期边界/吊销拒绝；非法有效期拒绝；双连接并发更新均保留；失败事务不改变策略；缺失/损坏数据库拒绝打开。运行 `python3 -m pytest tests/unit/security/test_key_store.py -q`，确认缺实现失败。
- [ ] **Step 2 — GREEN:** 按以上接口实现摘要比对、元数据和事务。不要用一个可预测的“主 KEY”开启鉴权。
- [ ] **Step 3 — Verify:** 同命令全部通过，再运行 `python3 -m pytest tests/unit/ tests/integration/ -q`。
- [ ] **Step 4 — Commit:** 仅暂存本任务文件，提交 `feat(security): add digest-only API key store`，附项目要求 attribution。

### Task 2: 兼容 facade 与双协议策略验证

**Files:** Create `inferfabric/security/auth_facade.py`; Modify `inferfabric/proxy_manager.py`; Test `tests/unit/security/test_auth_facade.py`, `tests/integration/test_security_auth.py`。

**Interfaces:**
- Consumes Task 1 的 KeyStore 和旧 AuthManager。
- `SecurityAuth(legacy, store)` 提供 `enabled` 属性、`check(header: str, model: str) -> tuple[bool, str]`、`key_name(header: str) -> str`、`reload(config_path=None) -> None`。
- facade 的 enabled=True 让既有检查点执行；匿名兼容放行归 check，不能在外围跳过检查。旧 enabled=True 时其原强制范围继续成立。新旧合法 KEY 各保留自己的权限，不合并扩权。
- 初始化顺序：控制台 CLI 显式 provision 后才注册 facade；现有部署未配置新安全功能时保留旧 AuthManager。已启用部署使用独立启用标记/显式配置且文件缺失必须失败，不能用“数据库不存在”推导未启用。

- [ ] **Step 1 — RED:** 参数化 off/pools/all × 匿名/合法/无效/过期/吊销 × 池/直连；断言匿名矩阵为 off=(allow,allow)、pools=(deny,allow)、all=(deny,deny)；旧 enabled=True 时匿名始终 deny。断言 Bearer/x-api-key 两协议实际请求一致，池 KEY 不可调用直连候选；热加载错误不开放，冷启动缺 store 不就绪，日志 key_name 不含原值。先运行两个新文件，观察预期失败。
- [ ] **Step 2 — GREEN:** 实现 facade 并仅在构造处注入，核对所有 auth 消费者（包括 config_reloader、key_name 与 enabled 的使用）接口。新功能未配置时无行为改变。
- [ ] **Step 3 — Verify:** `python3 -m pytest tests/unit/ tests/integration/ -q` 全通过；`git diff` 确认 PR-14 冻结实现零改动。
- [ ] **Step 4 — Commit:** 提交 `feat(security): add staged authentication facade`，附 attribution。

### Task 3: 密码、会话与管理 API/CLI

**Files:** Create `inferfabric/security/console_auth.py`, `inferfabric/security/admin.py`; Modify `inferfabric/cli.py`, `inferfabric/proxy/handler.py`（仅路由项）、`inferfabric/proxy_manager.py`（依赖装配）；Test `tests/unit/security/test_console_auth.py`, `tests/unit/security/test_key_admin.py`, `tests/integration/test_security_admin.py`。

**Interfaces:**
- Consumes Task 1/2；管理服务直接使用同一 KeyStore，提交后新请求立即见吊销结果。
- CLI `iff keys init`（显式 provision、密码 getpass）、`iff keys set-password`（重置并废止会话）；重复 init 不覆盖存储。生产执行另获授权。
- `ConsoleAuth.login(password: str) -> tuple[str, str]` 返回随机 session/CSRF；`validate(session: str, csrf: str | None, *, write: bool) -> bool`；注入时钟便于测 15 分钟到期。生产密码哈希只存 scrypt 参数、盐和派生结果；参数有资源上界。
- Routes：`POST /admin/keys/login`、`POST /admin/keys/logout`、`GET /admin/keys`、`POST /admin/keys`、`POST /admin/keys/revoke`、`POST /admin/keys/policy`。没有 reveal；列表只元数据。
- 所有路由继承 admin 外层守卫；会话、Host/Origin/CSRF 是额外内层。默认可信来源仅明确配置的 loopback 地址和端口，不能照抄任意 Host/Origin。CLI 策略变更与 HTTP 策略变更一样要求明确确认；all 要求二次确认，部署不调用它。

- [ ] **Step 1 — RED:** 测错误密码/正确密码、5 次失败后第 6 次 429 且不计算 scrypt、15 分钟失效、重启和重置密码失效；测试跨源/Host 欺骗/缺 CSRF/未登录均不能创建吊销切策略；登录不受理任意 Origin；已登录列表无原值，创建响应只返回本次原值且 no-store；拒绝过大请求和非法 scope/有效期；CLI 重复 init 不覆盖；错误日志和审计不含凭证。先运行新测确认失败。
- [ ] **Step 2 — GREEN:** 实现安全存储/会话与管理路由，在隔离临时数据目录和非 8999 端口启动验证，不使用真实凭证。确认同步/异步 HTTP 两个入口对 Cookie、Origin、CSRF、admin guard 的行为一致。
- [ ] **Step 3 — Verify:** 全 unit+integration、import smoke、隔离服务核心健康端点返回 200；真实 HTTP 验证登录→创建→调用→吊销→调用拒绝和策略矩阵。
- [ ] **Step 4 — Review/Commit:** 一次 fresh-context whole-branch review；Important/Critical 做一次 TDD 修复轮，Minor 留档；干净提交树复验；补丁版本及 README 同步；提交并按已授权工作流合并，不运行生产初始化/强制切换。提交附 attribution。

## PR-23b（另立计划，不在本计划内实施）

新「密钥」页消费上述接口；先核对 Dashboard 组装入口及真实浏览器可用性，再写面板实施计划。验收必须包括登录、生成显示一次、复制、离页清除原值、列表无找回入口、吊销、会话到期、跨源拒绝与匿名迁移提示。UI 不把管理密码放入 sessionStorage。

## 计划自检

- 已覆盖已批准三决策；KEY 页面由单独 PR-23b 交付，非本 PR 遗漏。
- 原“两文件复用 AuthManager、最终零代码”方案撤回：与摘要存储冲突且有双写风险。
- Review Focus 五项分别落在 Task 1/2/3 的测试中。
- 不自动更改生产 admin token，不初始化生产密码，不自动开启 pools/all。
- 具体部署标记与会话跨 worker 实现须在 Task 2/3 开始时按实际进程模型核对；若与上述接口冲突，先修计划再实现，不以猜测扩展冻结转发路径。
