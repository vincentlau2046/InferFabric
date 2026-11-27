# Dashboard UX v3 实施计划（A–F）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement an approved phase task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将控制台从 10 项模块导航重排为状态、请求、用量、路由、模型、配置六区，保留现有操作与本机访问方式。

**Architecture:** 继续由 Python 网关直出页面，沿用 fragment/JS 占位装配、原生 JS/CSS 与本地 ECharts。新增 hash 路由，保留快照 3s + ETag；请求日志和用量统计按需读取，不加写侧采集。

**Tech Stack:** Python 3.10+、SQLite、vanilla JS/CSS、vendored ECharts、pytest、Node VM；关键动线用浏览器在隔离实例验收。

**Spec:** [定稿设计](../../../specs/dashboard-ux-v3/design.md) §3–§7、[接口与 UI 对接契约](../../../specs/dashboard-ux-v3/interface-contract.md)、[交互样机](../../../specs/dashboard-ux-v3/preview.html)（示例数据不代表后端已有字段；遥测语义以契约为准）。本文件为实施计划，**未开始实现**。事实底账：`main` @ `03cabb1`。

## 全局约束

1. **PR-14**：不修改 `inferfabric/proxy/chat_handlers.py`、`inferfabric/forwarder.py`、`inferfabric/proxy/response_cache.py`，也不绕道修改模型池转发、日志写入或埋点。定稿 §5 允许的后端读侧文件为 `inferfabric/proxy/handler.py`、`inferfabric/monitor_report.py`、`inferfabric/dashboard/__init__.py`；B1 的额外文件是**待裁决提案**，不是现行白名单。
2. **零构建**：不加前端框架、依赖、外链脚本或 WebSocket，不放宽 CSP，不改变本机免登录和既有授权规则。凭证只写 `secrets.env`（600）；Key 明文不进入 GET 响应、YAML、日志、错误或测试快照。日志展示不透出上游错误体、请求原文或完整 metadata；新可选字段走白名单，不把任意字符串插入 HTML。
3. **轮询收敛**：`/api/snapshot` 3s + ETag 保留；能力包页可见时才以 3s 节奏查独立状态，离开即停；向导仅运行中 2s，关闭即停；请求 Live 仅在日志子页可见且开启时查，至多一个在途请求；用量仅首次进入及时间范围变更时刷新。旧响应不得覆盖新筛选/页面状态。
4. **视觉不换主题**：暖瓷白 `#f5f3ee`、暖炭黑 `#16130e`；accent `#b45a3c`（light）/ `#d98e6e`（dark）；4 枚状态色与固定图表系列色不混用。F 新增 on-accent 令牌，实际前景/背景对比度达 WCAG AA 4.5:1。术语用「对账／部署／自动切换」，保留 GPU、VRAM、KV Cache、TTFT、TPOT、tokens。
5. **每期门禁**：从最新 main 拉独立分支、发 Draft PR 并完成两端开工回执，再 sandbox-first 实施；每期一个 patch + README 版本行（以开工时的 main 为准），不预占号；pytest 全套 unit + integration、import smoke、隔离网关及受影响页面 smoke、**真实浏览器交互走查**（各期 task 项的浏览器验收为门禁组成部分，非可选）、fresh-context subagent cross-review（不绑定模型、不经云路由 —— 2026-10-25 用户裁决），按[双机协同 §4](../../../steering/dual-machine-collaboration.md#4-评审与验证)在同一 SHA 签收后才合并，禁止直推 main。**C 必须实测 Mac 的统一内存、压力与预算，以及 vince-pc 的 NVIDIA 显存、利用率、功耗、温度和时钟**。
6. **验证隔离**：Mac 用主仓 `.venv/bin/python` 的绝对路径，但 CWD 为当前 sandbox；下文 `$PY` 表示对应平台已核实的 Python。每期先跑相关基线，新增用例 RED → GREEN 后再提交；全套命令为 `"$PY" -m pytest tests/unit/ tests/integration/ -q`，import smoke 为 `"$PY" -c 'import inferfabric'`。运行前核对隔离 HOME/数据目录、独立端口和目标实例身份；真机窗口另获许可，不默认跑 operational、触碰生产 :8999 或切 VL。长验证监看成功／失败／超时三种终态，收尾核对进程、端口、显存归还；证据只记录脱敏结论，不贴 Key、原始请求或私有日志。

## 评审重点

| 容易漏掉的情况 | 验收归属 |
|---|---|
| 旧收藏/本地存储、浏览器前进后退与切模型时的导航锁；旧部署 tab 也能迁到能力包 | A1 |
| wizard 占位符嵌在 packs 内；配置并入后仍能加载各面板、向导和既有云别名操作 | A2、E2 |
| 真实日志远多于 500 条；过滤后分页总数准确，旧库或损坏摘要不坏整页、不透原文 | B1 |
| 用户快速切过滤或切页，旧响应晚到；关 Live 不停全站健康快照 | B2、D2 |
| Mac 统一内存与推理预算不同；部分采样缺值、读数过期与真 `0` 不混用；未知成本、用量缺失不显示成 `0` | C1、C2、D1、D2 |

## A — 六区导航与存量迁移

**交付：** 六区均可达，原有功能仍能操作；本期不重写日志、状态、用量业务逻辑。

### A1：路由与兼容

**文件**：新增 `inferfabric/dashboard/js/router.js`、`tests/unit/dashboard/test_router.py`；修改 `inferfabric/dashboard/js/store.js`、`inferfabric/dashboard/base.html`、`inferfabric/dashboard/__init__.py`（A1 即注册 router，暂以 URL 映射至现有 DOM；A2 更换布局）。

**接口（B/C/E 消费）**：`window.ifRouter.parseHash(hash)` → `{view, sub, params}`；`navigate(view, sub?, params?)` 驱动变更。正式 URL 为 `#/status` / `#/requests` / `#/usage` / `#/routing` / `#/models` / `#/config`；请求子页为 `#/requests/switches`、`#/requests/anomalies`，配置子页为 `#/config/cloud`、`#/config/auth`、`#/config/connect`、`#/config/packs`、`#/config/sys`。保留 `store.switchTab(tabId)` 作为兼容入口，不再负责直接切 DOM。

- [ ] 在 `test_router.py` 写 Node VM 行为断言：有效 hash 优先于存储（包括 URL 过滤参数）；无 hash 从 `iff_view` 恢复，否则迁移旧键，否则 `status`；无效子页回退到区默认，畸形输入/存储不可用不抛错；锁定期间 click、程序调用、hashchange 不绕过既有锁；合规的前进/后退可返回原子页且不循环。
- [ ] `"$PY" -m pytest tests/unit/dashboard/test_router.py -q`：RED（尚无新路由或兼容语义）。
- [ ] 合并 `switchTab` 双定义，保留一套迁移表：`tab-overview→status`、`tab-monitor→requests`、`tab-anomaly→requests/anomalies`、`tab-inference→models`、`tab-model-pools→routing`、`tab-packs` **及** `tab-deploy→config/packs`、`tab-security→config/auth`、`tab-connect→config/connect`、`tab-settings→config/sys`、`tab-cloud→config/cloud`。`iff_view` 只保存 view/sub；成功写新键后清旧键，不存过滤词。
- [ ] 同命令 GREEN；单独提交路由及测试，现有内联调用不丢。

### A2：六区装配与搬运

**文件**：`inferfabric/dashboard/base.html`、`inferfabric/dashboard/__init__.py`、`inferfabric/dashboard/fragments/`、`inferfabric/dashboard/js/`；新增 `tests/unit/dashboard/test_fragment_integrity.py`，更新 `test_dashboard_v2.py`、`test_inference_primary_discipline.py` 等受影响断言。

- [ ] 写装配与导航用例：`test_assembled_dashboard_has_no_unresolved_placeholders` 检查 FRAGMENTS/JS_MODULES 每个注册文件存在且引用唯一，包含经 config → packs → wizard 的嵌套引用；实际装配后无残留占位符、保留唯一挂载 ID；`test_primary_nav_has_six_views` 检查正式顺序及旧结构移除。`"$PY" -m pytest tests/unit/dashboard/test_fragment_integrity.py -q`：RED（旧导航结构不符）。
- [ ] `base.html` 改为 200px 文字侧栏、去分组标题；保留现有主题、确认弹窗、toast 与切换遮罩。六区壳为 `fragments/{status,requests,usage,routing,models,config}.html`；优先复用既有挂载 ID/处理函数，不强制全量 JS 重命名。由 `store.js` 按 view/sub 组合现有 renderer；不允许后注册 renderer 覆盖先注册的同页区块。
- [ ] 按 §3.3 逐项迁移：总览 → 状态；监控的请求日志、切换历史、异常 → 请求三个子页；Token/Agent/延迟 → 用量；实时遥测 → 状态，引擎 KPI → 模型；推理及网关控制暂驻模型；池 → 路由；五个配置 fragment 保留独立装配，A 用临时页内入口，E 定型为二级导航。功耗双轴旧图仅作为用量临时内容，D 在电费 KPI 接通后撤除，避免中间版本丢失电费入口。
- [ ] 能力包和向导按全局约束收敛定时器。保留 `deploy.js` 与 packs 挂载、wizard 唯一部署主入口、云端原有连接及别名操作；验证 `/reload-config` 仍可刷新 dashboard 装配缓存。
- [ ] `"$PY" -m pytest tests/unit/dashboard/ -q`：GREEN；隔离浏览器走六区/刷新/锁定及配置关键入口。逐行迁移矩阵、测试和签收入 PR-A。

## B — 请求页

### B1：过滤、计数及安全摘要（范围提案，先裁决再实现）

**文件**：白名单内 `inferfabric/proxy/handler.py`；**推荐额外批准只读** `inferfabric/db.py`、`inferfabric/telemetry.py`，后者是 handler 实际调用的查询门面。测试用 `tests/unit/proxy/test_handler.py`、`tests/integration/test_request_log_db.py`。

**取舍**：推荐将过滤、计数、分页下推 SQLite，由专用只读查询返回同一读快照中的页和计数。原 `query_request_log` 继续服务 snapshot/monitor-report，不变签名或分页语义。严格只用 §5 白名单的备选，需要在 handler 逐页扫描既有查询再计数；不能拿最近 500 条冒充总数，长窗口 Live 会反复读大量历史。若不批准额外文件，**B1 停在评审**，不默认接受性能退化，也不影响 A 先行。

**建议接口（B2 消费）**：旧 `/api/request_log?limit=&since=` 保持 `logs/count` 与旧默认范围；请求页显式传 `window=1h|24h|7d`（UI 默认 24h，与样机一致）、`status=all|2xx|4xx|5xx`、`model`、`agent`、`q`、`limit`（默认 50、上限 500）、`offset`（非负整数）、`until`（可选 Unix 秒，首屏由服务端取当前时刻；只接受有限且非未来值）。有 `window` 的请求返回 `{logs,count,total,failed_total,until}`；`total` 是分页前同过滤集合的条数，`failed_total` 是该集合的 4xx/5xx 条数；状态用整数区间，模型/客户端使用选中 ID 精确匹配，搜索仅对 req_id/模型/客户端 ID 做有长度上限的字面子串匹配。边界窗 `[since, until)`，同秒按 `timestamp DESC, id DESC`；Live 回第一页，暂停后翻页使用首次响应的固定 `until`，与随后新入库的在途事件不承诺长期历史快照。

- [ ] RED：`test_request_log_filtered_page_counts` 用临时 SQLite 写超过 500 条（含同秒、跨窗口、非 2xx），断言过滤在 LIMIT 前且 `total` 不随页数缩小、无同秒排序漂移；`test_request_log_safe_pool_summary` 覆盖旧参数、非法参数 400 不回显、损坏/旧摘要、无准入及上游错误原文不透出。
- [ ] `"$PY" -m pytest tests/unit/proxy/test_handler.py tests/integration/test_request_log_db.py -q`：RED（新契约断言失败）。
- [ ] GREEN：绑定参数查询；仅新查询模式输出行新增 `req_id` 和 `pool`。`pool` 严格投影为 `{name, route_kind, target_port, retry_count, fallback_count}` 或 null；`route_kind` 从最后一次 admitted 记录的 `pool:…/local:…` 或 `/cloud:…` 识别，重试与回退分别取 `retry_count` / `fallback_count`（`attempts` 是数组，不是次数）。**当前 `pool_summary` 不记录端口**，`target_port` 显示「—」；不得用当前配置反推历史，不加埋点。旧请求、无可靠记录或异常值照样可读；新页面只凭 HTTP 状态与安全分类显示错误，不传原始 error/完整 metadata/成本。
- [ ] 同命令 GREEN；验 7 天大样本下 Live 查询耗时、读取量及 `/api/snapshot` 的回归，记入 PR-B。

### B2：日志工作台

**文件**：`inferfabric/dashboard/fragments/requests.html`、`inferfabric/dashboard/js/requests.js`（由 A 的日志/异常 renderer 接线或迁入），新增 `tests/unit/dashboard/test_requests_panel.py`。

- [ ] RED：Node VM 检查筛选行、URL 参数、红色失败计数、列选择持久化 `iff_req_cols`、分页、池/非池展开、切换历史/异常子页；用慢旧请求验证切筛选或离开不被晚到响应覆盖。
- [ ] `"$PY" -m pytest tests/unit/dashboard/test_requests_panel.py -q`：RED；新增页面消费 B1 契约后同命令 GREEN。异常关联仅按请求模型、同错误状态（若事件可比）与时间邻近推断，标注「按模型/时间推断」；无可靠候选显示「—」。协议列取消。Live 关或离页时停止日志请求但保留全局快照；加载中、无结果、读取失败三态区分。
- [ ] 浏览器验过滤/展开/Live、子页直达、从历史深页回 Live；完成全局门禁，PR-B。

## C — 状态页

### C1：状态与遥测接口

**文件**：`inferfabric/proxy/handler.py`、`api-spec/openapi.yaml`、`api-spec/components/schemas.yaml`、`tests/unit/proxy/test_snapshot_etag.py`、`tests/unit/proxy/test_async_server.py`。字段以[接口与 UI 对接契约](../../../specs/dashboard-ux-v3/interface-contract.md) §C 为准；不改现有采集器和设备控制路径。

- [ ] RED：在 HTTP 和 async 的 snapshot 回归中钉住 `device` 值／能力变化时 ETag 更新、只变时间仍可 `304`；新 `X-IFF-Device-Sample-Age` 同样适用于 `200/304`，不由 3 秒轮询频率反推。`"$PY" -m pytest tests/unit/proxy/test_snapshot_etag.py tests/unit/proxy/test_async_server.py -q`：新增头部断言 RED。
- [ ] GREEN：复用已缓存的 `device.snapshot/capabilities`；增加采样年龄头，不加额外采集；OpenAPI 补响应 schema、头及样例，旧字段值不改。重跑同命令 GREEN。

### C2：状态 UI 与双机核对

**文件**：`inferfabric/dashboard/fragments/status.html`、`inferfabric/dashboard/js/{store,overview,inference}.js`（若 A 已拆分则用对应 renderer）；新增 `tests/unit/dashboard/test_status_panel.py`。

- [ ] RED：用契约样本验 Apple 统一内存总量/压力/预算、NVIDIA 五项、真实 `0`、部分/全部缺值、旧响应、`304`、过期和离线；另验运行／空闲／切换中、空历史、`200` 业务失败及部分重载。`"$PY" -m pytest tests/unit/dashboard/test_status_panel.py -q`：RED。
- [ ] GREEN：UI 按能力和实际值显示，不按 OS 名写死版式；版本/连接与硬件读数独立，不显示不准确的网关运行时长。态势计数按契约的 UTC 当天及已保留警告，最近五条计数不冒充总数；可用快捷模型与维护动作复用原操作和严格结果判定。重跑同命令 GREEN。
- [ ] 在 Mac 隔离实例对照 `sysctl`/`vm_stat`，在 vince-pc 获准窗口对照 `nvidia-smi`；脱敏双端证据、真实终态及清退记入 PR-C。真实 GPU 窗口未授权不算验完。

## D — 用量页

### D1：同快照聚合

**文件**：`inferfabric/monitor_report.py`、`tests/unit/proxy/test_monitor_report_api.py`。**接口（D2 消费）**：同一 `/api/monitor-report?window=1h|24h|7d|30d&scope=all` 增量输出：`totals` 与 `models[]` 补 `tokens_in_cached/cache_share`，模型补 `cost_estimate`；`agents[]` 按既有客户端 ID（保留未识别分组）汇总；`series[]` 增加按 local/cloud/unknown 的请求、tokens、成本/cache 统计，保持既有字段及桶边界，供趋势图与 KPI sparkline 共用。

- [ ] RED：多模型、同名不同厂商、不同 token 权重、未识别客户端、未配价、空窗口样例；断言图桶/表/总量同区间，`cache_share=Σcached/Σinput`（分母无数据则无值），不把未知价格或未知 route 归作免费或本地。
- [ ] `"$PY" -m pytest tests/unit/proxy/test_monitor_report_api.py -q`：RED。GREEN：按既有键集扫描一次，复用 `cost_of_row`/identity 规则及覆盖标记，不在 snapshot 轮询中跑长聚合。不用 `/api/agent-stats?window=…` 拼报告（该 API 当前只接受 granularity，窗口不等价）。同命令 GREEN；增加报告兼容与扫描上限断言。

### D2：一个窗口驱动整页

**文件**：`inferfabric/dashboard/fragments/usage.html`、`inferfabric/dashboard/js/usage.js`，复用 `charts.js`；新增 `tests/unit/dashboard/test_usage_panel.py`。

- [ ] RED：窗口变更后 KPI、sparkline、堆叠图和按模型/按客户端分解一次成组换新；旧窗口响应晚到无效；模型分解展示份额、tokens、请求、成本、延迟 p50/p95；保留客户端认领路径。`"$PY" -m pytest tests/unit/dashboard/test_usage_panel.py -q`：RED。
- [ ] GREEN：报告驱动请求/Token/估算成本/KV 前缀缓存命中 token 份额四个 KPI；长窗口用报告桶合成日图，短窗口保留可读时间粒度，本地/云用指定蓝/琥珀；unknown 另作覆盖说明。电费从 `/api/device_power` 取有覆盖的聚合区间（与所选窗口核对），单列云费和电费来源及缺测，覆盖不齐不伪造完整合计；上线时撤掉临时功耗双轴。响应缓存命中率不混入 KV 命中指标。
- [ ] 同命令 GREEN；浏览器验四档窗口、快速切换、认领、无价格/功耗数据的状态，PR-D。

## E — 路由、模型与配置

### E1：池与链路树

**文件**：`inferfabric/dashboard/fragments/routing.html`、`inferfabric/dashboard/js/model-pools.js`、`tests/unit/dashboard/test_model_pools_panel.py`。

- [ ] RED：四角色固定顺序；五态灯、策略/角色、启用开关；候选的优先序、local/cloud、协议/视觉标、在役与延迟、行内调序/移除，编辑器/添加、revision、dry-run 和链路树可达。`"$PY" -m pytest tests/unit/dashboard/test_model_pools_panel.py -q`：RED。
- [ ] GREEN：只改呈现与接线，复用既有 CAS/409、保存、预演行为；旧 revision 冲突后不盲重试覆盖。重复同命令 GREEN。隔离 mock 上游验调序及多个客户端读取同一新 revision，不发真实云推理。

### E2：模型列表与配置二级导航

**文件**：`inferfabric/dashboard/fragments/{models,config,settings,cloud}.html`（延用 A 的实际子 fragment 命名）、`inferfabric/dashboard/js/{inference,cloud,store,router}.js`；更新现有 dashboard tests，新增 `tests/unit/dashboard/test_config_routes.py`。

- [ ] RED：本地独占/共享两组、运行控制与仅运行中显示的引擎指标；云端按厂商分组、相同模型 ID 不跨厂商串状态；本地/云均可见「在池」与优先级。五个配置子页刷新复原；窄屏横滚 chips；模型页部署跳 `#/config/packs`，云连接/密钥跳 `#/config/cloud`；系统显示 LRU 缓存、自动切换及**只读限流**，旧位置无重复操作。
- [ ] `"$PY" -m pytest tests/unit/dashboard/ -q`：RED。GREEN：按既有候选类型/模型/厂商身份 join 池配置，云可路由开关复用现有 curation 授权与写端点，别名/连接/密钥管理仍留云端配置；限流不引入新写入口。能力包保持唯一向导主入口、不展开旧表单；关于卡含版本/rev/OpenAPI。
- [ ] 同命令 GREEN；隔离浏览器跑 `tests/interactive/test_model_pools_browser.py`、`tests/interactive/test_cloud_caps_browser.py` 受影响用例，另验向导完整动线。PR-E。

## F — 视觉与可达性

### F1：组件、颜色与术语

**文件**：`inferfabric/dashboard/fragments/style.css`、`inferfabric/dashboard/base.html` 与受影响页面文案；更新 `tests/unit/dashboard/test_dashboard_contrast.py` 和相关 DOM/CSS 断言。

- [ ] RED：解析最终 token/选择器的实际前景背景，覆盖两主题主按钮、选中态、`::selection`，不是断言一个写死的比值；另验 200↔56px 折叠记忆、键盘焦点、40px 表格行高、数字列对齐、空态以及用户所见中文术语。
- [ ] `"$PY" -m pytest tests/unit/dashboard/ -q`：RED。GREEN：保留 accent 本色，dark `--accent-ink:#241309`（配 `#d98e6e` 实算 6.86:1）；light 保持 `#fff` 配 `#b45a3c`（4.70:1；样机的 `#fff7f2` 在此背景仅 4.44:1，不照搬，可用于达标的深 accent 背景）。统一卡片标题/说明/动作及 1px 边框、10px 圆角，状态 pill 统一，用文字区分而非只依赖颜色；不因替换界面中文改内部类型/历史 API 值。
- [ ] 同命令 GREEN。浏览器用亮暗两主题、键盘、窄屏复核六区，检查 tooltip/弹窗/空态/主题对比度及受影响交互测试。完成全局门禁，PR-F；回填 STATUS 与验收记录。

## 实施顺序与审阅重点

- 按 **A → B → C → D → E → F** 合并；A 是后续页面的共同底座，C 的「查看全部」消费 B 的请求过滤，F 在六区完成后统一打磨。各期在合并边界保持可用；如需回退已被后续阶段消费的接口，先核对依赖，不能承诺任意单期无影响回滚。
- 审阅时重点看 B1 是否允许两个额外文件的**只读**改动、历史端口缺失时的降级，以及 C 的双机验收是否有窗口。三个事项未获许可不默认通过；计划 PR 不启动实现、验收或生产重启。
- 推荐按期由同一主责端执行、每期一次独立复审；不是六期并行改同一工作树。计划获准后先发 PR-A 草稿并完成双端握手。
