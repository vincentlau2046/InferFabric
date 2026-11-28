# STATUS — dashboard-ux-v3

- **阶段（2026-11-27）**：用户批准设计及样机定稿，**PR #83 已合并**（`03cabb1`）。三轮裁决：模型池独立为「路由」，最终导航为六区；模型页保留本地/云端清单与在池提示。
- **产物**：[设计](design.md)、[可点击样机](preview.html)（示例数据）、[A–F 实施计划](../../docs/superpowers/plans/2026-11-27-dashboard-ux-v3-impl.md)、[接口与 UI 对接契约](interface-contract.md)（后两者随草稿 PR #86 审阅）。
- **C 期补充裁决（2026-11-27）**：用户确认平台适配层供数、UI 按能力消费；Mac 展示已可采集的统一内存和压力，NVIDIA 展示 GPU 遥测，本期不新增 Mac GPU 探针。
- **A 期**：六区导航与旧功能迁移已由 [#88](https://github.com/vincentlau2046/InferFabric/pull/88) 合并（7.12.14）；导航与渲染边界回归由 [#89](https://github.com/vincentlau2046/InferFabric/pull/89) 跟进（7.12.15），双端签收以对应 PR 的最终 SHA 为准。
- **执行委托（2026-11-28）**：用户委托后续阶段自主按计划发布，完成统一汇报。长任务记录截止时间并监控实际验收结果，失败／超时即排障，不把缺失回执记成通过。双机、审查、隔离验证和转发红线不变。
- **下一步**：A 期跟进收口后从最新 main 开始 B。按计划的推荐路径仅扩展 `db.py`／`telemetry.py` 读侧接口；旧查询、日志写入和转发核心不变。同页过滤参数变化纳入 B2 回归。之后依次 C → D → E → F。生产上线状态不由 PR 合并推定。
- **B 期开工（2026-11-28）**：从 main `8dcedc9` 切出 `feat/dashboard-ux-v3-b`。B1 只读查询（`db.py`/`telemetry.py` 独立读方法 + `/api/request_log` 显式 `window` 模式：过滤、分页、同快照计数、安全池摘要）；B2 请求工作台（同页 hash 过滤、列选择、分页、池详情、迟到响应失效）。无迁移、无写入、无采样埋点、PR-14 转发核心零改动；双端回执先行，未签收不合并。
- **B 期实施完成（2026-11-28，7.12.16）**：B1 `query_request_log_window`（commit `9fdbc3b`，19 RED→75 targeted→全量 3028）；B2 `requests.js` 请求工作台 + router 参数契约 + monitor `renderLogTable` 移除（18 RED→dashboard 353→全量 3047）。浏览器门禁：隔离实例 :8991 + headless Chrome for Testing CDP 冒烟 18/18、控制台零错误、截图目检；实测修复子页往返丢筛选（navigate 参数缺省沿用同视图参数）。待 vince-pc 同 SHA 隔离签收后合并 #90。
