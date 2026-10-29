# PR-22A: NInfer 引擎 detached 化 — 引擎幸存修复

## 背景（承接 PR-20）

PR-20 ② 的三态收养（健在健康 → 收养 / 年轻未就绪 → 等宽限 / 否则重建）在
2026-10-28 21:12 生产重启验收中被证伪：**收养从未有对象** —— proxy 停机时
引擎容器随之死亡，重启后 `_container_info` 永远查无此容器。

### 根因（生产实证，docker events + journalctl）

```
systemctl --user restart inferfabric-proxy
  → systemd KillMode=control-group：SIGTERM 发给 unit cgroup 内所有进程
  → attached `docker run`（无 -d）客户端是 proxy 子进程，同 cgroup → 收到 SIGTERM
  → docker 默认 sig-proxy：客户端把信号转发进容器主进程
  → 引擎优雅退出（docker events: kill signal=15 → die exitCode=0）
  → `--rm` 自动销毁容器（destroy 事件）
  → 重启后无容器可收养 → ~15min NVFP4 冷加载重演
```

PR-20 评审发现 6 的「attached 容器可幸存」实验之所以得出错误结论：实验里的
attached docker 客户端**不在被停 unit 的 cgroup 里**（手工前台运行），而
生产中客户端由 proxy 启动、必在其 cgroup。结论已回写 pr20 STATUS.md。

### 修复选项（用户拍板 A）

| 选项 | 做法 | 评估 |
|---|---|---|
| **A（采纳）** | `docker run -d` | 客户端即退，cgroup 内无长驻子进程；架构最干净 |
| B | `--sig-proxy=false` | 客户端仍长驻（挂着 stdout 流）；日志仍走客户端落盘 |
| C | unit 改 `KillMode=process` | 单点修 systemd 配置，docker 之外的子进程全部逃逸管理，风险大 |

用户原话：「A docker run -d — 客户端即退，架构最干净；日志改走 docker logs」。

## 修复设计

### 1. `process_manager/ninfer.py` — start_ninfer detached 化

- cmd 首位 `docker run -d`，附 `--log-opt max-size=50m --log-opt max-file=3`
  （宿主 docker 日志驱动 json-file，实测接受旋转选项；防长驻容器日志无界）。
- `subprocess.Popen`（长驻客户端）→ `subprocess.run(timeout=60)`，rc≠0 →
  error + stderr 尾 500 字符。
- **立即退出探测**（评审 I-1 修复后语义）：attached 时代靠客户端 `poll()`
  观察 ~3s；detached 等价物 = 6 轮 `_probe_container_running()`（单轮
  `docker inspect -f {{.State.Running}} <name>`，0.5s 间隔），**观察满
  全窗口，不得首轮 true 即断**（否则 0.3–3s 快退带漏检 → 烧满
  startup_timeout 误报 timeout 且零诊断）。三态：
  - `true` → 在跑，继续观察到满窗（healthy 启动多付 ~3s，对 15min
    NVFP4 冷载可忽略）；
  - `false`（已退出、`--rm` 未及移除）→ 立即断出，抢 `_docker_logs_tail()`
    在移除前拿到真实引擎报错尾（移除后只剩 "No such container"）；
  - 未确认（探测异常 / rc≠0 容器已被 `--rm` 移除）→ 按旧 6 轮语义重试，
    探测异常绝不抛（fail-soft）；6 轮全未确认 → 失败路径 + docker logs
    best-effort 诊断，回滚容器追踪。
- `pid` 恒 `None`（与收养路径同构，容器非本进程子进程）；消费方
  `gpu_state._detect_orphan_pids`（跳过 falsy pid）与 `facade.stop` 均已容忍。
- 收养前探测 / pre-start cleanup / 三态收养逻辑零改动。

### 2. `engine_adapter/ninfer.py` — fetch_engine_metrics 日志源切换

- 旧：attached 客户端把引擎 stdout 流式落盘 `log_file`，metrics 读文件尾
  500KB 窗口。detached 后无客户端 → **改 `docker logs --tail 5000`**
  （行数 ≈ 原 500KB 窗口等价；日志经 --log-opt 旋转有界）。
- 失败（容器不在 / docker 不可用）→ 返回 None，与旧「缺 log_file」同语义
  （上层按无 metrics 处理，/metrics Prometheus 覆盖路径不受影响）。
- 解析逻辑（throughput / running batch / req#done 正则）零改动。

### 3. 不做的事

- **不改 systemd unit**（选项 C 否决）：修复在代码层，不依赖部署配置。
- **不动转发核心路径**（PR-14）。
- `stop_ninfer` / `container_exists` / `_container_info` 零改动。

## 验证

1. **单测 TDD**：`TestStartDetached` 4 例（-d+旋转 opt / 成功追踪容器且
   pid=None / run 失败 error / 立即退出+docker logs 诊断+回滚）+ 评审
   I-1 修复 3 例（观察满 6 轮退出窗口 / 窗口内快退抢 docker logs /
   探测异常 fail-soft），另收养与
   重建旧测试迁至 subprocess.run 契约（inspect 双格式区分：3 字段身份
   探测 vs 1 字段存活探测）。
2. **沙箱 systemd 实弹冒烟**（假引擎镜像，真实 docker + 真实 systemd
   cgroup 语义，零 GPU 占用）：
   - A 启动：`systemd-run --user -p KillMode=control-group` 跑真实
     `start_ninfer` → 4s 健康，LogConfig 实证 `max-file:3 max-size:50m`；
   - B 幸存（核心断言）：`systemctl --user stop`（整 cgroup kill，生产
     同语义）→ 容器 `Running=true`，HTTP 200 不中断；
   - C 收养：新 unit 二次 `start_ninfer` → 日志
     `NInfer adopt: ... reusing (no cold start)`，RESULT `adopted: true`；
   - 附带实证：`docker logs` 可作引擎日志源；立即退出路径的
     `docker logs` 诊断（首次 alpine 冒烟 httpd applet 缺失误退出，
     错误信息含 docker 诊断文本）。

## 生效条件

代码合并后**下次引擎启动**即 detached；生产验证需重启 proxy（需用户授权）。
已在跑的 attached 容器不受影响（下次由它自然死亡后的重启切换）。
