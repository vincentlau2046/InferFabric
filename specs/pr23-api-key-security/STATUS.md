# PR-23a API KEY 安全后端 — STATUS

日期：2026-10-29。分支：sandbox/main（pr23a-security 已合并）。版本：7.6.4（off 宽限语义修正）。
规格：`../pr23-api-key-security-spec.md`。

## 授权与边界

用户批准：KEY 生成时只展示一次、渐进强制、独立密钥页；并批准本会话直接实施 + 一次独立评审。
本次仅 PR-23a 后端；PR-23b 面板未实施。没有初始化生产密码/KEY，没有启用生产强制鉴权，没有重启生产服务。

## 质量门

| 项 | 状态 | 证据 |
|---|---|---|
| 摘要存储 TDD | ✅ | 新文件缺实现 RED → 14 passed；SHA-256 摘要、随机 32 bytes、事务回滚、并发、吊销 |
| 渐进鉴权 | ✅ | auth facade 单测 + 双协议 × Bearer/x-api-key 真实 HTTP；全量 1846 passed |
| 管理密码/会话/API/CLI | ✅ | scrypt、15min 固定过期、重置失效、限速、CSRF/Host/Origin、一次性返回、CLI 初始化；完整 suite 1864 passed |
| 评审修复 pass（I-1 受控恢复） | ✅ | 4 个新用例 RED→GREEN：丢失/损坏重建为 all、取消零改动、init 准备失败零残留、旧管理会话失效；全量 1868 passed |
| 隔离 HTTP 冒烟 | ✅ | ThreadingHTTPServer 与 aiohttp ephemeral loopback 端口，临时 store；health200→登录200→生成201→列表200→策略→吊销200→拒绝401→登出；没有真实后端/GPU推理，合法池请求抵达 unknown_pool404 证明通过鉴权 |
| import | ✅ | 7.6.3 |
| 独立评审 | ✅ | 一次 fresh-context subagent（opus）评审 ef3c5d5..7984273：0 Critical / 1 Important / 8 Minor。Important 已修复（上行）；Minor 全部留档（见评审记录） |
| 干净提交树复验 | ✅ | detached checkout 07cd511（干净 worktree，PYTHONPATH 含 _deps）：1868 passed（31.47s），import 7.6.3 |
| off 宽限语义修正（7.6.4） | ✅ | TDD：5 用例 RED（401≠404、key_status 缺失）→ GREEN；`KeyStore.key_status`（valid/unknown/revoked/expired）+ facade off 放行 unknown/expired + warning（吊销/scope 不放宽）；集成回归：off 下未注册 key 过鉴权（404 证明）|
| 生产合并/激活 | 部分执行 | 7.6.3 已部署并初始化（off + 全权限 key claude-code）；强制策略 pools/all 仍未启用，须独立授权 |

## 评审记录（durable）

**Important（已修复）**：损坏/丢失安全存储后 CLI 无受控恢复路径；init 在 marker 之后失败留下不支持的部分激活状态；错误信息误导为 set-password。
修复（TDD，4 用例先 RED 后 GREEN，全量 1868 passed）：

- `iff keys recover`：键入 `RECOVER SECURITY` + 新密码。旧库连同 `-wal/-shm/-journal` 归档至 `security-recovery-<ts>/`（0700），新库策略为 `all`，绝不静默退回匿名；marker 保留（缺失则补建）。暂存库完整构建（含密码、策略）后才归档旧库并原子替换，中途失败保持失败关闭且可重试。
- init 改为私有暂存目录内构建完整存储（含密码）后原子发布，先库后 marker——两个方向都保持启用语义；准备阶段失败零残留（无 marker、无 security.db）。
- 损坏存储上的 init/set-password/policy 错误改为指向 recover；主 CLI `keys` 分发补捕获 `sqlite3.Error`，不再 traceback。
- 运行中的网关无需重启即在新请求使用新库（每操作按路径新开 mode=rw 连接）；旧管理会话随密码记录变更失效。

**off 宽限 7.6.4 评审（fresh-context subagent，2831d6a..78765e9）**：0 Critical / 0 Important，裁决 Clean。3 项 Minor 已当场修复：(1) 过期+scoped key 在 off 下按 validity 宽限放行（视同匿名、不扩权）→ spec 补一行边界说明；(2) 补两个测试 pin（legacy 开启时 off 无宽限、过期 key 在 pools 拒绝）；(3) 收紧 warning 断言（钉 `off policy`+`unknown` 且原值 key 不进日志）。2 项 Informational 留档不修：legacy YAML 开启时宽限不生效（生产无 api_keys.yaml，非现状）；off 下宽限 warning 无速率上限（迁移期可接受）。

**Minor（留档不修）**：

1. 默认端口来源 `http://127.0.0.1:80` / `:443` 的 Host 匹配缺口（当前 8999 部署不受影响）
2. 登录限速把成功登录也计入（比规格的仅失败计数更严格；保留更安全方向）
3. `iff` 帮助文本未列出 keys 子命令
4. 存储不可用的固定日志缺少异常类，诊断信息有限
5. `iff/` 前缀字面量重复而非共享常量
6. 每请求新建 SQLite 连接；极端情况下瞬时锁拒绝
7. 管理 API 16KB body 上限低于理论 200×200 字符 scope 载荷
8. ProxyManager origins/security_admin 装配无直接单测（集成路径覆盖）

**裁定**（评审 declined-to-judge 项 + 实施裁定）：

- 恢复不做跨进程 flock：存储访问为每操作新建 `mode=rw` 连接、替换走暂存+rename（间隙失败即拒绝）、facade 对存储不可用一律拒绝；最坏竞态是瞬时拒绝或归档副本收到写入，不存在策略绕过。多进程部署需连同会话共享一并重行设计。
- PR-23b UI 另行计划，必须含真实浏览器交互门禁；本次不悄悄交付。
- 既有 wildcard OPTIONS/通用 CORS 属冻结转发面，未改动；`/admin/keys` 自带 no-store responder + Origin 拒绝。
- 跨凭证响应缓存共享：既有单用户行为；鉴权先于缓存、池请求跳过缓存；不声称新多租户数据隔离。
- `/v1/models` 与面板元数据保持匿名：既有信息面，不在推理鉴权规格内。
- 多进程会话共享：会话为进程内存；当前两种 HTTP 模式均为单 ProxyManager。
- 旧全名池 scope 与直连剥前缀 scope 的不对称：保留既有行为，不扩大权限。
- 空 `IFF_ADMIN_TOKEN` 的 loopback 外层守卫：继承既有 `_admin` 语义；新密钥端点另有密码会话+CSRF 内层守卫。
- GPU 运行验证：operational 门禁测试有意不执行；不做推理冒烟声明。
- 迁移可见行为（**7.6.4 已修正**）：原实现 off 下未注册 KEY 也 401 → 2026-10-30 实测打挂本地 claude-code/curl 等带占位 key 客户端（79 条 401 × 22min，临时用全权限 key 恢复）。修正：off = 迁移宽限——未注册/过期 KEY 放行 + warning；吊销与 scope 边界在 off 下始终生效；pools/all 不变。生产当前：off + 1 个全权限 key（claude-code）。

## 技术裁定

- 不复用旧 AuthManager 保存新 KEY，不双写 YAML；新安全存储只存摘要。
- facade 在既有鉴权点执行策略；旧鉴权已启用时不允许 off/pools 降低保护范围。
- security.enabled 是持久启用标记，删除 security.db 不会退回匿名。冷启动缺失/损坏失败；运行时存储不可用拒绝调用。
- 当前两 HTTP 模式均为单 ProxyManager + 线程 worker；会话进程内存保存，密码 hash 变更通过 SQLite 使旧会话失效。多进程部署需要另行设计会话共享/亲和性。
- management 使用专用 no-store JSON responder，不沿用带 wildcard CORS 的转发 helper；冻结转发代码未修改，handler 仅新增路由表项与 import。
- 全量首次收集碰到重名 test_admin 模块，改名 test_key_admin 后通过；无运行时行为影响。

## 操作说明（仅说明，不在本任务执行）

1. 完成代码部署后，由用户在终端运行 `./iff keys init`，交互输入控制台密码（至少12 UTF-8 bytes）；原值不经 argv、不输出到对话。
2. 初始化创建私有 security.db + security.enabled，策略 off；重复 init 拒绝覆盖。网关要显式重启才装配新管理能力，不自动重启。
3. API：POST /admin/keys/login 返回 HttpOnly SameSite=Strict Cookie + csrf；后续管理写请求带 Cookie、X-CSRF-Token、可信 Origin，以及原有部署配置要求的 X-Admin-Token。
4. POST /admin/keys 创建时唯一一次返回 key；GET /admin/keys 只返回元数据；没有 reveal 接口。
5. 当前仅接受 full access（models=null）或精确 `iff/<pool>` 权限。逐客户端迁移后，`./iff keys policy pools` 要求输入确认；全局 all 另需用户批准并二次确认。不是部署步骤。
6. 重置管理密码：`./iff keys set-password`，旧会话失效。丢失 KEY 只能新建并吊销旧 KEY。
7. 默认管理来源 http://127.0.0.1:<port>、http://localhost:<port>、http://[::1]:<port>；远程必须明确配置 HTTPS `IFF_CONSOLE_ORIGINS` 并由可信 TLS 入口终止 TLS，不能裸露明文 HTTP。新功能未初始化时维持原 AuthManager 行为。
8. 存储丢失/损坏，或忘记控制台密码需要完全重置：`./iff keys recover`，键入 `RECOVER SECURITY` 并设置新密码。旧 KEY 全部作废（旧库归档于 `security-recovery-<ts>/`），新库策略为 `all`；如需放宽再显式执行 `./iff keys policy pools|off`。运行中的网关无需重启，旧管理会话全部失效。
