# IFF Constitution

## 治理原则
- **绝对稳定优先**：IFF 是 AI 平台核心推理代理，proxy 挂 = 全链路挂
- **沙箱先行**：所有修改在沙箱完成 → pytest → 冒烟 → diff 审查 → 合入生产
- **增量收敛**：每个 PR 独立 spec → 独立验证 → 独立 commit
- **风险显式化**：每个变更必须声明风险爆炸链 + 缓解措施

## 质量门禁
- **678** pytest 全量通过（`tests/unit/ + tests/integration/`，2026-09-15 实测）
- `python3 -c "import inferfabric"` 启动冒烟
- 运行时冒烟：`GET /` 和 `GET /status` 返回 200
- Dashboard 每个面板必须有功能实现（不允许 placeholder）
- 失败数据不得污染成功指标的统计
- 版本号在 `__init__.py` 中更新
- **测试/出站卫生（#78 复盘，2026-11-26）**：集成与子进程类测试显式设 `no_proxy=localhost,127.0.0.1,::1`（防系统/环境代理劫持回环流量制造假失败）；云端出站调用点禁用 urllib 默认 opener——一律用 `inferfabric.egress.NO_PROXY_OPENER`（默认 opener 对系统代理做一次性快照且永不刷新，代理中途退出即出网全断）；出站回归须覆盖「代理中途失效」场景（参照 `tests/integration/test_cloud_egress.py` 死代理复现器）。
- **重启门禁（方案 B，2026-11-26 用户批准）**：生产网关重启前在部署 commit 打 `vX.Y.Z` annotated tag 并推送；重启后必跑 `scripts/iff-post-restart-check.sh` 且 PASS（/health 版本一致 + 模型注册表非空 + 云端 discovery 成功行 + 可选微云请求）才算上线完成；金丝雀不 PASS 不离场。

## 技术约束
- Python 3.10+
- vLLM 0.24（暂不升级）
- 不改 proxy 转发核心路径（PR-14 排除）
