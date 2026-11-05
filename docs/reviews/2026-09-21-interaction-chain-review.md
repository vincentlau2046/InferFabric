# IFF 全链路交互 Trace Review（2026-09-21）

**方法**：4 条链路并行 code-review（A 本地数据面 / B 云路由 / C Dashboard+遥测 / D 控制面），
每条链路由独立 review 代理逐跳 trace（file:line 级），critical/high 级 finding 由主审
亲自读码二次复核（本报告标注 ✅ 表示已独立读码确认）。只读审计，未改代码。

**规模**：41 个原始 finding（A:12 / B:12 / C:6 / D:11），
主审复核后 30 个置信度 ≥0.5 列入优先级清单。

---

## 一、链路 A：本地数据面（client → proxy → engine）

**Trace**：async_server `_handle_forward` → handler 路由表 → auth → cache.get → switch guard →
`dual_gate.acquire` → forward（3 次重试）→ 引擎端口。/v1/messages 走
`forward_anthropic_local`；/v1/embeddings、/v1/rerank 是**独立短路径**。

### Findings（主审全部读码复核 ✅）

| # | 级别 | 位置 | 缺陷 |
|---|---|---|---|
| A1 | **CRIT** ✅ | handler.py:1481-1543/1545-1607 | `/v1/embeddings`、`/v1/rerank` **无 auth**（无 `pm.auth.check`），且 async_server 已注册 → 生产可达。开 API key 后任何人免 key 烧 GPU |
| A2 | **CRIT** ✅ | response_cache.py:37 + forwarder.py:333 | 缓存键错位：Anthropic 路径 get 在 `data["model"]=served_name` mutation **之前**（键含别名），put 在 **之后**（键含 served_name）→ 客户端用别名时缓存永不命中 |
| A3 | **HIGH** ✅ | chat_handlers.py:409-511 | OpenAI `/v1/chat/completions` **只读缓存不写**（无 `cache.put`）→ 该协议缓存永久冷。R5 缓存功能在两个协议上实际都是无效的 |
| A4 | **HIGH** ✅ | handler.py:517-518 | `model_obj.port = _selected_port` 直接改共享 ModelConfig → 多副本并发 Anthropic 请求互相串端口（OpenAI 路径用参数传 port，不对称） |
| A5 | **HIGH** ✅ | chat_handlers.py（全文无 SWITCHING） | OpenAI 路径**无 switch guard**：切换中打到将死端口 → 502，而 messages 路径正确 503+Retry-After |
| A6 | MED | handler.py:520-541 | `_forward_local` 的 try/finally 起在 forward 调用**之后** → forward 抛异常时信号量泄漏 + port 不还原 |
| A7 | MED | handler.py:1481-1607 | embeddings/rerank 绕过限流/日志/switch guard/AUTO_SWITCH，且直接 `pm.mgr.switch()` 可能杀掉独占模型（AUTO_SWITCH=0 时也不理会） |
| A8 | MED | handler.py:523-536 | 本地转发失败（status=None，客户端收 503）不写 RequestLog |
| A9 | MED | chat_handlers.py:384-398 | 非流式 POST 重试：后端已处理但 TCP 断在 `resp.read()` → 重试 = 双推理双计费（流式无此问题） |
| A10 | MED | forwarder.py:74 | sync dev server 的 chunked body 无上限（`rfile.read()` 读到 EOF）→ 线程可被挂死（生产 async 有 100MB 上限，不受影响） |
| A11 | LOW | handler.py:159 | CORS 预检 Allow-Headers 缺 `Authorization`/`x-api-key`（流式响应里却有 → 不一致） |
| A12 | LOW | forwarder.py:155-161 | Anthropic 路径后端 4xx 错误体被吞，替换成通用 502（OpenAI 路径保留原状态+body） |

**Clean**：auth 在 cache 前查、RPM token 生命周期正确（无泄漏）、unknown model 404+AnomalyEvent 正确触发、cloud retry 3x 退避健全、`/admin/cloud/test` SSRF 防护（HTTPS+私网拒绝+白名单+TOCTOU 缓解）健全。

---

## 二、链路 B：云路由（CloudDiscovery → forward_to_cloud）

**Trace**：chat/messages → `resolve_route()`（本地名优先，云注册表双键）→ `forward_to_cloud`
（openai→`{openai_base}/chat/completions`，anthropic→`{anthropic_base}/messages`；3 次重试+退避）。
密钥本体存 `~/.inferfabric/secrets.env`（0600），YAML 只存 `${REF}` 引用。

### Findings

| # | 级别 | 位置 | 缺陷 |
|---|---|---|---|
| B1 | **CRIT/HIGH** | handler.py:1407-1449 | POST 添加 provider 后，内存 `api_key` 是**未展开的字面量 `${REF}`** → 直到 reload/重启前，实际发 `${VAR}` 给云端 → 401。dashboard 添加 provider 的主流程坏掉 |
| B2 | **HIGH** ✅ | config_reloader.py:71 | `self._cloud.reload()` **缺必需的 `config_path` 参数**（签名 `reload(self, config_path)`）→ TypeError 被 except 吞，`/reload-config` 仍回 "reloaded"。**SIGHUP 云配置热加载是假的** |
| B3 | MED | forwarder.py:205-208 | Anthropic 协议转发**缺 `anthropic-version` 请求头** → 官方 api.anthropic.com 必 400；"双协议零转换对 9 preset 都工作"不成立（只有兼容网关可用） |
| B4 | MED | cloud_discovery.py:573-579 | 先展开 `${VAR}` 再解析 YAML → 用户自定义 env 引用被静默改写成 `IFF_<NAME>_KEY`，原变量名孤立 |
| B5 | MED | cloud_discovery.py:348-355 | 短名优先（first-wins）→ provider 前缀消歧形同虚设；且 OpenAI/Anthropic 两协议查找顺序相反，同模型可能解析到不同 provider |
| B6 | MED | cloud_discovery.py:360-367 | reload 失败后旧 `_cloud_models` 不清 → 已知模型退化成**误导性 404**（anomaly 文案与事实相反） |
| B7 | MED | cloud_discovery.py:296/360 | 字典锁序不一致：并发 reload+discover 可触发 "dictionary changed size during iteration" |
| B8 | LOW | proxy_manager.py:138-147 | 首次发现失败即置 `_cloud_discovered=True`（粘滞）；`discovery: false` 的 provider 永不轮询重试 |
| B9 | LOW | forwarder.py:218-222 | `${VAR}` 未设置 → 发**空 key**（仅 log warning），客户端收到 502 而非"provider X 未配置密钥" |
| B10 | LOW | handler.py:1151-1168 | `/admin/cloud/discover` 忽略文档化的 `{provider}` 过滤，且无时长上限（最坏 9×600s） |
| B11 | LOW | chat_handlers.py:361 vs forwarder.py:228 | tools 归一化只在本地上 OpenAI 路径做，云端原样透传 → 同客户端两路行为分裂 |
| B12 | LOW | cloud_discovery.py:168-182 | secrets.env 写入非原子（截断重写，非 tmp+rename）；保存的 cloud_provider.yaml 无 chmod |

**Clean**：admin 守卫全链路健全（hmac 时序安全、非 localhost 无 token 双入口拒启动）、密钥不落日志（RequestLog/anomaly 只含 provider 错误体前 500 字符，YAML 明文 key 自动转引用）。

---

## 三、链路 C：Dashboard/API + 遥测

**Trace**：store.js 3s 轮询 `/api/snapshot`（consolidate 7 组字段 + etag/304）→ monitor.js
（token 小时图走 `/api/token-curve`，15s TTL）→ overview.js。

### Findings

| # | 级别 | 位置 | 缺陷 |
|---|---|---|---|
| C1 | **CRIT** ✅ | handler.py:897-898 | **snapshot etag 只覆盖 status+models**，payload 其余 5 组字段（system/history/token_stats/request_log/metrics_24h）不参与失效；304 客户端保留全部旧值。稳态下（GPU 空闲或恒定负载——本工作站常态）etag 数小时不变 → **dashboard 的 recent requests 表、24h 指标、GPU 温度全部冻结**（刚合的 gpu_temp_c 会被此缺陷吞掉）。附带：store.js:353 在 6s 无 200 时误报"已断线" |
| C2 | HIGH | handler.py:858-903 | "便宜的 304" 不便宜：3× nvidia-smi 子进程 + 每 active 服务健康探测（3×3s 重试）+ 2 SQLite 查询 + 100k 样本 metrics 扫描，**全部跑完才**算 etag 判 304。生产 32 线程池与 chat 转发共享 → GPU 驱动挂起时轮询可占满池子，核心转发排队 |
| C3 | MED | handler.py:794-834 | `/api/token-curve` day/month 仍是**时钟桶**（跨午夜/跨月合并，x 轴非时序）——与刚修的 hour 桶同类缺陷，只是 dashboard 当前不走 day/month，影响 API 消费者 |
| C4 | MED | proxy/metrics_exporter.py:69-109 | Prometheus "counter" 非单调（24h 滑动窗口 + limit 2000 截断 → `rate()`/`increase()` 算错、忙日欠计），且同一 2000 行查询执行 4 次 |
| C5 | MED | handler.py:845-921 | `_handle_snapshot` 顶层无异常兜底（status/list_models/_system_info 裸跑）→ 异常时线程版静默断连、dashboard 显示"API 连接异常" |
| C6 | LOW | dashboard/__init__.py:69 | `__TOKEN_STATS__` 仅页面加载注入，长开 tab 的 7 天 sparkline 不刷新（`/api/token-stats` 端点存在但无 JS 调用） |

**Clean**：XSS 全走 escHtml/esc（model 名/异常文案/`</script>` 注入均已中和）、SQLite WAL+busy_timeout 跨进程竞争已处理、token-curve hour 相对年龄桶与 gpu_temp_c 错误路径无残留缺陷。

---

## 四、链路 D：控制面（CLI/manager → lifecycle → adapter → PM）

**Trace**：`./iff` CLI / admin HTTP → `manager.switch`（GPULock 非阻塞 flock）→
`model_lifecycle._deploy_model` → adapter 注册表分发 → facade → 引擎 launcher（`start_new_session` 全覆盖 ✅）。

### Findings

| # | 级别 | 位置 | 缺陷 |
|---|---|---|---|
| D1 | **CRIT** ✅ | process_manager/base.py:72-100 | **`_validate_pid` 函数截断**：解码 cmdline 后**没有最终 return**，成功路径隐式返回 None（falsy）→ P1-2 PID 复用保护在全部调用方失效。vLLM 优雅 killpg 路径成死代码，恒走 pkill/单 PID 兜底 → `VLLM::EngineCore` 子进程可被孤儿化继续占 VRAM；tts 的 `stop_tts_server(port=None)` 恒清 tracked PID 后**不杀进程**返回 "not running" |
| D2 | **CRIT** ✅ | state.py:167-192 | `gpu_mode` getter 从 active_services 推导且**完全忽略 sleep 状态**（sleep 不移除 active_services）→ 休眠 exclusive 模型推导恒为 exclusive → `wake` 的 `validate_transition(exclusive, exclusive)` 恒 False → **`iff wake` 对休眠 exclusive 模型永远失败** |
| D3 | **HIGH** ✅ | model_lifecycle.py:669-693 | wake 成功后**从不重启被杀进程**：exclusive 分支只写 state（EXCLUSIVE+HEALTHY）不 `_deploy_model`；shared 分支 `gpu_mode==IDLE` 分支不可达 → `_shared_add_service` 恒 "already_active"。两类模型 wake 后都是"state 声称 active，进程已死" |
| D4 | HIGH | facade.py:364-428 | `force_kill_all` 无 sglang/ninfer 容器的 docker stop、无 ninfer pkill，state 清理漏 sglang_pid/sglang_container/ninfer_* → `iff reset` 后容器存活占 GPU，DB 却声称 idle，下次 switch 被占用守卫拒绝 |
| D5 | MED | gpu_lock.py:77-86 | `force_clear()` unlink 锁文件：并发持有者的 flock 留在旧 inode 上仍有效，新进程 O_CREAT 新 inode 再拿"锁" → 互斥被破坏（reset 与代理 auto-switch 并发时双部署） |
| D6 | MED | watchdog.py:78-217 | watchdog 无 sleep 状态感知：休眠 vLLM 健康检查 5 连败（~2.5 分钟）→ 冷部署用户**故意**休眠的模型，破坏 L2 sleep 目的 |
| D7 | MED | db.py:193-215 | `add/remove_active_service` 读-改-写仅每进程 threading.Lock → CLI 与代理两进程间丢更新窗口，模型可静默掉出 active_services |
| D8 | MED | model_lifecycle.py:470/584 | `stop_service`/`sleep_model` 不取 GPULock（switch 有）→ CLI stop/sleep 与代理 auto-switch 并发可杀部署中进程 / 翻转转换校验 |
| D9 | MED | proxy_manager.py:340-388 | auto-switch 健康等待在锁外（最长 500s）+ switching_target 被 mgr.switch 的 finally 清掉 → 等待窗口内第二个 ensure_service 发起并发双部署 |
| D10 | LOW | engine_adapter/ollama.py:24-25 | ollama 健康检查是 daemon 级 → reconcile 每次 tick 把**所有** ollama 模型收养进 active_services（churn） |
| D11 | LOW | manager.py:299-302 | 已 active 的 shared 模型检测到 config drift → 走 `_switch_to_idle()` 停掉**所有** GPU 服务，而非只重启漂移的那一个 |

**Clean**：GPULock 正常路径健全（flock 崩溃自动释放、finally 双清）、start_new_session 全覆盖（vllm/sglang/ninfer/comfyui/tts/asr/llama_cpp/ollama-daemon）、Ruling-H docker-stop 返回码检查全引擎一致、CLI 错误报告充分、热加载原子 dict swap 不干扰 in-flight switch。

---

## 五、跨链路系统性主题

1. **R5 响应缓存功能实际是 no-op**：A2（Anthropic 别名键错位）+ A3（OpenAI 永不写）
   叠加 → 两个协议的缓存都打不中。
2. **状态机与进程现实漂移**：D2/D3（sleep 破坏 gpu_mode 推导、wake 不重启进程）、
   D4（reset 漏容器）、C1（dashboard 冻结在旧值）——设计目标是"state 从现实推导"，
   但 sleep 状态与 dashboard 304 都破坏了它。
3. **文档与实现脱节**：B2（SIGHUP 云热加载断链但 CLAUDE.md 宣称支持）、
   路由表实际比文档多（`/v1/embeddings`、`/v1/rerank`、`/api/anomalies`、SSE buffer、13 个 admin 路由未入 CLAUDE.md）。
4. **新端点绕过安全管道**：embeddings/rerank（A1/A7）无 auth、无限流、无日志、
   可触发 kill 独占模型。
5. **共享可变状态并发纪律**：A4（port mutation）、B7（dict 锁序）、D7/D8/D9（锁缺口）
   ——同一类模式：对共享对象原地修改 + 锁外窗口。

## 六、优先级清单（影响 × 置信度 × 修复成本）

**P0 — 功能损坏 / 安全（建议优先修）**
| 序 | 项 | 一句话 | 成本 |
|---|---|---|---|
| 1 | B2 | SIGHUP 云热加载断链（缺 `config_path` 参数） | 一行 |
| 2 | D1 | `_validate_pid` 截断 → vLLM 优雅停止死代码、孤儿进程 | 小（补 return + 子串检查） |
| 3 | A2 | 缓存键错位（别名永不命中） | 小（canonical 排除 model 或 get 前统一） |
| 4 | A3 | OpenAI 路径补 `cache.put` | 小 |
| 5 | A1+A7 | embeddings/rerank 接入 auth+限流+日志+AUTO_SWITCH 开关 | 中 |
| 6 | C1 | snapshot etag 覆盖全部 payload（或加 304 max-age） | 小-中 |
| 7 | D2+D3 | gpu_mode 推导计入 sleep 状态 + wake 真正重启进程 | 中 |
| 8 | B1 | POST provider 后内存 api_key 需展开（或转发前解析） | 中 |

**P1 — 高影响并发/行为缺陷**：A4（port 竞态）、A5（OpenAI 无 switch guard）、
A6（信号量泄漏）、B3（anthropic-version 头）、C2（304 昂贵 + 池挤占）、
D4（reset 漏容器）、D9（auto-switch 双部署）。

**P2 — medium/low**：A8/A9/A10/A11/A12、B4-B12、C3/C4/C5/C6、D5-D8、D10/D11。
（C3 day/month 桶与 hour 桶同类，若修 C3 可顺手用相对年龄统一。）

## 七、建议修复顺序与 TDD 锚点

1. **B2**（一行传参）→ 回归现有 reload 测试 + 新增 SIGHUP 云 reload 集成测试
2. **D1** → 新增 `_validate_pid` 单测（正常/PID 不存在/cmdline 不匹配/空 cmdline）
3. **A2+A3** → 扩 `tests/unit/proxy/test_r5_response_cache.py`（别名 get/put 键一致；OpenAI 非流式写缓存）
4. **A1+A7** → 新增 `tests/unit/proxy/test_embeddings_rerank.py`（auth 401、限流 429、日志写入）
5. **C1** → 新增 snapshot etag 测试（request_log 变化 → etag 变化）
6. **D2+D3** → 新增 sleep/wake 集成测试（sleep exclusive → wake 成功且进程重启）
7. **B1** → 新增"POST provider 后立即转发"集成测试

每项走 TDD（RED→GREEN）+ 回归 + worktree → merge → 重启代理 → push，与既有流程一致。
