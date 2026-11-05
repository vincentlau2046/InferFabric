# InferFabric 软件工程审计报告

> 审计范围：全量源码（`inferfabric/` 包约 60+ 文件，8 个架构层）+ `tests/`（18 个测试文件，10,513 行）。
> 审计方法：逐层通读源码，重点核对跨层契约（方法签名、状态键、API 字段）与并发/状态机逻辑。
> 日期：2025-07（v5.2 代码基线）

## 架构分层总览

```
CLI (cli.py / __main__.py)
  └─ ModelManager (manager.py) — 编排 + 三态 GPU 状态机
       ├─ GpuStateMachine (gpu_state.py) — 状态推导/reconcile/孤儿 PID
       ├─ ModelLifecycle (model_lifecycle.py) — deploy/stop/sleep/wake
       ├─ ProcessManager 门面 (process_manager/facade.py) — 各引擎子管理器
       ├─ EngineAdapter (engine_adapter/*) — 每引擎适配器
       └─ Proxy (proxy/handler.py + proxy_manager.py) — OpenAI 兼容网关
            ├─ chat_handlers / forwarder / metrics / sse_buffer / request_logger / auth
       ├─ 持久化 (state.py / db.py / migrations/) — SQLite (IFFDB)
       ├─ Telemetry (telemetry.py / metrics_aggregator.py / token_stats.py)
       └─ Dashboard (dashboard/ + js) — 前端轮询
```

---

## 一、P0：核心功能中断

### P0-1 Ollama 适配器调用了门面类上不存在的方法
- **位置**：`inferfabric/engine_adapter/ollama.py:36`
- **问题**：`OllamaAdapter.start()` 执行 `self._proc._start_ollama_model(model)`。`self._proc` 是 `ProcessManager` 门面（`process_manager/facade.py`），而 `_start_ollama_model` 只定义在 `ModelLifecycle`（`model_lifecycle.py:71`）。运行时必然抛 `AttributeError: 'ProcessManager' object has no attribute '_start_ollama_model'`。
- **影响**：任何 `type: ollama` 模型经 `switch` / 自动切换部署时，异常被 `manager.switch` 的 `except` 捕获（manager.py:319-325），返回 error + `profile_state=ERROR`。Ollama 模型永远部署不起来。
- **修复建议**：适配器改为调用门面已有的 `run_ollama(model_ref, keep_alive, num_gpu)`（facade.py:177），或在门面暴露 `_start_ollama_model`（更干净的做法是把 daemon 启动逻辑下沉到门面层）。

### P0-2 `ModelManager.reload_models()` 被调用但未定义
- **位置**：调用点 `config_reloader.py:60,85`、`proxy/handler.py:744`；`specs/pr16a-deploy-closure/spec.md` 明确要求在 `manager.py` 新增该方法，但全仓库 grep 确认 `manager.py` 中没有 `def reload_models`。
- **影响**：SIGHUP / SIGUSR1 信号和 POST `/reload-config`（fallback 分支）都会触发 `AttributeError`，被 try/except 吞掉（config_reloader.py:62-63），HTTP 仍返回 "reloaded" 但模型配置实际未刷新。
- **修复建议**：在 `ModelManager` 增加 `reload_models()`：持 GPU 锁执行 `self._models = load_models(self.models_dir)` 并失效 dashboard 缓存。

### P0-3 `auto_deploy` 后 `self._models` 陈旧
- **位置**：`model_discovery.py:206-208`（`auto_deploy` 末尾）+ `manager.py:457-463`
- **问题**：`auto_deploy` 生成新 YAML 后调用 `load_models(models_dir)`（只返回新 dict，无模块级缓存可"刷新"），随后 `switch_fn(name)` 走 `ModelManager.switch()`，而 `switch()` 用构造时加载的 `self._models`（manager.py:78）做 `self._models.get(target)`（manager.py:194）→ 新模型不在内存 dict 里 → "Unknown model"。
- **影响**：首次部署（`already_configured` 之外的新模型）永远切换失败；只有 YAML 已存在（`already_configured` 分支，handler.py:763-764）才能成功 switch。
- **修复建议**：`ModelManager.auto_deploy` 在生成 YAML 后先调用 `self.reload_models()` 刷新 `self._models`，再 `switch(name)`。

---

## 二、P1：逻辑缺陷

### P1-1 `wake_model` 共享模型路径：进程被杀但状态仍 active
- **位置**：`process_manager/vllm.py:304-313`（`wake_vllm` 杀进程并返回 `killed_for_restart`）→ `model_lifecycle.py:700-717`（共享路径）→ `model_lifecycle.py:243-248`（`_shared_add_service` 早退 `already_active`）
- **问题**：`sleep_model` 不把模型从 `active_services` 移除（只写 sleep_state 表，db.py:196-218）。wake 时 `wake_vllm` 把进程杀掉了，但模型仍在 `active_services`，于是 `_shared_add_service` 判定 "already_active" 直接返回，进程不再重启。
- **影响**：模型在状态里是 active、进程却是死的 → 对该模型的请求 503/502，直到下次 reconcile 才修复状态。
- **修复建议**：`wake_model` 共享路径在调用 `_shared_add_service` 前先 `state.remove_active_service(name)`，让增量部署真正执行。

### P1-2 切换门（switching_target）TOCTOU
- **位置**：`manager.py:266-269`（gate 检查）+ `manager.py:326-328`（finally 里 `set("switching_target", "")`）；`proxy_manager.py:335-356`（`ensure_service` 的锁只包住 switch 发起阶段，健康等待在锁外，最长 500s）
- **问题**：`ensure_service` 在调用 `mgr.switch()` 时设了 `switching_target`，但 `switch()` 的 `finally` 块把它清掉；`_wait_healthy`（proxy_manager.py:355）期间门是开的。两个并发请求（如 shared→shared）可以交错：A 部署中，B 又能通过 gate 发起自己的 switch。`_last_switch` 的 10s 冷却只覆盖前 10s。
- **修复建议**：把 `switching_target` 的清除从 `switch()` 的 finally 移到健康检查完成之后（或由 `ensure_service` 在 `_wait_healthy` 返回后清除，proxy_manager.py:356 已有这行，但被 finally 提前清除使该行为失效）。

### P1-3 `_deploy_model` 失败清理缺 `sglang_ports`
- **位置**：`model_lifecycle.py:167-200`
- **问题**：部署失败时 `stop_all(...)` 只传 `vllm_ports/tts_port/asr_port/comfyui_port/active_services`，没传 `sglang_ports`；随后 `set_multi` 只清 `vllm_pid/comfyui_pid/tts_pid/asr_pid`，不清 `sglang_pid`/`sglang_container`。
- **影响**：SGLang 部署失败后容器/进程可能残留，且陈旧 PID 键残留，影响孤儿 PID 检测。
- **修复建议**：失败路径补传 `sglang_ports`，并清理 `sglang_pid`、`sglang_container` 键。

### P1-4 `AuthManager.reload()` fail-open
- **位置**：`proxy/auth.py:79-106`
- **问题**：`reload()` 先把 `_primary/_guests/_key_map` 清空，再 `_load()`；YAML 解析失败或结构非法时早退，鉴权被整体关闭（fail-open）。对安全组件而言，加载失败保留旧 key（fail-closed）更安全。
- **修复建议**：先加载到临时变量，成功再整体替换；失败则保留旧状态并告警。

### P1-5 `StateDB.set_multi` 并非原子
- **位置**：`state.py:121-124`
- **问题**：docstring 声称 "Atomically set multiple state keys"，实现是逐键 `set()` 循环。IFFDB 每个键独立加写锁，两个键之间崩溃会留下部分状态（如 `gpu_mode=exclusive` 但 `active_services` 未更新）。
- **修复建议**：在 IFFDB 增加事务化的 `set_multi`（单个事务内多键写入），或修正 docstring 并加注释说明崩溃窗口。

### P1-6 `gpu_state._port_pid` 正则取 PID 可能取错列
- **位置**：`gpu_state.py:48-58`
- **问题**：`re.search(r'(\d+)', result.stdout)` 取 fuser 输出中**第一个数字**。fuser -v 输出首行是进程名（如 `python3` 含数字），第一个匹配可能是进程名里的数字（PID=3 即 init！）。
- **影响**：孤儿 PID 检测 / 死 PID 恢复（`_detect_orphan_pids` / `_restore_dead_pids`，gpu_state.py:87-168）可能拿到错误 PID。
- **修复建议**：解析 "TCP" 行后的数字列（PID 列是 fuser 输出中该行的第一个数字字段），或改用 `fuser` 无 -v 的紧凑输出。

### P1-7 `_derive_gpu_mode` 的 KeyError
- **位置**：`gpu_state.py:71-83`
- **问题**：第 74 行用 `self._models.get(s)`（安全），第 77 行却用 `self._models[s]`（不安全）。当某个 active service 的 YAML 被删掉（模型从 dict 中消失）时，reconcile 直接抛 KeyError。
- **修复建议**：第 77 行改为 `self._models.get(svc_name)`。

### P1-8 `stop_service` 的 `force_kill_all` 误伤其他共享服务
- **位置**：`model_lifecycle.py:534-540`
- **问题**：停止单个共享服务后若 GPU 未释放，`force_kill_all()` 会按端口 pkill 掉**所有** vllm/comfyui/tts/asr 进程；其余共享服务的进程被杀，但 `active_services` 只移除了当前这一个（model_lifecycle.py:543-544）→ 状态漂移。
- **修复建议**：只 force-kill 目标服务的端口；或 force_kill_all 后重新推导 active_services（调用 `_gpu_state.reconcile()`）。

---

## 三、P2：健壮性与一致性问题

| # | 位置 | 问题 |
|---|------|------|
| P2-1 | `prometheus.py:180-206` | `handle_vllm_metrics` 使用 `parse_qs`/`urlparse` 但未 import（潜在 NameError；实际使用的副本在 `proxy/metrics.py:9,199`，有正确 import） |
| P2-2 | `prometheus.py:176` 与 `proxy/metrics.py:184` | `gen_counters[port] = (cur_ts, gen_counter)` 会存 `None`；下次调用 `int(None)` 抛 TypeError。`compute` 未包 try/except，metrics 端点会 500/无响应 |
| P2-3 | `token_stats.py:130,168,225` vs `185-186` | `query()` 用 UTC+8 做日聚合分桶，`query_db()` 用 UTC——同一系统内"今天"的边界差 8 小时 |
| P2-4 | `process_manager/base.py` `_pkill_by_port` | pkill 回退模式是 vLLM 专用（`vllm.*:{port}`、`VLLM::EngineCore`），却被所有端口类型复用，非 vLLM 端口的回退清理基本无效 |
| P2-5 | `process_manager/llama_cpp.py:129` | `run_ollama` 超时 60s，对大模型加载偏短（对比 `manager.pull_model` 用 1800s，manager.py:474） |
| P2-6 | `cloud_discovery.py:636` | `pattern.match(mid)` 是前缀匹配，易过度匹配（如模式 "gpt-4" 匹配 "gpt-4-turbo"）；语义上应 `fullmatch` 或 `search` |
| P2-7 | `metrics_aggregator.py:226-232` | `AggregatorThread.run()` 用阻塞 `queue.get()`，无 stop 事件，线程无法干净退出（仅靠 daemon 在进程退出时被杀） |
| P2-8 | `proxy/request_logger.py:147-156` | `close()` 先置 `_enabled=False` 再 join flush 线程；关窗内并发的 `log()` 调用被静默丢弃 |
| P2-9 | `gpu_state.py:340-350` + `db.py:196-218` | `force_reset` 的 `set_multi` 写 KV 键 `sleep_state="{}"`，但 sleep state 实际存于独立 `sleep_state` 表 → 复位后陈旧 sleep 行残留 |
| P2-10 | `manager.py:361-375` vs `dashboard/js/state.js:110` | `/status` 响应没有 `switch_target` 字段，state.js 期望 `status.switch_target` → "正在切换到 X" 提示/遮罩永远不触发；且 dashboard HTML 缓存无 TTL（dashboard/__init__.py:15-55），陈旧 HTML 直到显式失效 |
| P2-11 | `model_discovery.py:101-114` | Ollama 大小解析假设 "4.1 GB" 双 token 格式，而 `ollama list` 实际输出 "4.1GB" 单 token → 发现的 ollama 模型 size_mb 恒为 0 |
| P2-12 | `db.py:182-192` | `add_active_service`/`remove_active_service` 是"读-改-写"，读阶段无锁 → 并发（ensure_service + /switch）存在丢失更新 |
| P2-13 | `proxy_manager.py:207-234` | `_load_runtime_config` 出错时返回 `{}`（fail-open 到默认值），配置错误被静默吞掉 |
| P2-14 | `prometheus.py` 与 `proxy/metrics.py` | `VllmMetricsCollector` 与 `parse_prometheus_text`/`quantile` 在两个模块各有一份，存在 DRY 违规与行为漂移风险 |
| P2-15 | `watchdog.py:120-176` | `_restart_model` 在独立线程里执行 `reconcile()`，与用户触发的 switch 并发，同属读-改-写丢更新 |
| P2-16 | `proxy/chat_handlers.py:274-288` | vLLM 路径重试仅 2 次、0.5s 退避，对慢启动的 vLLM 偏短；409 分支（switch 冲突）未写请求日志 |
| P2-17 | `dashboard/js/app.js:5-16` | 跨标签页切换锁 TTL 30s：标签页崩溃时锁残留最多 30s |
| P2-18 | `process_manager/vllm.py:149-163` | `stop_vllm` 的 docstring 重复粘贴了两遍 |
| P2-19 | `dashboard/js/app.js:1000-1001` | 死代码 `if (window.store) { }` 空块 |
| P2-20 | `process_manager/vllm.py:237-280` | `_pkill_vllm_fallback` 在 `load_models()` 无参调用（无 models_dir 时扫默认目录），与 `ModelManager` 的 `self.models_dir` 可能不一致 |

---

## 四、架构层面观察

1. **切换门逻辑双写**：`switching_target` 的门禁检查在 `proxy_manager.ensure_service`（proxy_manager.py:326-332）和 `manager.switch`（manager.py:266-269）各实现一遍，语义容易漂移，建议收敛到单点。
2. **接口未强制**：`interfaces.py` 的协议（IStateDB/IProcessManager/...）靠结构类型运行，`reload_models` 缺失未被任何启动期自检发现——建议加一个启动自检（鸭子类型断言），把 P0-2 这类"调用不存在的方法"问题在启动时暴露。
3. **GPULock 是进程内锁**：flock 锁文件保证单进程串行，但多进程（CLI + proxy 同时操作）下 `switching_target` gate 存在 TOCTOU（与 P1-2 同源）。
4. **失败清理路径不对称**：`_deploy_model`/`_switch_to_idle`/`force_reset` 的失败/复位清理各自手写 `set_multi` 键列表，`sglang_*` 键反复遗漏（P1-3、P2-9），建议抽一个统一的 `clear_engine_state_keys()`。
5. **前端-后端契约**：dashboard 轮询的字段（`switch_target`、`sleep_state`、`gpu_util`）与 `/status`、`/system` 实际返回字段有错位（P2-10）。

## 五、测试覆盖评价

`tests/` 共 18 个文件、约 10.5k 行，覆盖 rate limit、SQLite、TTS/ASR、云发现、集成等。但本次发现的 P0/P1 多为**跨层契约错位**（方法缺失、状态键遗漏、API 字段错位），现有测试以单组件行为测试为主，缺少：
- Ollama 部署路径的端到端测试（P0-1 未被覆盖）；
- `reload_models` 存在性断言（P0-2/3）；
- 并发 switch 的竞态测试（P1-2）；
- sleep/wake 状态一致性测试（P1-1）。

## 六、修复优先级建议

1. **立即修复**（P0）：Ollama 适配器方法错位、`reload_models` 缺失、auto_deploy 陈旧模型表。
2. **尽快修复**（P1）：wake 共享路径、switch 门 TOCTOU、SGLang 失败清理、auth fail-open、set_multi 原子性、`_port_pid` 解析、`_derive_gpu_mode` KeyError、force_kill_all 误伤。
3. **排期修复**（P2）：按上表顺序处理，重点先修 P2-2（metrics 端点 TypeError）和 P2-12（active_services 丢更新）。
