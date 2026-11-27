# Dashboard UX v3 — 接口与 UI 对接契约

状态：2026-11-27，用户确认「后端按平台采集、前端按能力展示；本期不新增 Mac GPU 探针」。本文随计划 PR 审阅，不代表应用已实现或完成双机验收。配套：[设计](design.md) · [A–F 实施计划](../../docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md)。

**阅读规则**：「复用」指当前已有；「新增」指计划交付；「待裁决」不构成授权。页面 hash 不是 API。样机决定布局，不作为字段来源。不改 PR-14 转发核心、日志写入、采样策略或既有授权；不为展示开启提权或新运行时依赖。

## 六区对接表

| 页面 | 端点与字段 | UI 行为与验收 |
|---|---|---|
| A · 导航与状态栏 | **复用** `GET /api/snapshot` 的 `rev`、`system.version` 与 ETag；URL 由 hash router 管理 | `200` 更新数据；`304` 只确认本次连接并保留数据。六区刷新、前进后退和切换锁见 A1。 |
| B · 请求 | **复用** snapshot 的 `history`、`GET /api/anomalies`；**新增／待裁决**的日志查询见下文 | 区分加载、空结果和失败；匹配总数不等于当前页长度。 |
| C · 状态 | **复用** snapshot 的 `status`、`models`、`device`、`token_stats`、`history`、`request_log`；动作与遥测见下文 | 网关在线与硬件读数可用分别呈现。操作按服务端结果确认，不把 `2xx` 一律当成功。 |
| D · 用量 | **复用 + 新增聚合** `GET /api/monitor-report`；复用 `GET /api/device_power`、`GET/POST /api/agents` 和 `POST /api/agents/reclassify` | 一个时间范围驱动 KPI、趋势、分解；旧窗口响应晚到不得覆盖新窗口。 |
| E · 路由 | **复用** `GET /admin/model-pools`、`GET /admin/model-pools/{status,runtime,latency,events,catalog}`；保存用 `POST /admin/model-pools`，另有 `POST /admin/model-pools/{toggle,strategy,validate,dry-run}` | 保存、启用和策略写带 `expected_revision`；`409` 重读并提示冲突，不自动覆写。预演不保存配置；缺运行态不等于空闲。 |
| E · 模型 | **复用** snapshot 的 `models/status`、`GET /api/engine_metrics?model=…`、`GET /admin/model-pools`、`GET /admin/cloud/providers` 与 `POST /admin/cloud/provider-models` | 仅运行引擎读指标；仅回 `sleep_state` 的 `200` 不当作有性能指标。模型入池以类型、厂商和模型身份关联，不跨厂商混合同名模型。操作与 C 共用。 |
| E · 配置：云端 | **复用** `GET /admin/cloud/{providers,presets,provider-impact}`，以及现有连接、测试、发现、更新和别名端点 | GET 只读脱敏状态；模型页只做模型策展，连接与密钥管理留在配置。 |
| E · 配置：鉴权／接入 | **复用** `GET /admin/security/status`、`/admin/keys` 的现有管理流程；接入 `GET /admin/connect/status`、`POST /admin/connect/{apply,rollback}` | 保留本机免登录与敏感操作门禁；GET 不含明文 Key。接入写需用户确认、带 `expected_revision`，冲突重读。 |
| E · 配置：能力包／系统 | **复用** `GET /admin/packs/{catalog,status,policy,uninstall-preview}`，部署向导沿用 `/admin/deploy` 的 `options/search/download/download/status` 和现有安装／自启动流程；缓存／自动切换复用原 toggle；限流读 `snapshot.local_models.rate_limit`；关于链到 `/api/openapi.json` | 不因 UI 移动改写权限或动作；卸载先预览再确认；异步任务按真实终态呈现，没进度不画百分比。限流不新增写入口。 |
| F · 视觉 | 无新 API | 状态文字、不支持／暂不可用文案与键盘操作必须保留，不以颜色替代状态。 |

## C：状态与遥测

### C1. 硬件字段：仅以 `device.snapshot` 与 `device.capabilities` 为准

数据合同见 [`DeviceSnapshot` / `DeviceCapabilities`](../../inferfabric/platform/telemetry.py)。`platform`/`accelerator` 是采集后端身份提示，不等于硬件已探测成功；`memory_kind` 决定标签，`capabilities` 是该后端支持的项，本次是否有数据看字段值。状态页不再从旧 `status.gpu_*`、`system.gpu_*` 或 Mac 的 `system.ram_*` 取硬件真值；旧字段不删、不改语义。

| UI | 字段与单位 | Apple Silicon | Linux + NVIDIA |
|---|---|---|---|
| 设备内存 | `memory_kind` + `memory_used_mb` / `memory_total_mb`（整数 MiB，可显示 GiB） | `unified`：`vm_stat` 已用水位 / `sysctl hw.memsize` 物理总量；这是整机统一内存，不是 Metal 独占量 | `vram`：`nvidia-smi` 显存已用 / 物理总量；多卡是聚合值，不标为单卡 |
| 推理预算 | `memory_budget_mb`（MiB） | 物理总量减系统保留量，单列「推理预算」，不是实时剩余量、物理总量或启动承诺 | 策略上限，不混入 VRAM 物理总量；不是实时剩余量 |
| 内存压力 | `pressure_level`：`1` 正常、`2` 警告、`3` 严重 | `sysctl kern.memorystatus_vm_pressure_level` | 此后端不支持 |
| GPU 指标 | `utilization_pct`（%）、`power_w`（W）、`temperature_c`（°C）、`clock_mhz`（MHz） | 当前无已验证的非提权来源；本期不采、不拿压力或内存占用率代替 | `nvidia-smi`：多卡功耗求和、其余三项取平均；某卡缺该项时该项为 `null`，不是部分聚合 |

- 支持且有有效值：照实显示，`0` 是有效读数；总量不存在或 ≤0 时只展示有证据的值，不画比例。
- 支持但本次值为 `null`：该项显示「暂不可用」，其他项继续显示。无 `device` 是旧接口或资料未知；全部不支持或全部缺失时显示健康卡和对应提示，不猜硬件存在与否。已声明不支持的指标不占默认卡片；未来若真有采集源，可在后端新增能力，无需前端产品分支。
- Linux 选到 NVIDIA 后端但没有可用驱动/读数，也可能收到 `accelerator=nvidia` 加空值；不得因此显示「GPU 在线」。Linux 主机 RAM 如需展示，使用有效的 `system.ram_*` 并标「主机内存」，不和设备 VRAM 合并。Mac 首屏不能因没有 NVIDIA 指标而丢掉有效统一内存与压力。

### C2. 刷新与健康：三个时钟分开

1. **连接**：保持 3 秒轮询和现有 ETag；`200/304` 均更新最近成功同步时间，不把断联当内存/GPU 空闲。
2. **硬件采样**：现有 `TelemetryHub` 15 秒缓存继续单飞，不为前端另开探针或轮询。`device.snapshot.timestamp` 是后端采样尝试时间，不保证每个字段成功；`sources` 指明有来源的指标。ETag 保留现有语义：值、来源或能力变化返回 `200`；只有采样时间变化可返回 `304`。**新增（C）**：同源响应的 `200` 和 `304` 均带 `X-IFF-Device-Sample-Age`（发送时对该份采样计算的非负整数秒）。前端接收后按本地单调时间累加；超过 30 秒标「读数已过期」。没有头时可显示最近收到的旧数据，标「新鲜度未确认」，不假定 `304` 即新采样。有效时间与空值分开判定；失败尝试即便很新，`null` 仍是「暂不可用」。
3. **网关版本与运行时长**：版本复用 `snapshot.system.version`，在线状态看 snapshot 连接。`system.uptime_seconds` 是 Linux 主机时长或默认值；现有 `/metrics` 每次新建 exporter，其 `iff_uptime_seconds` 不能作持久进程时钟。**C 不把这两个数挂到「网关在线时长」**；没有可靠的网关启动时刻时省略该数值，不为此增加探针或重构启动路径。

### C3. 态势和操作

- 当前模型：用 `status.active_services/services_info/services_health/sleep_states/switch_target` 联结 `models`；共享模式允许多项，不能只认一个 GPU 位。空闲快捷项取 `history` 中成功到达、且仍可切换的最近三个去重模型。无可信启动时刻不把浏览器打开时间算作模型运行时长。
- 请求数：取 `token_stats.local/cloud` 的 UTC 当天 `requests` 分桶（界面注明 UTC），不拿最近一页长度当今日总量。`GET /api/anomalies?severity=warning&since=…` 是内存环形缓冲区，标「已保留警告」，不是完整日计数；老记录被淘汰或服务重启不能冒充零警告。最近请求卡只统计所显示的最近五条，详情跳 `#/requests`（有适用的过滤才带）。

| 按钮 | 复用命令 | 用于判断结果的规则 |
|---|---|---|
| 切换／停止／休眠／唤醒 | `POST /switch`、`/stop`、`/sleep`、`/wake`，JSON `{model}` | 用现有动作逻辑，保持能力门禁、确认和锁。切换拒绝/回滚、错目标、`409`、休眠唤醒 `200` 中的 `status:error` 均不能报成功；完成后刷新快照核对目标态。 |
| 释放 GPU | `POST /switch`，`{model:"idle"}` | 保留危险确认并说明模型会停止；返回错误或未到 idle 不报成功。不是 `POST /admin/gpu-clear`，不从遥测卡新接 CUDA reset。 |
| 对账／重载配置 | `POST /reconcile`、`POST /reload-config` | 对账展示结果并刷新；重载 `200` 如含 `failed` 则报部分失败，不能报「全部重载」。 |
| 强制重置 | `POST /reset` | 明示影响并保留危险确认；取消不发 POST。`status:reset` 后刷新核对，不自动重试或替用户改走其他端点。 |

管理操作仍走现有授权（浏览器沿用 `UI.adminHeaders()`）；能力旗标（含 `reset`）不是权限。`401/403` 或网络失败保留此前状态并提示，写操作不自动重试；不展示上游原始错误体或私有日志。

### C4. 验收证据

- **契约**：在 C 同一实现 PR 补 [OpenAPI](../../api-spec/openapi.yaml) 的 snapshot `device` schema、`200/304` 响应头与示例，`device` 对旧响应兼容可缺，有对象时字段缺值用 JSON `null`。用同一组脱敏样本验 handler、前端 renderer；覆盖有值、真 `0`、部分／全部 `null`、旧接口、无驱动、迟到响应、同值新采样 `304`、过期、部分重载及拒绝动作。验证 HTTP 与 async 网关的响应头兼容。
- **Mac 隔离验收**：核对同一台机器上 `sysctl` 物理总量、`vm_stat` 水位和内存压力档位；确认物理总量 ≠ 网关预算，不出现假 GPU 指标，硬件取不到时仍有健康信息。
- **vince-pc 隔离验收**：获准窗口后对照同一短时间窗的 `nvidia-smi`，确认五项单位与聚合、空闲 `0%` 保留、缺项只影响对应读数。不为测试故意让生产驱动失效，失败路径用 fixture。记录 SHA/平台/驱动和脱敏结果，验后清退；不自动重启生产或切换 VL。

## B：请求页

- **复用** `GET /api/request_log?limit=&since=`。**新增／待裁决**的查询模式及文件边界见 [实施计划](../../docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md) B1：过滤后才分页，`total/failed_total` 不受页大小影响；固定 `until` 翻页；Live 只在请求子页可见且开启时工作。
- 扩展行仅透出安全 `req_id` 与白名单 `pool`，重试和换目标回退是不同计数；当前日志没有历史端口，显示「—」，不反查当前配置冒充。异常关联按模型、时间和可比状态推断，并用文字标注；不显示全量 metadata、上游原文、原始请求或凭证。旧参数保持兼容。

## D：用量页

- **复用** `GET /api/monitor-report?window=1h|24h|7d|30d&scope=all` 的 `since/until/totals/models/series/coverage`（`[since,until)`，Unix 秒）。**新增**：成本、`tokens_in_cached/cache_share`、按客户端 `agents[]`，及 `series[]` 按 local/cloud/unknown 的分解，见 [实施计划](../../docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md) D1。不能用只接受 granularity 的旧 `/api/agent-stats` 拼成同窗报告。
- 缓存份额是 `sum(tokens_in_cached) / sum(tokens_in)`；分母为零无值，不画成 `0%`；没有计价证据标「未计价」。返回 `coverage.truncated` 必须提示不完整。模型身份保留厂商和位置；unknown 不并入 local，未识别客户端单列。
- 电费来自现有 `GET /api/device_power?gran=hour|day|week|month`，仅是设备功耗估算，不是整机电费。该端点桶边界可能与报告不同；只有能确认区间和采样覆盖时才计入同窗合计，否则单列可核对的估算或标「覆盖不足」，不把空桶或缺源当免费。C 的即时指标不依赖此接口。
