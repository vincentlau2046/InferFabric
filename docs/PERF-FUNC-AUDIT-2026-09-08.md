# InferFabric 项目审计报告 — 性能 + 功能双视角

**日期**: 2026-09-08
**方式**: 双 agent 并行只读审计（性能 agent + 功能 agent）+ 主会话独立核查（测试基线 / 网关现场 / 仓库卫生）
**代码基线**: v5.6.5（commit 4dc92c0），工作区未提交改动为 qwen38/gemma4 的 `max_model_len` 回退

## 一、总体判断

- **功能面**：核心路由、三态状态机、云端预设、Dashboard 主流程完整可用；主要风险集中在**并发竞态**与**热重载失效**
- **性能面**：热路径存在**双重健康等待（最长阻塞 ~480s）**、**每请求新建连接（无连接池）**、**nvidia-smi 子进程风暴**、**前端多路高频轮询 + 全量重渲染**
- **测试面**：基线 **49 failed / 568 passed**（`pytest tests/ -q --ignore=tests/test_local.py`）；`test_local.py` 导入已删除的 `Profile` 类，在收集阶段 ImportError 中断整个套件

## 二、高严重度发现

| # | 视角 | 发现 | 位置 |
|---|------|------|------|
| H1 | 功能 | **SIGHUP 热重载中云端配置实际未重载**：`self._cloud.reload()` 缺必填参数 `config_path` → `TypeError` 被 except 吞掉，云端 provider 配置从未真正重载 | `config_reloader.py:71`（`cloud_discovery.py:360`） |
| M1 | 功能 | **切换竞态**：`current_mode` 在获取 GPU 锁（`manager.py:358`）之前读取（309 行），分支决策（368-378）用过期值，并发切换时先拿锁的线程可能按"仍是 IDLE"走 `_deploy_model`，与进行中切换在同端口双重部署 | `manager.py:309` |
| F1 | 性能 | **请求线程最长阻塞 ~480s**：auto-switch 路径 = `start_vllm` 内 `wait_http(300s)` + `ensure_service` 内 `_wait_healthy(180s)` 双重等待；503 报文 `retry_after=30` 与 180s 不匹配 | `chat_handlers.py:200-210`、`handler.py:330-354`、`proxy_manager.py:279-356`、`model_lifecycle.py:69-197`、`vllm.py:51-147`、`health.py:88-114` |
| F2 | 性能 | **每请求新建 HTTPConnection，无连接池**：高并发下 TIME_WAIT 累积、fd 线性消耗；`proxy_manager.py:363-369` 注释自证 "no pool" | `forwarder.py:440`、`proxy_manager.py:363-369` |
| T1 | 测试 | **`test_local.py` 导入已删除的 `Profile`/`load_profiles`**（`config.py` v4.2 拆包后移除），收集阶段 ImportError 中断全部测试 | `tests/test_local.py:15`（`config.py:827 行`，无 `Profile`） |

## 三、中严重度

| # | 视角 | 发现 | 位置 |
|---|------|------|------|
| M2 | 功能 | **reconcile 不持 GPU 锁**：HealthMonitor 每 60s 的 reconcile 可能在切换中途清掉 `switching_target` 或覆写 `active_services`（`gpu_state.py:223-277`） | `health_monitor.py:49-55/78-86`、`model_lifecycle.py:408` |
| M3 | 功能 | **GPULock.force_clear TOCTOU**：unlink 锁文件后，等待线程仍持旧 inode 的 flock，新进程 open 新文件 → 双持锁 | `gpu_lock.py:77-86`、调用方 `gpu_state.py:342` |
| M4 | 功能 | **force_reset 写遗留 KV 键**：v003/v005 已迁表，但 force_reset 仍写旧键，reconcile 只查表导致状态不一致 | `gpu_state.py:344-354` |
| F3 | 性能 | **JSONL 日志每请求同步写 + flush**（SQLite 路径已是 batch=50 + 后台 flush 线程，JSONL 未对齐） | `request_logger.py:141-144`（SQLite 好模式在 `request_logger.py:104-125`） |
| F4 | 性能 | **nvidia-smi/fuser 子进程风暴**：`_wait_gpu_idle` 每 3s 轮询一次 nvidia-smi（`handler.py:770-779`）；HealthMonitor 每 60s reconcile 又各调 `gpu_used_mb`/`gpu_total_mb`（`health_monitor.py:49-55`，内部 `health.py:32-49` 每次 fork 两个子进程）；`_wait_gpu_idle` 每秒最多 60 次 nvidia-smi（`base.py:126-156`）；`status()` 每次 2 个 nvidia-smi + N 次健康检查（N+1）（`manager.py:418-459`） |
| F5 | 性能 | **token_stats N+1**：N 个活跃端口触发 N+1 次 `mgr.status()` | `token_stats.py:289-301` |
| F6 | 性能 | **query_request_log 每次先取列名**：`SELECT * LIMIT 0` 一次额外往返 | `db.py:357`；调用方 `handler.py:629`（每 5s）、`handler.py:691` |
| F7 | 性能 | **仪表盘 5 路定时器（3s/5s/5s/10s/60s）+ 全量 innerHTML 重建** | `app.js:298/563/1219-1232`、`monitor.js:23/128-139/147`、`state.js:71-187`（ETag/304 已是好模式） |

## 四、低严重度

- **L1** `state.py:41` 表项 `("exclusive","exclusive"): False` 与 docstring（59 行 "✅ same-port swap"）矛盾，实际放行靠 `manager.py:314-316` 特判
- **L2** `db.py:379` `PRAGMA auto_vacuum` 是只读查询，prune 后并未真正 vacuum（`db.py:109` 连接池已设 INCREMENTAL）
- **L3** `ratelimit.py:300-307` `_release` 收 `rpm_held` 参数却只用 `sem_held`；observe 模式 300s 超时返回 `ok=True`（`ratelimit.py:283-288`）
- **L4** `cloud_discovery.py:554` 已做 `${VAR}` 展开，576 行 `startswith("${")` 判断几乎恒 False（死代码）
- **L5** `facade.py:332-333` `force_kill_all` 硬编码 `[8000,8001,8002]`，漏杀 8003/8004/8006/8008/8010/8011
- **L6** `db.py:338-352` `INSERT OR IGNORE` 对重复 `req_id` 静默丢弃
- **F8** `metrics_aggregator.py:146-215` 每次 `get_metrics` 做 5 次 O(n log n) 排序（deque 100000，`metrics_aggregator.py:27-36`）
- **F9** `chat_handlers.py:282-296` 重试退避在请求线程内 `time.sleep`（0.5/1/2s）；`forwarder.py:437-515`
- **F10** `token_stats.py:257-273` 每次调用同步读全量 JSON 状态文件
- **F11** `health_checker.py:28-35` 1s 粒度轮询至 300s

## 五、现场活证据（2026-09-08 会话实测）

- 本会话分类器模型 qwen38-27b-abliterated 的 auto-switch 反复 503：`{"error":"Auto-switch to qwen38-27b-abliterated failed, retry later","status":"switch_failed","retry_after":30}`
- `/status` 快照：`gpu_mode=exclusive`，active_services=[qwen38-27b-abliterated, bge-m3]；qwen38 服务 ✅ 健康（port 8002, PID 1963256），bge-m3 ✅（port 11441）；GPU 30830/32607 MB（94.55%）
- **503 根因链**：手动停止 TTL=600s、10s 冷却（`proxy_manager.py:48-50`）、三态违规/GPU 占用守卫（`manager.py:312-348`）、VRAM 预算守卫（`model_lifecycle.py:90-108`）、`_wait_healthy` 180s 超时（`proxy_manager.py:279-310`）
- 工作区未提交改动 `max_model_len` 163840→139264 缩减直接降低 KV 预算需求 → 503 属临时故障，与 `retry_after=30s` 后恢复的现象一致

## 六、快速胜利（改动小、收益大）

1. **nvidia-smi 5s TTL 缓存**（`health.py:32-49`）— 一处改动消解 F4 子进程风暴
2. **消除双重健康等待** + `retry_after` 对齐 180s（`proxy_manager.py:279-356`、`handler.py:350-351`、`chat_handlers.py:210`）
3. **token_stats N+1 合并**（`token_stats.py:289-301`）
4. **query_request_log 列名缓存**（`db.py:357`）
5. **仪表盘防抖 + 合并 5s 双定时器**（`app.js:1219-1232`、`monitor.js:23/147`）
6. **JSONL 后台化**（`request_logger.py:141-144`）

## 七、测试缺口

1. 503 auto-switch 失败路径回归（mock 手动停止 TTL / 10s 冷却 / VRAM 预算拒绝 / 180s 超时，断言 503 报文与 retry_after）
2. 阻塞时长断言（最坏 480s → 消除双重等待后下降）
3. SSE usage 提取（`chat_handlers.py:191-194` include_usage 注入 + `sse_buffer.py` 有界缓冲）
4. ETag/304 分支（`handler.py:722-727`）
5. 双门限流 observe 300s / reject 5s 429（`ratelimit.py:213-315`）
6. 看门狗自动重启（`watchdog.py:88-93` 120s stuck 守卫、`watchdog.py:105-115` 连续 5 次失败自动重启）
7. VRAM 预算守卫对新 `max_model_len` 的断言
8. 仪表盘轮询回归（DOM 有界、304 服务端行为、子进程次数）
9. 连接池回归（连接复用）
10. ConfigReloader 信号路径（H1 的 TypeError）无行为测试
11. 并发竞态（M1/M2）无测试
12. `force_clear` TOCTOU 无测试（现有 `test_robustness.py` 仅单线程）
13. `prune_request_log` 的 vacuum 行为无测试（L2）

## 八、测试基线（主会话独立核查）

- `pytest tests/ -q --ignore=tests/test_local.py`：**49 failed / 568 passed**（约 60s 跑完）
- 失败聚类：
  - 陈旧断言：`test_v45_comprehensive.py::test_version_format` 写死 `startswith("5.3")`，实际版本 5.6.5
  - 文件迁移：`test_robustness.py` 仍读拆包前的 `inferfabric/dashboard.py`（FileNotFoundError，10 处）
  - 行为差异：`test_v462_sqlite.py::test_insert_and_query_basic`（插入顺序断言 `iff-b != iff-a` 失败，SQLite 不保序）
  - TTS：`test_tts_server.py` 5 处 AttributeError/AssertionError（API 变了测试没跟上）
  - 进程 kill：`test_gap_phase1_process_kill.py` 5 处失败（fallback 逻辑变更）
- `tests/test_local.py` 在收集阶段 ImportError（导入已删除的 `Profile`、`load_profiles`）→ 中断整个套件

## 九、文档 vs 代码不一致

- 主 README API 表：`GET /reload-config` 实际是 **POST**（`handler.py:103`）；`/deploy`、`/pull` 同样只有 POST
- 主 README 端口表过期：写 `8001 | qwen38-27b-vl`，实际 `qwen38-27b-abliterated.yaml` 用 **8002**；`8002 | qwen3-vl-4b` 实际是 **8003**；漏掉 8006（muse-glimmer）、8008（qwen36-35b-vl）、8010/8011（P/D 分离）、11434（ollama-daemon）
- 主 README 只提 SIGHUP，代码还注册了 **SIGUSR1**（`config_reloader.py:40-41`）
- 主 README 宣称"状态永不漂移"，但 `state.py:194-198` 的 `gpu_mode` setter 仍持久化 KV，`force_reset` 仍写遗留键
- 主 README 版本史只写到 v5.5.1，实际 `__version__ = "5.6.5"`
- `iff` 启动器 docstring 还是 v3 "Profile Switcher" 文案（`iff list` 等旧命令），实际委托 v4+ 的 `inferfabric.cli.main()`
- `models.d/README.md:131` 验证命令写的是 `~/projects/inferfabric-sandbox/inferfabric/cli.py`（旧路径）
- 路由表 `"/admin/cloud/providers"` 出现 3 次（`handler.py` L84/L107/L113，dict 去重后等价，冗余）

## 十、仓库卫生

1. `inferfabric/__init__.py.merge`：git 跟踪的合并冲突残留（仅 `__version__ = "5.5.2"`），实际版本 5.6.5
2. 怪名文件 `"=,  font-size=')\npathlib.Path('iff-compare.svg').write_text(c)\nprint('Fixed SVG')\n"`：shell 引号事故产物，git 跟踪，工作区已删（`D` 状态）
3. `sandox/`：`sandbox/` 的拼写错误残留目录（16K，仅 web-dashboard 子目录）
4. `sandbox/`：整仓旧副本（5.2M），已被 .gitignore 忽略
5. `models.d/*.yaml.bak` × 2：旧模型（qwen36-27b-vl、qwen36-35b-vl）的备份
6. 未提交改动：`models.d/qwen38-27b-abliterated.yaml` 与 `models.d/gemma4-31b-vl.yaml`：`max_model_len` 163840→139264（上下文回退）；`models.d/muse-glimmer-vl.yaml`：仅补末尾换行；`inferfabric/forwarder.py` 的守卫改动已随 commit 40d231c/4dc92c0 提交

## 十一、值得保留的好模式

- `db.py:96-113`：连接池 WAL + `synchronous=NORMAL` + `busy_timeout=5000`
- `state.js:71-187`：ETag/304 + 乱序快照丢弃 + 批量渲染
- `monitor.js`：`visibilitychange` 暂停/恢复轮询
- `request_logger.py:104-125`：SQLite batch=50 + 后台 flush 线程
- `sse_buffer.py`：有界缓冲
- `watchdog.py:88-93` 120s stuck 守卫、`watchdog.py:105-115` 连续 5 次失败自动重启

## 十二、建议修复优先级

1. **先修 H1**（SIGHUP 云端重载失效）+ **T1**（test_local.py 阻塞套件）——低成本高收益
2. **M1/M2** 并发竞态 + **F1** 双重等待（与现场 503 直接相关）
3. 性能侧按"快速胜利"清单逐项落实（nvidia-smi 缓存、连接池、N+1 合并、前端防抖）
4. 测试侧：修复 49 个陈旧断言 + 补齐第七节测试缺口

## 十四、模型配置元数据驱动方案审查（2026-09-09）

> 审查视角：模型配置以 `models.d/*.yaml` 为唯一事实源（元数据驱动），逐层核查代码是否真正读取元数据，找出"应读元数据却硬编码"的 bug 与优化点。

### 审查结论

- **主路径是元数据驱动的**：核心启动参数（port、conda_env、served_name、model_type、gpu_role、max_model_len、gpu_memory_utilization 等）全部由 `models.d/*.yaml` 声明，`config.py:523-735` 的 `load_models()` 是单一加载入口，带 `name == 文件名` 校验（`config.py:548-552`）。
- **413 上下文守卫已元数据驱动**（commit 4dc92c0/40d231c）：`forwarder.py:371-415` 的 `check_context_window()` 读取 `ModelConfig.max_context_len`（`config.py:431-443` 多态分发 `vllm.max_model_len` / `sglang.context_length` / `llama_cpp.context_size`），超限 413 拒绝且不重试（`forwarder.py:453-465` 对上游 500 "maximum context length" 也统一转 413）。
- **VRAM 预算守卫也是元数据驱动**：`model_lifecycle.py:90-108` 读 `peak_vram_mb` 元数据（`config.py:729` 从 YAML 解析）。
- 但仍存在多处**未读元数据、直接硬编码**的位置，按严重程度排列如下。

### 硬编码 bug（应读元数据却写死）

**P0 — 影响正确性**

1. **Ollama daemon 端口 11434 多处硬编码**：`config.py:410`（`ModelConfig.port` 的 `type: ollama` 分支直接 `return 11434`）、`config.py:509`（`health_url` 硬编码 `http://localhost:11434/api/tags`）、`engine_adapter/ollama.py:25`（健康检查硬编码）、`manager.py:254`（`check_ollama_health(port=11434)` 默认参数）。而 `ollama-daemon.yaml` 已声明 `ollama_daemon.port: 11434` 和 `health_url`——daemon 端口一旦在 YAML 变更，`type: ollama` 模型的 `.port`/`.health_url` 仍指向旧端口。**建议**：统一改为读取 daemon 的 `ollama_daemon.port` / `health_url` 元数据。
2. **`process_manager/facade.py:332`**：`force_kill_all` 硬编码端口列表 `[8000, 8001, 8002]`。实际 vLLM 端口分布在 8002/8003/8004/8005/8008/8010/8011（8000/8001 无模型使用）→ 清理覆盖不全。**建议**：从 `load_models()` 动态读取 `.vllm.port` / `.sglang.port` 生成列表。
3. **`process_manager/vllm.py:264`**：`_pkill_vllm_fallback` 同样硬编码 `[8000, 8001, 8002]`（257-261 行先尝试 `load_models` 动态读取，异常时才回退到该硬编码 fallback，但 fallback 列表覆盖不全）。
4. **`forwarder.py:424`**：`served_name` 缺失时 fallback 到硬编码旧模型名 `"vllm_qwen27b"`（已不存在的模型）。**建议**：fallback 改为 `model_obj.name`（YAML key）。

**P1 — 影响扩展性**

5. **`facade.py:355-367`**：`force_kill_all` 中 TTS/ASR 清理硬编码端口 8880（TTS）/ 8881（ASR）及进程名 `Qwen3-TTS-Openai-Fastapi/api`、`funasr-server`。建议从 `load_models()` 读 `tts_server.port` / `asr_server.port` 与进程名元数据。
6. **`model_discovery.py:174-178`**：`auto_deploy_model()` 生成的 vLLM YAML 模板硬编码 `conda_env: qw36-27b-vllm`、`gpu_memory_utilization: 0.83`、`max_model_len: 131072`、`max_num_seqs: 4`——对新模型类型不适用。建议：这些参数由仪表盘 Deploy 表单传入（表单已有对应输入项），模板仅保留结构。
7. **`forwarder.py:267-285`**：`estimate_tokens` 的 `len(text)//4` 粗估系数与每图 `1000` token 估值均为硬编码。**建议**：YAML 增加可选字段 `estimate_ratio`（默认 0.25）、`estimate_image_tokens`（默认 1000），`ModelConfig` 暴露后 `estimate_tokens()` 读取。
8. **`forwarder.py:385`**：413 守卫中 `max_output` 默认值 8192 硬编码。**建议**：YAML 增加可选字段 `default_max_output_tokens`（默认 8192），守卫优先读取。
9. **`config.py:91-110`**：`VLLMConfig.build_cmd()` 中 `--host 0.0.0.0`（99 行）与 `--kv-offloading-backend native`（104 行）为代码内写死；高频参数 `--trust-remote-code`、`--tool-call-parser`、`--reasoning-parser`、`--enable-prefix-caching`、`--enable-chunked-prefill`、`--async-scheduling`、`--enable-auto-tool-choice`、`--max-num-batched-tokens`、`--attention-backend` 目前藏在 `extra_flags` 自由字符串里。**建议**：提升为 `VLLMConfig` 显式结构化字段（`SGLangConfig` 已完成同类迁移：`config.py:128-139` 的 `context_length`/`reasoning_parser`/`tool_call_parser` 是好的迁移范例），`build_cmd()` 自动 emit。
10. **`config.py:207`**：`ComfyUIConfig` 默认 `extra_flags = "--cache-none --enable-manager"`，而 `comfyui.yaml` 已移除 `--cache-none`（commit 6c1a5ed）→ 默认值与 YAML 值不一致；YAML 未声明 `extra_flags` 时 `--cache-none` 会被默认值重新注入。**建议**：默认值改 `""`，参数强制由 YAML 声明。

**P2 — 文档/元数据一致性**

11. **主 README 端口表过期**：`8001 qwen38-27b-vl`（实际 `qwen38-27b-abliterated` 用 8002）、`8002 qwen3-vl-4b`（实际 8003）；漏掉 8004（ovis-ocr2）、8005（gemma4-31b-vl）、8006（muse-glimmer）、8008（qwen36-35b-vl）、8010/8011（P/D 分离）、8881（asr-sensevoice）、11442（bge-reranker-v2-m3）。
12. **`models.d/README.md`**：`muse-glimmer-vl.yaml` 的类型列登记为 `vllm`，而 YAML 实际 `type: sglang`（同一行描述列写 "(SGLang)"，与类型列自相矛盾）。
13. **主 README 版本史只写到 v5.5.1**，实际 `__version__ = "5.6.5"`。

### 已做对的（元数据驱动，值得保留）

- `config.py:431-443` `ModelConfig.max_context_len` 多态分发（`vllm.max_model_len` / `sglang.context_length` / `llama_cpp.context_size`）
- `config.py:396-428` `ModelConfig.port` / `served_name` 多态接口
- `config.py:523-735` `load_models()` 元数据加载单一入口 + `name==文件名` 校验（548-552）
- `config.py:88-111` `VLLMConfig.build_cmd()` 端口/模型路径/env 均读元数据；`extra_env` 保护键校验（`config.py:567-578`）
- `config.py:145-173` `SGLangConfig.build_cmd()` 结构化字段已从 `extra_flags` 迁移（`config.py:128-139`）
- `forwarder.py:371-415` 元数据驱动 413 守卫（`model_obj.max_context_len` + `estimate_tokens` 粗估 + `forwarder.py:326-353` `_vllm_tokenize` 精确计数 + `forwarder.py:453-465` 500 溢出检测统一转 413）
- `model_lifecycle.py:90-108` 与 `:219-231` VRAM 预算守卫读 `peak_vram_mb`
- `engine_adapter/{vllm,sglang,ollama,comfyui,tts,asr}.py` 全部从 `ModelConfig` 元数据读 port / health_url
- 仪表盘 JS（`state.js`/`monitor.js`/`app.js`）无硬编码端口/模型名，全部来自 `/api/snapshot` 的 `services_info[].port` 与 `models[]`

## 十三、元数据驱动视角审查（2026-09-09）

> 专项只读审查（Explore agent，36 次工具调用，覆盖 .py/.js/tests/docs）。结论先行：核心启动参数（port / conda_env / served_name / model_type / gpu_role / max_model_len / gpu_memory_utilization）绝大部分已由 `models.d/*.yaml` 驱动；413 上下文窗口守卫在 4dc92c0/40d231c 后已改为元数据驱动。以下硬编码问题按优先级排列。

### P0 — 影响正确性的硬编码 bug

1. **`process_manager/facade.py:332`**：`force_kill_all` 硬编码端口列表 `[8000,8001,8002]`。实际 vLLM 端口分布在 8002/8003/8004/8005/8008/8010/8011（8000/8001 无模型使用）→ 清理覆盖不全。建议：从 `load_models(MODELS_DIR)` 动态读取 `.vllm.port` / `.sglang.port` 生成列表。
2. **`forwarder.py:424`**：`served_name` 缺失时 fallback 到硬编码旧模型名 `"vllm_qwen27b"`（已不存在的模型）。建议：fallback 用 `model_obj.name`（YAML key）。
3. **`process_manager/vllm.py:264`**：`_pkill_vllm_fallback` 同样硬编码 `[8000,8001,8002]`（257-261 行先尝试 `load_models` 动态读取，异常时才回退到该硬编码 fallback，但 fallback 列表覆盖不全）。

### P1 — 影响扩展性的硬编码

4. **`facade.py:355-367`**：`force_kill_all` 中 TTS/ASR 清理硬编码端口 8880/8881 与进程名 `Qwen3-TTS-Openai-Fastapi/api`、`funasr-server`。建议：从 `load_models()` 读 `tts.port` / `asr.port` 与进程名元数据。
5. **`model_discovery.py:166-179`**：`auto_deploy_model()` 生成 vLLM YAML 模板时硬编码 `conda_env: qw36-27b-vllm`、`gpu_memory_utilization: 0.83`、`max_model_len: 131072`——对新模型类型不适用。建议：这些参数经仪表盘 Deploy 表单传入，模板中留注释提醒用户自定义。
6. **`forwarder.py:267-285`**：`estimate_tokens` 的 `len(text)//4` 粗估系数与每图 `1000` token 估值均为硬编码。建议：YAML 增加可选字段 `estimate_ratio`（默认 0.25）、`estimate_image_tokens`（默认 1000），`ModelConfig` 暴露后 `estimate_tokens()` 读取。
7. **Ollama daemon 端口 11434 多处硬编码**：`config.py:410`（`ModelConfig.port` 的 ollama 分支直接 `return 11434`）、`facade.py:187/208`（`http://localhost:11434/api/tags`）、`engine_adapter/ollama.py:25`（健康检查）、`config.py:509`（`health_url`）。`ollama-daemon.yaml` 已声明 `ollama_daemon.port: 11434`，应统一改为读取 daemon 元数据。
8. **`forwarder.py:385`**：413 守卫中 `max_output` 默认值 8192 硬编码。建议：YAML 增加可选字段 `default_max_output_tokens`，守卫优先读取。

### P2 — 增强元数据覆盖

9. **`config.py:91-110`**：`VLLMConfig.build_cmd()` 中 `--host 0.0.0.0`、`--kv-offloading-backend native` 为代码内写死；`--trust-remote-code`、`--tool-call-parser`、`--reasoning-parser`、`--enable-prefix-caching`、`--enable-chunked-prefill`、`--async-scheduling`、`--enable-auto-tool-choice`、`--max-num-batched-tokens`、`--attention-backend` 目前藏在 `extra_flags` 自由字符串里。建议：将高频参数提升为 `VLLMConfig` 显式结构化字段，`build_cmd()` 自动 emit；低频的 `--mamba-ssm-cache-dtype`、`--kv-transfer-config` 可留在 `extra_flags`。
10. **`config.py:203`**：`ComfyUIConfig` 默认 `extra_flags = "--cache-none --enable-manager"`，而 YAML 的 `comfyui.yaml` 已移除 `--cache-none`（commit 6c1a5ed）→ 默认值与 YAML 不一致。建议默认值改为 `""`，参数强制由 YAML 声明。
11. **`process_manager/vllm.py:72`**：KV offloading 靠字符串搜索 `"--kv-offloading-size" in cmd` 检测；待 `kv_offloading_size` 升级为结构字段后可改用 `cfg.kv_offloading_size is not None`。

### P3 — 文档与测试维护

12. **`tests/test_v45_comprehensive.py:1146`**：版本断言 `startswith("5.3")` 与当前 5.6.x 不符（已计入测试基线 49 failed）。
13. **`models.d/README.md`**：`muse-glimmer-vl.yaml` 类型登记为 `vllm`，实际 YAML 为 `type: sglang`，需更正。
14. **主 README 端口登记表**与 `models.d/README.md` 漂移：缺 8004 (ovis-ocr2)、8005、8006 (muse-glimmer)、8008 (qwen36-35b)、8010/8011 (P/D)、8881 (asr-sensevoice)、11442 (bge-reranker-v2-m3)。

### 已做对的（元数据驱动，值得保留）

- `config.py:431-443` `ModelConfig.max_context_len` 多态分发（`vllm.max_model_len` / `sglang.context_length` / `llama_cpp.context_size`）
- `config.py:396-428` `ModelConfig.port` / `served_name` 多态接口
- `config.py:523-735` `load_models()` 元数据加载单一入口
- `config.py:88-111` / `config.py:145-173` `build_cmd()`（vllm/sglang）全结构化元数据
- `forwarder.py:371-415` `check_context_window()` 元数据驱动 413 守卫（`model_obj.max_context_len`）
- `forwarder.py:326-353` `_vllm_tokenize()` 经 `model_obj.port` 连接 vLLM `/tokenize`
- `model_lifecycle.py:90-108` 与 `:219-231` VRAM 预算守卫读 `peak_vram_mb`
- `engine_adapter/{vllm,sglang,ollama,comfyui,tts,asr}` 全部从 `ModelConfig` 读 port/health_url
- `manager.py:145-184` `_extract_engine_params()` 供 API 输出引擎参数
- 仪表盘 JS（`state.js`/`monitor.js`/`app.js`）无硬编码端口/模型名，全部来自 `/api/snapshot` 的 `services_info[].port` 与 `models[]`
