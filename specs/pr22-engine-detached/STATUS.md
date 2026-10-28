# PR-22A NInfer 引擎 detached 化 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 诊断 | ✅ 2026-10-28 | 21:12 生产重启 docker events 实证 kill signal=15 → die exitCode=0 → --rm destroy；sig-proxy 机制链完整（另见 pr20 STATUS.md 第二次验收记录） |
| 实现 | ✅ 2026-10-29 | PM detached 化 + `_docker_logs_tail`；adapter metrics 日志源 → `docker logs --tail 5000`；收养/stop/container_exists 零改动 |
| TDD | ✅ | TestStartDetached 4 例先 RED（detached 不存在：无 -d / pid 非 None / 无立即退出探测）；metrics 4 例重写 GREEN（test_docker_logs_unavailable 系回归钉，非严格 RED，已注明）；收养 6 测 + 参数名 1 测迁 subprocess.run 契约 |
| 全量回归 | ✅ | 1813 passed（unit+integration；1809 + 4 detached + 1 logs-unavailable − 1 被 TestStartDetached 取代的旧 pid 追踪测） |
| 沙箱实弹冒烟 | ✅ 2026-10-29 | 假引擎（python:3.13-slim 自制 ninfer-serve=http.server）+ `systemd-run --user -p KillMode=control-group`：A) 4s 健康、LogConfig `max-file:3 max-size:50m`；B) **unit stop 整 cgroup kill → 容器 Running=true 仍服务 200**；C) 二次启动 `NInfer adopt — reusing (no cold start)` + `adopted:true`（PR-20 收养首次有对象）；`docker logs` 请求行可取 |
| import 冒烟 | ⏳ | 提交前跑 |
| 干净 worktree 复验 | ⏳ | 沙箱工作区绿 ≠ 提交树绿（per 记忆），提交树复跑全量 |
| cross-review | ⏳ | fresh-context subagent |
| 版本 | ✅ | 7.6.2（补丁位自主升级） |
| 合并/部署 | ⏳ | 合并生产 + push 后，**下次引擎启动生效**；生产实弹验证需用户授权重启 proxy |

## 冒烟过程记录

- 首次 alpine 假引擎（busybox httpd）误退出 → 意外实弹验证了立即退出路径：
  6 轮探测判 exit，错误信息含 `docker logs` 诊断文本（"No such container"，
  --rm 已移除时的 best-effort 语义），追踪回滚正确。换 python:3.13-slim
  （http.server）后全通。
- B 步是本 PR 的存在意义：同一台机、同一 systemd 语义（control-group kill），
  旧 attached 客户端在此序列下必死（21:12 生产实证），新 detached 容器幸存。
