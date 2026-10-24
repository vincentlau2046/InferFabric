# Spec: NInfer 引擎升级到 V3（artifact 容器 v3 + upstream master 引擎）

> 创建：2026-10-22 ｜ 状态：待确认执行范围
> 关联：specs/ 无前置；触发事件为 2026-10-22 Qwen38-27B-VL 启动失败（v2/v3 格式错配）

## 1. 背景与动机

2026-10-22 VL 模型启动失败，根因是 **09-25 重建 fork 镜像时把 `ninfer:vl` tag 指到了只认 v2 artifact 的引擎**，而 VL artifact 早已是 v3 格式（`NINFER\x00\x03`）。该回归潜伏 27 天才暴露。同日排查确认：

- TXT 模型跑在 fork 引擎（09-25，仅支持 v2），落后 upstream master **204 个提交**
- VL 模型跑在 09-22 的 upstream 快照引擎（支持 v3）——**两个模型两套引擎**
- TXT artifact 是社区转换的 v2（18.3GB），VL artifact 是社区转换的 v3（23.7GB）——**两种容器格式并存**
- 本地 fork 有 1 个自有提交（beae4840，keyless /metrics）已进入 upstream，且上游有增强版（per-request 直方图）

版本断层的代价已经真实发生（VL 停机 27 天 + 今天的人工排查）。v3 统一后，"镜像/artifact 格式错配"这一整类故障被消除。

## 2. 现状盘点（2026-10-22 实测）

| 项 | 当前 | 目标 |
|---|---|---|
| TXT 引擎 | fork @aa900fd0 镜像 `ninfer:auto`（e75caf24da08，v2-only） | upstream master @64d10fbf 统一镜像 |
| VL 引擎 | 09-22 master 快照镜像 `ninfer:vl`（6b7da6a5a592，v3） | 同上 |
| TXT artifact | 社区 v2 `qwen3_8_27b_nvfp4.ninfer` 18.3GB | 离线升级为 v3（权重字节不变） |
| VL artifact | 社区 v3 `qwen3_8_27b_vl_nvfp4.ninfer` 23.7GB | 不变 |
| 投机解码 | MTP1 | MTP1（保持；MTP3/DFlash2 为后续 track） |
| KV | nvfp4，TXT 632K / VL 410K 池 | 不变（master 源码确认 `--kv-dtype nvfp4` 受支持） |
| 内核驱动 | 615.71.09（四模块一致，容器内 NVML 正常） | **不动** |

### 驱动安全结论（用户重点关注项）

- 内核 nvidia/nvidia_uvm/nvidia_modeset/nvidia_drm **全部 615.71.09**，与磁盘 DKMS、userspace 库一致
- 容器内 nvidia-smi 完全正常（RTX 5090 D，32GB，实时读数正确）
- **宿主机 `nvidia-smi` 报 "Failed to initialize NVML" 是 DSH agent 沙箱（landlock workspace-write）拦截 `/dev/nvidiactl` 写打开所致，不是主机故障**——用户自己终端运行应正常（待用户一句话确认）
- 历史复盘：6/9 装 595 → 6/20 换 580 → 9/25 晚 purge 580→装 610 → 10/7 装 615 → **10/10 晚 3 次 30 分钟级崩溃重启**（即"反复重启"事件）→ 10/17 起稳定至今
- **本升级全程不碰内核驱动/DKMS/内核，无需重启**。615.71.09 远超 CUDA 13.x 所需的 580 门槛；upstream 官方 Dockerfile 基镜像为 13.1.2（本机已缓存）；本机 09-22 的 13.0.1 镜像已实证可在此驱动上跑 v3 引擎。官方 artifact 卡片的 617.14 仅为官方测试台版本，非硬性要求

## 3. 升级收益（对照当前配置）

1. **正确性**：`fix(ops): restore accurate silu in nvfp4 swiglu` 等修复直接影响现行 NVFP4 权重路径的数值质量
2. **性能**：204 提交含 W4A4 TMA 路由、linear dispatch 按 shape 调优、MoE pipeline、CUDA sync 策略——均为 5090+NVFP4 场景的直接优化
3. **可观测**：/metrics（自有提交已上游化 + 上游直方图增强）在 TXT/VL 双模型恢复一致
4. **模板**：维护版 chat template 随 v3 容器内置（社区 v2 模板边缘 case 修复），Anthropic 协议路由直接受益
5. **架构**：v3 "recipe 不参与执行注册"，`src/targets/` 已删除——本地 package.cpp patch 永久退役，不再需要每次 rebase
6. **新能力**：GBNF 约束解码、`--thinking-budget`、`--kv-capacity auto`、自定义 jinja 模板
7. **前瞻**：qwen4-exp（Qwen3.8-Flash-Next）已并入 master，后续模型升级不再需要引擎迁移
8. **DFlash2 通路打开**（后续 track）：官方 artifact 内置 DFlash2 companion，针对长上下文 MTP 接受率衰减（bench 实测 0.86@1K → 0.26@100K）的对症方案

## 4. 决策与理由

- **先迁移引擎+现有 artifact，不换官方 artifact**：社区 artifact 离线升级零下载、权重字节不变、风险最小；官方 artifact（DFlash2+Vision 统一）作为后续独立 track（P6），避免一次变更过多变量
- **worktree 构建**：本地 checkout 有脏文件（Dockerfile/CMakeLists 改动 + 过时 patch），用 `git worktree` 从 upstream/master 干净构建，互不干扰
- **镜像策略**：新镜像打独立 tag `ninfer:v3`，验证通过后才接管 `auto`/`vl` tag；旧镜像保留为 `*-pre-v3`，回滚 = 两条 re-tag 命令
- **YAML 最小改动**：仅 TXT `weight_path` 一处；镜像 tag 名不变故 YAML 无需改 image 字段

## 5. 非目标

- 不动 vLLM 0.24（章程锁定）
- 不升级内核驱动 / 不清内核模块（610 userspace 残留清理为可选卫生项，用户自行决定）
- 不在本轮引入 MTP3 / DFlash2 / scenarios 重调（P6 track）
- 不更换 VL artifact（已为 v3 且工作正常）
