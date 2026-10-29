# PR-22A NInfer 引擎 detached 化 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 诊断 | ✅ 2026-10-28 | 21:12 生产重启 docker events 实证 kill signal=15 → die exitCode=0 → --rm destroy；sig-proxy 机制链完整（另见 pr20 STATUS.md 第二次验收记录） |
| 实现 | ✅ 2026-10-29 | PM detached 化 + `_docker_logs_tail`；adapter metrics 日志源 → `docker logs --tail 5000`；收养/stop/container_exists 零改动 |
| TDD | ✅ | TestStartDetached 4 例先 RED（detached 不存在：无 -d / pid 非 None / 无立即退出探测）；metrics 4 例重写 GREEN（test_docker_logs_unavailable 系回归钉，非严格 RED，已注明）；收养 6 测 + 参数名 1 测迁 subprocess.run 契约 |
| 全量回归 | ✅ | 1816 passed（unit+integration；评审修复后：+3 新测 I-1 窗口/快退抢日志/探测 fail-soft） |
| 沙箱实弹冒烟 | ✅ 2026-10-29 | 假引擎（python:3.13-slim 自制 ninfer-serve=http.server）+ `systemd-run --user -p KillMode=control-group`：A) 4s 健康、LogConfig `max-file:3 max-size:50m`；B) **unit stop 整 cgroup kill → 容器 Running=true 仍服务 200**；C) 二次启动 `NInfer adopt — reusing (no cold start)` + `adopted:true`（PR-20 收养首次有对象）；`docker logs` 请求行可取 |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.6.2 |
| 干净 worktree 复验 | ✅ | 提交树全量 1813 passed（首commit；评审修复后另行复验）|
| cross-review | ✅ 2026-10-29 | fresh-context subagent：无 Critical；I-1（退出窗口缩水+诊断死带）当场 TDD 修复；M-1/M-2/M-5 并入同段；M-3/M-4/M-6 存档下方 |
| 版本 | ✅ | 7.6.2（补丁位自主升级） |
| 合并/部署 | ⏳ | 合并生产 + push 后，**下次引擎启动生效**；生产实弹验证需用户授权重启 proxy |

## 冒烟过程记录

- 首次 alpine 假引擎（busybox httpd）误退出 → 意外实弹验证了立即退出路径：
  6 轮探测判 exit，错误信息含 `docker logs` 诊断文本（"No such container"，
  --rm 已移除时的 best-effort 语义），追踪回滚正确。换 python:3.13-slim
  （http.server）后全通。
- B 步是本 PR 的存在意义：同一台机、同一 systemd 语义（control-group kill），
  旧 attached 客户端在此序列下必死（21:12 生产实证），新 detached 容器幸存。

## 评审记录（fresh-context subagent，2026-10-29）

评审 `d833802..7d3779f` 全分支 diff + spec，独立复跑专项 91 passed + 全量 1813。

- **无 Critical。** PR-14 零触碰、pid=None 全消费方容忍、收养路径字节同一、
  版本/README 唯一性、4 个新测诚实性、`-d` 后 name 立即可解析（无探测竞态）、
  metrics 调用方语义等价或更严、daemon 重启容器消失走重建路径 —— 均确认。
- **Important I-1（已修复）：** 首轮 `Running=true` 即 break → 退出观察窗从
  ~3s 缩到单轮，0.3–3s 快退带（kv_capacity 参数 throw、早期 CUDA 失败）漏检
  → 烧满 startup_timeout 误报 timeout 且零诊断；且唯一捕获带里 `--rm` 已移除
  容器，`docker logs` 只回 "No such container"（本 STATUS 冒烟记录亦实证）。
  修复 = `_probe_container_running` 单轮探活（True/False/None 三态，异常不抛）
  + 观察满 6 轮 + 见 `false` 立即断出抢 `docker logs`（--rm 移除前拿真实报错）
  + 未确认按旧语义重试。TDD：3 新测先 RED（窗口 1≠6 / 快退误判 healthy /
  OSError 逃逸）后 GREEN；健康启动多付 ~3s，对 15min 冷载可忽略。
- **Minor 并入修复轮（同段代码）：** M-1 探测 subprocess.run 无守卫 →
  异常按未确认重试、6 轮全败走失败路径不抛；M-2 docker logs 双助手
  `text=True` 严格解码 → `errors="replace"`（PM 诊断 + adapter metrics +
  docker run stderr）；M-5 测试补钉 — docker logs 命令形状（--tail 5000 +
  容器名）、移除带可观察行为（"No such container" 而非假日志文本）。
- **Minor 留档不修：** M-3 `--log-opt` 钉死宿主默认驱动 json-file（换驱动
  宿主启动即失败，错误信息清晰 — 本机已实测接受，接受此设计）；M-4
  `_restore_dead_pids` 会把 fuser 找到的 docker-proxy pid 写进 ninfer_pid
  （无杀伤方、orphan 检测自清 — 语义噪音非错误）；M-6 `NInferConfig.log_file`
  成零读者死字段（无害，后续清理决策归用户）。
