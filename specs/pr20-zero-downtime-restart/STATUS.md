# PR-20 Zero-Downtime Restart Status

**发布阻塞：尚未完成，禁止合并/部署。** 独立评审发现总排空 deadline、封印竞态、
收养与 VRAM 守卫顺序、后台生命周期写入者等问题；完整处置见 `REVIEW.md`。
当前工作分支：sandbox repo 的 `restart-p1`。生产保持原样。

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 诊断 | ✅ | 2026-10-27 | 纯观测：91.7s 拒连 / 323ms 新进程 / ~15min 引擎冷启解剖 |
| P1 拍板 | ✅ | 2026-10-27 | 用户批准四项口径（P2 socket activation 缓议） |
| Specify | ✅ | 2026-10-27 | spec.md（含评审修复 7 的契约收窄） |
| Implement ③ StateDB 封印 | ✅ | 2026-10-27 | seal + begin_shutdown（TDD）+ 有界 seal（87bce42） |
| Implement ① 排空+硬退出 | ✅ | 2026-10-27 | 总排空 deadline + 终刷 + os._exit；TimeoutStopSec=45 为发版时部署步骤 |
| Implement ② 三态收养 | ✅ | 2026-10-27 | _container_info + adopt 分支 + 守卫前现实识别（ebf6da1） |
| Implement ④ 面板热失效实证 | ✅ | 2026-10-27 | 隔离子进程真实 HTTP + SIGHUP 热更新通过；运维文档已补 |
| 评审修复 1–5 | ✅ | 2026-10-27 | 全部 RED→GREEN，处置见 REVIEW.md |
| 评审遗留 6 cgroup | ✅ | 2026-10-27 | 实验解决：attached/detached 容器均不随 user unit 死；默认 KillMode 足够 |
| 评审遗留 7 同名镜像 | ✅ | 2026-10-27 | spec.md 契约收窄：重启网关 ≠ 重配引擎 |
| Quality gates | ✅ | 2026-10-28 | 全量 pytest ✅ 1802 + 干净 worktree 复验 ✅ + import 冒烟 ✅；fresh-context 重审 ✅ 无 Critical/Important（7 Minor 留用户决策，见 REVIEW.md） |
| Release | ✅ | 2026-10-28 | 合并生产 5f5e68a + push ✅；用户授权重启验收 ✅ 拒连 9.77s（基线 91.7s）；TimeoutStopSec=75 已写入 unit + daemon-reload；版本 7.6.0 |

## 当前验证记录

- 全量回归（评审修复 1–5 + 有界 seal 之后）：**1802 passed in 59.87s**（sandbox
  工作区）；**1802 passed in 60.28s**（干净 worktree 复验提交树 — 沙箱工作区绿
  ≠ 提交树绿）。
- `python3 -c 'import inferfabric'`：通过，版本保持 7.5.0（两处均验）。
- 分项套件：lifecycle+ninfer-PM 48 / watchdog 18 / request-logger+shutdown 21 /
  seal 12 / async-server 34，全绿；各修复均先观察到 RED（见 REVIEW.md 各条）。
- `tests/integration/test_restart_smoke.py`：真实子进程 + 在途 HTTP 流 + SIGHUP
  热更新 + SIGTERM 退出码 0，通过。
- 评审 7 项发现全部处置完毕（1–5 修复、6 实验解决、7 spec 契约收窄），
  详见 `REVIEW.md`；修复后自动安全评审的两项（TOCTOU、有界 seal）已处置，
  后者以 TDD 修复（87bce42）。
- Fresh-context 重审（a2863c1..87bce42 全分支 diff + 两条安全评审处置）：
  **无 Critical/Important**（2026-10-28）。评审者独立复跑：专项 186 passed、
  全量 1803 passed（较我方 1802 有一例计数漂移，两边全绿）。7 项 Minor 留用户
  决策（REVIEW.md「Deferred Minors」），其中 #2 涉及发版参数（TimeoutStopSec
  45 vs 75）；两条安全评审处置均获评审者同意。
- 分支 `restart-p1` 七个提交：a2863c1 spec / d22c462 seal / 3328459 发现 1-2 +
  收养 + 冒烟 + 文档 / ebf6da1 发现 3 / 57816e3 发现 4 / 8cd15f1 发现 5 /
  87bce42 有界 seal。
- 尚未合并生产、push、修改 systemd 或重启。
- 流程偏差（存档）：收养实现先于运行新增失败测试（当时执行分类器持续超时），
  不标作已完成 TDD；时间戳测试曾错误期望清零小数，已改成精确匹配微秒。
  不使用临时 `p1_smoke.sh`（完整 ProxyManager 可能触发真实模型 reconcile/
  watchdog），一律用隔离的 `tests/integration/test_restart_smoke.py`。

## 部署步骤（已执行，2026-10-28）

1. ✅ 合并 sandbox/main → 生产 main（5f5e68a），push。
2. ✅ systemd unit 写入 `TimeoutStopSec=75`（用户定夺 45→75：采纳重审建议，
   覆盖最坏停机链 ≈65s；`systemctl --user show` 确认 TimeoutStopUSec=1min 15s）。
   （该文件含真实凭据，全程未回显内容。）
3. ✅ `systemctl --user daemon-reload`。
4. ✅ 重启验收（用户授权）：见下方验收记录。

## 重启验收记录（2026-10-28 11:45）

- 拒连窗口 **9.77s**（重发起到 :8999 应答；P0 基线 91.7s）：
  旧进程关停 ~4s（旧代码，executor 挂死未复现）+ RestartSec=5 + 新进程 <1s 绑端口。
  注：本次关停侧仍是旧代码；PR-20 的有界关停从下一次重启兑现。
- 新进程 startup reconcile 干净：DB 记 exclusive+TXT、现实容器已死（重启前自行
  崩溃）→ 按现实清为 idle，未继承垂死误判。
- 引擎：无活容器可收养 → 11:45:50 重建 → ~30s 健康（热页缓存；P0 ~15min 是
  含杀健康重建+冷盘）。三态收养的「收养/等待」分支留待下次带活引擎重启实测。
- 切换期一个请求收规范 503（switch guard 按设计）；云路由全程正常。
- 终态：exclusive + Qwen38-27B-TXT ✅ + GPU 31.8/32.6G + dashboard 200 +
  /api/metrics 200。

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
