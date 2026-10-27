# PR-20 Zero-Downtime Restart Status

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 诊断 | ✅ | 2026-10-27 | 纯观测：91.7s 拒连 / 323ms 新进程 / ~15min 引擎冷启解剖 |
| P1 拍板 | ✅ | 2026-10-27 | 用户批准四项口径（P2 socket activation 缓议） |
| Specify | ✅ | 2026-10-27 | spec.md |
| Implement ③ StateDB 封印 | ⏳ | 2026-10-27 | seal + begin_shutdown（TDD） |
| Implement ① 排空+硬退出 | ⏳ | 2026-10-27 | _build_runner/_shutdown_gracefully + TimeoutStopSec=45 |
| Implement ② 三态收养 | ⏳ | 2026-10-27 | _container_info + adopt 分支 |
| Implement ④ 面板热失效实证 | ⏳ | 2026-10-27 | 运行时冒烟 + 运维文档 |
| Quality gates | ⏳ | | 全量 pytest + import 冒烟 + 交叉评审 |
| Release | ⏳ | | 合并生产 + push；生产重启验收（用户授权后） |

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
