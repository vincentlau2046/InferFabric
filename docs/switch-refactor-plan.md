# Switch 重构蓝图：`gpu_role: none` 正交性修复

> **Date:** 2026-07-07
> **Scope:** `switch()` 分流逻辑、`_shared_add_service()` 扩展、`_start_model()` 中心化、`phi3-mini` 配置调整
> **Principles:** `gpu_role`（资源角色）与 `type`（运行框架）正交；`switch` 只负责 GPU 调度，框架透明

---

## 1. 问题全景

### 1.1 `switch()` 对 `gpu_role: none` 的断裂

**当前行为：** `target_mode = model.gpu_role` 得到 `"none"`，但 `validate_transition(current, "none")` 查找 `_VALID_TRANSITIONS` 时返回 `None`（`"none"` 不在表中），fall 到 `else` 分支报 `"Invalid transition"`。

**根因：** `switch()` 将 `gpu_role` 值直接当作 GPU 状态机（`idle / exclusive / shared`）的 `target_mode`。`none` 不是 GPU 状态——它是"不参与 GPU 状态机"的标记。

**架构语义：** `gpu_role: none` 的模型不改变 `self._gpu_mode`。它们可以任意 GPU 模式下并存运行。

### 1.2 `_shared_add_service()` 缺少 `ollama` / `llama_cpp` 分支

manager.py:625-628 只处理 `is_vllm` 和 `is_comfyui`。如果 `phi3-mini` 改为 `shared`，调用 `_shared_add_service(phi3_mini)` 时 `results` 为空，模型不会启动。

### 1.3 `_deploy_model()` 与 `_shared_add_service()` 代码重复

两处都有一份 `if model.is_vllm / elif model.is_comfyui / ...` 的 type 分发。新增框架类型时容易遗漏。

---

## 2. 重构方案

### 2.1 `switch()` 分流：基于 `model.is_cpu_only` 的分支点

```
switch(target)
  │
  ├─ target == "idle" ────────────→ _switch_to_idle()
  │
  ├─ model 不存在 ────────────────→ error
  │
  ├─ target already active ───────→ already_active (with drift check)
  │
  ├─ model.is_cpu_only ───────────→ Path A: _switch_cpu_only(model)
  │   │
  │   └─ 不改变 self._gpu_mode
  │   └─ 不需要 GPU lock（或轻量 lock 防止并发启动）
  │   └─ 启动模型（委托 _start_model）
  │   └─ 记录到 active_services
  │   └─ 返回 {status: "switched", model: target, gpu_mode: unchanged}
  │
  └─ 需要 GPU ───────────────────→ Path B: GPU 状态机路径
      │
      ├─ validate_transition(current_mode, target_mode) ?
      │   ├─ No ──→ error（exclusive→shared, shared→exclusive 等）
      │   └─ Yes ──→ acquire lock, continue
      │
      ├─ current_mode == IDLE ──→ _deploy_model(model, target_mode)
      ├─ EXCL → EXCL ──────────→ _switch_exclusive(model)
      ├─ SHARED → SHARED ──────→ _shared_add_service(model)
      └─ 其他 ──→ error
```

**关键设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| CPU-only 模型是否需要 GPU lock | **不需要** | 不占 GPU 资源，不影响 GPU 状态机 |
| CPU-only 模型能否与 exclusive 模型并存 | **可以** | CPU-only 不消耗 GPU，exclusive 锁的是 GPU |
| CPU-only 模型能否与 shared 模型并存 | **可以** | 同上 |
| 多个 CPU-only 模型能否并存 | **可以** | 它们是独立进程，互不干扰 |
| CPU-only 模型的 "stop" 入口 | `stop_service()` | 已有路径，只需从 `active_services` 移除 |

**关于 `_switch_to_idle()` 的行为：**
- 当前 `_switch_to_idle()` 停止 **所有** `active_services`（包括 CPU-only 模型）。
- **建议保持现有行为**：`switch idle` = "全部停掉"。这是用户直觉——idle 意味着什么都不跑。
- 如果未来需要"只释放 GPU 但保留 CPU-only 模型"，可以新增 `switch gpu-idle` 命令。

### 2.2 `_switch_cpu_only()` 新入口

```python
def _switch_cpu_only(self, model: ModelConfig) -> dict:
    """切换 CPU-only 模型 — 不改变 GPU 状态机。

    CPU-only 模型：
    - 不改变 self._gpu_mode
    - 可以与任意 GPU 模式并存
    - 使用轻量锁防止并发启动（非 GPU 锁，普通 threading.Lock）
    """
    t0 = time.time()

    # 直接启动
    result = self._start_model(model)
    if result.get("status") not in ("healthy", "started", "ok"):
        return result

    # 更新 active_services
    remaining = list(self.active_services)
    if model.name not in remaining:
        remaining.append(model.name)
    self.state.set_active_services(remaining)

    # 记录 config hash
    self.state.set(f"config_hash:{model.name}", model.config_hash())

    elapsed = round(time.time() - t0, 1)
    return {
        "status": "switched",
        "model": model.name,
        "gpu_mode": self.gpu_mode,  # 不变
        "elapsed_sec": elapsed,
    }
```

### 2.3 `_start_model()` 中心化分发

当前 `_deploy_model()` 和 `_shared_add_service()` 各自维护一份 type 分发。抽取公共分发器消除重复：

```python
def _start_model(self, model: ModelConfig) -> dict:
    """启动一个模型 — 统一的分发入口。

    消除了在 _deploy_model() 和 _shared_add_service() 中重复
    的 if-is_vllm / elif-is_comfyui / ... 分支。
    """
    if model.is_vllm:
        return self._proc.start_vllm(model.vllm, model.model_type)
    elif model.is_comfyui:
        return self._proc.start_comfyui(model.comfyui)
    elif model.is_ollama_daemon:
        return {"status": "ok", "message": "Ollama daemon external — verify with 'ollama serve'"}
    elif model.is_ollama:
        return self._start_ollama_model(model)
    elif model.is_llama_cpp:
        return self._proc.start_llama_cpp(model.llama_cpp)
    else:
        return {"status": "error", "message": f"Unknown model type: {model.type}"}


def _start_ollama_model(self, model: ModelConfig) -> dict:
    """启动 type=ollama 模型 — 通过 Ollama daemon API。"""
    # 1. 验证 daemon 在运行
    daemon_healthy = self.check_ollama_health(11434)
    if daemon_healthy != "✅":
        return {
            "status": "error",
            "message": "Ollama daemon not running. Start with: ollama serve",
        }
    # 2. 调用 'ollama run' 拉取/加载模型
    model_ref = model.ollama.model_ref
    keep_alive = model.ollama.keep_alive or "5m"
    return self._proc.run_ollama(model_ref, keep_alive)
```

**注意：** `_start_ollama_model()` 需要 `ProcessManager` 新增一个 `run_ollama()` 方法（见 3.2）。

### 2.4 `_shared_add_service()` 扩展

只需将 type 分发委托给 `_start_model()`：

```python
def _shared_add_service(self, model: ModelConfig) -> dict:
    """Add a shared-mode service. Caller must hold self._lock."""
    if not self._lock.is_held:
        raise RuntimeError("_shared_add_service called without holding GPU lock")

    if model.name in self.active_services:
        return {"status": "already_active", "model": model.name, "gpu_mode": GPUMode.SHARED}

    # VRAM headroom check
    if model.typical_vram_pct > 0:
        current_pct = self._get_current_vram_pct()
        if current_pct + model.typical_vram_pct > 95:
            return {"status": "error", "message": f"Insufficient GPU memory: ..."}

    # 统一分发（消除 is_vllm / is_comfyui 分支重复）
    result = self._start_model(model)

    if result.get("status") not in ("healthy", "started", "ok"):
        self.state.set("profile_state", ProfileState.ERROR)
        return {"status": "error", "message": f"Failed to start: {model.name}", "results": {model.name: result}}

    # 更新状态
    remaining = list(self.active_services)
    remaining.append(model.name)
    self.state.set_active_services(remaining)
    self.state.set(f"config_hash:{model.name}", model.config_hash())
    self.state.set("profile_state", ProfileState.HEALTHY)

    elapsed = round(time.time() - t0, 1)
    return {
        "status": "switched",
        "model": model.name,
        "gpu_mode": GPUMode.SHARED,
        "elapsed_sec": elapsed,
        "active_services": remaining,
    }
```

### 2.5 `_deploy_model()` 同步重构

`_deploy_model()` 同样委托 `_start_model()`，简化后：

```python
def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
    """Deploy a model from idle state."""
    # ... YAML reload, services_to_start 逻辑不变 ...

    # 统一分发
    results[model.name] = self._start_model(model)

    # 验证 + 状态更新（保持不变）
    # ...
```

---

## 3. 需要新增的 `ProcessManager` 方法

### 3.1 `run_ollama()` — 启动 Ollama 模型

```python
def run_ollama(self, model_ref: str, keep_alive: str = "5m") -> dict:
    """拉取并启动 Ollama 模型（通过 CLI 触发 daemon API）。

    使用 'ollama run model_ref --keepalive DURATION' 确保模型加载到 daemon。
    Ollama 是 daemon 模式：多模型共享同一个 ollama serve 进程。
    InferFabric 不管理 daemon 生命周期，只负责触发模型加载。
    """
    cmd = ["ollama", "run", model_ref, "--keepalive", keep_alive, "--input", ""]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return {"status": "ok", "message": f"Loaded {model_ref} into Ollama daemon"}
        else:
            return {"status": "error", "message": f"ollama run failed: {result.stderr.strip()}"}
    except FileNotFoundError:
        return {"status": "error", "message": "ollama CLI not found in PATH"}
```

**Daemon vs 独立进程的区分：**

| 类型 | 进程模型 | 管理方式 |
|------|----------|----------|
| `type: ollama` + `gpu_role: shared` | 共享 Ollama Daemon | InferFabric 只触发 `ollama run`，模型由 daemon 生命周期管理 |
| `type: ollama` + `gpu_role: none` | 共享 Ollama Daemon | 同上，但 daemon 的 GPU 占用不影响 InferFabric 状态机 |
| `type: llama_cpp` + `gpu_role: shared` | 独立进程（llama-server） | InferFabric 完整管理生命周期（start/stop by port） |
| `type: llama_cpp` + `gpu_role: none` | 独立进程 | 同上 |

**关键区别：** `ollama` 类型的 shared 模型之间共享同一个 daemon 进程（端口 11434），但 `llama_cpp` 的 shared 模型各自独立进程。

**VRAM 协调：** Ollama daemon 内部自动管理模型加载/卸载（`keep_alive` 策略）。当多个 `type: ollama` shared 模型并存时，daemon 自行决定哪些模型驻留 GPU、哪些卸载。InferFabric 的 `typical_vram_pct` 检查仍是有效的——它代表 daemon 可能需要的峰值 VRAM。

---

## 4. `phi3-mini` 配置调整

### 4.1 当前 vs 目标

```yaml
# 当前 (models.d/phi3-mini.yaml)
gpu_role: none

# 目标
gpu_role: shared
typical_vram_pct: 4.2       # 3.8B Q4_0 ~ 2GB VRAM, 48GB GPU ≈ 4.2%
```

### 4.2 为什么改 `shared`

| 维度 | 当前 `none` | 改为 `shared` |
|------|-----------|--------------|
| 推理性能 | CPU-only，慢 | GPU 加速（Ollama num_gpu=-1 自动 offload） |
| GPU 状态机参与 | 不参与 | 参与（但因为是 Ollama daemon 模式，daemon 自行管理 GPU） |
| 与 exclusive 并存 | 可以 | 可以（Ollama daemon 和 vLLM 共享同一物理 GPU，靠 VRAM 容量协调） |
| switch 流程 | 断裂（validate_transition 失败） | 通畅（idle→shared 是合法转换） |

### 4.3 验证路径

改完 `gpu_role` 后的 `switch(phi3-mini)` 流程：

```
switch(phi3-mini)
  → target_mode = "shared"
  → current_mode = "idle"
  → validate_transition("idle", "shared") = True ✅
  → acquire lock
  → _deploy_model(phi3_mini, SHARED)
    → _start_model(phi3_mini)
      → is_ollama → _start_ollama_model()
        → check ollama daemon healthy
        → ollama run phi3:mini --keepalive 5m
    → set gpu_mode = SHARED, active_services = ["phi3-mini"]
  → success ✅
```

如果当前已有 `qwen35-9b`（shared vLLM）在运行：

```
switch(phi3-mini) with qwen35-9b running
  → target_mode = "shared", current_mode = "SHARED"
  → validate_transition("shared", "shared") = True ✅
  → _shared_add_service(phi3_mini)
    → VRAM headroom check: qwen35-9b ~10% + phi3-mini ~4% = 14% < 95% ✅
    → _start_model(phi3_mini) → ollama run
    → append to active_services
  → success ✅
```

---

## 5. 架构优雅性检查

### 5.1 `is_exclusive` / `is_shared` / `is_cpu_only` 属性

**评价：** 足够清晰，保持不变。

这三个属性是 `gpu_role` 字符串的布尔视图——语义明确，零歧义。它们是 `ModelConfig` 的属性，不是独立类或枚举，所以改动成本极低。

**是否添加 `GPUMode.NONE` 常量？**

```python
class GPUMode:
    IDLE = "idle"
    EXCLUSIVE = "exclusive"
    SHARED = "shared"
    # 不建议添加：
    # NONE = "none"  ← none 不是 GPU mode，是"不参与 GPU 状态机"
```

**结论：** `GPUMode` 只表示 GPU 硬件状态（idle/exclusive/shared）。`none` 不在其中——它是 `gpu_role` 字段的值，不是 GPU 状态。保持 `GPUMode` 三元，用 `model.is_cpu_only` 判断分流。

### 5.2 `validate_transition()` 的签名

**保持现状。** `validate_transition(from_mode, to_mode)` 只接受 `idle / exclusive / shared`。`none` 在 `switch()` 中就被分流出去，不会到达这个方法。

如果未来需要更明确的类型安全：

```python
def validate_transition(from_mode: GPUMode, to_mode: GPUMode) -> bool:
    # 只接受 GPUMode 枚举，不接受 "none"
    ...
```

当前 `from_mode: str` 已足够——`"none"` 在调用前就被 filter 掉了。

### 5.3 `change_mode()` 不存在——这是正确的

`change_mode()` 方法在代码库中 **不存在**。GPU mode 变更是通过以下路径隐式完成的：

| 方法 | 如何改 mode |
|------|-----------|
| `_deploy_model()` | `set_multi(gpu_mode=target_mode, ...)` |
| `_switch_to_idle()` | `set_multi(gpu_mode=IDLE, ...)` |
| `_switch_exclusive()` | 调用 `_deploy_model(model, EXCLUSIVE)` |
| `stop_service()` | 最后无服务时 `set(gpu_mode=IDLE)` |

**不引入独立的 `change_mode()` 方法的理由：**
- GPU mode 变更总是伴随具体的模型操作（部署、停止、切换），不是独立事件
- 引入 `change_mode()` 会抽象层过多——它只做 `self.state.gpu_mode = X`
- 当前模式是命令式且内联的，更易追踪数据流

---

## 6. 文件改动清单

### 6.1 需要重构的方法（manager.py）

| 方法 | 改动 | 原因 |
|------|------|------|
| `switch()` | 新增 `is_cpu_only` 分流分支 | 修复 `none` 模型无法切换的断裂 |
| `_switch_cpu_only()` | **新增** | CPU-only 模型的独立切换路径 |
| `_start_model()` | **新增** | 中心化 type 分发，消除重复 |
| `_start_ollama_model()` | **新增** | Ollama 模型启动逻辑 |
| `_shared_add_service()` | 委托 `_start_model()` | 支持 ollama/llama_cpp，消除重复 |
| `_deploy_model()` | 委托 `_start_model()` | 同步支持 ollama/llama_cpp，消除重复 |

### 6.2 保持不变的方法

| 方法 | 理由 |
|------|------|
| `validate_transition()` | 只处理 GPU 状态（idle/exclusive/shared），`none` 不进入此路径 |
| `GPUMode` 类 | 三元状态机足够，`none` 不是 GPU 状态 |
| `_switch_to_idle()` | 语义不变——停止所有服务 |
| `_switch_exclusive()` | exclusive→exclusive 交换逻辑不变 |
| `stop_service()` | 已有的 CPU-only 过滤（`model.needs_gpu`）已正确 |
| `reconcile()` | `is_cpu_only` 过滤在 L181 已正确工作 |

### 6.3 需要新增的 ProcessManager 方法（process_manager.py）

| 方法 | 作用 |
|------|------|
| `run_ollama(model_ref, keep_alive)` | 触发 `ollama run` 加载模型到 daemon |

### 6.4 配置文件改动

| 文件 | 改动 |
|------|------|
| `models.d/phi3-mini.yaml` | `gpu_role: none` → `gpu_role: shared`，新增 `typical_vram_pct: 4.2` |

---

## 7. 迁移风险与回滚

### 7.1 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `_start_ollama_model()` 的 `ollama run` 阻塞时间过长 | 中 | 首次拉取模型可能需 30-60s | 设置 60s 超时；后台 pull，前台 run |
| CPU-only 模型与 exclusive 模型并存时端口冲突 | 低 | Ollama 用 11434，vLLM 用 8000+，不冲突 | 端口空间天然隔离 |
| `phi3-mini` 改 shared 后 VRAM headroom 过严 | 低 | Ollama daemon VRAM 不透明，`typical_vram_pct` 是估算 | 保守设置（4.2%），实际 Ollama 在模型未活跃时自动卸载 |
| `_start_model()` 中心化破坏已有路径 | 低 | 行为等价，只是提取公共代码 | 先跑现有模型验证 diff |

### 7.2 回滚步骤

1. Revert `models.d/phi3-mini.yaml` 到 `gpu_role: none`
2. Revert `manager.py` 中 `switch()` 的 `is_cpu_only` 分支
3. `_start_model()` 是纯提取，无回滚风险（行为等价）

---

## 8. 实施顺序

```
Phase 1 — 修复断裂（P0）
  1. switch() 新增 is_cpu_only 分流 + _switch_cpu_only()
  2. 验证：iff switch llama3-1b 正常工作

Phase 2 — 扩展 shared（P0）
  3. _start_model() 中心化 + _start_ollama_model() + run_ollama()
  4. _shared_add_service() 委托 _start_model()
  5. _deploy_model() 委托 _start_model()

Phase 3 — 配置调整（P1）
  6. phi3-mini.yaml: gpu_role none → shared + typical_vram_pct
  7. 验证：switch phi3-mini → switch idle → switch qwen36-27b 全流程

Phase 4 — 清理（P2）
  8. 验证 reconcile()、stop_service()、sleep/wake 不受影响
  9. 更新 TOOLS.md / CLAUDE.md 文档
```