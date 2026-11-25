# PR3 · 授权设置页（auth-settings）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 设置页呈现并管理两件用户主动启用的授权事物——推理 API Key 鉴权（客户端 401 与否）与 Key 保护密码（仅重看等敏感操作验密）——本机控制台全程无登录、能力包安装永不过密码。

**Architecture:** 新增 `tab-settings`（网关策略组、鉴权旁）+ `settings.js`，消费既有 `/admin/security/*` 与新增两个端点：`GET /admin/security/status` 增 `password_set` 字段；`POST /admin/security/console-password`（首次设密，管理员凭证恒校验，`iff keys init` 语义初始化）；`POST /admin/keys/password`（改密 = 控制台会话 + CSRF + 旧密码，SecurityAdmin 新 `password` 动作）。不改 `_admin_gate` 语义、不改 `/admin/security/enable` 行为、不做远程管理开关。

**Tech Stack:** Python 3.10 stdlib（http.server / sqlite3 / scrypt），dashboard 原生 JS（IIFE + 事件委托，无 inline onclick），pytest + node vm 面板测试。

**Spec:** `specs/console-ux-batch/spec.md` §4（含 2026-11-25 授权体系修订——用户批准：本机日常操作零门槛；保护范围仅 API Key 敏感操作；由用户主动启用且有可见退出/恢复路径）+ 本计划。分支 `feat/console-ux-pr3-settings-password`，目标版本 **7.12.2**（patch，自主）。

## Global Constraints

- 本机控制台（回环）默认零登录：装包/部署/切模型/开设置永不过密码；不新增全站锁。
- 控制台密码只保护 API Key 敏感操作（PR4 reveal）；≥12 UTF-8 字节（`_password_bytes`，上限 1024）。
- 不静默关闭已启用的鉴权：`enforce=false` 只置 policy 'off'（迁移宽限），key 全保留。
- 首次网页设密必须验证管理员凭证（`X-Admin-Token` vs `_admin_token`，恒校验），无凭证 → 指引本机 `iff keys init`。
- 忘记密码：健康库 → `iff keys set-password`（保留 key 与策略）；仅库损坏 → `iff keys recover`（作废全部 key）。
- Python-only；fragments 拼装；无 emoji；写操作 `UI.confirm`；明文密码只经 textContent/请求体，不进日志与 innerHTML。
- 每任务 RED→GREEN→commit；全量 unit+integration 收口；fresh-context 评审后开 PR。

## Review Focus

1. **拒绝的首次设密不得落任何盘**——错/缺管理员 token 时 `security.db` 与 `security.enabled` 都不得出现 → `test_console_password_missing_token_rejected_and_no_store` / `wrong_token`（Task 4）。
2. **改密失败不动旧会话**——持有效会话验旧密码失败：口令记录不变、该会话仍可用 → `test_password_change_failure_keeps_session`（Task 2）。
3. **`password_set` 三态**——未初始化库（legacy auth）、已初始化无口令、已设口令，status 字段都不得 500 → `test_status_reports_password_set_field`（Task 3/4）。
4. **设置页密码卫生**——密码值只出现在 login/console-password/keys-password 三个请求体，绝不进 GET、日志、innerHTML → `test_settings_js_password_hygiene`（Task 5）。
5. **nav 迁移不破旧 tab**——9→10 项后 caps 不变、旧 localStorage tab 正常、tab-settings 注册 → `test_nav_grouped_ia_and_aria_current` 更新 + `test_settings_panel.py`（Task 5）。

---

### Task 1: `password_set(store)` 助手

**Files:** Modify `inferfabric/security/console_auth.py`；Test `tests/unit/security/test_console_auth.py`（RED 已在盘）
**Interfaces:** Produces `password_set(store) -> bool`（settings.id=1 password 非空；db 层错误返回 False 不抛）

- [ ] 跑 `pytest tests/unit/security/test_console_auth.py::test_password_set_reports_initialization -q` 确认 RED（ImportError）
- [ ] 实现 `password_set`（SELECT password FROM settings WHERE id=1；bool(row and row[0])；sqlite3.Error → False）
- [ ] 同文件跑 GREEN；commit `feat(security): password_set 判定助手（PR3 WS4）`

### Task 2: SecurityAdmin `password` 动作（改密）

**Files:** Modify `inferfabric/security/console_auth.py`（加 `verify_current`）、`inferfabric/security/admin.py`（import set_password + `elif action == 'password'` 分支）；Test `tests/unit/security/test_key_admin.py`（RED 已在盘，含 4 例）
**Interfaces:** Consumes Task 1；Produces `ConsoleAuth.verify_current(password) -> None`（错/未设 raise `LoginError`，不计登录限流窗、不建会话）；action 契约：body `{current_password, new_password}`，成功 200 且旧会话随记录更新失效；错旧密码 → 400，error 含「当前密码」与 `iff keys set-password` 指引；缺字段/新密码 <12B → 400。

- [ ] 跑 `pytest tests/unit/security/test_key_admin.py -q -k password_change` 确认 RED（404 not found）
- [ ] 实现 `verify_current`（复用 login 的 record 解析 + scrypt + compare_digest，不触碰 `_attempts`）与 action 分支（先验旧 → `set_password`；LoginError 转 ValueError 携恢复指引）
- [ ] 同文件跑 GREEN（全量 test_key_admin 防回归）；commit `feat(security): 控制台会话内改密 — 旧密码验证 + 会话自动失效（PR3 WS4）`

### Task 3: status 增 `password_set` 字段

**Files:** Modify `inferfabric/proxy/handler.py` `_handle_security_status` 两分支；Test `tests/unit/test_dashboard_security_responses.py`（RED 待写：`test_status_reports_password_set_field`）
**Interfaces:** Produces `{"password_set": bool}`（legacy 分支恒 False；SecurityAuth 分支 = Task 1 助手）

- [ ] 重读该测试文件尾部（先前 Edit 锚点失效）→ 追加 RED 测试 → 确认 FAIL（KeyError password_set）
- [ ] 实现两分支字段 → GREEN → commit `feat(security): /admin/security/status 增 password_set（PR3 WS4）`

### Task 4: `POST /admin/security/console-password`（首次设密）

**Files:** Modify `inferfabric/proxy/handler.py`（`_handle_security_console_password` + `_POST_ROUTES` 注册 `"/admin/security/console-password": _admin(lambda h, pm: h._handle_security_console_password(pm))`，紧邻 enable）；Test `tests/unit/test_dashboard_security_responses.py`（`_TokenHandler` + 7 例，见 Review Focus 1/3）
**Interfaces:** Consumes Task 1/3；body `{new_password}`；成功 `{status:"ok", provisioned:bool}`；401（token 缺/错/未配置，error 指引 `iff keys init`）、400（<12B）、409（已设置，error 含「已设置」）。库缺失 → `KeyStore(provision=True)` + `build_auth` + `SecurityAdmin(origins)` 热接（**不置 policy、不建 key**）；库在无口令 → 仅 `set_password`；明文不落盘。**禁止**借用 `_handle_security_enable`。

- [ ] 追加 RED 测试 → FAIL；实现 → GREEN（整文件回归）→ commit `feat(security): 首次网页设密 — 管理员凭证恒校验 + iff keys init 语义（PR3 WS4）`

### Task 5: 设置页 UI（授权设置）

**Files:** Create `inferfabric/dashboard/fragments/settings.html`、`inferfabric/dashboard/js/settings.js`；Modify `inferfabric/dashboard/base.html`（鉴权按钮后插 `tab-settings`，`#s-gear` 图标）、`inferfabric/dashboard/__init__.py`（JS_MODULES + fragment 序列各加 "settings"）；Test：Create `tests/unit/test_dashboard_settings_page.py`（拼装/端点消费/无 inline onclick/密码卫生四个源级钉，仿 test_dashboard_security_page.py）、Create `tests/unit/dashboard/test_settings_panel.py`（node vm：未设置渲染首设表单/已设置渲染改密表单/首设提交命中 console-password/改密两跳 login→password 带 CSRF/推理鉴权卡状态与开关调用 enable）、Modify `tests/unit/dashboard/test_inference_primary_discipline.py`（nav 9→10 + tab-settings 位于网关策略组）
**Interfaces:** 消费 Task 3/4 端点 + 既有 `/admin/security/enable`（推理鉴权开/关，写前 `UI.confirm`：开=客户端将 401、关=仅宽限不删 key）；首建 key 明文一次性展示（textContent，Esc/切页即清，仿 security.js 代次守卫）。
- [ ] nav/页面/面板 RED → FAIL；实现 UI → GREEN → commit `feat(dashboard): 授权设置页 — 推理鉴权开关 + Key 保护密码卡（PR3 WS4）`

### Task 6: 版本与文档收口

**Files:** `inferfabric/__init__.py`（7.12.2）、`README.md`（版本历史行：授权设置页 + password_set/首次设密/改密端点 + 本机零门槛边界）、`specs/console-ux-batch/STATUS.md`（PR3 行）、`specs/console-ux-batch/spec.md` §4（补 2026-11-25 审计后修订记录）
- [ ] 全量 `pytest tests/unit tests/integration -q`（基线 2766 passed + 新增；3 例 connect_clients[codex] 既有失败 #44）→ 版本/文档 commit → push → 开 PR → fresh-context 评审 → 修入
