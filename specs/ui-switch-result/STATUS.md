# ui-switch-result 状态

日期：2026-10-23

| 项目 | 状态 | 证据 |
|---|---|---|
| 范围确认 | 完成 | 用户批准小批推进；sleep/wake 暂缓 |
| RED | 完成 | 首轮10个行为用例：7 failed / 3 passed，失败均为错误返回被提示ok |
| 最小实现 | 完成 | 只修改 sandbox/inferfabric/dashboard/js/inference.js 的切换反馈 |
| 首轮 GREEN | 完成 | 定向10例全部通过 |
| 旧测试迁移 | 完成 | 首轮全量1299 passed / 1 failed；唯一失败依赖 UI.toast(op.ok 字面文本，已由同名可执行行为测试替代并扩充 HTTP 失败覆盖 |
| 全量回归复跑 | 通过 | 最终版本 unit + integration：1304 passed in 41.65s；新增行为测试15例 |
| JS 语法 / diff check | 通过 | 最终回归命令后 node --check 与 git diff --check 均退出0 |
| import / 隔离 smoke | 通过 | 确认导入sandbox包；真实aiohttp路由在127.0.0.1临时端口：GET /、/health、/status均200；POST /switch使用内存假生命周期，保留HTTP200/status:error契约。无真实GPU操作 |
| 独立代码评审 | 通过 | 只读独立审查未发现阻断问题；已采纳完成态文案的测试补充建议，并重跑全量回归 |
| 指定云端GLM交叉评审 | 阻塞，未执行 | 两次请求命令均在执行前被工具安全分类器glm-5超时挡住；没有发出评审请求，不据此判断云Provider故障，也不声称此门已通过 |
| 合入 / 部署 | 未执行 | 指定交叉评审未完成，暂留sandbox；生产代码与运行服务未修改，不做整树同步 |
