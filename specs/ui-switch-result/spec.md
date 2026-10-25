# 推理页切换结果反馈修复

日期：2026-10-23
范围依据：用户同意按小批节奏修复；sleep/wake 明确暂缓。

## 问题

`/switch` 可能返回 HTTP 200 + `status:error`；推理页仅检查 `res.ok`，误显示成功。目标启动失败但旧模型回滚成功时，后端返回 `status:switched, model:旧模型, rollback:succeeded`，也不能被描述为目标切换成功。

## 本批改动

- 仅对 `doModelAction` 的 switch 分支解析并检查业务结果。
- 成功状态仅接受 switched/already_active；有目标模型字段时需与请求目标一致。
- 错误、空/未知结果、无效 JSON 不显示成功提示；保留后端提供的失败原因。
- 回滚成功显示目标切换失败及已恢复的模型，并刷新面板状态。
- 同步切换成功显示已完成，而非“切换中”；已运行时如实提示。
- 原有确认交互、请求 URL/方法/目标参数和 HTTP 错误处理保持不变。

## 非目标

不改后端状态码、模型路由、转发核心、引擎、并发协调或 sleep/wake 业务逻辑；不启用睡眠特性；不重启生产或操作真实 GPU。

## 验证

使用 pytest + Node VM 加载真实 inference.js，以受控 HTTP 返回替代网络，在确认回调后执行真实 doModelAction，验证用户可见反馈。原 test_dashboard_v2.py 中依赖 `UI.toast(op.ok` 字面字符串的结构断言，由新文件中的同名参数化行为测试替代，保留所有操作的 HTTP 失败回归覆盖。

质量门：定向回归、完整 unit/integration、JS 语法、import 与隔离 HTTP/UI smoke、独立评审、差异审查。任何门未完成须记录，不直接合入或部署。
