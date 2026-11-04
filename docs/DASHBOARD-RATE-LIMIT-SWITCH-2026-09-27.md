# Dashboard「速率限制」卡片调查（2026-09-27）

> 只读调查：Dashboard 前端（`inferfabric/dashboard/`）× 流控实现（`ratelimit.py`）× 注入点（`proxy_manager.py`）× 请求路径（`proxy/handler.py`、`proxy/chat_handlers.py`）× 运行时配置（`~/.inferfabric/iff.yaml`）。未修改任何代码/配置，未重启任何服务。

---

## 结论（TL;DR）

**(a) 速率限制功能已实现并挂在请求处理路径上（三个入口全部过门）；但当前运行时配置下 RPM 门实际"不拒绝"。**

- `DualGateLimiter` 在 `ProxyManager.__init__` 构造，随 proxy 实例常驻：`proxy_manager.py:80-90`。
- 三个转发入口都在转发前执行 `pm.dual_gate.acquire(model)`，拒绝时回 429：
  - OpenAI 聊天路径（`/v1/chat/completions` 等）：`proxy/chat_handlers.py:375`（`handle_chat`，line 194），429 分支 line 376-386，`gate.release()` 在 finally（line 410-411）。
  - Anthropic 路径（`/v1/messages`）：`proxy/handler.py:629`（`_forward_local`，line 626），429 分支 line 630-634，release 在 finally（line 664-665）。
  - `/v1/embeddings` / `/v1/rerank`：`proxy/handler.py:1940-1942`（`_pipeline_guard` 第 6 步）。
- 但当前 `~/.inferfabric/iff.yaml:7-12` 配置为 `mode: observe, server_rpm: 0, model_rpm_default: 0, max_concurrent: auto`。`rpm=0` 时令牌桶直接禁用（`ratelimit.py:49-53`），observe 模式下 RPM 超限只打 warning 不拒绝（`ratelimit.py:225-230`）。**实际生效的只有并发信号量**（per-model，`auto` 时取 vLLM `max_num_seqs` 最大值，下限 4：`proxy_manager.py:215-226`），且 observe 模式下信号量排队而非 429（`ratelimit.py:239-244, 255-262`，300s 兜底）。切到 `mode: reject` 后 RPM/并发超限才会真正 429（`ratelimit.py:231-234, 245-251, 263-271`）。

**(b) Dashboard 的「速率限制」卡片是纯只读展示，不是开关；实际配置在 iff.yaml，改完需重启 proxy 才生效。**

- 卡片 HTML 里 LRU 缓存行有 `#cacheToggle` 按钮（`fragments/inference.html:50`）、自动切换行有 `#autoSwitchToggle` 按钮（line 57），**速率限制行只有 label + 副标签 `#rlMeta`（初始文案"配置见 iff.yaml"），没有任何按钮**：`fragments/inference.html:59-64`。
- JS 渲染端 `renderGateway()` 对 `#rlMeta` 的处理就是写死文案：`js/inference.js:255-259`（注释原文："R11：静态标签（manager.status 无 rate-limit 字段）"）。
- 点击事件分发器只认识 `cache-toggle` / `auto-switch-toggle` / `goto-deploy` / `scenario` / 模型操作，**不存在任何 rate-limit 相关 action**：`js/inference.js:595-625`。
- 后端路由表里也**没有**任何速率限制管理端点（全量 `_GET_ROUTES` / `_POST_ROUTES` / `_DELETE_ROUTES` 中只有 `/admin/cache/toggle`（`handler.py:230`）和 `/admin/auto-switch/toggle`（`handler.py:231`），无 `/admin/rate-limit/*` 或类似路径）。对照两个"真开关"的完整闭环：`js/inference.js:389-403`（POST /admin/cache/toggle）与 `js/inference.js:405-421`（POST /admin/auto-switch/toggle），后端实现分别在 `handler.py:1388`、`handler.py:1400`。

**(c) 缺的东西：Dashboard 侧的"开关"链路整体不存在（无按钮、无 action、无 API 端点、snapshot 无 rate-limit 字段）；功能侧则"已接线但默认配置关闭了拒绝行为"。** 具体缺口：

1. 卡片无法展示生效值：后端 snapshot（`manager.status`）不含 rate_limit 字段，JS 只能硬编码"配置见 iff.yaml"（`js/inference.js:255`）。
2. 无运行时管理端点：路由表无 rate-limit 端点（`proxy/handler.py:200-246` 全量路由清单），无法像 cache/auto-switch 一样即时切换。
3. 当前配置下 RPM 门形同虚设：`~/.inferfabric/iff.yaml` `server_rpm: 0` + `model_rpm_default: 0` + `mode: observe`（line 7-12），即"limiter 构造了、也挂在路径上，但按配置不拒绝"。若期望硬限流，需把 iff.yaml 改为 `mode: reject` 并给出非 0 RPM，且重启 proxy（`dual_gate` 只在 `ProxyManager` 构造时读取一次，`proxy_manager.py:66-90`，无热加载路径——`/reload-config`（`handler.py` POST 路由，line 225）不重建 limiter）。

---

## 证据（逐条 file:line）

### 1. Dashboard 端：速率限制卡片是静态标签

| 位置 | 内容 |
|---|---|
| `fragments/inference.html:39-43` | `#gwCard`「网关控制」卡，副标题"响应缓存 · 自动切换 · 速率限制" |
| `fragments/inference.html:50` | LRU 缓存行：`<button id="cacheToggle" data-action="cache-toggle">`（真按钮） |
| `fragments/inference.html:57` | 自动切换行：`<button id="autoSwitchToggle" data-action="auto-switch-toggle">`（真按钮） |
| `fragments/inference.html:59-64` | 速率限制行：仅 `gw-label-title`「速率限制」+ `#rlMeta`（初始文案"配置见 iff.yaml"），**无 button 元素** |
| `js/inference.js:255-259` | `renderGateway()` 中 `rlMeta.textContent = '配置见 iff.yaml'`，注释"R11：静态标签（manager.status 无 rate-limit 字段）" |
| `js/inference.js:595-625` | 全局 click 分发：只处理 `cache-toggle`/`auto-switch-toggle`/`goto-deploy`/`scenario`/模型动作，无 rate-limit action |
| `js/inference.js:389-403` | `toggleCache()` → `POST /admin/cache/toggle`（admin header） |
| `js/inference.js:405-421` | `toggleAutoSwitch()` → `POST /admin/auto-switch/toggle`（admin header） |

### 2. 后端：无速率限制管理端点，cache/auto-switch 端点存在

| 位置 | 内容 |
|---|---|
| `proxy/handler.py:200-246` | 全量路由表（`_GET_ROUTES` / `_POST_ROUTES` / `_DELETE_ROUTES`）：无 `/admin/rate-limit*` 任何端点 |
| `proxy/handler.py:230` | `"/admin/cache/toggle": _admin(...)` → `handler.py:1388` 实现 |
| `proxy/handler.py:231` | `"/admin/auto-switch/toggle": _admin(...)` → `handler.py:1400` 实现 |

### 3. 实现端：DualGateLimiter 构造与注入

| 位置 | 内容 |
|---|---|
| `ratelimit.py:39-110` | `TokenBucket`：`rpm<=0` 时 `_disabled=True`，acquire 恒 True（line 48-53, 64-65） |
| `ratelimit.py:113-182` | `RateLimiterV2`：server 桶 + 每模型桶两级，`acquire`（139-146） |
| `ratelimit.py:187-290` | `DualGateLimiter`：Gate1 RPM（observe 只记日志 225-230 / reject 拒绝 231-234）；Gate2a 全局信号量（可选 237-251）；Gate2b per-model 信号量（254-271，observe 排队 300s 兜底 256-262） |
| `ratelimit.py:293-330` | `GateResult`：acquire/release 对称，支持 `with` |
| `proxy_manager.py:66-79` | `ProxyManager.__init__` 从 `_load_runtime_config()`（iff.yaml）取 `rate_limit` 段，默认 `mode=observe, server_rpm=0, model_rpm_default=0, timeout=5, max_concurrent=auto` |
| `proxy_manager.py:75-78, 215-226` | `max_concurrent: auto` → `_compute_max_concurrent()`：取本地 vLLM 模型 `max_num_seqs` 最大值，保守下限 4 |
| `proxy_manager.py:80-90` | 构造 `self.dual_gate = DualGateLimiter(...)`，并打日志 `Rate limit: mode=... server_rpm=...` |
| `proxy_manager.py:232-276` | `_validate_runtime_config`：rate_limit 各 key 的类型/取值校验（ConfigError） |

### 4. 请求路径：三个入口都过门

| 位置 | 内容 |
|---|---|
| `proxy/chat_handlers.py:194` | `handle_chat`（OpenAI `/v1/chat/completions`、`/v1/completions`、`/api/chat`、`/api/generate`，见 `handler.py:214-217`） |
| `proxy/chat_handlers.py:375` | `gate = pm.dual_gate.acquire(model)`（vLLM/NInfer 路径，line 361 注释"apply dynamic rate limiter"）；不 ok → 记 RequestLog(429) + 回 429（376-386）；`finally: gate.release()`（410-411） |
| `proxy/handler.py:626-668` | `_forward_local`（Anthropic `/v1/messages`）：`acquire`（629）→ 429（630-634）→ 转发 → `finally: gate.release()`（664-665） |
| `proxy/handler.py:1844-1951` | `_pipeline_guard`（embeddings/rerank 共享管线）：第 6 步 `pm.dual_gate.acquire`（1940）→ 429（1941-1942） |
| `proxy/handler.py:1853` | 文档串明示管线顺序："6. dual_gate rate limit (429)" |

注：cloud 路由与 ollama native 路径（`chat_handlers.py:357-359`）在 rate limiter 之前 return，不走 dual_gate（`_compute_max_concurrent` 注释"cloud 模型不走本地并发门"，`proxy_manager.py:219`）。

### 5. 配置端：运行时 iff.yaml

| 位置 | 内容 |
|---|---|
| `~/.inferfabric/iff.yaml:7-12` | 当前生产配置：`mode: observe / server_rpm: 0 / model_rpm_default: 0 / max_concurrent: auto / timeout: 5` |
| `inferfabric/config.py:33` | `IFF_DATA_DIR = ~/.inferfabric`（iff.yaml 实际路径，**不在仓库根目录**） |
| `proxy_manager.py:309-337` | `_load_runtime_config`：文件不存在/非法 → 返回 `{}` 走默认值（observe/0/0/auto/5），仅打 warning，不 fail-fast |

---

## 附：语义澄清

- "observe" 不等于"没有限流"：并发信号量仍会**排队**（per-model 上限 = `max_concurrent`），只是不把超限请求打 429；RPM 门在 observe 下只记 `RPM observe: ... would be rate-limited` warning（`ratelimit.py:230`）。
- `mode: reject` 下：RPM 超时 → `rpm_limit` 429（`ratelimit.py:234`）；全局/每模型并发超时 → 429（`ratelimit.py:250, 270`）。
- 改 iff.yaml 的 rate_limit 段需要**重启 proxy 服务**（`dual_gate` 构造于 `ProxyManager.__init__`，无热加载；`/reload-config` 不触及 limiter）。
