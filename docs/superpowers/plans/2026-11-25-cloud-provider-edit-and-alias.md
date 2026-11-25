# 云提供方原地编辑 + 模型调用别名 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 已接入云提供方可在控制台原地编辑（显示名/Key/端点/启停），且同名上游模型可用显式调用别名精确路由。

**Architecture:** 全部落在管理面（`cloud_discovery.py` + `proxy/handler.py` admin 端点 + dashboard）。凭证明细只进 `secrets.env`，YAML 存引用；编辑以「配置指纹 CAS + 服务端真实探针」防覆盖与防误配；别名是 cloud_provider.yaml 中 provider 级显式映射，路由解析别名优先、目标不可用显式失败不回退。`forwarder.py` 不改（PR-14 边界）。

**Tech Stack:** Python 3.10 标准库 HTTP handler + YAML；现有 pytest 单测风格；dashboard 原生 JS（无 Node 工具链）。

**Spec:** `docs/superpowers/specs/2026-11-25-cloud-provider-connection-and-model-alias-design.md`（含「评审后补记的首版建议」——与本文冲突时以 spec 为准并回报）

## Global Constraints

- 不改 `forwarder.py`；`proxy/handler.py`、`proxy/chat_handlers.py`、`proxy/responses_bridge.py` 仅允许动 admin/错误分支，不动成功转发路径（PR-14）。
- Key 明文永不出现在：任何 GET 响应、YAML、日志、错误信息、测试快照。凭证只写 `secrets.env`（600）。
- 每任务 RED→GREEN；PR 合并前全量 unit+integration 绿 + `python3 -c "import inferfabric"` 冒烟。
- 版本 patch 自升（PR1→7.12.9，PR2→7.12.10）+ README 版本历史表加行。
- UI：破坏性操作 `UI.confirm`；无 emoji；键盘 focus 可见；`data-tab` 与 fragments 拼装机制不动。
- 双端验收按 spec §4 表格执行（本机隔离网关 `scripts/iff-acceptance.sh` + 模拟上游，不占 GPU）。

## Review Focus

1. **外部托管的 Key（env 已设但 secrets.env 没有）在换域时**：期望显式拒绝（`destination_key_unconfirmed`），绝不把旧值发往新 host —— Task 3 钉死。
2. **公共 `/models` 返回 200 被当作鉴权通过**：期望凭证/端点变更的 apply 门禁只认真实 1-token 生成探针 —— Task 3 钉死。
3. **别名与既有云短名/本地名/`fabric-` 前缀撞名**：期望保存被拒且不写入 —— Task PR2-2 钉死。
4. **重启时别名目标已停用**：期望该别名显式 404（带上下文），老裸名客户端与其他模型完全不受影响 —— Task PR2-3 钉死。
5. **UI 编辑期间外部手改 YAML**：期望 409 冲突提示刷新，不覆盖他人修改 —— Task 2 钉死。

---

## PR 1：云提供方原地编辑（branch `feat/cloud-provider-edit`）

### Task 1: 配置指纹、显示名字段、Key 来源判定

**Files:**
- Modify: `inferfabric/cloud_discovery.py`（`ProviderConfig`、`SecretsManager`、`CloudDiscovery`）
- Modify: `inferfabric/proxy/handler.py`（`_handle_cloud_providers` GET 分支）
- Test: `tests/unit/config/test_cloud_discovery.py`（沿用该文件既有 tmp YAML fixture 风格）

**Interfaces:**
- Produces: `SecretsManager.has(var: str) -> bool`；`CloudDiscovery.config_fingerprint() -> str`（文件字节 sha256 前 16 位，文件缺失返回 `"none"`）；`ProviderConfig.display_name: str = ""`；GET providers 每项新增 `display_name`、`config_fingerprint`、`key_source`（`"managed"`=secrets.env 有该 var｜`"external"`=仅 os.environ 有｜`"unset"`）。

- [ ] **Step 1: 失败测试** —— `test_display_name_roundtrip_yaml`（写 YAML→load→GET 可见）；`test_config_fingerprint_changes_on_save`（save 前后指纹不同、相同内容指纹稳定）；`test_key_source_external_vs_managed`（secrets.env 有→managed；仅环境变量→external）。
- [ ] **Step 2:** `pytest tests/unit/config/test_cloud_discovery.py -v` 确认 3 例 FAIL。
- [ ] **Step 3:** 实现三处接口（fingerprint 在 `_load_config` 与 `save_config` 末尾各自刷新缓存值；`_serialize_providers` 仅在 display_name 非空时写 `display_name` 键）。
- [ ] **Step 4:** 同命令 PASS；跑 `pytest tests/unit/proxy/test_cloud_probe.py tests/unit/config/test_cloud_curation.py -v` 确认无回归。
- [ ] **Step 5:** Commit `feat(cloud): provider config fingerprint + display_name + key_source`。

### Task 2: `POST /admin/cloud/provider-update`（非凭证字段 + CAS）

**Files:**
- Modify: `inferfabric/proxy/handler.py`（新端点，注册进 admin 路由表，`_admin` 包裹）
- Test: `tests/unit/proxy/test_cloud_provider_update.py`（新建；HTTP 层测试沿用 `tests/unit/test_connect_admin.py` 的 handler 调用风格）

**Interfaces:**
- Consumes: Task 1 的 `config_fingerprint`。
- Produces: body `{name, expected_config_fingerprint, display_name?, timeout?, discovery_enabled?, discovery_interval?, include_pattern?}`（缺省=不改）；`404 {error: provider_not_found}`；`409 {error: config_conflict, config_fingerprint}`；`500 {error: save_failed, detail}`（内存与磁盘均已回滚）；`200 {status: updated, config_fingerprint}`。启用/停用（`enabled`）不在本任务。

- [ ] **Step 1: 失败测试** —— `test_update_display_name_only_succeeds`；`test_update_fingerprint_conflict_409`（过期指纹→409 且响应带当前指纹）；`test_update_save_failure_restores_memory`（monkeypatch `save_config` 抛错→500，且 GET 显示旧值、重载 YAML 后旧值不变）；`test_update_unknown_provider_404`。
- [ ] **Step 2:** 确认 FAIL（404 路由不存在）。
- [ ] **Step 3:** 实现：读 body→校验指纹→内存 cfg 上改目标字段→`save_config()`；save 抛错时用编辑前 `copy.deepcopy` 的 cfg 恢复内存并重抛为 500。不改 `_handle_cloud_reload`。
- [ ] **Step 4:** PASS + 该文件全量绿。
- [ ] **Step 5:** Commit `feat(cloud): in-place provider update (non-credential fields, fingerprint CAS)`。

### Task 3: 凭证/端点变更门禁（key_action + 服务端真实探针）

**Files:**
- Modify: `inferfabric/proxy/handler.py`（provider-update 的凭证分支）
- Modify: `inferfabric/proxy/responses_bridge.py` 仅当现有探针 helper 不支持 anthropic 口径时，补 `x-api-key` 探针函数（不触碰转发路径）
- Test: `tests/unit/proxy/test_cloud_provider_update.py` 追加

**Interfaces:**
- Consumes: `/admin/cloud/test` 既有探测能力；`forward_to_cloud` 的 header 口径（openai=`Authorization: Bearer`，anthropic=`x-api-key`）。
- Produces: body 扩展 `api_key?`（非空=换 Key）、`key_action`（`keep` 默认｜`replace`｜`none`）、`probe_model?`。规则：任一协议 base 的 scheme+host+port 变化且 `key_action=="keep"` → `400 {error: destination_key_unconfirmed}`；`replace` 需 `api_key` 非空；`none` 置空凭证。api_key 或任一 base 变更时：对每个「仍配置了 base 且 provider 处于启用态」的协议，用**拟应用值**发 1-token 真实生成探针（探针目标 = `probe_model` 或该 provider 首个 `enabled_models` 条目；都没有 → `400 {error: probe_target_missing}`；探针超时 60s）；任一失败 → `400 {error: probe_failed, protocol, detail}`，零写入。成功才：`replace` 时 `secrets.write` + 内存换新值，随后 `save_config()`。

- [ ] **Step 1: 失败测试**（本地 HTTP mock 上游记录收到的 header）—— `test_replace_key_probe_pass_persists`（新 Key 后新请求带新 Bearer）；`test_wrong_key_probe_fail_no_write`（磁盘与内存均旧值）；`test_host_change_keep_key_rejected_before_network`（mock 收到任何请求即 fail 测试）；`test_host_change_replace_sends_new_key_to_new_host_only`（旧 host 收不到新 Key）；`test_anthropic_probe_uses_x_api_key`。
- [ ] **Step 2:** 确认 FAIL。
- [ ] **Step 3:** 实现探针门禁（在改动 cfg 前用临时 `ProviderConfig` 副本探测；全部通过才落盘）。外部 Key（`key_source=="external"`）+ 换域 + `keep` → 同样 `destination_key_unconfirmed`。
- [ ] **Step 4:** PASS + 回归 `pytest tests/unit/proxy -v`。
- [ ] **Step 5:** Commit `feat(cloud): credential/endpoint edit gated by real upstream probe`。

### Task 4: 停用影响预览 + enabled 切换

**Files:**
- Modify: `inferfabric/proxy/handler.py`（`GET /admin/cloud/provider-impact?name=`；provider-update 接受 `enabled`）
- Test: `tests/unit/proxy/test_cloud_provider_update.py` 追加

**Interfaces:**
- Produces: impact 响应 `{models: [{model_id, pools: [pool_name…]}], routable_count}`（pools 从 model_pool registry 快照的候选里按 provider 归并）。`enabled=false` 走 Task 2 路径（无探针）；启用同理。

- [ ] **Step 1: 失败测试** —— `test_impact_lists_pools_referencing_provider`（构造池候选引用→响应含池名）；`test_disable_no_probe_required`；`test_disable_persists_and_unregisters`。
- [ ] **Step 2:** FAIL → **Step 3:** 实现（enabled 变更走 CAS + save，不触探针）→ **Step 4:** PASS → **Step 5:** Commit `feat(cloud): provider impact preview + enabled toggle`。

### Task 5: Dashboard 编辑入口

**Files:**
- Modify: `inferfabric/dashboard/js/cloud.js`（行内「编辑」按钮 + 模态：显示名/Key（placeholder 留空不改）/双 base/启用 + 「高级」折叠发现参数；提交 `provider-update`；409 横幅提示刷新；`probe_failed`/`destination_key_unconfirmed` 显示后端 detail）
- Modify: `inferfabric/dashboard/fragments/cloud.html` 仅当需要模态骨架
- Test: `tests/unit/dashboard/test_cloud_edit_attrs.py`（新建，沿用 `test_dashboard_cloud_attrs.py` 断言风格）

- [ ] **Step 1: 失败测试** —— js 含 `data-action="provider-edit"`、提交走 `/admin/cloud/provider-update`、`key_source=="external"` 时 Key 输入禁用并标注外部管理。
- [ ] **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS + `pytest tests/unit/dashboard -v` → **Step 5:** Commit `feat(dashboard): provider edit modal with probe gate feedback`。

### Task 6: 双端验收 + 收尾

- [ ] 按 spec §4「PR 1」行执行本端（Mac）验收：隔离网关 + 本地 mock 上游，记录 SHA/用例/结果；README 版本历史 + `__init__.py` → 7.12.9；全量 `pytest tests/unit tests/integration -q`；推送分支开 PR（按 `steering/dual-machine-collaboration.md` §4.2 握手，@ Ubuntu 接收人）。

---

## PR 2：同名模型区分与调用别名（branch `feat/cloud-model-alias`，基于 PR1 合并后的 main）

### Task 1: 别名存储与加载

**Files:**
- Modify: `inferfabric/cloud_discovery.py`（`ProviderConfig.aliases: dict[str, str]`；`_load_config`/`_serialize_providers`；`CloudDiscovery.alias_target(alias) -> tuple[str, str] | None`）
- Test: `tests/unit/config/test_cloud_alias.py`（新建）

**Interfaces:**
- Produces: YAML provider 段 `aliases: {别名: 上游 model_id}`；非字符串/空值条目 → 解析抛错走既有 corrupt 保注册表路径；`alias_target` 从内存 providers 构建（save/load 后刷新）。**删除 provider 时**其 aliases 移入 YAML 顶层 `retired_aliases: {provider: {alias: model_id}}`（未激活、不可路由、不列出）；重建同名 provider 不读 retired_aliases（不自动复活）；`provider-alias` 端点对同名 (alias, model_id) 显式设置时从 retired 移除。

- [ ] **Step 1: 失败测试** —— `test_alias_roundtrip_yaml`；`test_alias_invalid_entry_rejected_preserves_registry`；`test_alias_target_lookup`；`test_provider_delete_retires_aliases_no_auto_reactivate`（DELETE 后 GET 无此别名；重建同名 provider 后仍不可路由；显式重设后激活且 retired 清空）。
- [ ] **Step 2:** FAIL → **Step 3:** 实现（retire 逻辑放 `handler.py` 的 DELETE 分支调用的 `CloudDiscovery.retire_provider_aliases(name)`）→ **Step 4:** PASS → **Step 5:** Commit `feat(cloud): per-provider model alias storage + retire-on-delete`。

### Task 2: 别名校验 + `POST /admin/cloud/provider-alias`

**Files:**
- Modify: `inferfabric/cloud_discovery.py`（`validate_alias(alias, provider, model_id, *, local_names, registry_short_names, other_aliases) -> tuple[bool, str]`）
- Modify: `inferfabric/proxy/handler.py`（新端点，指纹 CAS 同 Task 2/PR1）
- Test: `tests/unit/config/test_cloud_alias.py` 追加

**Interfaces:**
- Produces: 规则——非空、不含 `/` 与空白、不匹配 `^[A-Za-z0-9._:@-]+$` 拒绝；保留前缀 `fabric-` 拒绝；与本地名/池逻辑名/任一云短名/其他别名撞名拒绝。body `{provider, model_id, alias, expected_config_fingerprint}`（`alias:""` =移除）；`model_id` 必须在该 provider 可路由 floor（enabled_models∪specs）内否则 `400 {error: model_not_routable}`；`409`/`200` 同 PR1 语义。

- [ ] **Step 1: 失败测试** —— `test_alias_conflict_local_name`；`test_alias_conflict_reserved_fabric_prefix`；`test_alias_conflict_existing_short_name`；`test_alias_slash_rejected`；`test_alias_remove_via_empty`；`test_alias_fingerprint_conflict_409`。
- [ ] **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS → **Step 5:** Commit `feat(cloud): alias validation + admin endpoint`。

### Task 3: 路由解析（别名优先、不回退）+ `/v1/models`

**Files:**
- Modify: `inferfabric/cloud_discovery.py`（`resolve_route` 别名命中先于短名查表；`resolve_cloud_model` 识别别名；`explain_unknown(model_name) -> str | None` 返回别名未激活上下文）
- Modify: `inferfabric/proxy/handler.py`、`proxy/chat_handlers.py`、`proxy/responses_bridge.py` 仅在既有「未知模型」错误分支接 `explain_unknown`；`_handle_v1_models` 追加激活别名条目 `{id, object: model, owned_by: iff-alias}`
- Test: `tests/unit/model_pool/test_v1_models.py` 回归 + `tests/unit/proxy/test_cloud_alias_routing.py`（新建）

**Interfaces:**
- Consumes: Task 1 `alias_target`。
- Produces: 调用别名 → 命中目标 provider 的 CloudModel，上游收到原始 `model_id`（`forward_to_cloud` 既有 override，零改动）；目标不可用（停用/撤销/无该协议）→ 显式失败（404 + `explain_unknown` 上下文），绝不落到本地/另一 provider/池。

- [ ] **Step 1: 失败测试**（两个同 model_id 的本地 mock 上游）—— `test_alias_resolves_to_target_provider`（别名 `glm-5-b` → B 收到请求且 body model=`glm-5`）；`test_alias_inactive_no_fallback`（B 停用→404 且错误含别名上下文，A 零请求）；`test_v1_models_lists_active_alias_only`；`test_bare_name_and_pool_behavior_unchanged`（旧回归断言）。
- [ ] **Step 2:** FAIL → **Step 3:** 实现（`resolve_route` 内别名命中：目标激活→返回 `cloud:P`；未激活→返回 None，但 `explain_unknown` 可解释）→ **Step 4:** PASS + `pytest tests/unit/model_pool tests/unit/proxy -q` → **Step 5:** Commit `feat(routing): explicit model alias resolution without fallback`。

### Task 4: Dashboard 别名入口

**Files:**
- Modify: `inferfabric/dashboard/js/cloud.js`（模型策展面板行加「别名」动作：输入+保存走 `provider-alias`，展示冲突原因；已设别名的行显示别名徽标 + 复制调用片段用别名）
- Test: `tests/unit/dashboard/test_cloud_alias_attrs.py`（新建，attrs 风格）

- [ ] **Step 1:** 失败测试 → **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS → **Step 5:** Commit `feat(dashboard): model alias editing + copy snippet`。

### Task 5: 双端验收 + 收尾

- [ ] 按 spec §4「PR 2」行执行本端验收（同名双 mock 上游、双协议、含 `/` 的 model_id、重启/重载/再发现）；README + 版本 → 7.12.10；全量回归；PR 握手同 PR1。
