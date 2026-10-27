# PR-20 重启零中断（P1）规格

> 来源：P0 诊断（2026-10-27，纯观测，见下）→ 用户拍板 P1 四项实施口径。
> P2（systemd socket activation）观察后再议，不在本 PR。

## P0 诊断结论（实测）

一次 `systemctl --user restart inferfabric-proxy` 的中断解剖：

| 组件 | 现状 | 根因 |
|---|---|---|
| 端口拒连窗口 | **91.7s** | 旧进程关不掉：0–60s aiohttp `shutdown_timeout=60` 排空在途；60–91.7s 非守护 `ThreadPoolExecutor` 转发线程 join 阻塞退出，最终靠 systemd 90s `TimeoutStopSec` SIGKILL |
| 新进程冷启 | 323ms | 非问题 |
| 引擎冷启 | **~15 min** | 新进程 `start_ninfer` pre-start **无条件 `docker stop`**（ninfer.py:60-63）杀掉健在容器（bge-m3 幸存 + alpine 实验证明引擎天然不随 proxy 死亡）；垂死进程 12:27:58 reconcile 毒写 `gpu_mode=idle` + 清空 services（gpu_state.py:262-268） |
| 面板发版 | 需重启 | **误判**——`ConfigReloader.reload_all()` 已调 `invalidate_cache()`（config_reloader.py:122-127），SIGHUP 链路已存在，本 PR 只做实证 + 文档 |

## P1 四项（用户已批准口径）

核心：**重启只轮换网关进程，不陪葬引擎 / 不毒化状态 / 排空有界 / 面板发版零重启。**
不碰转发核心路径（PR-14 红线）。

### ① 有界排空 + 硬退出（进程侧）

- `AppRunner(..., shutdown_timeout=EDGE_SHUTDOWN_DRAIN)`，默认 **30s**（原 60）。
- 关停序列（`_shutdown_gracefully` 提取）：
  `begin_shutdown`（封印 + 停后台线程）→ `shutdown_event.set()` → `watchdog.stop()` → `site.stop()` → `runner.cleanup()`（≤30s 有界排空）→ `executor.shutdown(wait=False)` → `sd_notify("STOPPING=1")` → **`os._exit(0)`**。
- `os._exit(0)` 绕过非守护线程 join（91.7s 根因）；`EDGE_HARD_EXIT=0` 可关（调试用，走 start_async finally 兜底）。
- systemd 单元 `TimeoutStopSec=45`（排空 30s + 停线程余量，最终兜底）。
- 效果：无在途请求时中断 ~1–2s；最坏 30s（原 91.7s）。

### ② 三态收养（引擎侧，最大头）

`start_ninfer` 收养逻辑（P0 实测引擎天然幸存，收养 = 承认现实）：

```
docker inspect（一次调用取 Running/StartedAt/Config.Image）
├─ 不存在 ──────────────────────────→ 原路径创建
├─ running + 健康（/v1/models ✅）──→ 收养：status="healthy"+adopted=True,
│                                      pid=None，不动容器、不 sleep
├─ running + 年轻（age < IFF_NINFER_ADOPT_GRACE，默认 1200s）
│        + 未就绪 ──────────────────→ wait_http 等宽限剩余 → 成则收养
├─ running + 年老不健康 / 镜像不匹配 → 原路径重建（stop 才 sleep 2）
```

- 返回 `status="healthy"` 保持消费方兼容（model_lifecycle 接受 healthy/started/ok）。
- adopt-wait 上限 = 宽限剩余（非 startup_timeout=120——挡不住 15min 冷加载）。
- 镜像变更（如 ninfer:auto 重 tag）自动重建；**参数变更**（models.d YAML）需显式
  `iff stop`/restart 才生效（重启网关 ≠ 重配引擎，语义如此，文档说明）。
- 效果：健在引擎零冷启（15min → ~1s）；新代理收养后 ~2-3s 恢复服务。

### ③ StateDB 封印（状态侧）

- `StateDB.seal(reason)`：封印后**所有写方法 no-op**（set/set_multi/active_services
  增删/manual_stop/gpu_mode setter/sleep_state/history），读不受影响，幂等。
- `ProxyManager.begin_shutdown()`：幂等；封印 StateDB → 停 health_monitor（毒写源头，
  60s 周期 reconcile）/ token_collector / power_sampler。
- SIGTERM 第一时间执行（先于 site.stop），垂死进程不再毒写
  （12:27:58 场景：reconcile 把 idle 写进 state.db 交给下一个进程）。
- 请求日志（request_log.db）**不封印**——排空期在途请求仍正常落日志。

### ④ 面板热失效实证（需求侧）

- 链路已存在：SIGHUP/SIGUSR1 → `reload_all()` → `invalidate_cache()` → 下次 GET / 重读盘。
- 本 PR：沙箱运行时冒烟实证（改 fragment → HUP → 内容更新），运维文档化：
  **面板发版 = 落盘 + `systemctl --user kill -s HUP inferfabric-proxy`，零重启**。

## 验收

1. 全量 pytest 绿（unit + integration）；import 冒烟；交叉评审通过。
2. 沙箱运行时冒烟：SIGHUP 热更新实证；SIGTERM 关停序列日志正确。
3. 生产重启验收（用户授权重启后测量）：拒连窗口 ≤ ~31s（最坏）/ ~2s（常态）；
   健在 NInfer 容器收养（日志 `NInfer adopt`），无 15min 冷启；
   重启后 state.db 无垂死进程毒写。
