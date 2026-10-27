# PR-20 Zero-Downtime Restart Status

**发布阻塞：尚未完成，禁止合并/部署。** 独立评审发现总排空 deadline、封印竞态、
收养与 VRAM 守卫顺序、后台生命周期写入者等问题；完整处置见 `REVIEW.md`。
当前工作分支：sandbox repo 的 `restart-p1`。生产保持原样。

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 诊断 | ✅ | 2026-10-27 | 纯观测：91.7s 拒连 / 323ms 新进程 / ~15min 引擎冷启解剖 |
| P1 拍板 | ✅ | 2026-10-27 | 用户批准四项口径（P2 socket activation 缓议） |
| Specify | ✅ | 2026-10-27 | spec.md |
| Implement ③ StateDB 封印 | ⏳ | 2026-10-27 | seal + begin_shutdown（TDD） |
| Implement ① 排空+硬退出 | ⏳ | 2026-10-27 | _build_runner/_shutdown_gracefully + TimeoutStopSec=45 |
| Implement ② 三态收养 | ⏳ | 2026-10-27 | _container_info + adopt 分支 |
| Implement ④ 面板热失效实证 | ✅ | 2026-10-27 | 隔离子进程真实 HTTP + SIGHUP 热更新通过；运维文档已补 |
| Quality gates | ⏳ | | 全量 pytest + import 冒烟 + 交叉评审 |
| Release | ⏳ | | 合并生产 + push；生产重启验收（用户授权后） |

## 当前验证记录

- `python3 -m pytest tests/unit/ tests/integration/ -q`：1777 passed（新增进程冒烟之前）。
- `python3 -m pytest tests/integration/test_restart_smoke.py -q`：1 passed。
  真实子进程、临时 dashboard 文件、内核分配端口；SIGHUP 后页面更新；
  SIGTERM 后退出码 0，未等待仍阻塞 60s 的 executor worker。
- `python3 -c 'import inferfabric'`：通过，版本保持 7.5.0。
- 独立 fresh-context 评审完成：**需修复后再合并**，详见 `REVIEW.md`。
- 后续完整回归（新增进程冒烟后）：1778 passed in 58.42s。
- 评审新增两条复现测试：2 failed（RED）。对应写屏障/总排空 deadline 修复已写入，
  但未复验（安全分类器连续 server error）；带真实在途 HTTP 流的冒烟扩展也未运行。
- 尚未合并生产、push、修改 systemd 或重启；此前通过数不覆盖评审后修复。
- 流程偏差：收养实现先于运行新增失败测试（当时执行分类器持续超时）；
  不能标作已完成 RED→GREEN，后续回归不追认 TDD。时间戳测试曾错误期望
  清零小数，已改成精确匹配微秒 123456。
- 不使用此前临时 `p1_smoke.sh`：完整 ProxyManager 可能触发真实模型 reconcile/
  watchdog；改用 `tests/integration/test_restart_smoke.py` 隔离生命周期依赖。

## 部署步骤（Release 时执行）

1. 合并 sandbox/main → 生产 main，push。
2. 编辑 `~/.config/systemd/user/inferfabric-proxy.service`：加 `TimeoutStopSec=45`
   （注意：该文件含真实凭据，不回显内容）。
3. `systemctl --user daemon-reload`。
4. 重启（用户授权后）：`systemctl --user restart inferfabric-proxy`，测量验收指标。

## 面板发版（今后，零重启）

```bash
# 落盘新 fragments/js 后：
systemctl --user kill -s HUP inferfabric-proxy   # SIGHUP → reload_all → invalidate_cache
```

链路（v5.2 已有，PR-20 实证 + 文档化）：SIGHUP → `ConfigReloader.reload_all()`
→ models/auth/cloud/agents/model_pools 五域重载 + `dashboard.invalidate_cache()`
→ 下次 `GET /` 重读盘组装。SIGUSR1 = 仅 models 域 + 面板失效。
HTTP 替代：`POST /reload-config`（同链路）。
注意：引擎**参数**变更（models.d YAML）不走此路径——SIGHUP 只重载网关配置
视图；引擎收养语义下（PR-20 ②）参数变更需显式 `iff stop` / restart 重建容器。

## 关停/重启语义（PR-20 后）

| 事件 | 行为 |
|---|---|
| SIGTERM/SIGINT | 封印 StateDB → 停 health_monitor/watchdog/telemetry → 端口收 → 在途排空 ≤30s（`EDGE_SHUTDOWN_DRAIN`）→ `os._exit(0)` |
| 在途请求 | ≤30s 内正常完成；超限被取消（客户端见连接断，可重试） |
| systemd 停止 | TimeoutStopSec=45 兜底（新单元值，部署时 daemon-reload 生效） |
| 引擎容器 | **不随 proxy 死亡**；重启后 start_ninfer 三态收养（健康→收养 / 年轻未就绪→等待 / 年老不健康→重建） |
| state.db | 关停期封印禁写；新进程启动 reconcile 从现实推导 |
| 面板发版 | 落盘 + SIGHUP，零重启（见上） |
