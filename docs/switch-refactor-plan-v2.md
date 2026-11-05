# Switch 重构蓝图 v2：`is_gpu_none` 语义修正与架构方案

> **Date:** 2026-07-07
> **Supersedes:** `docs/switch-refactor-plan.md`（v1）
> **Scope:** `is_cpu_only` → `is_gpu_none` 重命名、`switch()` 分流逻辑、`_switch_gpu_none()` 命名、`phi3-mini` 配置语义一致性
> **Principles:** `gpu_role`（资源角色）与 `type`（运行框架）正交；`switch` 只负责 GPU 调度，框架透明；派生属性命名须反映真实语义而非实现巧合

---

## 1. 语义重命名分析

### 1.1 问题：`is_cpu_only` 的命名歧义

**当前定义**（`config.py` L234-235）：

```python
@property
def is_cpu_only(self) -> bool:
    return self.gpu_role == "none"
```

**歧义来源：** `is_cpu_only` 字面意为"只能在 CPU 跑"，但真实语义是 **"该模型 GPU 占用为 0，不参与 GPU 状态机"**。这两者并不等价：

| 模型实例 | `is_cpu_only` 字面解读 | 真实语义 |
|----------|------------------------|----------|
| `llama3-1b`（ollama, `num_gpu: 0`） | ✅ CPU-only | ✅ GPU 占用为 0 |
| `phi3-mini`（ollama, `num_gpu: 0`） | ✅ CPU-only | ✅ GPU 占用为 0 |
| `qwen25-omni-3b`（llama_cpp, `gpu_layers: 0`） | ✅ CPU-only | ✅ GPU 占用为 0 |
| `qwen25-omni-3b` 若改 `gpu_layers: 20` | ❌ 不再 CPU-only，但若仍标 `gpu_role: none` 则 `is_cpu_only` 误判 | ⚠️ 实际占 VRAM，应参与状态机 |
| 未来 `llama_cpp` + `gpu_role: shared` | ❌ 不是 CPU-only | ✅ 占 VRAM，参与状态机 |

**关键观察：** `is_cpu_only` 把"运行框架的硬件选择"（CPU vs GPU）与"调度策略标记"（是否参与状态机）耦合在一个布尔里。当一个 `llama_cpp` 模型部分 GPU offload（`gpu_layers > 0`）但被标 `gpu_role: none` 时，名字误导，代码不会报警。

**结论：** 重命名为 `is_gpu_none`，直接对齐 `gpu_role: none` 的字段语义——"GPU 占用为 0"，而非推断其运行位置。

### 1.2 所有引用点

| 文件 | 行号 | 当前引用 | 重命名后 |
|------|------|----------|----------|
| `inferfabric/config.py` | L234-235 | `def is_cpu_only(self) -> bool: return self.gpu_role == "none"` | `def is_gpu_none(self) -> bool: return self.gpu_role == "none"` |
| `inferfabric/manager.py` | L179 注释 | `# ... (cpu_only services don't count)` | `# ... (gpu_none services don't count)` |
| `inferfabric/manager.py` | L181 | `gpu_services = [s for s in actual_services if not (self._models.get(s) and self._models[s].is_cpu_only)]` | `... self._models[s].is_gpu_none]` |
| `inferfabric/manager.py` | `switch()` 重构新增分流点 | `if model.is_cpu_only: → _switch_cpu_only(model)` | `if model.is_gpu_none: → _switch_gpu_none(model)` |
| `tests/test_robustness.py` | L49 周边 | 当前用 `m.is_exclusive = (mode == "exclusive")` 直接打属性面板，未直接设 `is_cpu_only` | 若新增 `gpu_role: none` 测试用例，须用 `is_gpu_none` |
| 文档 `docs/architecture-review-gpu-role.md` | L36, L73, L104, L155 | 多处引用 `is_cpu_only` | 同步更新为 `is_gpu_none` |
| 文档 `docs/switch-refactor-plan.md`（v1） | L42, L314, L331, L371, L387, L417, L426 | v1 蓝图沿用旧名 | v2 取代，不回改 v1 |
| 文档 `docs/dashboard-gpu-free-plan.md` | L163 | 描述 manager.py L181 的过滤机制 | 同步更新为 `is_gpu_none` |

**代码层面改动总计：** 2 个源文件（`config.py`, `manager.py`），共 3 处源码引用 + 1 处注释。测试与文档同步更新。

### 1.3 评估：直接用 `model.gpu_role == "none"` 替代 `is_gpu_none` 属性？

**选项 A — 保留派生属性 `is_gpu_none`：**

```python
if model.is_gpu_none:
    return self._switch_gpu_none(model)
# manager.py L181:
gpu_services = [s for s in actual_services
                if not (self._models.get(s) and self._models[s].is_gpu_none)]
```

**选项 B — 直接字段比较 `model.gpu_role == "none"`：**

```python
if model.gpu_role == "none":
    return self._switch_gpu_none(model)
# manager.py L181:
gpu_services = [s for s in actual_services
                if not (self._models.get(s) and self._models[s].gpu_role == "none")]
```

**对比矩阵：**

| 维度 | 选项 A（保留属性） | 选项 B（直接字段比较） |
|------|--------------------|------------------------|
| 显式布尔语义 | ✅ `is_gpu_none` 自描述 | ⚠️ 需读者解析 `"none"` 字面值 |
| 与同族属性一致性 | ✅ 与 `is_exclusive` / `is_shared` / `needs_gpu` 对齐成族 | ❌ 打破族——其余三个仍为属性 |
| 洐生属性维护成本 | 1 处定义，零额外维护 | 0 定义 |
| 字段值变更影响（若将来 `none` 改名 `zero`） | 只改属性体，调用点零改 | 全代码库 `"none"` 字面量逐处改 |
| 集合表达力 | `is_gpu_none` 可单独组合 | `gpu_role == "none"` 同样可组合 |
| 与 `needs_gpu` 的冗余度 | `needs_gpu == not is_gpu_none`，存在派生冗余 | 同样冗余，只是换地方 |
| 调用点可读性 | `model.is_gpu_none`（动词式） | `model.gpu_role == "none"`（陈述式） |

**权衡分析：**

- **冗余度伪命题：** `is_exclusive` / `is_shared` / `needs_gpu` 已存在，`is_gpu_none` 与之构成完整的 `gpu_role` 三元布尔视图族。删除单个属性会打破族对称，反而增加阅读心智成本。
- **"减少派生属性"的实际收益极低：** `is_gpu_none` 体仅 2 行（`return self.gpu_role == "none"`），无逻辑、无副作用、无测试桩。维护成本接近 0。
- **"显式布尔语义"的真实价值：** 在 `switch()` 分流条件中，`if model.is_gpu_none:` 读作"若是 GPU 无占用的模型"，比 `if model.gpu_role == "none":`（"若资源角色字串等于 none"）更贴近调度意图。`manager.py` L181 的过滤器同理——过滤的是"不占 GPU 的服务"，不是"role 字串为 none 的服务"。
- **未来演化风险：** 若 `gpu_role` 值域扩展（如新增 `partial`、`managed-by-daemon`），选项 B 需在每个调用点审阅字面值；选项 A 只需更新属性体或新增属性。低演化风险是派生属性族的核心价值。

**结论：推荐选项 A — 保留并重命名为 `is_gpu_none`。** 理由：

1. 与 `is_exclusive` / `is_shared` / `needs_gpu` 保持族对称，删除单个会引入不对称心智负担；
2. 派生属性维护成本接近 0，"减少派生属性"的收益不抵对称性损失；
3. 在分流条件中 `is_gpu_none` 更贴近调度意图（"GPU 无占用"）而非字段实现（"role 字串为 none"）；
4. 未来若 `gpu_role` 值域扩展，属性族是单一改动面，分散的字面比较则需全库审阅。

**附注 — `needs_gpu` 的定位：** `needs_gpu`（`gpu_role != "none"`）与 `is_gpu_none` 互为反义。两者并存是合理的——`needs_gpu` 用于"需要 GPU 资源"的肯定判断（如 `stop_service()` L685 的 `wait_gpu_free()` 门），`is_gpu_none` 用于"不参与状态机"的否定判断（如 `switch()` 分流、`reconcile()` L181 过滤）。语义焦点不同，不宜合并。

---

## 2. 更新架构方案

### 2.1 `switch()` 分流逻辑（基于 `is_gpu_none`）

```
switch(target)
  │
  ├─ target == "idle" ────────────→ _switch_to_idle()
  │
  ├─ model 不存在 ────────────────→ error
  │
  ├─ target already active ───────→ already_active (with drift check)
  │
  ├─ model.is_gpu_none ───────────→ Path A: _switch_gpu_none(model)
  │   │
  │   └─ 不改变 self._gpu_mode
  │   └─ 不需要 GPU lock（用轻量 self._lock 防并发启动）
  │   └─ 启动模型（委托 _start_model）
  │   └─ 记录到 active_services
  │   └─ 返回 {status: "switched", model: target, gpu_mode: unchanged}
  │
  └─ 需要GPU（is_exclusive 或 is_shared）──→ Path B: GPU 状态机路径
      │
      ├─ target_mode = model.gpu_role  # 'exclusive' | 'shared'
      ├─ validate_transition(current_mode, target_mode) ?
      │   ├─ No ──→ error（exclusive→shared, shared→exclusive 等）
      │   └─ Yes ──→ acquire lock, continue
      │
      ├─ current_mode == IDLE ──→ _deploy_model(model, target_mode)
      ├─ EXCL → EXCL ──────────→ _switch_exclusive(model)
      ├─ SHARED → SHARED ──────→ _shared_add_service(model)
      └─ 其他 ──→ error
```

**分流条件的位置（manager.py `switch()` 内）：**

应放在 "already active" 检查之后、`target_mode = model.gpu_role` 赋值之前。原因：`is_gpu_none` 模型不应进入 `validate_transition()` 路径——`"none"` 不是合法的 GPU 状态转换目标。

**关键设计决策（与 v1 一致，仅命名更新）：**

| 决策 | 选择 | 理由 |
|------|------|------|
| `is_gpu_none` 模型是否需要 GPU lock | **不需要** | GPU 占用为 0，不影响 GPU 状态机 |
| `is_gpu_none` 模型能否与 exclusive 模型并存 | **可以** | 不消耗 GPU，exclusive 锁的是 GPU |
| `is_gpu_none` 模型能否与 shared 模型并存 | **可以** | 同上 |
| 多个 `is_gpu_none` 模型能否并存 | **可以** | 独立进程，互不干扰 |
| `is_gpu_none` 模型的 "stop" 入口 | `stop_service()` | 已有路径，只需从 `active_services` 移除 |
| `_switch_to_idle()` 是否停止 `is_gpu_none` 模型 | **是** | `switch idle` = "全部停掉"，符合用户直觉 |

### 2.2 `_switch_cpu_only()` → `_switch_gpu_none()` 命名决策

**候选名：**

| �候选 | 优点 | 缺点 |
|------|------|------|
| `_switch_gpu_none()` | 与 `is_gpu_none` 属性同根，语义一致："切到 GPU 无占用模型" | 字面略生硬，`gpu_none` 不是自然语言短语 |
| `_switch_independent()` | 强调"独立路径，不依赖 GPU 状态机" | **语义漂移**——`is_gpu_none` 模型并非真"独立"，它们仍记录在 `active_services`、仍由 `_switch_to_idle()` 统一停止；"独立"指代不清 |
| `_switch_cpu_only()`（旧名） | 无改动 | **继承歧义**——与重命名初衷矛盾，且 `qwen25-omni-3b` 改 `gpu_layers > 0` 后名实不符 |
| `_switch_no_gpu()` | 与 `is_gpu_none` 近义，自然 | 与属性名不完全对齐，增加一族两种表述 |

**决策：采用 `_switch_gpu_none()`。** 理由：

1. **与属性同根对齐：** 分流条件 `if model.is_gpu_none: → self._switch_gpu_none(model)`，读作"若模型 GPU 无占用 → 走 GPU 无占用切换路径"，零认知跳转。
2. **拒绝 `_switch_independent()`：** "独立"在此架构中是过载词——`llama_cpp` 独立进程、`ollama` daemon 共享进程、vLLM 独立进程都各自"独立"于不同维度。用它命名会引入新歧义。
3. **拒绝保留 `_switch_cpu_only()`：** 与重命名初衷矛盾，且未来 `llama_cpp` + `gpu_layers > 0` + `gpu_role: none` 的组合（理论可能）下名实不符——它占 GPU 但不参与状态机。
4. **拒绝 `_switch_no_gpu()`：** 引入一族两种表述（`is_gpu_none` vs `_switch_no_gpu`），增加阅读心智。

**方法体（与 v1 等价，仅命名更新）：**

```python
def _switch_gpu_none(self, model: ModelConfig) -> dict:
    """切换 GPU 无占用模型 — 不改变 GPU 状态机。

    is_gpu_none 模型：
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

---

## 3. YAML 配置策略：`phi3-mini` 改 `shared` 的语义一致性

### 3.1 当前与目标

```yaml
# 当前 (models.d/phi3-mini.yaml)
name: phi3-mini
type: ollama
gpu_role: none
ollama:
  model_ref: "phi3:mini"
  keep_alive: "5m"
  num_gpu: 0
```

```yaml
# 目标
name: phi3-mini
type: ollama
gpu_role: shared
typical_vram_pct: 4.2       # 3.8B Q4_0 ~ 2GB VRAM, 48GB GPU ≈ 4.2%
ollama:
  model_ref: "phi3:mini"
  keep_alive: "5m"
  num_gpu: -1                # Ollama 自动 offload（当前 0 = 强制 CPU）
```

### 3.2 改 `shared` 后的语义一致性矩阵

**核心问题：** 3.8B Q4_0 在 2-13B shared 区间，改 `shared` 是否与其他维度自洽？

| 维度 | 当前 `none` + `num_gpu: 0` | 改 `shared` + `num_gpu: -1` | 一致性 |
|------|----------------------------|------------------------------|--------|
| **策略表归属** | < 3B 才 `none`，3.8B 应 `shared` | 2–13B 区间 `shared` | ✅ 修正归属 |
| **`is_gpu_none` 判定** | `true`，不参与状态机 | `false`，参与状态机 | ✅ 与实际 GPU 占用对齐 |
| **`needs_gpu` 判定** | `false`，`stop_service` 不调 `wait_gpu_free` | `true`，停止后验证 GPU 释放 | ✅ 正确——Ollama daemon卸载后 VRAM 应归零 |
| **`switch()` 流程** | 断裂（`validate_transition("idle", "none")` 失败） | 通畅（`idle → shared` 合法） | ✅ 修复断裂 |
| **与 `qwen35-9b`（shared vLLM）并存** | 可以（但不参与状态机，无 VRAM 协调） | 可以，且 VRAM headroom 检查生效：9B ~10% + phi3 4.2% = 14.2% < 95% | ✅ 有协调更安全 |
| **与 `qwen36-27b`（exclusive）并存** | 可以（phi3 CPU 跑，不抢 GPU） | ❌ 不能并存——exclusive 锁 GPU，shared 须先 idle | ⚠️ 行为变化，但符合状态机语义 |
| **`_shared_add_service()` 路径** | 不触发（因 `is_gpu_none` 分流） | 触发，需 `_start_model()` 中心化支持 `ollama` 类型 | ✅ 由 v2 重构解决 |
| **Ollama daemon GPU 不透明问题** | daemon 占 GPU 但 InferFabric 不计 | 同样不计，`typical_vram_pct` 为估算值 | ⚠️ 估算精度风险，见 §3.4 |
| **`reconcile()` L181 过滤** | phi3-mini 被过滤出 `gpu_services` | phi3-mini **不再被过滤**，参与 `gpu_mode` 判定 | ✅ 正确——它现在占 GPU |

### 3.3 行为变化的关键提示

**1. `reconcile()` 行为变化：** 改 `shared` 后，phi3-mini 会出现在 `gpu_services` 列表中（L181 的 `is_gpu_none` 过滤不再排除它）。这影响 `actual_gpu_mode` 的判定——若只有 phi3-mini 在跑，`actual_gpu_mode` 会是 `SHARED`（而非 `IDLE`）。这是正确行为，但须验证 `reconcile()` 后续逻辑对 "只有 ollama daemon 类 shared 模型" 的处理。

**2. `stop_service()` 行为变化：** L685 的 `if model.needs_gpu:` 现在对 phi3-mini 为真，会调 `wait_gpu_free(timeout=20)`。Ollama daemon 卸载模型可能比 vLLM 进程退出慢，`wait_gpu_free` 超时风险存在。**建议在 `_start_ollama_model()` / `stop_service()` 对 ollama 类型放宽超时到 40s**，或跳过 `wait_gpu_free`（daemon 自管理卸载）。

**3. 与 exclusive 并存受限：** 改 `shared` 后 phi3-mini 不再能与 `qwen36-27b`（exclusive）并存。用户若习惯 "phi3 小模型常驻 + 大模型按需切换" 的工作流，会感到受限。**这是 `shared` 的正确语义**——shared 模型占 VRAM，与 exclusive 天然互斥。若需 "常驻 + 按需"，应保持 phi3-mini 为 `none` 并接受 `switch()` 断裂（由 v2 的 `_switch_gpu_none()` 修复）。

### 3.4 Ollama daemon VRAM 不透明风险

**问题：** `typical_vram_pct: 4.2` 是静态估算。Ollama daemon 实际 VRAM 占用取决于：

- `keep_alive` 内是否活跃（idle 时 daemon 可能卸载）
- daemon 同时加载的其他模型（多 `type: ollama` shared 模型并存）
- `num_gpu: -1` 的自动 offload 层数（daemon 自决）

**风险：** `phi3-mini` 改 `shared` 后，`_shared_add_service()` 的 VRAM headroom 检查（`current_pct + model.typical_vram_pct > 95`）依赖 `self._get_current_vram_pct()`。但 Ollama daemon 占的 VRAM 不被 InferFabric 的 `gpu_used_mb()` 直接测量（daemon 是独立进程，VRAM 在其上下文）——**这是 v1 `architecture-review-gpu-role.md` §3.1 已记录的固有风险**。

**缓解：**

1. `typical_vram_pct: 4.2` 取保守值（3.8B Q4_0 实际约 2GB，4.2% 留余量）；
2. 若同时部署多个 `type: ollama` shared 模型，daemon 内部加载策略不定——建议**同一时刻只部署一个 `type: ollama` shared 模型**，其余 ollama 模型保持 `gpu_role: none`；
3. 长期解：引入 `manages_gpu: bool` 字段（`architecture-review-gpu-role.md` §4 已建议），gate 状态机参与，与 `gpu_role` 解耦。

### 3.5 `phi3-mini` 改 `shared` 的先决条件

**不可单独改 YAML——须与代码重构同步：**

| 先决条件 | 状态 | 阻塞原因 |
|----------|------|----------|
| `switch()` 支持 `gpu_role: none` 分流 | v2 重构交付 | 否则 phi3 若回 `none` 仍断裂 |
| `_start_model()` 中心化支持 `ollama` 类型 | v2 重构交付 | 否则 `_shared_add_service()` fall through，silent success |
| `ProcessManager.run_ollama()` 方法新增 | v2 重构交付 | `_start_ollama_model()` 委托需要 |
| `reconcile()` 对 ollama shared 模型的 `gpu_mode` 判定验证 | 需测试 | 行为变化，可能触发意外状态转换 |
| `stop_service()` 对 ollama 类型的 `wait_gpu_free` 超时调整 | 需调整 | daemon 卸载时序与 vLLM 不同 |

**建议实施顺序：** 先交付 `_start_model()` 中心化 + `_switch_gpu_none()` 分流（让 `none` 模型先能切），再改 `phi3-mini` 为 `shared`（进入 shared 路径），最后验证 `reconcile()` / `stop_service()` 边角。

---

## 4. 文件改动清单（v2）

### 4.1 源代码改动

| 文件 | 改动 | 行号 |
|------|------|------|
| `inferfabric/config.py` | `is_cpu_only` → `is_gpu_none`（属性重命名） | L234-235 |
| `inferfabric/manager.py` | 注释 `cpu_only services` → `gpu_none services` | L179 |
| `inferfabric/manager.py` | `self._models[s].is_cpu_only` → `self._models[s].is_gpu_none` | L181 |
| `inferfabric/manager.py` | `switch()` 新增 `is_gpu_none` 分流分支 | L306 前（`target_mode` �赋值前） |
| `inferfabric/manager.py` | `_switch_gpu_none()` **新增**（v1 的 `_switch_cpu_only()` 改名） | 新方法 |
| `inferfabric/manager.py` | `_start_model()` **新增**（中心化 type 分发） | 新方法 |
| `inferfabric/manager.py` | `_start_ollama_model()` **新增** | 新方法 |
| `inferfabric/manager.py` | `_shared_add_service()` 委托 `_start_model()` | 现有方法改造 |
| `inferfabric/manager.py` | `_deploy_model()` 委托 `_start_model()` | 现有方法改造 |
| `inferfabric/process_manager.py` | `run_ollama(model_ref, keep_alive)` **新增** | 新方法 |
| `tests/test_robustness.py` | 若新增 `gpu_role: none` 用例，用 `is_gpu_none`；现有 `is_exclusive` 打桩不变 | L49 周边 |

### 4.2 配置文件改动

| 文件 | 改动 |
|------|------|
| `models.d/phi3-mini.yaml` | `gpu_role: none` → `shared`；新增 `typical_vram_pct: 4.2`；`num_gpu: 0` → `-1` |

### 4.3 保持不变的方法

| 方法 | 理由 |
|------|------|
| `validate_transition()` | 只处理 GPU 状态（idle/exclusive/shared），`none` 在 `switch()` 中被 `is_gpu_none` 分流前置 |
| `GPUMode` 类 | 三元状态机足够，`none` 不是 GPU �状态 |
| `_switch_to_idle()` | 语义不变——停止所有服务（含 `is_gpu_none` 模型） |
| `_switch_exclusive()` | exclusive→exclusive 交换逻辑不变 |
| `stop_service()` | `model.needs_gpu` 过滤已正确（`needs_gpu` 与 `is_gpu_none` 互为反义，命名无歧义） |
| `reconcile()` | L181 改用 `is_gpu_none` 后逻辑等价；`phi3-mini` 改 shared 后行为变化见 §3.3 |

### 4.4 文档同步

| 文件 | 改动 |
|------|------|
| `docs/architecture-review-gpu-role.md` | 多处 `is_cpu_only` → `is_gpu_none`（L36, L73, L104, L155） |
| `docs/dashboard-gpu-free-plan.md` | L163 `is_cpu_only` → `is_gpu_none` |
| `docs/switch-refactor-plan.md`（v1） | 标记为 `superseded by v2`，不回改内容 |
| `docs/switch-refactor-plan-v2.md` | **新增**（本文件） |

---

## 5. 迁移风险与回滚

### 5.1 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `is_gpu_none` 重命名遗漏引用点 | 低 | 运行时 AttributeError | 用 `grep -r is_cpu_only` 全库扫描；CI 跑测试 |
| `_switch_gpu_none()` 与 `_switch_to_idle()` 交互错误 | 中 | idle 时未停 `is_gpu_none` 模型 | `_switch_to_idle()` 保持停止所有 `active_services`，不须改 |
| `phi3-mini` 改 shared 后 `reconcile()` 误判 `gpu_mode` | 中 | daemon VRAM 不透明，状态漂移 | 验证：只跑 phi3-mini 时 `actual_gpu_mode` 应为 `SHARED` |
| `stop_service()` 对 ollama 类型 `wait_gpu_free` 超时 | 中 | daemon 卸载慢于 vLLM 进程退出 | 对 `type: ollama` 放宽超时到 40s，或跳过（daemon 自管理） |
| `_start_model()` 中心化破坏已有路径 | 低 | 行为等价，只是提取公共代码 | 先跑现有模型验证 diff |
| 多个 `type: ollama` shared 模型并存 VRAM 失协 | 中 | daemon 加载策略不定，`typical_vram_pct` 失准 | 约束：同一时刻只部署一个 `type: ollama` shared 模型 |

### 5.2 回滚步骤

1. Revert `models.d/phi3-mini.yaml` 到 `gpu_role: none`, `num_gpu: 0`，移除 `typical_vram_pct`
2. Revert `inferfabric/manager.py` 中 `switch()` 的 `is_gpu_none` 分流分支
3. `_start_model()` / `_start_ollama_model()` / `run_ollama()` 是纯提取与新增，无回滚风险（行为等价或独立方法）
4. `is_gpu_none` 重命名回 `is_cpu_only`（若重命名单独发布，可独立回滚）

---

## 6. 实施顺序

```
Phase 0 — 语义重命名（独立可发布）
  1. config.py: is_cpu_only → is_gpu_none
  2. manager.py L181 + L179 注释同步
  3. 文档 architecture-review-gpu-role.md, dashboard-gpu-free-plan.md 同步
  4. 验证：iff switch llama3-1b（仍断裂，但属 Phase 1 范畴）

Phase 1 — 修复断裂（P0）
  5. switch() 新增 is_gpu_none 分流 + _switch_gpu_none()
  6. 验证：iff switch llama3-1b 正常工作（gpu_role: none 模型可切）

Phase 2 — 扩展 shared（P0）
  7. _start_model() 中心化 + _start_ollama_model() + run_ollama()
  8. _shared_add_service() 委托 _start_model()
  9. _deploy_model() 委托 _start_model()

Phase 3 — 配置调整（P1）
  10. phi3-mini.yaml: gpu_role none → shared, num_gpu 0 → -1, +typical_vram_pct 4.2
  11. stop_service() 对 ollama 类型放宽 wait_gpu_free 超时
  12. 验证：switch phi3-mini → switch idle → switch qwen36-27b 全流程
  13. 验证：reconcile() 在只跑 phi3-mini 时正确判 SHARED

Phase 4 — 清理（P2）
  14. 验证 stop_service()、sleep/wake 不受影响
  15. 更新 TOOLS.md / CLAUDE.md 文档
```

---

## 7. 与 v1 的差异摘要

| 项目 | v1 | v2 |
|------|----|----|
| 核心属性名 | `is_cpu_only` | `is_gpu_none`（消除歧义） |
| 分流条件 | `model.is_cpu_only` | `model.is_gpu_none` |
| 新方法名 | `_switch_cpu_only()` | `_switch_gpu_none()` |
| 是否评估直接用 `gpu_role == "none"` | 未评估 | §1.3 评估，推荐保留属性 |
| `phi3-mini` 改 shared 的先决条件 | 未明确列出 | §3.5 列出 5 项 |
| `phi3-mini` 改 shared 的 `reconcile` / `stop_service` 行为变化 | 未分析 | §3.3 分析 3 项 |
| `num_gpu` 字段调整 | 未提及 | `0` → `-1`（Ollama 自 offload） |
| 文档同步范围 | 未列出 | §4.4 列出 4 文件 |
| 实施顺序 | 4 phase | 5 phase（新增 Phase 0 独立重命名） |

**v1 未被推翻的部分：** §2.3 `_start_model()` 中心化、§3 `ProcessManager.run_ollama()`、§5 架构优雅性检查（`GPUMode` 三元、不引入 `change_mode()`）——这些与重命名无冲突，v2 沿用。

---

## 8. 附录：`is_gpu_none` 族属性完整视图

```python
# config.py ModelConfig 派生属性族（v2）
@property
def needs_gpu(self) -> bool:      return self.gpu_role != "none"      # 肯定：需要 GPU 资源
@property
def is_exclusive(self) -> bool:    return self.gpu_role == "exclusive" # GPU 状态机分流
@property
def is_shared(self) -> bool:       return self.gpu_role == "shared"    # GPU 状态机分流
@property
def is_gpu_none(self) -> bool:     return self.gpu_role == "none"      # 不参与 GPU 状态机
``

**族对称性：** `gpu_role` 三元值各对应一个布尔属性（`is_exclusive` / `is_shared` / `is_gpu_none`），`needs_gpu` 为跨值族的聚合反义（`exclusive` 或 `shared` → `needs_gpu` 为真）。删任一成员即破坏对称。v2 重命名 `is_cpu_only` → `is_gpu_none` 维持族完整性，仅修正成员命名歧义。
