# NInfer Qwen38-27B-TXT 参数关联分析（2026-09-26）

> 只读分析：access 日志 × 配置 git 历史 × 容器引擎日志 × 基准测试。未修改任何代码/配置，未重启任何服务/容器。
> 模型：Qwen38-27B-TXT（Qwen3.8-27B NVFP4，NInfer 引擎，端口 8007，容器 `iff-ninfer-qwen38`，镜像 `ninfer:auto`，host KV offload 8 GiB pinned）。
> 分析窗口：2026-09-14 19:38 → 2026-09-26 03:15（+0800），42,550 条 access 记录（仅 `Qwen38-27B-TXT` / `qwen38-27b-text` 两个 model 名，已排除 `qwen38-27b-abliterated`）。

---

## 0. TL;DR

**当前最佳参数组合（日志实证 + 基准交叉验证）：**

| 参数 | 推荐值 | 证据级别 | 一句话依据 |
|---|---|---|---|
| `max_concurrency` | **6（保持）** | 日志实证 | con=6 深夜低载窗口 J：138.4 t/s、err 0.7%、cache 99.2%；当前容器实际 running max=5、waiting≤2，有头寸；晚间峰值（H）吞吐跌至 89 t/s 是负载效应而非参数缺陷 |
| `kv_capacity` | **600000（保持，硬顶）** | 日志实证（FATAL 边界） | 622592/640000/950000 全部 OOM 启动失败（需 14.95–21.36 GiB > 可用 14.83–14.93 GiB）；512000 可启动但中途崩溃；600K 余量仅 ~0.4 GiB（KV 池 8.0 GiB，free 1.02 GiB） |
| `draft_tokens` | **1（保持）** | 日志实证 + 基准 | 长上下文实测 MTP 命中率 draft=1 时 74–92%（中位 83%、整体 83.7%）；bench（draft=4）102K 上下文命中率仅 0.26–0.37；draft=2 实测（F 窗口）无增益（out 117.9 vs 126.7 t/s） |
| `prefill_chunk` | **2048（保持）** | 无不利证据 | D→E 由 4096→2048 后 TTFT 无回退（64K+ p50 2943→3050 ms）；与 draft/media 同窗口变更，无法单独归因，但无反例 |
| `enable_mtp` / `mtp_max_context` | **true / 0（不设阈值）** | 日志实证 | 80d5aee（09-15 16:59）修复 50K 阈值误禁用 MTP 后，64K+ 上下文 decode 稳定 105–111 tok/s，MTP 有效 |
| `media_cache_mib` / `media_live_mib` | **0 / 384（保持）** | 无不利证据 | 纯文本模型不产生 media 流量；D（1024/2048）→E（0/384）无任何指标回退 |
| `peak_vram_mb` | **30500（保持）** | proxy 侧预算 | 仅 proxy 预算护栏用，不传给容器；注：38fa7ff 提交信息数字写反（实际 diff 是 31000→30500，不是 30500→30900） |

**关键实测数字（当前配置 con=6/kv=600K/prefill=2048/draft=1）：**
- 64K+ 请求 cache hit 95–99%（J 窗口 99.2%，K 窗口 93.6%），TTFT p50 2.4–3.4 s；
- TPOT（decode 每 token 间隔）64K+ 桶 7.1–8.9 ms；decode 每流中位 108.7 tok/s（引擎日志）；
- MTP accepted 中位 83%（p10 75 / p90 92；64K+ 桶 85.1%），queue p50 31 ms / p90 58 ms；
- 全窗口错误率：稳定 regime 0–1.3%；故障 regime（G/I，模型不可用）27–29%。

**最值得保留的历史对照**：D 窗口（con=4/kv=600K/prefill=4096/draft=4/media 1024-2048，09-22 12:56→09-24 15:59）是实测数据量最大、表现最好的 regime（n=12,012，err 0.2%，TPOT 5.6 ms，179.1 t/s，cache 95.3%）。其 draft=4+prefill=4096 组合的 decode 吞吐（179 t/s）高于 E/K（draft=1）的 118–138 t/s，但 E 窗口四参数同改无法干净归因，且 179 t/s 的混合负载以 64K+ 长会话为主、缓存命中 95%——**不建议**据此回退 draft；若追求单流速度可在低载时段 A/B 复测 draft=4。

---

## 1. 数据源与口径

| 源 | 路径 | 覆盖 | 说明 |
|---|---|---|---|
| A. access JSONL | `~/.inferfabric/logs/access-2026-*.jsonl`（59 个文件，07-30 起） | 本分析 09-14→09-26 | 字段：`req_id/key_name/model/status/ttft_ms/tokens_in/tokens_in_cached/tokens_out/duration_ms/route/cloud_provider/error/timestamp(unix)/ts(ISO,UTC)`。`tokens_in_cached` 自 2026-09-21 14:37Z 起才有（此前的 regime 无 cache 命中率） |
| B. NInfer 引擎日志 | 容器 `iff-ninfer-qwen38` json.log（`docker logs` 导出至 `/tmp/ninfer-cur.log`，1216 行，297 条 `req#N done`） | 仅当前容器（docker Created 2026-09-25T18:14:07Z，即 09-26 02:14 +08），且导出时只覆盖启动后前 ~49 min（18:14→19:03 UTC） | 历史容器均 `--rm` 销毁，json.log 已丢失；`/var/lib/docker/containers/` 目录与旧容器日志需 root，本环境无 sudo |
| C. 基准 | `bench_results/ninfer-bench.{md,json}`（2026-09-14 23:10 生成） | con 1/2/4/8 × in 1K/10K/102K，**当时参数：draft=4、prefill=4096、kv=auto（09-14 kv=auto 时代）** | MTP 命中率与 draft 深度强相关，跨 draft 配置不可直接比 |
| D. 进程/状态 | `~/.inferfabric/logs/proxy.log`（09-22 10:07→09-26 02:46）与 `proxy.log.1`（09-15 20:42→09-22 10:07）；`~/.inferfabric/state.db`（只读，history 仅覆盖 09-25 04:37→18:14） | NInfer 启动事件、FATAL OOM、重启风暴 | 容器命令行参数以 proxy.log 中 `Starting NInfer: docker run …` 行为准（这是"实际生效参数"的唯一权威来源） |

**口径约定：**
- TPOT = `(duration_ms − ttft_ms) / (tokens_out − 1)`，仅统计 decode 窗口 ≥100 ms 且 `tokens_out>1` 的成功请求（剔除首 token 后秒取消的请求，否则分母≈0 会污染中位数；每 regime 报告的 `early-cancel-ish` 计数即被剔除量）。
- 吞吐 `out_tok/s` = `tokens_out / decode窗口(s)` 的中位数（同一筛选条件）。
- TTFT 百分位为线性插值；TPOT 按 `tokens_in` 分桶（<4K / 4–16K / 16–64K / 64K+）以消除上下文长度混杂。
- cache hit = `tokens_in_cached / tokens_in` 中位数。
- 时区：access 日志 `ts` 与容器引擎日志为 **UTC**；proxy.log 与 state.db、git 提交为 **+0800**。本文窗口一律 +0800，UTC 时间单独标注。

---

## 2. 配置变迁（git）与"实际生效窗口"修正

### 2.1 该 YAML 的全部提交（`git log -- models.d/Qwen38-27B-TXT.yaml`）

| 提交 | 时间（+0800） | 内容 | 生效方式 |
|---|---|---|---|
| 8ec2c2f | 09-14 21:21 | rename qwen38-27b-text → Qwen38-27B-TXT | — |
| 0839064 | 09-14 21:59 | 升级 NInfer HEAD（NVFP4 KV cache，615K tokens） | 下次容器重启 |
| 56861a2 | 09-14 22:16 | `kv_capacity: auto`（最大化 VRAM 利用） | 下次容器重启 |
| 44bdd61 | 09-15 16:19 | tier2 C2+C3：bench VRAM + MTP 动态禁用（引入 `mtp_max_context` 50K 阈值） | — |
| 80d5aee | 09-15 16:59 | **修复**：`mtp_max_context=0`（MTP 被 50K 阈值全局禁用） | 09-15 21:14 起重启生效 |
| 2a7321c | 09-16 00:54 | v5.7.1 release（unified engine metrics） | — |
| cc44890 | 09-17 02:05 | 该提交后 yaml 含 `max_concurrency: 4` | **09-17 22:59/23:00 重启才生效**（proxy.log.1 L27350） |
| 51a8b53 | 09-22 12:57 | KV 显式定容：TXT 固定 600K（VL 420K→410K） | **09-22 12:56 重启生效**（proxy.log L818） |
| b6d38e2 | 09-24 15:57 | 调 MTP/prefill/media：`prefill_chunk 4096→2048`、`draft_tokens 4→1`、`media 1024/2048→0/384`、`peak_vram` 对齐 | **09-24 15:59 重启生效**（proxy.log L34901） |
| 38fa7ff | 09-24 15:59 | 提交信息称 "30500→30900"，**实际 diff 为 `peak_vram_mb 31000→30500`**（信息数字写反） | 同上窗口 |
| a1a063c | **09-26 02:52** | `max_concurrency 4→6` | **09-25 17:10 起以"未提交工作区修改"生效**（H/J/K 窗口），09-26 02:52 才落库——比 K 容器启动（02:14）晚 38 分钟 |

### 2.2 实际生效窗口（regime 表，边界 = proxy.log 中成功的 "Starting NInfer" 事件）

| Regime | 生效窗口（+0800） | 实际容器参数 | 修正说明 |
|---|---|---|---|
| **A** | 09-14 19:38 → 09-15 20:42 | con=3, kv=262144(int8), prefill=4096, draft=4, 镜像 `ninfer:qwen38-nvfp4-cuda13` | 首次启动早于 proxy 日志（proxy.log.1 始于 09-15 20:42），参数按 8817df3 时代 yaml **推断**；窗口内 0839064/56861a2（NVFP4+kv=auto）若中途重启过则窗口为混合态——**低置信** |
| **B** | 09-15 20:42 → 09-17 22:59 | con=3, kv=auto, nvfp4, prefill=4096, draft=4 | proxy.log.1 L47 首次 proxy 托管启动；5 代容器；**09-16 01:00→01:43 模型宕机**（watchdog 放弃，v5.7.1 proxy 重启，L4724–4925） |
| **C** | 09-17 22:59 → 09-22 12:01 | con=4, kv=auto, prefill=4096, draft=4, 无 media | cc44890（提交于 09-17 02:05）在 09-17 22:59 重启才生效；09-17 22:59→09-20 09:32 连续运行 ~34 h |
| **D** | 09-22 12:56 → 09-24 15:59 | con=4, kv=600000, prefill=4096, draft=4, media 1024/2048 | 51a8b53 提交 12:57、生效 12:56（L818）；其间 12:01/12:53 两代为 kv=auto 过渡 |
| **E** | 09-24 15:59 → 09-25 12:37 | con=4, kv=600000, prefill=2048, draft=1, media 0/384（peak_vram 30500，proxy 侧） | b6d38e2+38fa7ff 四参数同窗口变更（L34901） |
| **F** | 09-25 12:37 → 15:54 | 同上但 **draft=2（当时未提交的工作区修改）** | 5 代容器：12:37 启动后 **13:33 容器死**（Connection reset，`nvidia-smi --gpu-reset` rc=3 失败）→13:34 重启；**13:45 proxy 重启**（HealthMonitor 重起，孤儿 pid 3727131 清理）；**14:11 容器再死**→14:12/14:15 两代。根因不可考（`--rm` 容器 json.log 已丢失） |
| **G** | 09-25 15:54 → 17:10 | **全部启动失败**：con=6, kv=950000/640000/512000/622592, draft=2 | kv=950000 需 21.36 GiB > 可用 14.83–14.93 GiB（FATAL ×16，L46436–47151）；kv=640000 需 15.29 GiB（L47497/47775/47823）；kv=512000 启动后 16:19 请求中途崩溃（L48166）；kv=622592 需 14.95 GiB（L48706 起） |
| **H** | 09-25 17:10 → 20:57 | con=6, kv=600000, prefill=2048, draft=1（均未提交） | 单容器稳定 3 h 47 min（L49694），晚高峰负载 |
| **I** | 09-25 20:57 → 22:01 | **全部启动失败**：docker nvidia-container-runtime-hook ret=125/127（~36 次尝试，L51375–52506） | GPU 驱动/运行时层故障，非参数问题 |
| **J** | 09-25 22:01 → 09-26 02:14 | 同 H（参数同、未提交） | 4 代容器（22:01/22:08/22:41/00:38，L52884/53057/53550/55296），夜间低载 |
| **K** | 09-26 02:14 → 现在 | 同 H，**02:52 由 a1a063c 落库** | 当前容器（L56850；docker Created 2026-09-25T18:14:07Z），引擎日志可用 |

**修正要点**：按"提交时间"切窗会错——cc44890（con=4）提交于 09-17 02:05 却 21 h 后才生效；a1a063c（con=6）09-26 02:52 才提交却 09-25 17:10 起已生效；51a8b53 提交（12:57）晚于生效（12:56）。一切以 proxy.log 的 `Starting NInfer` 命令行参数为准。

---

## 3. 分 regime 指标（access 日志，n / 错误率 / TTFT / TPOT / 吞吐 / cache）

| Regime | 参数 | n | err% | 主要错误 | TTFT p50（全） | TTFT p50/p90（64K+） | TPOT med（16-64K / 64K+ / 全） | out t/s med | cache med | tin 构成 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | con3 kv262K-int8 d4 | 56 | 28.6% | 503×12, 404×4 | 839 | — | 6.3(16-64K) / 6.1 | 162.8 | —（无字段） | 59% <4K |
| B | con3 kvauto d4 | 8405 | 4.1% | **503 model_not_active×297**（09-16 宕机）, 502×28 | 2401 | 32974 / 91024 | 8.9 / 7.4 / 5.8 | 171.4 | — | 62% <4K |
| C | con4 kvauto d4 | 14539 | 2.4% | **502×265**（09-19 23UTC×159、09-20 05UTC×86 两簇）, 503×90 | 2171 | 13208 / 49355 | 5.5 / 6.1 / 5.6 | 179.7 | 95.1%（n=622，仅窗口尾部有字段） | 53% <4K，5% 64K+ |
| **D** | con4 kv600K d4 pf4096 media1024/2048 | **12012** | **0.2%** | 503×20, 502×6 | 3098 | **2943 / 33288** | **5.6 / 5.6 / 5.6** | **179.1** | **95.3%** | 43% 16-64K，56% 64K+ |
| E | con4 kv600K **d1 pf2048** media0/384 | 3205 | 0.2% | 502×5 | 3502 | 3050 / 28431 | 8.5 / 7.9 / 7.9 | 126.7 | 91.5% | 44% 16-64K，55% 64K+ |
| F | con4 kv600K **d2**（未提交） | 1140 | 3.6% | **503 auto_switch_failed×41**（重启抖动） | 4003 | 3458 / 48985 | 11.3 / 8.1 / 8.5 | 117.9 | 84.6% | 46% 16-64K，50% 64K+ |
| G | 全失败（kv 950K/640K/512K/622592） | 302 | 29.1% | 503×88（模型宕机） | 2709* | 2709 / 17285 | 7.1* | 140.3* | 98.7%* | *仅崩溃前存活请求 |
| H | **con6** kv600K d1 pf2048（未提交） | 700 | 1.3% | 503×9 | 7966 | 6085 / 53063 | 12.5 / 11.1 / 11.2 | 89.3 | 92.1% | 51% 16-64K，48% 64K+ |
| I | 全失败（docker 驱动故障） | 110 | 27.3% | 503×30 | 2133* | 2230 / 8019 | 6.6 / 8.6 / 7.4 | 134.5* | 98.5%* | *风暴前存活请求 |
| J | **con6** kv600K d1 pf2048 | 1582 | 0.7% | 503×6, 502×5 | 2484 | **2452 / 16838** | 9.4 / 7.1 / 7.2 | **138.4** | **99.2%** | 48% 16-64K，52% 64K+ |
| K | **con6** kv600K d1 pf2048（a1a063c 落库） | 490 | **0.0%** | 无 | 4009 | 3364 / 32609 | 8.9 / 8.9 / 8.9 | 112.7 | 93.6% | 47% 16-64K，53% 64K+ |

注：G/I 行标注 `*` 的指标来自故障前仍活着的容器，仅作参照；A 的 64K+ 桶无样本（当时负载以短请求为主）；B/C 的 cache 列在 09-21 14:37Z 之前无数据。

**当前容器引擎日志（K，18:14–19:03 UTC，297 条 done 请求）：**
- MTP accepted 整体 **83.7%**（74,868 / 89,423）；分桶 16–64K **82.5%**（n=83）、64K+ **85.1%**（n=97）；单请求命中率 p10 75 / med 83 / p90 92。
- decode 每流中位 **108.7 tok/s**（p10 95.2 / p90 124.5）；64K+ 桶 105.5。
- queue p50 **31.4 ms** / p90 57.8 / max 785；prefill 中位 3.1 k tok/s（chunked）；running 中位 2 / max 5（≤con=6）；throughput 行 waiting 中位 1 / max 2。
- 启动行：`engine ready | qwen3.8-27b/nvfp4 | total 6.8s | weights 16.4 GiB`；`capacity | KV 600,000 tokens, nvfp4, explicit | pages 9,375/24,576 | runtime 13.5 GiB | free 1.02 GiB`；`context cache | 6 active + 6 cached device states | host 8 states, 8.00 GiB KV`。

---

## 4. 分参数归因

### 4.1 `max_concurrency`：3 → 4 → 6
- **3→4（B→C）**：同桶 TPOT 改善——64K+ 7.4→6.1 ms、16-64K 8.9→5.5 ms；吞吐 171.4→179.7 t/s。B 的错误率（4.1%）主要是 09-16 01:00–01:43 宕机窗口的 297 条 503，与参数无关。
- **4→6（E/F→H/J/K）**：同参数不同负载强度下，H（晚高峰）out 89.3 t/s、TPOT 11.2 ms vs J（深夜）out 138.4 t/s、TPOT 7.2 ms、cache 99.2%——**H 与 J 的参数完全相同**，差异全部来自负载强度（晚间多 agent 会话并发 + 排队）。当前容器 running max=5、waiting≤2 说明 con=6 尚未饱和；引擎 queue p50 仅 31 ms，因 64K+ 请求 90%+ 缓存命中、prefill 成本被大幅压低。
- bench 交叉（in=1K：con 1→2→4→8 out_tps 208→321→365→387；in=10K：96→154→156→194；in=102K：20→22→23→23）：**con=4 之后聚合吞吐进入边际递减区**，con=6 仍处平台段，不劣。
- **结论：con=6 对本工作负载（高缓存命中的 agent 长会话）无实测伤害，且留有并发余量；若在意晚高峰单流延迟，con=4 是 bench 支持的回退点。**

### 4.2 `kv_capacity`：262144(int8) → auto → 600000（硬顶）
- 09-25 15:54–16:20 的 OOM 风暴给出精确 VRAM 边界（FATAL 行）：需求 = 基线(~2.7 GiB 非 KV 运行时) + kv×~19.6 KB/token。kv=512000 需 ~12.8 GiB（可启动，但 16:19 请求中途崩溃）；**622592 需 14.95 GiB > 可用 14.83 GiB（差 ~12 GiB 中 0.12 GiB，仅差 ~22K tokens）**；640000 需 15.29 GiB；950000 需 21.36 GiB。可用量随 host 状态在 14.80–14.96 GiB 间浮动。
- kv=600K 实测：KV 池 8.0 GiB（9,375/24,576 页），runtime 13.5 GiB，free 1.02 GiB → 可行但贴顶。
- 定性对照 C（kv=auto）→D（kv=600K）：64K+ TTFT p50 13.2→2.9 s、错误率 2.4%→0.2%。注意 C 的 64K+ 仅占 5% 流量（新会话多、前缀复用低），D 占 56%（长 agent 会话、复用高），**该改善部分是流量构成变化而非纯参数效应**；auto 时代的引擎日志已丢失，无法确认 auto 实际池大小。

### 4.3 `prefill_chunk`：4096 → 2048
- 与 draft 4→1、media 0/384、peak_vram 变更同窗口（E），无法单独归因。观测：TTFT 无回退（64K+ p50 2943→3050 ms，p90 反而 33288→28431 略好），prefill 速度 chunked 后 2.5–7 k tok/s 仍可接受。
- **无不利日志证据；保持 2048。**

### 4.4 `draft_tokens`：4 → 1（2 短暂试过一次）
- bench（draft=4，09-14）：MTP 命中率随上下文衰减——1K/0.86 → 10K/0.34–0.43 → 102K/0.26–0.37（con=8 时 102K 队列 73.7 s、dec 24.7 tok/s）。
- 生产实测（draft=1）：64K+ 上下文命中率 85.1%（16-64K 82.5%），decode 105–111 tok/s。**在本工作负载的主体桶（16K+ 上下文）上，draft=1 的每步 MTP 效率显著高于 draft=4 在 102K 的表现**——长上下文下多 token 前瞻的连击接受率急剧下降。
- draft=2（F 窗口，n=1140）：TPOT 64K+ 8.1 vs E(draft=1) 7.9 ms，out 117.9 vs 126.7 t/s，cache 84.6%（重启清缓存所致）——**无增益**，且该窗口 41 条 503 来自容器两次死亡+proxy 重启的抖动，样本不干净。
- **结论：本负载 draft=1 最优；draft=2/4 无数据支持。**

### 4.5 `media_cache_mib` / `media_live_mib`：1024/2048 → 0/384
- 纯文本模型无 media 流量，D→E 变更对全部指标无可见影响。**保持 0/384（省显存给 KV）。**

### 4.6 `peak_vram_mb`：19000 → 31000 → 30500
- proxy 侧 VRAM 预算护栏（97% 规则），**不传入容器**，对引擎行为无直接影响；仅影响 proxy 判断"GPU 是否还能装下本模型"。当前 30500 满足 bge-m3(~480 MB) 共存预算。38fa7ff 提交信息数字写反（"30500→30900"，实际 diff 31000→30500）——建议后续修信息，不影响运行。

### 4.7 非参数故障（记录在案，不计入参数归因）
- **F 窗口（09-25 12:37–15:54）**：draft=2 容器 13:33 与 14:11 两次死亡（`Connection reset by peer`→`Connection refused`，`nvidia-smi --gpu-reset` rc=3 失败），13:45 proxy（systemd）重启（孤儿 pid 3727131 清理）。41 条 503 全部来自此抖动。根因不可考（`--rm` 容器日志丢失）。
- **I 窗口（09-25 20:57–22:01）**：docker nvidia-container-runtime-hook ret=125/127 风暴（~36 次启动尝试全败），GPU 驱动/运行时层故障，与参数无关。
- **C 窗口 502×265**：集中在 09-19 23:00–23:59 UTC（159 条，=09-20 07:00–08:00 +08）与 09-20 05:00–05:59 UTC（86 条，=13:00–13:59 +08）；后者与 13:23/13:27 两代容器重启时间吻合；前者在 proxy.log.1 中无对应启动/停止事件（疑似整夜模型不可用或 proxy 转发层问题），error 字段为空，根因不可考。

---

## 5. 建议（每参数一条，标注证据级别）

| 参数 | 建议 | 级别 | 依据 |
|---|---|---|---|
| `max_concurrency` | **保持 6**；若晚高峰单流延迟敏感，可 A/B 回 4 | 日志实证（con=6 深夜 138 t/s/err 0.7%；当前 running max 5）+ bench（con4→8 边际递减） | J/K 窗口 + bench in=1K/10K 行 |
| `kv_capacity` | **保持 600000，不要上调**（622592 仅差 0.12 GiB 也 OOM）；想要更大上下文容量应优先释放 bge-m3/host 占用而非贴顶 | 日志实证（FATAL 边界 4 组数据点 + 512K 中途崩溃） | proxy.log L46436/47497/48706；容器 capacity 行 |
| `draft_tokens` | **保持 1**；不建议升到 2/4（F 窗口 draft=2 无增益；bench 显示长上下文 draft=4 命中率仅 0.26–0.37） | 日志实证 + bench | K 引擎日志 MTP 85.1%@64K+；bench 102K 行；F 窗口 |
| `prefill_chunk` | **保持 2048**（无不利证据；如需独立验证可在低载时段 4096/2048 各跑 30 min 对照） | 无不利证据 | D→E 对比（TTFT 持平） |
| `enable_mtp` | **保持 true**（64K+ decode 105–111 t/s 依赖 MTP；80d5aee 后的 mtp_max_context=0 勿回退） | 日志实证 | K 引擎日志；80d5aee |
| `media_cache_mib/live` | **保持 0/384**（纯文本模型） | 无不利证据 | D→E 对比 |
| `peak_vram_mb` | **保持 30500**；顺手修正 38fa7ff 的提交信息（可选） | proxy 侧护栏，不涉引擎 | 38fa7ff diff vs 提交信息 |
| （流程） | 未来调参**一次只动一个参数**，且固定"晚高峰 1 h + 深夜 1 h"两个负载剖面各跑 ≥30 min 再比——本次 E 窗口四参数同改是最大归因障碍 | 方法论 | E 窗口混杂 |

---

## 6. 数据覆盖与局限

1. **E 窗口（draft=1/prefill=2048 的主证据）n=3205、约 22 h**，且 draft/prefill/media/peak_vram 四参数同改，单参数归因只能靠 bench 与当前容器日志间接佐证；draft=1 的最干净证据是 K 窗口引擎日志（但仅覆盖启动后前 49 min，297 条）。
2. **H/J/K 只有一天**，且 J/K 处于深夜低载——"con=6 无伤害"的结论基于低载剖面；晚高峰（H）只有 3.8 h 单容器样本，H 的 TPOT 11 ms 是负载效应，无法与"con=6 在高峰固有开销"分离。
3. **B/C（kv=auto 时代）引擎日志丢失**（`--rm` 容器 + docker json.log 需 root），auto 实际 KV 池大小未知，C→D 的 64K+ TTFT 改善无法完全归因给 kv=600K。
4. **A 窗口低置信**：首次启动早于 proxy 日志，参数为推断值；窗口内可能横跨 0839064/56861a2 两次配置变更。
5. **F 窗口（draft=2）被重启污染**：4 代容器、41 条 503、cache 冷启动（84.6%），其吞吐数字偏悲观，draft=2 的真实水平需干净窗口复测。
6. **state.db history 仅覆盖 09-25 04:37→18:14**，更早的切换/停止记录不可考；proxy.log.1 始于 09-15 20:42，09-14 19:38→20:42 与 09-15 20:42 前的事件无过程日志。
7. **bench 与生产配置不可完全对齐**：bench 是 kv=auto/draft=4/prefill=4096 时代（09-14 23:10），MTP 命中率跨 draft 深度不可比，仅 concurrency 扫描方向可用。
8. C 窗口 502×265 与 F 窗口容器两次死亡的**根因均不可考**（error 字段为空 / 容器日志丢失），只能标注时间关联。

---

## 7. 附录：可复现性

### 7.1 命令清单
```bash
# access 日志分 regime 统计（完整脚本 /tmp/ninfer_analysis.py，关键逻辑见 7.2）
python3 /tmp/ninfer_analysis.py

# NInfer 启动事件 + FATAL（regime 边界与 kv 上限证据）
grep -n "Starting NInfer" ~/.inferfabric/logs/proxy.log{,.1}
grep -n "requested Engine runtime reservation" ~/.inferfabric/logs/proxy.log   # FATAL OOM

# 当前容器引擎日志（无需 sudo）
docker logs iff-ninfer-qwen38 > /tmp/ninfer-cur.log
# 引擎行：req#N done | ... | TTFT/total/queue/prefill/decode/mtp accepted
#        throughput | 5.0s | ... running N | waiting N

# state.db 只读探查（勿写）
sqlite3 ~/.inferfabric/state.db ".tables" ".schema history"
sqlite3 ~/.inferfabric/state.db "SELECT * FROM history ORDER BY ts DESC LIMIT 50"

# 配置历史
git -C /home/vince/inferfabric log --date=format:'%Y-%m-%d %H:%M' --pretty='%h %ad %s' -- models.d/Qwen38-27B-TXT.yaml
```

### 7.2 解析脚本关键逻辑（/tmp/ninfer_analysis.py）
```python
# 1) 过滤：model ∈ {"Qwen38-27B-TXT","qwen38-27b-text"}，排除 abliterated；按 timestamp 排序
# 2) regime 边界 = proxy.log "Starting NInfer" 成功事件（+0800 转 UTC epoch）：
#    A: Z(09-14,11,38)→Z(09-15,12,42)   B: →Z(09-17,14,59)   C: →Z(09-22,4,1)
#    D: Z(09-22,4,56)→Z(09-24,7,59)     E: →Z(09-25,4,37)    F: →Z(09-25,7,54)
#    G: →Z(09-25,9,10)  H: →Z(09-25,12,57)  I: →Z(09-25,14,1)  J: →Z(09-26,18,14)-86400  K: →now
#    （Z(day,hh,mm) = 2026 年 UTC epoch；本地=UTC+8）
# 3) TPOT = (duration_ms - ttft_ms) / (tokens_out - 1)
#    仅 status==200 且 tokens_out>1 且 (duration_ms-ttft_ms)>=100ms（剔除早取消）
# 4) 桶：<4K / 4-16K / 16-64K / 64K+（按 tokens_in）；每桶取 TPOT 中位数
# 5) out_tok/s = tokens_out / decode窗口s，同 3) 筛选，取中位数
# 6) cache = median(tokens_in_cached / tokens_in)（字段 2026-09-21 14:37Z 起）
# 7) 百分位为线性插值（非最近邻）
```
引擎日志聚合（/tmp/ninfer-cur.log）：正则 `req#(\d+) done \| … \| mtp accepted ([\d,]+)/([\d,]+) \(([\d.]+)%\)` 解析 297 条；按 prompt 长度分桶（16-64K / 64K+）汇总 accepted/total 比率；queue/prefill/decode 取中位；`throughput` 行解析 running/waiting。

### 7.3 关键行号证据（proxy.log，+0800）
| 事件 | 文件:行 |
|---|---|
| 09-15 20:42 首次 proxy 托管启动（regime B 起点） | proxy.log.1 L47 |
| 09-16 01:26–01:38 watchdog 放弃风暴 | proxy.log.1 L4724–4925 |
| 09-17 22:59 con=4 生效（regime C 起点） | proxy.log.1 L27350 |
| 09-20 09:32/12:49/13:23/13:27/14:11 重启簇（C 窗口 502 关联） | proxy.log.1 L47343/48263/49570/49694/50658 |
| 09-22 12:56 kv=600000 生效（regime D 起点） | proxy.log L818 |
| 09-24 15:59 prefill=2048/draft=1 生效（regime E 起点） | proxy.log L34901 |
| 09-25 12:37 draft=2 启动（regime F 起点） | proxy.log L42676 |
| F 窗口 13:33:42 容器死（Connection reset）→ 13:34:33 gpu-reset rc=3 → 13:34:37 重启 | proxy.log ~L43405/43475/L43485 |
| F 窗口 13:45 proxy 重启（孤儿 pid 3727131）→ 14:11:56 容器再死 → 14:12/14:15 两代 | proxy.log L43684/L44290/L44557 |
| kv=950000 FATAL（需 21,361,159,680 B > 14,930,560,256 B）×16 | proxy.log L46436–47151 |
| kv=640000 FATAL（需 15,289,806,336 B） | proxy.log L47497/47775/47823 |
| kv=512000 启动（16:19，后中途崩溃） | proxy.log L48166 |
| kv=622592 FATAL（需 14,948,888,064 B > 14,827,078,912 B） | proxy.log L48706 起 |
| 09-25 17:10 con=6/draft=1 生效（regime H 起点，单容器 3h47m） | proxy.log L49694 |
| I 窗口 docker nvidia-container-runtime-hook 风暴（~36 次） | proxy.log L51375–52506 |
| J 窗口 4 代容器（22:01/22:08/22:41/00:38） | proxy.log L52884/53057/53550/55296 |
| 09-26 02:14 当前容器启动（regime K 起点） | proxy.log L56850 |

### 7.4 基准数据（bench_results/ninfer-bench.md，2026-09-14 23:10；draft=4/prefill=4096/kv=auto）
| in | con | wall_s | TTFT_s | pf_tps | dec_tps | MTP | q_s | out_tps |
|---|---|---|---|---|---|---|---|---|
| 1024 | 1/2/4/8 | 2.5/3.2/5.6/10.6 | 0.16/0.29/1.0/3.28 | 6586/5335/6814/6476 | 222.6/193.4/166.7/158.3 | 0.86/0.77/0.66/0.65 | 0/0.08/0.84/3.12 | 208/321/365/387 |
| 10240 | 1/2/4/8 | 5.3/6.6/13.1/20.8 | 0.94/1.45/3.53/7.63 | 10750/10428/10136/10068 | 117/115/90/96 | 0.35/0.41/0.34/0.43 | 0/0.47/2.53/6.62 | 96/154/156/194 |
| 102400 | 1/2/4/8 | 26/47/90/175 | 20.1/30.9/52.0/93.6 | 5003/4883/4989/5063 | 87.3/54.0/41.6/24.7 | 0.26/0.35/0.37/0.34 | 0/10.2/31.8/73.7 | 19.7/21.7/22.7/23.4 |

*报告完。数据截至 2026-09-26 03:15 +0800；K 窗口指标随时间继续累积。*
