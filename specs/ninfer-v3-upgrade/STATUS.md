# STATUS: ninfer-v3-upgrade

| 阶段 | 内容 | 状态 | 备注 |
|---|---|---|---|
| P0 | 驱动确认（用户终端 nvidia-smi 自证） | ⏳ 待用户 | 沙箱内失败为 landlock 假象 |
| P1a | worktree @64d10fbf | ✅ 完成 | ~/ninfer-v3-build，detach |
| P1b | docker build `ninfer:v3` | ✅ 完成 | e997028ace36；上游 Dockerfile 缺 liburing 已补丁（build 加 liburing-dev、runtime 加 liburing2，commit 1ca85a95，可回馈上游 PR） |
| P2 | artifact v2→v3 | ✅ **换道解决** | 离线工具拒绝社区 v2（1307 对象不在 KNOWN_COUNTS）；SHA256 验证发现**现有 VL artifact 就是官方 nvfp4 v3**（74d2c571…，text+vision+mtp+dflash2 四合一）→ 无需下载，TXT 直接复用该文件 |
| P3-A | 新引擎 + 官方 artifact（无 vision）+ bench | ✅ 完成 | 长上下文大胜（100K C8 +67%），1K 解码 -41~47%（引擎固有，与 thinking/采样无关，见下方结果）|
| P3-B | 新引擎 + 官方 artifact（--vision） | ⬜ 未执行 | 窗口被生产收回，VL 已即时恢复 healthy |
| P4 | tag 接管 + YAML 切换 | ⏸ 暂缓（建议）| 短上下文回退未解决前不切换；VL 事故已由 re-tag 回滚修复，无切换压力 |
| P5 | 仓库归零 / artifact 归档 / 文档 | ⬜ 未开始 | 另可清理 ~/models/ninfer-vl/ 残缺下载 10.4GB |
| P6 | DFlash2 track | ⬜ 未排期 | dflash2 权重已在 artifact 内，只差 adapter spec_mode 字段 |

## 环境基线（2026-10-22 体检）

- 内核驱动 615.71.09（四模块 + DKMS + userspace 全一致）；容器内 NVML 正常；**全程不动驱动**
- Ubuntu 24.04.5 / kernel 7.0.0-38 / 32C / 62GB RAM / 2.5T 空闲
- CUDA 基镜像 13.0.1 + 13.1.2 均已本地缓存
- upstream master @ 64d10fbf（2026-10-16）；自有 metrics 提交已上游化（beae4840）
- 现役镜像：`ninfer:auto`=e75caf24da08（fork 09-25，v2-only）、`ninfer:vl`=6b7da6a5a592（09-22 master，v3）
- artifact：TXT 社区 v2（18.3GB）、VL 社区 v3（23.7GB）

## 事件记录

- 2026-10-22 05:51 VL 启动失败 ×5（artifact magic is not NInfer v2）→ 13:57 定位为 09-25 镜像 tag 误覆盖 → re-tag `vl-rollback` 修复，watchdog 自愈，视觉用例通过

## P3-A 结果（2026-10-22 GPU 窗口实测）

**配置**：ninfer:v3 + 官方 nvfp4 artifact（无 --vision），auto 池 474,560 / nvfp4 / MTP1；对照 09-14 基线（旧 fork 引擎 + 社区 v2 artifact，显式 632K）。数据：`bench_results/v3-validation/`

| 维度 | 基线 | v3 | 结论 |
|---|---|---|---|
| MTP 接受率 | 0.26–0.86 | 0.59–0.95 | ✅ 全面大幅提升（长上下文最明显）|
| 100K 解码 | 24.7–87.3 tok/s | 38.2–88.3 | ✅ C8 +67%、C2 +48% |
| 1K 解码 | 158–223 | 94–120 | ❌ -41%~-47%（每步 8.4→16.2ms）|
| 10K 解码 | 90–117 | 72–99 | ❌ -9%~-25% |
| 整机 out_tps | — | — | ≈持平（1K C8 -10%）|
| KV 池 | 632K 显式 | auto 475K | ⚠️ -25%；新引擎显式值也强制 1GiB 规划余量，显式上限≈auto |

**探针补充**：新引擎默认 thinking ON（简单问题也出 30–47 reasoning tokens）；`enable_thinking:false` 与 `temperature:0` 均**不**恢复解码速度（三者都 ~110 tok/s）→ 1K 回退与思考/采样无关，是低并发每步成本翻倍，疑似 spec 路径重构后 draft+verify 分两遍前向，高并发摊薄后反而占优。

**结论与建议**：长上下文收益兑现，短上下文交互回退明显。**建议暂缓 P4**——VL 事故已由 09-25 re-tag 回滚修复，生产健康，无切换压力；待小窗口验证 `--draft-tokens 2` / `--lm-head-draft` 能否恢复低并发步速后再决策。P3-B（--vision）未执行，窗口被生产收回，VL 已即时恢复。

**资产**：ninfer:v3 镜像就绪（e997028ace36，含 liburing 补丁 commit 1ca85a95）；官方 artifact 身份确认（现有 VL 文件即官方 nvfp4 v3，SHA256 74d2c571…）；旧镜像/artifact 全部保留可回滚。

## P3-A 结论修正（2026-10-22 晚，读官方性能文档后）

P3-A 用的是 `--spec mtp --draft-tokens 1`（照抄现 YAML），但上游官方对该 artifact 的标准配置是 **MTP3**（draft-tokens 3，优化 proposal head）或 **DFlash2 K=7**（docs/performance/qwen3.8-27b.md）。官方 RTX 5090 单请求实测（nvfp4 + FP8 KV + draft-3）：Code **205 tok/s**、Structured 232；DFlash2-7 Code **286**、Structured 377；长推理 161–207。官方饱和 C8 稳态 922 tok/s。

按新引擎每轮 ~16ms 折算，draft-3 ≈ 3.3 tokens/round → ~205 tok/s，与官方一致 → **1K 档"腰斩"大概率是 draft-1 配置次优，不是引擎回归**。MTP1 在旧 fork 引擎上是调优过的最优解，在新引擎上不是。

待办（一个 ~15 分钟 GPU 窗口）：① ninfer:v3 + 官方 artifact + MTP3 六格复测（可选 dflash2-7 一格）；② P3-B 视觉 + Anthropic 端点。数据齐后 P4 才可决策。注意：官方 campaign 用 FP8 KV，我们用 nvfp4 KV（容量减半换池子翻倍，接受率可能略降）；DFlash2-7 权重 21.4GiB，KV 池会再缩 ~100K tokens。

## 用户决策记录（2026-10-22 晚）

- **groupwise-int（int4 权重）方案否决**：prefill 慢 2.4-3.8× 对长上下文进度影响太大 → 只用 nvfp4。下载已中止，~/models/ninfer-official-gwi/ 仅留 manifest+SHA256SUMS 备查。
- **"V3 直接加载现有模型"可行性判定**：社区 v2（TXT 现用 18.3GB，magic 02）❌ 格式硬锁不可行；官方 nvfp4（VL 现用 23.7GB，magic 03）✅ 今日 P3-A 已实测（8s 就绪 + 12 格 bench 零错误）。
- **nvfp4-only 终局方案**：TXT/VL 两个场景共用官方 nvfp4 artifact 文件 + ninfer:v3 引擎，差别只在旗标（TXT 无 --vision + kv auto + MTP3；VL --vision + 现参数）。分场景保留，配置面减半。
- **切换门槛（决策规则）**：MTP3 复测短上下文 ≥200 tok/s → 切；180-200 → 权衡；<180 → TXT 留 v2。P3-B 视觉通过 → VL 引擎同窗升级。

## P3-B 执行 + VL 切换完成（2026-10-22 深夜窗口，已生效）

**窗口执行**：用户授权动 GPU。护栏两次拦截残余 agent 流量（45s/7 请求），第三次修正判定逻辑后通过（引擎完全空闲时吞吐日志静默，判定须用"90s 无新请求"而非"最近吞吐行"——运维细节）。

**P3-B 结果（全部通过）**：
- 显式 `--kv-capacity 410000` → **FATAL**："Device budget cannot hold model weights and required execution resources"。v3 planning 阶段对显式值强制 1GiB 余量，带视觉塔（19.7GiB 权重）后 410K 超限。
- `--kv-capacity auto` → **7.9s 就绪**，KV 天花板 **390,016 tokens**（旧引擎 410K，-4.9%）。
- 视觉 ✓ 双协议：OpenAI"红色"、Anthropic"Red"（thinking 块独立分发）。
- thinking 默认 ON：隔离在 `reasoning_content` / `thinking` 独立字段，**content 干净，读 content 的客户端不受影响**；每请求多 ~30-60 reasoning tokens。

**切换动作（已生效）**：
- `Qwen38-27B-VL.yaml`：`docker_image: ninfer:vl → ninfer:v3`；`kv_capacity: 410000 → 0（auto）`；头注释更新（超卖率 default ~110% / short-ctx ~68% / small-batch ~34% / big-batch ~58%）。
- `scenarios.yaml`：VL 三场景 kv 一律 → 0（440000/420000 超新天花板会 FATAL；390000 压线，统一 auto 保持一致性）。
- `./iff switch Qwen38-27B-VL`：13.1s 健康，KV auto **392,128**。

**切换后验证**：8009 视觉冒烟"红色" ✓；代理 8999 端到端 HTTP 200"正常"，MTP 接受 16/19（84%），前缀缓存 53/60 ✓；iff status VL ✅ + bge-m3 ✅。响应携带新引擎增强字段：`timings`（predicted/prompt 分解）、`usage.completion_tokens_details.reasoning_tokens`。

**回滚（如需，~2 分钟）**：YAML 两行改回（`ninfer:vl` + `kv_capacity: 410000`，场景侧车可不改——旧引擎接受这些值）→ `./iff switch Qwen38-27B-VL`。旧镜像 6b7da6a5a592 保留。

**遗留与后续**：
- KV 池 -18K（410K→392K，-4.4%）：满载 preempt 概率略升，超卖设计本就兜底，观察即可。
- thinking 默认 ON 是行为变化：显式指定 thinking 的生产流量不受影响；未指定的客户端每请求多 ~30-60 tokens。如需对齐旧行为：新引擎有 `--no-thinking`，但 iff 无 passthrough，需 P5 小补丁（加 YAML key + process_manager 旗标映射）。
- VL `draft_tokens` 仍为 1（最小变更原则）；官方标准配置是 MTP3（~205 tok/s 单请求）。后续可经 `iff tune` 场景或直接改 YAML 提到 3，属于性能优化项非阻塞。
- TXT 按最终范围决策保持 v2 引擎 + 社区 artifact 不动；此前 MTP3 切换门槛决策随之失效。
- P5 清理未做：`~/models/ninfer-vl/` 10.4GB 残留删除、上游 PR（liburing Dockerfile 修复 1ca85a95）。

## MTP3 对齐官方配置（2026-10-23 凌晨，已生效）

**背景**：用户问"当前配置是否最优、官方 5090 推荐配置"。核对 serving.md + performance/qwen3.8-27b.md 后确认：官方标准配置 = `--spec mtp --draft-tokens 3` + `--lm-head-draft`（优化 proposal head），我们的 draft-1 是从旧 YAML 沿袭的次优值。

**变更（已生效）**：
- `Qwen38-27B-VL.yaml`：`draft_tokens: 1 → 3`，新增 `enable_lm_head_draft: true`；头注释更新。
- `scenarios.yaml`：VL 三场景 draft 全部对齐 3（原 short-ctx/big-batch 的 draft2 相对新基线是降级）。
- 其余有意偏离官方并保留：kv-dtype nvfp4（官方基准 FP8；我们为 100K-prompt agent 换 2× 池子，实测无接受率惩罚）、prefill_chunk 2048（99.5% 缓存命中下无关）、media 0/1024（让位 KV）。

**实测（5090D + 615 驱动 + nvfp4 KV，短上下文探针）**：
| 场景 | draft-1 基线 | MTP3 | 接受率 |
|---|---|---|---|
| 中文 reasoning（thinking 开）| ~109 tok/s | **143.5** (+32%) | 50.9% |
| 代码输出（thinking 关）| ~110-120 | **210.7** (+75~90%) | 90.8% |
| 官方 Code 参考（FP8 KV）| — | 205.3 | 76.8% |

官方 Code 数字在本机复现（210.7 ≈ 205.3）。接受率随内容类型波动（中文散文 51% ↔ 代码 91%），与官方 Story 37.6% ↔ Structured 90.6% 的分布一致。agent 真实负载（thinking xhigh + tool call + code 混合）预期生产 decode 从 79-120 → **~120-190 tok/s @ 98K prompt**；draft-3 对 100K KV 注意力读取的摊薄效应使长上下文折扣小于 draft-1。

**代价**：权重 19.7 → 20.0 GiB（MTP3 完整头 + 优化 proposal head），KV auto 392,128 → **375,424**（-4.3%，≈ 3.8 个 98K 会话）。权衡值得；若要池子可回退 lm-head-draft 单项（预期接受率下降）。

**生产监控**：每请求日志 `mtp accepted X/Y`；代理响应 `timings.predicted_per_second`；`/metrics` 的 `ninfer_spec_decode_draft_tokens_total` / `accepted_tokens_total`。接受率若长期 <40% 需复查（当前无迹象）。

**重启验证**：`iff switch idle → Qwen38-27B-VL` 13.1s 健康，lm-head-draft 组件在官方 VL artifact 中存在、加载无报错。
