# Mac 受管能力包 LLM 启动链（#23 Mac 移植）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通 Mac 侧受管能力包 LLM（llama.cpp 原生）启动链，产出真机证据，解除 :8999 生产迁移 gate。

**Architecture:** 平台无关底座已在 main（Task 3 调度边界 gate、facade/adapter managed 透传、#29 生产装配、`llama-cpp-macos-arm64-v1` 模板、qwen3-4b 包已装）。缺口仅两处代码：受管健康探针按包契约分派（chat vs embeddings）、受管加速策略按审核 YAML（chat Metal / embedding 强制 CPU）。其余为真机取证与排查（操作，不改码）。

**Tech Stack:** Python 3.12（3.10+ 兼容写法）、pytest、llama.cpp b12361（包内二进制，Metal）。

**Spec:** `specs/packs-platform-adaptation/spec.md`（§4 受管执行约束 / §5 阶段 1 Mac 试点）

## Global Constraints

- 未经单独授权不重启生产 :8999（spec §5）；本计划全部真机动作在隔离沙盒实例（非 :8999/:8998 端口）
- 受管调度边界 fail-closed 契约不可削弱：坏引用/身份不匹配/注册表缺失一律拒绝，绝不落 legacy（Task 3 已钉，本计划不改 `managed_runtime.py` 的拒绝逻辑）
- embedding/rerank 受管启动维持强制 CPU（Task 5 既有契约；旧 bge 包 YAML gpu_layers=99 是前 Task5 生成物，不得因此被改 Metal）
- 受管 chat 的加速参数只信**审核生成的 models.d YAML**（config_gen `gpu_layers = 0 if embedding else 99`）；launcher 不二次放宽
- 旧包不迁移、不重装、不自动停止（spec §3）；试点用已装包 `llm.qwen3-4b-instruct-2507@1.0.0`（GGUF 已在场，零下载）
- 隔离 = degraded（模板 `allow_degraded=True` 显式允许）；不声称 SBPL verified（无证据，spec §4 Mac 原生条）
- 提交纪律：Python 3.10+、`import inferfabric` 冒烟、gofmt 无涉、每任务 RED→GREEN

## Review Focus

1. **facade 签名漏透传**（#27/#30 同型真机炸雷）：凡 adapter→facade→launcher 链签名变更，facade 层必须有 RED→GREEN 测试 → Task 1/2 各含 facade 级用例
2. **受管 chat 包被 embeddings 探针误杀**：healthcheck 不分派 → LLM 包启动后 `/v1/embeddings` 探测失败遭收割 → Task 1 分派测试（openai_chat_ready_v1 → chat 探针）
3. **YAML 篡改提权**：手改 pack YAML gpu_layers 绕过 CPU 强制 → embedding/rerank 无视 YAML 强制 0（Task 2 用例）；chat 包 gpu_layers 越权由既有 memory-ledger 准入兜底（Task 3 排查清单核对账本日志）
4. **冷启动 dylib RUNPATH 断链**（Linux 真机教训）：包内 `runtime/engine` 装包成功 ≠ 冷启动可拉起 → Task 3 真机冷启动实测（rpath=@loader_path 同目录 dylib）
5. **legacy 包受管语义回归**：managed=None 路径行为逐字节不变（0.0.0.0 绑定、`-ngl` 透传、无探针）→ Task 1/2 各含 legacy 不变用例

---

### Task 1: 受管健康探针按包契约分派（chat / embeddings）

**Files:**
- Modify: `inferfabric/packs/managed_runtime.py`（`ResolvedRuntime` 加字段 + `resolve()` 填充）
- Modify: `inferfabric/process_manager/llama_cpp.py`（managed 探针分派 + `_probe_chat_ready`）
- Test: `tests/integration/test_managed_pack_pilot.py`（追加 chat 包用例）+ `tests/unit/engine/test_pm_facade.py`

**Interfaces:**
- Consumes: `ManagedRuntime.resolve(model) -> ResolvedRuntime`（既有）；`parse_manifest(...).execution.healthcheck`（schema `Execution.healthcheck: str`）；`_probe_embeddings(port)`（既有）
- Produces: `ResolvedRuntime.healthcheck: str = ""`（新字段，resolve 从 manifest `execution.healthcheck` 填充，缺失 = `""`）；`pm.start_llama_cpp` managed 分支按 `managed.healthcheck == "openai_chat_ready_v1"` 分派到新方法 `_probe_chat_ready(port) -> tuple[bool, str]`（POST `http://127.0.0.1:{port}/v1/chat/completions`，body `{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}`，2s 超时，200 即 ok）；其余 healthcheck 走 `_probe_embeddings`（行为不变）

- [ ] **Step 1: 写失败测试（integration：chat 包探针分派）**

在 `tests/integration/test_managed_pack_pilot.py` 追加（复用 `_bge_pack` 模式造 chat 包：marker `runtime_template="llama-cpp-macos-arm64-v1"`，manifest execution healthcheck `openai_chat_ready_v1`；`_FakeProc` 捕获 Popen/探针调用；monkeypatch `_probe_chat_ready` 返回 `(True, "ok")`）：

```python
def test_managed_chat_pack_probes_chat_not_embeddings(tmp_path, monkeypatch):
    """openai_chat_ready_v1 包：受管健康探测走 chat 端点，不走 embeddings
    （LLM 包被 embeddings 探针误杀 = Review Focus 2）。"""
    # _chat_pack 造包 + ResolvedRuntime(healthcheck="openai_chat_ready_v1")
    # monkeypatch pm._probe_chat_ready -> (True, "ok")；记录调用
    # monkeypatch pm._probe_embeddings -> (False, "should not be called")
    # pm.start_llama_cpp(cfg, "llm", managed=inst) -> status healthy
    # 断言 _probe_chat_ready 调用且 _probe_embeddings 未调用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/Users/Apple/Projects/InferFabric/.venv/bin/python -m pytest tests/integration/test_managed_pack_pilot.py::test_managed_chat_pack_probes_chat_not_embeddings -q`
Expected: FAIL — `ResolvedRuntime` 无 `healthcheck` 字段（TypeError）

- [ ] **Step 3: 实现**

`ResolvedRuntime` 加 `healthcheck: str = ""`；`resolve()` 返回处填 `healthcheck=str(getattr(manifest.execution, "healthcheck", "") or "")`。`start_llama_cpp` managed 分支把 `ok, detail = self._probe_embeddings(cfg.port)` 改为按 `managed.healthcheck` 分派；新增 `_probe_chat_ready`（urllib，2s 超时，异常 = (False, detail)）。

- [ ] **Step 4: facade 层 RED→GREEN（Review Focus 1）**

`tests/unit/engine/test_pm_facade.py` 追加：facade.start_llama_cpp 以 managed=ResolvedRuntime(healthcheck=...) 调用后，分派探针在 **pm 层**生效（mock `_ollama` 透传断言既有，加探针分派走真 pm 实例的用例——复用该文件既有 fake 模式）。

- [ ] **Step 5: legacy 不变用例（Review Focus 5）**

`managed=None` 时探针分派不触发（仅 `/health`，无追加探测）——断言既有行为测试仍绿。

- [ ] **Step 6: 全量 + 提交**

Run: `/Users/Apple/Projects/InferFabric/.venv/bin/python -m pytest tests/unit tests/integration/test_managed_pack_pilot.py -q` → 全绿
Commit: `feat(packs): managed healthcheck dispatch — openai_chat_ready_v1 probes chat endpoint`

### Task 2: 受管加速策略按审核 YAML（chat Metal / embedding 强制 CPU）

**Files:**
- Modify: `inferfabric/process_manager/llama_cpp.py`（managed 分支 `-ngl` 策略）
- Test: `tests/integration/test_managed_pack_pilot.py` + `tests/unit/engine/test_pm_facade.py`

**Interfaces:**
- Consumes: Task 1 的 `ResolvedRuntime.healthcheck`；`cfg.gpu_layers`（config_gen 生成：LLM=99 / 新装 embedding=0）
- Produces: managed 分支 cmd 规则——`model_type in ("embedding","rerank")` → 不透传 `-ngl`（强制 CPU，含 YAML 误配 99）；其余（chat）→ `cfg.gpu_layers > 0` 时 `["-ngl", str(cfg.gpu_layers)]`；managed=None 行为不变

- [ ] **Step 1: 写失败测试**

```python
def test_managed_chat_metal_from_approved_yaml(tmp_path, monkeypatch):
    """chat 包：受管 -ngl 透传审核 YAML 的 gpu_layers（Mac Metal 全卸载）；
    embedding 包即使 YAML gpu_layers=99 仍强制 CPU（Review Focus 3）。"""
    # chat 包（gpu_layers=99）：捕获 cmd 断言含 ["-ngl","99"]
    # embedding 包（gpu_layers=99 误配）：捕获 cmd 断言不含 -ngl
```

（cmd 捕获沿用 `_FakeProc` 模式：`Popen` 入参 argv 断言。）

- [ ] **Step 2: 确认失败** — Expected: FAIL（chat cmd 无 `-ngl`）

- [ ] **Step 3: 实现**

managed 分支：删去「gpu_layers 不透传」的整段条件，改为：

```python
if managed is None:
    if cfg.gpu_layers != 0:
        cmd.extend(["-ngl", str(cfg.gpu_layers)])
elif model_type in ("embedding", "rerank"):
    pass                      # 受管 embedding/rerank：强制 CPU（Task 5 契约）
elif cfg.gpu_layers > 0:
    cmd.extend(["-ngl", str(cfg.gpu_layers)])   # 审核 YAML 的加速策略
```

- [ ] **Step 4: facade 层用例**（同 Task 1 Step 4 模式，断言透传后 cmd 策略生效）

- [ ] **Step 5: 既有 Task 5 用例回归** — `test_bge_cpu_uses_approved_binary_and_explicit_degraded_mode`（YAML 99 → 强制 CPU）必须仍绿

- [ ] **Step 6: 全量 + 提交**

Run: 同 Task 1 Step 6 → 全绿
Commit: `feat(packs): managed acceleration from approved YAML — chat Metal, embedding/rerank CPU-forced`

### Task 3: 生产切换前详细排查（用户硬性要求；纯操作 + 取证，不改码）

**Files:**
- Create: `specs/packs-platform-adaptation/mac-pilot-evidence.md`（证据记录）
- 无产品代码改动

**Interfaces:**
- Consumes: Task 1/2 合入后的 main；干跑沙盒配方（记忆 `prod-migration-zero-risk`）；已装包 `llm.qwen3-4b-instruct-2507@1.0.0`
- Produces: 四项真机证据 + 排查清单核验记录（迁移 :8999 的放行依据）

- [ ] **Step 1: 沙盒重建 + 新代码冷启动实测（Review Focus 4）**

干跑配方起实例（IFF_STATE_DIR/IFF_MODELS_DIR=快照副本、packs 符号链接、:8997、模型端口副本内改 58401→58411）。`/switch` 启动 qwen3-4b → 断言 llama-server 进程在场（`lsof :58411`）、日志无 dylib 错误、`/v1/chat/completions` 真实请求返回合法 completion → `/switch` stop → 断言进程组收割（无残留监听）。

- [ ] **Step 2: 四项证据采集**

① `evaluate_pack` 三维准入输出（platform/isolation/resource 枚举）存档；② 两启动路径 provider 效应对照（受管 cmd 带包内 binary + `-ngl 99` + loopback vs legacy 路径拒绝/绕行证据）；③ 真实 API 请求 request/response 存档；④ 卸载所有权：试卸载流程对 external 产物不删除（dry-run store 副本上演练）。

- [ ] **Step 3: 生产一致性核对清单（迁移放行门）**

逐项核验并记录：migrations user_version 一致（state=5/log=11）；`IFF_MODELS_DIR` 覆盖后核心与 packs 同源；memory-ledger 准入在受管 chat 启动路径生效（干跑日志核对）；auth 语义与 prod 一致；secrets.env/admin token 复用；secrets、token-stats、request_log 读写正常；plist 变更 diff 审阅（仅加 IFF_MODELS_DIR）；回滚演练（plist .bak 恢复 → launchd reload → :8999 旧版健康）**在切换前的低峰窗口实演一次**。

- [ ] **Step 4: 证据落账**

`mac-pilot-evidence.md` 写入四项证据与排查清单核验表；issue #23 回帖（Mac 侧试点证据 + 迁移放行状态）；STATUS.md 更新。Commit: `docs(packs): mac pilot evidence + pre-switch checklist`

### Task 4: :8999 迁移切换（独立授权门——用户明确放行后才执行）

**Files:**
- Modify: `~/Library/LaunchAgents/com.macfabric.gateway.plist`（加 `IFF_MODELS_DIR`，备份先行）

- [ ] **Step 1: 用户对 Task 3 证据放行确认**（停点——不确认不执行）
- [ ] **Step 2: 备份 dbs + plist；launchd unload → 新 plist（PYTHONPATH 指新树）→ load**
- [ ] **Step 3: 健康检查**：:8999 起 7.11.2、模型列表 = 2 pack 模型、packs 页、真实请求、日志无错
- [ ] **Step 4: 异常即回滚**：plist .bak reload（KeepAlive 自动拉旧版）；成功则通知 peer 记 manifest 并同步 fusion-console 侧

## Self-Review 记录

- Spec 覆盖：§4 Mac 原生条（provider 证据）→ Task 3；§3 三维判定 → 证据①；§5 阶段 1 Mac 试点 → Task 1–3；旧包不动 → Global Constraints + Task 2 用例
- 类型一致：`ResolvedRuntime.healthcheck` 在 Task 1 定义、Task 2 消费；`_probe_chat_ready` 签名两处一致
- 比例：spec 91 行，本计划约 150 行，无代码转录
