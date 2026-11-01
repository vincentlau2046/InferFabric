# PR-26 云模型注册表 floor 语义 — spec

**日期：** 2026-11-01　**版本：** 7.9.1（补丁位，自主）
**级别：** P0（生产事故修复，用户提出，不能影响生产）

## 背景与事故（生产日志实证，2026-11-01）

用户报告：「模型注册会无缘无故不见，通过模型发现又可以添加回来」。

生产时间线（journalctl --user -u inferfabric-proxy）：

- 12:18:46 生产 proxy 重启（7.9.0 上线）。
- `tathr-glm` provider = `model_specs: []` + `enabled_models: [glm-5]` —
  glm-5 是**纯白名单模型**，可路由性完全依赖每次成功的上游 `/models` 抓取。
- 13:38:56 整点轮询：tathr-glm `/models` 请求挂死，13:49:02 以 600s SSL
  handshake timeout 失败 → `_discover_provider` 返回 `[]`（「抓取失败」与
  「上游 0 模型」不可区分）。
- 13:49:02 `discover_all()` **全量替换**注册表 → glm-5 被静默逐出（唯一的全空
  保护只覆盖「所有 provider 全失败」，merged 非空不触发）。
- 此后所有 glm-5 请求 → 404 `Unknown model: glm-5`（客户端渲染为
  "There's an issue with the selected model (glm-5)"）。
- 14:49:03 用户手点「模型发现」→ 上游恢复 → glm-5 回归。

**根因（一行）**：`discover_all()` 每轮全量重建 =（新鲜 /models ∩ 白名单）∪
specs，单一 provider 抓取失败被当作「该 provider 0 模型」，把用户显式勾选的
模型静默逐出。

连带缺陷（同文件，均已核实）：

1. `__init__` 只注册 spec 模型 → 重启后白名单模型存在最长 ~1h 的 404 空窗。
2. `_rebuild_provider_registry` 依赖未持久化的 `_candidates` 快照 → 重启后
   任何策展动作（enable/disable/add/remove/tag）静默丢弃该 provider 的
   白名单模型。
3. `reload()` 重设 `_providers` 但不协调注册表（已删/禁 provider 残留、新
   provider 不注册）；且 SIGHUP 路径只调 `reload()`，`stop_polling()` 后
   无人重启轮询线程 → SIGHUP 后轮询永久停摆。

## 用户裁决（2026-11-01）

1. **语义 = 保留（白名单赢）**：成功抓取但 `/models` 缺失的白名单模型
   **不逐出**。上游真删 ≠ 网关静默消失；失败以上游真实错误呈现，可见可
   归因。移除的唯一途径 = **显式用户动作**：取消勾选 / 移除 / 删 provider /
   禁用 provider。
2. **紧急缓解 = 执行（部署前立即做）**：对现网执行
   `POST /admin/cloud/provider-models {"provider":"tathr-glm","action":"add","model":"glm-5"}`
   → glm-5 成为 spec，在 7.9.0 现网代码下对轮询失败与启动窗口同时免疫。
   已于 15:17:29 执行（覆盖 15:38:56 轮询）。可逆（action: remove）。

## 核心不变量（floor 语义）

对每个 enabled provider P：discover_all / reload / `__init__` 之后，注册表
中 P 的条目**恰好 = floor(P) = specs(P) ∪ whitelist(P)**。上游 `/models`
只**丰富元数据 + 刷新候选快照**，从不缩减 floor。

floor 模型元数据三级回退：① 新鲜抓取条目（spec 已合并）→ ② 旧注册表条目
（保旧元数据，如 `discovered_at>0`）→ ③ 裸 `CloudModel`（`discovered_at=0.0`）。

## 实现（全部在 `inferfabric/cloud_discovery.py`，转发核心路径不动 — PR-14）

| 改动 | 说明 |
|---|---|
| `_discover_provider` 三态化 | `None` = 抓取失败（HTTPError/URLError/超时/JSON 解析失败）；`list`（可空）= 成功；`not cfg.openai_base` 保持 `[]`（永久配置状态，非瞬时失败） |
| `discover_all` 严格 floor | 锁内快照 `old` → 网络抓取锁外 → 逐 provider：`None` = 零贡献 + WARNING（含保留数）；成功 = 既有白名单过滤 + spec 合并 + 双键（短键 first-wins）→ 循环后 `_register_spec_only_models` + `_register_whitelist_models(merged, old=old)` → 锁内整体替换；既有全空保护原位保留（floor 后仅在完全无 floor 时触发，更窄） |
| `_register_whitelist_models` + `_bare_whitelist_model`（新增） | 镜像 spec 路径：enabled provider 的每个 `enabled_models` mid 不在 merged 时按 旧条目→裸 三级回退注册（含 `_merge_model_spec` 同名 spec 并入 + 双键）；**键用原始 mid**（与发现路径 `m.model_id` 一致）；短名碰撞时短键归配置序首 provider，被碰方仍注册自己的前缀键（与发现路径 :334-336 一致） |
| `__init__` | spec 注册之后追加 `_register_whitelist_models` — 启动 t=0 完整，消灭重启空窗 |
| `_rebuild_provider_registry` | 去 `_candidates` 依赖：清空前捕获旧条目（`v.provider==provider and v.model_id==mid` 匹配，防跨 provider 同 mid 误采）→ candidates∩whitelist 循环保留 → 白名单 floor 兜底；策展方法既有 RLock 内执行 |
| `reload()` | `_load_config` 后锁内：清 `_candidates`/`_known_absent` 已删/禁 provider 条目；**严格 per-provider floor 调和**（保留 `v.model_id in cfg.enabled_models or v.model_id in spec_ids or k in cfg.model_specs`，其余丢弃 — YAML 取消勾选/删 spec 于 reload 即时生效，安全加固 F-1）→ 重跑 spec+whitelist floor → 末尾幂等 `start_polling()`（SIGHUP 轮询自愈；handler.py:1648 显式调用变 no-op） |
| 可观测性 | 抓取失败触发 floor 保留时 WARNING（provider 名 + 保留数）；白名单 mid 缺席成功 `/models` 时按**状态迁移** WARNING 一次（"kept routable — whitelist wins; uncheck in panel to remove"，`_known_absent` 驱动，模型回归即清，不整点刷屏） |

**安全加固**（自动化评审 2×MEDIUM，commit 612a47f，各配 RED→GREEN）：

- **F-1 reload 严格 floor**：reload 时 per-provider 只保留当前 floor 声明的
  条目 — 堵「YAML 取消勾选后要等下一轮轮询才生效」的授权残留窗口。
- **F-2 发布临界区对账**：`discover_all` 发布前在 `_models_lock` 内按
  **当前** `_providers` 过滤 merged 并重跑 floor — 堵「抓取期间（最长
  600s）并发 reload 删/禁 provider，旧结果发布复活已禁 provider 条目」的
  双向竞态。全空保护细化：providers 空（reload 过渡态）→ 保旧；providers
  非空但 floor 空 → 诚实发布空注册表。

**cross-review 修复**（fresh-context subagent 全 diff 评审，verdict =
fix-then-ship，以下各配 RED→GREEN）：

- **I-1 corrupt reload 保注册表**：`reload()` 先清 `_providers` 再载入 —
  YAML 解析失败时 providers 保持空，612a47f 的严格 floor 调和会把注册表
  清成 {}（比事故前更糟：corrupt SIGHUP 全部云模型 404 且轮询无法重启）。
  修复：`_config_corrupt or not _providers` 时跳过调和、保留旧注册表
  （与 `discover_all` 的瞬态守卫对齐）。
- **I-2 发布 per-model 对账**（F-2 的 per-model 残留）：抓取按 OLD 白名单
  过滤，期间用户取消勾选 + reload 完成后，provider 级过滤不足以拦截 —
  显式撤销延迟一轮轮询（≤1h）生效。修复：发布过滤扩展为 per-model
  floor 谓词（`enabled_models ∪ spec_ids ∪ spec 键`，与 reload 同式）。
- **M-3**：无 openai_base（anthropic-only）provider 不抓取 /models，其
  白名单模型缺席是永久配置状态 — 跳过「absent from /models」误导性告警。
- **M-4**：reload 时 `_known_absent` 随白名单缩减/禁用 provider 收敛 —
  否则取消勾选再重新勾选后新一轮缺席转态被陈旧集合吞掉。

## 明确不做

- 无 dashboard 改动（whitelisted-but-not-in-snapshot 的面板展示 = 后续小项）。
- 不改 `ensure_cloud_discovered` 锁存（防重试风暴）。
- 转发核心路径（chat_handlers/forwarder）不加防御 — PR-14 边界外。
- 不自动逐出已死上游模型；Qianfan-Token 常态 404 是上游端点问题（修复后
  其模型全为 spec，抓取失败无害且不再刷屏）。

## 已知残留（记录在案，不修）

- `stop_polling()` join(10s) 超时可致旧轮询线程滞留 → 短暂双轮询（自限，
  两线程收敛同一 floor；加固 = 线程代际 token，follow-up）。
- 含 `/` 的模型 id（如 kimi/k3）：floor 用原始 mid 作键，既有非对称行为
  原样保留。
- `discover_all` 在锁外迭代 `self._providers.items()`，与 `drop_provider`
  /handler 添加路径的原地变更并发时可抛 `RuntimeError: dictionary changed
  size during iteration`（既有问题，非本 PR 引入；发布对账保证结果安全，
  旧注册表保留）。下次触碰该文件时改为锁内快照迭代。

## 部署后验证（零成本）

1. journalctl 启动行见 floor 注册。
2. `curl -s 127.0.0.1:8999/v1/models` 含 glm-5。
3. 下一整点轮询（或手动 `POST /admin/cloud/discover`）后仍在；glm-5 请求
   200。
4. 日志仅见「kept routable」迁移告警、无整点刷屏。
5. 可选一次性运维：`action: remove` 恢复白名单为单一事实源（保留 spec 则
   「取消勾选」不会移除 glm-5）。
