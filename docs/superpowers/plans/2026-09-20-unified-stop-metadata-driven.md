# 统一模型停止流程（元数据驱动）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前三套硬编码的模型停止路径收敛为单一元数据驱动入口——`ModelConfig.deployment` 字段 + `adapter.stop()` 按 deployment 分派，使未来任意「docker/conda × vllm/sglang/... × exclusive/shared/none」组合无需改代码即可正确停止，并顺带修复 exclusive 模型 Stop 按钮"假成功"的 bug。

**Architecture:**
当前停止逻辑散在三处且互相重复：① `_stop_model_process`（已正确走 `get_adapter(model.type)`，仅用于 shared/none）；② `_switch_to_idle` + `_switch_exclusive`（硬编码 `if is_vllm/elif is_sglang/.../elif is_ninfer`，且 ninfer docker stop **内联**两份）；③ `stop_all`（ProcessManager 持有 `vllm_ports=,sglang_ports=,...` 硬编码参数，无 ninfer 槽位）。本计划把 ②③ 改为统一遍历 `active_services` 调 `_stop_model_process`，停止逻辑下沉到 adapter 内部按 `model.deployment` 分派 docker/conda；ProcessManager 降级为纯工具层（pkill_by_port、wait_gpu_idle）。元数据新增 `deployment` 维度，让 docker 与否成为显式声明而非引擎隐含约定。

**Tech Stack:** Python 3.10+ dataclass（config.py）、abc（engine_adapter/base.py）、threading（lifecycle）、subprocess（docker/进程停止）、pytest（回归）。无新依赖。Frozen-API 约束：不动 proxy 转发核心，仅 handler.py 的 `_handle_stop` 响应码 + lifecycle/adapter/config 层。

**Spec:** 无独立 spec 文件；本计划的 Goal/Architecture/Global Constraints 即权威依据（架构改动范围已在上轮穷尽性核查中确定，证据见对话记录）。

## Global Constraints

- **Python 3.10+**，不升级 vLLM 0.24。
- **Frozen-API**：不新增/修改后端 HTTP 端点；`_handle_stop` 只改响应状态码语义，路由路径 `/stop` 不变。
- **Sandbox-first**：所有改动在 sandbox/ 开发 → pytest → smoke → diff review → merge（CLAUDE.md governance）。本计划代码位置以生产路径 `inferfabric/` 描述，实际在 sandbox 对应路径开发。
- **向后兼容**：现有 15 个 models.d/*.yaml **不能因加 `deployment` 字段而失效**——字段必须有从各 ConfigDef 现有 docker/conda 字段推导的默认值（property 回退），YAML 不显式写 `deployment` 时行为与现状完全一致。
- **停止逻辑不回退**：任何 adapter.stop 重构后，既有的 vllm 进程组 kill、sglang docker kill/rm、ninfer docker stop 三种底层行为必须保留（`stop_vllm`/`stop_sglang` 底层方法体不动，只改谁调用它们）。
- **返回值归一化**：停止操作对 CLI（cli.py:147 检查 `status=="stopped"`）和 API（handler.py 检查 `in ("stopped","already_stopped")`）必须返回 `status:"stopped"`；`_switch_to_idle` 当前返回 `status:"switched"`，B 方案转走时必须包装归一。
- **commit 归属**：每条 commit 消息以 `Co-Authored-By: Claude Code <noreply@anthropic.com>` 结尾。

---

## 文件结构总览

| 文件 | 责任 | 本计划改动 |
|---|---|---|
| `inferfabric/config.py` | ModelConfig + 各 ConfigDef | **Step 1**：加 `deployment` 字段 + `container_name` 通用化 + 各 ConfigDef `is_docker` 统一 |
| `inferfabric/engine_adapter/base.py` | EngineAdapter ABC | **Step 3**：加 `stop()` 的 deployment 分派契约（文档/默认实现） |
| `inferfabric/engine_adapter/vllm.py` | vLLM 适配器 | **Step 3**：`stop()` 按 deployment 分派 docker/conda；`validate_config` 放宽 conda_env 强制 |
| `inferfabric/engine_adapter/sglang.py` | SGLang 适配器 | **Step 3**：`stop()` 用 `model.deployment`/`container_name` 替代隐含 docker |
| `inferfabric/engine_adapter/ninfer.py` | NInfer 适配器 | **Step 3**：确认 `stop()` 已是 docker stop（已就绪，只需被统一入口调用） |
| `inferfabric/model_lifecycle.py` | 生命周期编排 | **Step 0**：`stop_service` 守卫转走；**Step 2**：`_switch_to_idle`/`_switch_exclusive` 删内联改遍历 |
| `inferfabric/process_manager/facade.py` | ProcessManager 门面 | **Step 2**：`stop_all` 改为接受 `active_services` 遍历调 adapter，或降级为薄包装 |
| `inferfabric/proxy/handler.py` | HTTP 路由 | **Step 0**：`_handle_stop` 对 error 发 4xx |
| `inferfabric/dashboard/js/inference.js` | dashboard Stop 按钮 | **Step 0**：`doModelAction` 改查 `data.status`（双保险，不依赖 HTTP 码） |
| `tests/unit/state/test_model_lifecycle.py` | 生命周期测试 | 各 Step 回归测试 |
| `tests/unit/engine/test_engine_adapters.py` | 适配器测试 | Step 3 deployment 分派测试 |
| `tests/unit/state/test_model_config.py` | 配置测试 | Step 1 deployment 推导测试 |

---

## Step 0: 修复 exclusive 模型 Stop 假成功（A+B，先行 bugfix）

> 独立于架构重构，但 B 的"转走停止路径"为 Step 2 的统一入口铺路。先行交付，用户已批准。

### Task 0.1: 响应归一——`_handle_stop` 对 error 发 4xx（方案 A）

**Files:**
- Modify: `inferfabric/proxy/handler.py:986-997`（`_handle_stop`）
- Test: `tests/unit/proxy/test_admin_routes.py`（若不存在则新建，确认既有 admin 测试位置）

**Interfaces:**
- Consumes: `pm.mgr.stop_service(target)` 返回 `{"status":"error"|"stopped"|"already_stopped", ...}`
- Produces: `_handle_stop` 对 `status=="error"` 发 HTTP 400 + body 含 `error` 字段；成功保持 200

- [ ] **Step 1: 定位既有 admin 路由测试，确认断言模式**

Run: `grep -rn '_handle_stop\|/stop\|stop_service' tests/unit/proxy/ tests/integration/`
Expected: 找到既有 `_handle_stop` 或 admin 路由测试文件；若无，在 `tests/unit/proxy/` 新建 `test_admin_stop.py`

- [ ] **Step 2: 写失败测试——exclusive 模型 stop 返回 400 非 200**

```python
# tests/unit/proxy/test_admin_stop.py
"""POST /stop 对 exclusive 模型应返回 4xx，而非 200 假成功。"""
import json
from unittest.mock import MagicMock


def _build_handler_with_exclusive_running():
    """构造 active_services 含 exclusive 模型、gpu_mode=exclusive 的 mock。"""
    from inferfabric.proxy.handler import ProxyHandler
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {"Content-Type": "application/json"}
    h.rfile = __import__("io").BytesIO(json.dumps({"model": "Qwen38-27B-TXT"}).encode())
    h._resp_status = 200
    h._resp_headers = {}
    h._headers_locked = False
    # 捕获 send_json 的 status
    sent = {}
    def _send_json(data, status=200, extra_headers=None):
        sent["data"] = data
        sent["status"] = status
    h._send_json = _send_json
    h._read_body = lambda: {"model": "Qwen38-27B-TXT"}
    pm = MagicMock()
    pm.mgr.stop_service.return_value = {
        "status": "error",
        "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'.",
    }
    return h, pm, sent


def test_stop_exclusive_returns_4xx_not_200():
    """A 方案：exclusive stop error 必须 4xx，不能默认 200 谎报成功。"""
    h, pm, sent = _build_handler_with_exclusive_running()
    h._handle_stop(pm)
    assert sent["status"] >= 400, "exclusive stop error 不应返回 200"
    assert sent["data"].get("error") or sent["data"].get("message")


def test_stop_shared_success_returns_200():
    """shared 模型 stop 成功仍返回 200。"""
    h, pm, sent = _build_handler_with_exclusive_running()
    pm.mgr.stop_service.return_value = {"status": "stopped", "model": "ovis-ocr2"}
    h._read_body = lambda: {"model": "ovis-ocr2"}
    h._handle_stop(pm)
    assert sent["status"] == 200
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_admin_stop.py -v`
Expected: FAIL（当前 `_handle_stop` 无条件 `_send_json(result)` 默认 200）

- [ ] **Step 4: 实现——`_handle_stop` 按 status 分派状态码**

修改 `inferfabric/proxy/handler.py` `_handle_stop`（当前 986-997），把最后一行 `self._send_json(result)` 改为：

```python
        result = pm.mgr.stop_service(target)
        if result.get("status") in ("stopped", "already_stopped"):
            pm.mgr.state.record_manual_stop(target)
            self._send_json(result, 200)
        else:
            # error（含 exclusive 守卫）——4xx，前端 doModelAction 显示 message
            self._send_json(result, 400)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_admin_stop.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add inferfabric/proxy/handler.py tests/unit/proxy/test_admin_stop.py
git commit -m "fix(stop): _handle_stop returns 4xx for error (no more silent 200 on exclusive)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 0.2: 前端双保险——`doModelAction` 改查 `data.status`

**Files:**
- Modify: `inferfabric/dashboard/js/inference.js:300-312`（`run()` 响应处理）
- Test: `tests/unit/dashboard/test_dashboard_v2.py`（加 JS 逻辑断言）

**Interfaces:**
- Consumes: `POST /stop` 响应 JSON `{status, message?, error?}`
- Produces: 即使后端误返 200，前端也按 `data.status !== 'stopped'` 走错误分支

- [ ] **Step 1: 写失败测试——doModelAction 对 status:error 弹错误 toast**

在 `tests/unit/dashboard/test_dashboard_v2.py` 末尾加：

```python
def test_stop_exclusive_shows_error_not_success(monkeypatch):
    """即使后端误返 200，data.status=='error' 时前端必须弹错误而非'已停止'。"""
    import json, types
    # 最小 stub：window + fetch + UI.toast 捕获
    g = {
        'window': {},
        'fetch_args': None,
        'toasts': [],
    }
    js = _load_js_module(['store.js', 'ui.js', 'inference.js'])  # 既有 helper
    # 注入 fetch stub 返回 200 + status:error
    # ...（沿用该测试文件既有的 node stub 模式，断言 toasts 中有 'error' 级别且不含 '已停止'）
```

> 注：若该测试文件的既有 stub 框架难以模拟 async fetch，可降级为对 inference.js 源码做字符串断言——确认 `run()` 体内存在 `j.status` 判读逻辑。实现时优先用既有框架；框架不支持再降级。

- [ ] **Step 2: 实现——`run()` 解析 body.status**

修改 `inference.js:300-312` 的 `run()`：

```javascript
        var res = await fetch(op.url, {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({ model: name }),
        });
        var j = null;
        try { j = await res.json(); } catch (_) {}
        if (!res.ok || (j && j.status && j.status !== 'stopped' && j.status !== 'already_stopped')) {
          var msg = (j && (j.error || j.message)) || ('HTTP ' + res.status);
          throw new Error(msg);
        }
        UI.toast(op.ok, 'ok');
        store.forceRefresh();
```

- [ ] **Step 3: 运行测试 + 全量 dashboard 测试**

Run: `python3 -m pytest tests/unit/dashboard/test_dashboard_v2.py -v --tb=short`
Expected: PASS（含新测试）

- [ ] **Step 4: Commit**

```bash
git add inferfabric/dashboard/js/inference.js tests/unit/dashboard/test_dashboard_v2.py
git commit -m "fix(dashboard): doModelAction checks data.status, not just HTTP ok

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 0.3: stop_service 对 exclusive 转走 _switch_to_idle（方案 B）

**Files:**
- Modify: `inferfabric/model_lifecycle.py:527-532`（`stop_service` exclusive 守卫）
- Test: `tests/unit/state/test_model_lifecycle.py`

**Interfaces:**
- Consumes: `_switch_to_idle()` 返回 `{status:"switched", model:"idle", stopped:[...], elapsed_sec}`
- Produces: `stop_service` 对 exclusive 模型返回 `{status:"stopped", model:name, gpu_mode:"idle", stopped:[...]}`（归一化为 stopped 语义）

**关键安全前提**：exclusive 模式下 active_services 恰好一个 GPU-bound 服务，`name in active_services && gpu_mode==EXCLUSIVE && !is_gpu_none` ⇒ name 即该 exclusive 模型 ⇒ 转 idle 等价于停它。

- [ ] **Step 1: 写失败测试——exclusive stop_service 返回 stopped 且调 _switch_to_idle**

```python
# tests/unit/state/test_model_lifecycle.py 末尾
def test_stop_service_exclusive_delegates_to_switch_idle(monkeypatch):
    """B 方案：exclusive 模型 stop_service 应转走 _switch_to_idle，返回 status=stopped。"""
    from inferfabric.model_lifecycle import ModelLifecycle
    from inferfabric.state import GPUMode
    lc = _make_lifecycle_with_active_exclusive("Qwen38-27B-TXT")  # 既有 helper 或新建
    called = {}
    def fake_switch_to_idle():
        called["switch_to_idle"] = True
        return {"status": "switched", "model": "idle", "stopped": ["Qwen38-27B-TXT"], "elapsed_sec": 1.2}
    monkeypatch.setattr(lc, "_switch_to_idle", fake_switch_to_idle)

    result = lc.stop_service("Qwen38-27B-TXT")
    assert called.get("switch_to_idle") is True, "exclusive stop 应转走 _switch_to_idle"
    assert result["status"] == "stopped", "返回值归一为 stopped（CLI/handler 依赖）"
    assert result.get("gpu_mode") == "idle"
    assert "Qwen38-27B-TXT" in result.get("stopped", [])


def test_stop_service_shared_still_calls_stop_model_process(monkeypatch):
    """B 方案不破坏 shared：shared 模型仍走 _stop_model_process，不转 idle。"""
    lc = _make_lifecycle_with_active_shared("ovis-ocr2")
    called = {"switch_to_idle": False, "stop_model_process": False}
    monkeypatch.setattr(lc, "_switch_to_idle", lambda: called.__setitem__("switch_to_idle", True))
    monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: called.__setitem__("stop_model_process", True))
    lc.stop_service("ovis-ocr2")
    assert called["stop_model_process"] and not called["switch_to_idle"]
```

> `_make_lifecycle_with_active_exclusive/shared` helper：若 test_model_lifecycle.py 既有 fixture 不覆盖，新建最小 helper——构造 `state.get_active_services()` 返回对应列表 + `state.gpu_mode` 返回对应模式 + `_models` 字典含对应 ModelConfig mock。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/state/test_model_lifecycle.py -v -k exclusive_delegates`
Expected: FAIL（当前守卫返回 error，不调 _switch_to_idle）

- [ ] **Step 3: 实现——守卫转走 + 归一化**

修改 `inferfabric/model_lifecycle.py:527-532`，把：

```python
        if self.state.gpu_mode == GPUMode.EXCLUSIVE:
            return {"status": "error", "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'."}
```

改为：

```python
        # exclusive 模型：单 GPU 上停它 = 释放整个 GPU → 转 idle。
        # active_services 在 exclusive 下恰为该模型，转 idle 等价停它。
        # 返回值归一为 stopped（CLI cli.py:147 / handler _handle_stop 依赖）。
        if self.state.gpu_mode == GPUMode.EXCLUSIVE:
            result = self._switch_to_idle()
            if result.get("status") == "switched":
                return {
                    "status": "stopped",
                    "model": name,
                    "gpu_mode": GPUMode.IDLE,
                    "stopped": result.get("stopped", [name]),
                    "elapsed_sec": result.get("elapsed_sec"),
                }
            return result  # error 透传
```

- [ ] **Step 4: 运行测试确认通过 + 跑全 lifecycle 套件**

Run: `python3 -m pytest tests/unit/state/test_model_lifecycle.py -v --tb=short`
Expected: PASS（含新测试 + 既有不破坏）

- [ ] **Step 5: Commit**

```bash
git add inferfabric/model_lifecycle.py tests/unit/state/test_model_lifecycle.py
git commit -m "fix(stop): stop_service delegates exclusive to switch_idle (Stop button works)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Step 1: 元数据——`deployment` 维度 + `container_name` 通用化

> 让 docker/conda/process 成为显式元数据，而非引擎隐含约定。向后兼容：YAML 不写 deployment 时从既有字段推导。

### Task 1.1: ModelConfig 加 `deployment` 字段 + 推导 property

**Files:**
- Modify: `inferfabric/config.py:346-410`（ModelConfig class）
- Test: `tests/unit/state/test_model_config.py`

**Interfaces:**
- Produces: `ModelConfig.deployment: str`（值 `""` 默认，由 property `resolved_deployment` 推导）；`resolved_deployment` 返回 `"docker"|"conda"|"process"`

**推导规则**（从既有字段，保证现有 YAML 不改即兼容）：
- `type == "ninfer"` → docker（NInferConfig 必有 docker_image）
- `type == "sglang"` → docker（SGLangConfig.build_docker_cmd 是默认路径，conda_env 默认空）
- `type == "vllm"` → conda（VLLMConfig.conda_env 必填，当前无 docker 路径）；**未来 YAML 写 `deployment: docker` 覆盖为 docker**
- `type in ("comfyui","tts_server","asr_server")` → conda
- `type == "llama_cpp"` → process
- `type == "ollama_daemon"` → process

- [ ] **Step 1: 写失败测试——deployment 推导覆盖所有引擎**

```python
# tests/unit/state/test_model_config.py
def test_deployment_derived_from_engine_type():
    """未显式写 deployment 时，按引擎类型推导（向后兼容现有 YAML）。"""
    from inferfabric.config import ModelConfig
    cases = {
        "ninfer": "docker",
        "sglang": "docker",
        "vllm": "conda",
        "comfyui": "conda",
        "tts_server": "conda",
        "asr_server": "conda",
        "llama_cpp": "process",
        "ollama_daemon": "process",
    }
    for etype, expected in cases.items():
        m = ModelConfig(name=f"t-{etype}", description="", type=etype)
        # 补最小 ConfigDef 让推导不 crash
        assert m.resolved_deployment == expected, f"{etype} 应推导为 {expected}"


def test_deployment_explicit_overrides_derived():
    """YAML 显式写 deployment 时覆盖推导值（未来 docker+vllm 的入口）。"""
    from inferfabric.config import ModelConfig
    m = ModelConfig(name="t-vllm-docker", description="", type="vllm", deployment="docker")
    assert m.resolved_deployment == "docker", "显式 deployment 应覆盖 vllm=conda 推导"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/state/test_model_config.py -v -k deployment`
Expected: FAIL（`ModelConfig` 无 `deployment` 字段 / `resolved_deployment` 属性）

- [ ] **Step 3: 实现——加字段 + property**

在 `inferfabric/config.py` ModelConfig 的 `type: str = "vllm"` 下方加：

```python
    deployment: str = ""  # '' (auto-derive) | 'docker' | 'conda' | 'process' — 部署方式显式声明
```

在 `is_exclusive` property 附近加：

```python
    @property
    def resolved_deployment(self) -> str:
        """Effective deployment: explicit value if set, else derived from engine type.

        推导规则保证现有 YAML（未写 deployment）行为不变。
        未来 docker+vllm 只需在 YAML 写 deployment: docker 即可启用 docker 路径。
        """
        if self.deployment in ("docker", "conda", "process"):
            return self.deployment
        if self.type in ("ninfer", "sglang"):
            return "docker"
        if self.type in ("vllm", "comfyui", "tts_server", "asr_server"):
            return "conda"
        if self.type in ("llama_cpp", "ollama_daemon"):
            return "process"
        return "process"
```

- [ ] **Step 4: 跑测试 + config_hash 确认 deployment 进 hash（防漂移）**

Run: `python3 -m pytest tests/unit/state/test_model_config.py -v --tb=short`
Expected: PASS

确认 `config_hash()` 已自动包含 `deployment`（它遍历所有非排除字段）——在测试里加 `test_deployment_in_config_hash`：两个仅 deployment 不同的 ModelConfig hash 不同。

- [ ] **Step 5: Commit**

```bash
git add inferfabric/config.py tests/unit/state/test_model_config.py
git commit -m "feat(config): ModelConfig.deployment field + resolved_deployment derivation

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 1.2: `container_name` 通用化到 ModelConfig

**Files:**
- Modify: `inferfabric/config.py`（ModelConfig 加 `container_name` property）
- Test: `tests/unit/state/test_model_config.py`

**Interfaces:**
- Produces: `ModelConfig.container_name` property，返回该模型的 Docker 容器名（docker 部署时用）；None 表示无容器

**当前散落**：`NInferConfig.container_name`（默认空→推导 `ninfer-{port}`）、`SGLangConfig.build_docker_cmd` 内硬编码 `sglang-{served_name}`。统一为 property 让 adapter.stop 不必各写一份推导。

- [ ] **Step 1: 写失败测试——container_name 统一访问**

```python
def test_container_name_unified_accessor():
    """docker 部署的模型都能经 ModelConfig.container_name 拿到容器名。"""
    from inferfabric.config import ModelConfig
    # ninfer：显式 container_name
    m_ninfer = ModelConfig(name="t", description="", type="ninfer")
    m_ninfer.ninfer = SimpleNamespace(container_name="iff-ninfer-qwen38", port=8007)
    assert m_ninfer.container_name == "iff-ninfer-qwen38"
    # ninfer：container_name 空 → 推导 ninfer-{port}
    m_ninfer2 = ModelConfig(name="t2", description="", type="ninfer")
    m_ninfer2.ninfer = SimpleNamespace(container_name="", port=8007)
    assert m_ninfer2.container_name == "ninfer-8007"
    # sglang：推导 sglang-{served_name}
    m_sglang = ModelConfig(name="t3", description="", type="sglang")
    m_sglang.sglang = SimpleNamespace(served_name="muse-glimmer-vl", port=8006)
    assert m_sglang.container_name == "sglang-muse-glimmer-vl"
    # conda 部署：无容器名
    m_vllm = ModelConfig(name="t4", description="", type="vllm")
    m_vllm.vllm = SimpleNamespace(port=8002)
    assert m_vllm.container_name is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/state/test_model_config.py -v -k container_name`
Expected: FAIL

- [ ] **Step 3: 实现——加 container_name property**

在 ModelConfig 加（紧邻 `port`/`served_name` property）：

```python
    @property
    def container_name(self) -> Optional[str]:
        """Docker 容器名（docker 部署时用）；conda/process 返回 None。

        统一 ninfer/sglang 两处散落的容器名推导，供 adapter.stop 复用。
        """
        if self.resolved_deployment != "docker":
            return None
        if self.ninfer:
            return self.ninfer.container_name or f"ninfer-{self.ninfer.port}"
        if self.sglang:
            return f"sglang-{self.sglang.served_name}"
        # 未来 docker+vllm 等：YAML 显式 container_name 字段（本计划不先加，留扩展点）
        return None
```

- [ ] **Step 4: 跑测试**

Run: `python3 -m pytest tests/unit/state/test_model_config.py -v --tb=short`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add inferfabric/config.py tests/unit/state/test_model_config.py
git commit -m "feat(config): unified ModelConfig.container_name property (docker models)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Step 2: 统一停止入口——删除 `_switch_to_idle`/`_switch_exclusive`/`stop_all` 的硬编码分发

> 把三处 `if is_vllm/elif is_sglang/.../elif is_ninfer` + ninfer 内联 subprocess 全部改为遍历 `active_services` 调 `_stop_model_process`（已正确走 `get_adapter`）。ProcessManager.stop_all 降级为可选的批量包装。

### Task 2.1: `_switch_to_idle` 改为遍历调 `_stop_model_process`

**Files:**
- Modify: `inferfabric/model_lifecycle.py:438-465`（`_switch_to_idle` 的停止段）
- Test: `tests/unit/state/test_idle_switch.py` + `test_model_lifecycle.py`

**Interfaces:**
- Consumes: `_stop_model_process(model, name)`（既有，走 `get_adapter(model.type).stop(model)`）
- Produces: `_switch_to_idle` 不再内联 ninfer docker stop、不再硬编码 if/elif 收集 ports

**改动要点**：
- 删除 `for svc_name in from_services: if m.is_vllm: ports.append... elif is_ninfer: subprocess.run(["docker","stop",...])` 整段
- 改为 `for svc_name in from_services: m = self._models.get(svc_name); if m and m.needs_gpu: self._stop_model_process(m, svc_name)`
- `self._proc.stop_all(...)` 调用：改为不再传引擎特定 ports（停止已由 adapter 完成）；`stop_all` 降级为"清理 tracked PID + force_kill_all 兜底"的薄方法（见 Task 2.3），或保留但 active_services 守卫使其不重复停止
- **关键不变量**：GPU 等待 + CUDA clear + state.set_multi(IDLE) 逻辑保留；ninfer 容器由 NInferAdapter.stop（Task 3 确认就绪）停止

- [ ] **Step 1: 写失败测试——_switch_to_idle 经 _stop_model_process 停所有引擎（含 ninfer，不再内联）**

```python
def test_switch_to_idle_stops_all_via_stop_model_process(monkeypatch):
    """_switch_to_idle 必须经 _stop_model_process 停每个 GPU-bound 服务，不内联 docker。"""
    lc = _make_lifecycle_with_active(["Qwen38-27B-TXT", "ovis-ocr2"])  # ninfer + vllm
    stopped = []
    monkeypatch.setattr(lc, "_stop_model_process", lambda m, n: stopped.append((n, m.type)))
    # 阻断内联 subprocess（若仍内联会调 docker，mock 掉 subprocess.run 验证不被调）
    import inferfabric.model_lifecycle as ml
    monkeypatch.setattr(ml._subprocess if hasattr(ml, '_subprocess') else __import__('subprocess'), 'run',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应内联 subprocess")))
    # mock _proc.stop_all / _wait_gpu_idle / lock
    monkeypatch.setattr(lc._proc, "stop_all", lambda **k: None)
    monkeypatch.setattr(lc._proc, "_wait_gpu_idle", lambda timeout=30: {"status": "ok"})
    monkeypatch.setattr(lc._proc, "clear_gpu_cuda_state", lambda force=False: None)
    lc._lock = __import__("threading").Lock()

    result = lc._switch_to_idle()
    names = [n for n, _ in stopped]
    assert "Qwen38-27B-TXT" in names and "ovis-ocr2" in names, "所有 GPU-bound 服务都应经 _stop_model_process"
    assert result["status"] == "switched"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/state/test_idle_switch.py -v -k via_stop_model_process`
Expected: FAIL（当前走硬编码 if/elif + 内联）

- [ ] **Step 3: 实现——重写停止段**

修改 `_switch_to_idle`（model_lifecycle.py 约 438-465），把收集 ports + 内联 ninfer + stop_all(引擎参数) 整段替换为：

```python
            # ── 经统一入口停止所有 GPU-bound 服务（adapter 按 deployment 分派）──
            for svc_name in from_services:
                m = self._models.get(svc_name)
                if m and m.needs_gpu:
                    self._stop_model_process(m, svc_name)
                # gpu_role=none 服务保留，由 set_multi 过滤
            # PID 清理 + GPU 兜底（stop_all 降级为薄方法，见 Task 2.3）
            self._proc.stop_all(active_services=from_services)
```

> 注意：`stop_all` 语义改为"不重复停止，仅清理 tracked PID + 兜底"。Task 2.3 同步改其实现，保证 `active_services` 传入时不触发引擎 stop（守卫已使然——当前 stop_all 在有 active_services 时对无 port 的引擎 skip，但 vllm_pid/comfyui_pid 分支仍会调 stop_vllm()。Task 2.3 修正这点）。

- [ ] **Step 4: 运行测试 + 既有 idle_switch 套件**

Run: `python3 -m pytest tests/unit/state/test_idle_switch.py tests/unit/state/test_model_lifecycle.py -v --tb=short`
Expected: PASS（若 stop_all 守卫问题导致 vllm 重复停止，Task 2.3 修后再全绿）

- [ ] **Step 5: Commit**

```bash
git add inferfabric/model_lifecycle.py tests/unit/state/test_idle_switch.py
git commit -m "refactor(lifecycle): _switch_to_idle stops via _stop_model_process (no hardcoded dispatch)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2.2: `_switch_exclusive` 同样删除内联 ninfer

**Files:**
- Modify: `inferfabric/model_lifecycle.py:150-165`（`_switch_exclusive` 停止当前 exclusive 段）

**改动要点**：`_switch_exclusive` 在 deploy 新模型前停当前 exclusive——当前也有 `elif model.is_ninfer: subprocess.run(["docker","stop",...])` 内联（第二处重复）。改为遍历调 `_stop_model_process`。

- [ ] **Step 1: 写测试——_switch_exclusive 经 _stop_model_process 停当前 exclusive**

```python
def test_switch_exclusive_stops_current_via_stop_model_process(monkeypatch):
    """_switch_exclusive 停当前 exclusive 经 _stop_model_process，不内联 docker。"""
    # 构造 active 含一个 ninfer exclusive，mock _stop_model_process 捕获
    # 断言：subprocess.run 不被调；_stop_model_process 收到该模型
```

- [ ] **Step 2: 实现**——把 `_switch_exclusive` 停止段（150-157 内联 ninfer + 158-165 stop_all 引擎参数）替换为遍历调 `_stop_model_process` + `stop_all(active_services=...)`，与 Task 2.1 同构。

- [ ] **Step 3: 测试 + Commit**（同 2.1 模式）

```bash
git commit -m "refactor(lifecycle): _switch_exclusive stops via _stop_model_process (dedup ninfer inline)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2.3: `stop_all` 降级——不再持有引擎分发，仅清理 PID + 兜底

**Files:**
- Modify: `inferfabric/process_manager/facade.py:236-300`（`stop_all`）
- Modify: `inferfabric/gpu_state.py:342`（`force_reset` 调用方，确认兼容）

**当前问题**：`stop_all` 在有 `active_services` 时仍会因 `self.vllm_pid`/`self.comfyui_pid` 触发 `stop_vllm()`/`stop_comfyui()`（elif 分支），与 Task 2.1 已由 adapter 停过的服务**重复停止**。

**改动要点**：
- `stop_all(active_services=...)` 时：不再调任何 `stop_<engine>`（停止已由调用方经 adapter 完成）；仅做 `_set_vllm_pid(None)`/`_set_sglang_pid(None)`/`_set_comfyui_pid(None)` 等 tracked-PID 清理 + 可选 `force_kill_all` 兜底标志。
- 保留 `active_services=None` 的 legacy 路径（向后兼容：bare `stop_all()` 仍停一切，供未见到的调用方）。
- `force_reset`（gpu_state.py:342）当前传 `comfyui_cfg + active_services`——它依赖 stop_all 真停。改为：force_reset 自己遍历调 `_stop_model_process`（或经 lifecycle 暴露的统一方法），stop_all 只做 PID 清理 + `force_kill_all`。**或**：给 stop_all 加 `stop_via_adapters=True` 参数时遍历 active_services 调 adapter。实现时选更小改动：force_reset 改为调 `lifecycle._stop_all_active()`（Task 2.4 抽出的公共方法）。

- [ ] **Step 1: 写测试——stop_all(active_services=) 不重复停止已停服务**

```python
def test_stop_all_with_active_services_does_not_restop(monkeypatch):
    """stop_all 在 active_services 给定时仅清 PID，不重复调 stop_vllm 等。"""
    pm = _make_pm_with_tracked_pids(vllm_pid=999)
    calls = []
    monkeypatch.setattr(pm, "stop_vllm", lambda **k: calls.append("vllm"))
    monkeypatch.setattr(pm, "stop_sglang", lambda **k: calls.append("sglang"))
    monkeypatch.setattr(pm, "stop_comfyui", lambda **k: calls.append("comfyui"))
    pm.stop_all(active_services=["ovis-ocr2"])
    assert calls == [], "active_services 给定时 stop_all 不应重复停止（已由 adapter 停）"
    assert pm.vllm_pid is None, "应清理 tracked PID"
```

- [ ] **Step 2: 实现**——重写 `stop_all`：`active_services is not None` 分支只清 PID + 不调 stop_<engine>；`None` 分支保留 legacy。

- [ ] **Step 3: 跑 force_reset 测试**

Run: `python3 -m pytest tests/unit/state/test_gpu_state.py -v -k reset --tb=short`
Expected: PASS（force_reset 仍能停服务——经 Task 2.4 公共方法或保留其自有遍历）

- [ ] **Step 4: Commit**

```bash
git add inferfabric/process_manager/facade.py inferfabric/gpu_state.py tests/
git commit -m "refactor(pm): stop_all degrades to PID cleanup when active_services given (no restop)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 2.4: 抽出 `_stop_all_active()` 公共方法（供 force_reset / _switch_exclusive 复用）

**Files:**
- Modify: `inferfabric/model_lifecycle.py`（加 `_stop_all_active(services=None)`）
- Modify: `inferfabric/gpu_state.py:331-355`（`force_reset` 改调它）

**动机**：`_switch_to_idle`、`_switch_exclusive`、`force_reset` 三处都要"停所有 GPU-bound active 服务"——抽成一个方法消除第三、四处重复。

**接口**：
```python
def _stop_all_active(self, services: list[str] | None = None) -> None:
    """经 _stop_model_process 停所有 GPU-bound active 服务（adapter 按 deployment 分派）。
    gpu_role=none 服务跳过（不影响 GPU）。"""
    svcs = services or list(self.state.get_active_services())
    for svc_name in svcs:
        m = self._models.get(svc_name)
        if m and m.needs_gpu:
            self._stop_model_process(m, svc_name)
```

- [ ] **Step 1-4**: 测试 → 实现 → force_reset 改调 → 跑全 state 套件 → Commit

```bash
git commit -m "refactor(lifecycle): extract _stop_all_active, force_reset uses it (single stop path)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Step 3: adapter.stop 按 deployment 分派（停止逻辑下沉，未来 docker+vllm 零改代码）

> 停止逻辑从 ProcessManager（按引擎方法名硬分发）下沉到 adapter 内部（按 `model.resolved_deployment` 分派 docker/conda）。ProcessManager 的 `stop_vllm`/`stop_sglang` 底层方法体保留（被 adapter 调用），但调度权归 adapter。

### Task 3.1: VLLMAdapter.stop 按 deployment 分派 docker/conda

**Files:**
- Modify: `inferfabric/engine_adapter/vllm.py:57-62`（`stop`）+ `validate_config`（放宽 conda_env）
- Modify: `inferfabric/engine_adapter/base.py`（加 docker stop 辅助方法或文档契约）
- Test: `tests/unit/engine/test_engine_adapters.py`

**当前**：`VLLMAdapter.stop` 无条件 `self._proc.stop_vllm(port=cfg.port)`（conda 进程组 kill）。`validate_config` 强制 `conda_env` 非空。

**改动**：
- `stop` 改为：
  ```python
  def stop(self, model: ModelConfig) -> dict:
      if self._proc is None:
          raise RuntimeError("ProcessManager not set")
      if model.resolved_deployment == "docker":
          return self._stop_docker(model)   # docker stop <container_name>
      cfg = getattr(model, 'vllm')
      return self._proc.stop_vllm(port=cfg.port)
  ```
- `validate_config`：`conda_env` 改为"仅 deployment==conda 时必填"——`if model.resolved_deployment == "conda" and not model.vllm.conda_env: issues.append(...)`
- `_stop_docker`：调 `docker stop <model.container_name>`（用 Task 1.2 的统一容器名），失败 fallback `_pkill_by_port`
- base.py 可加 `_stop_docker_container(self, model)` 辅助方法供所有 docker adapter 复用（ninfer/sglang/vllm-docker）

- [ ] **Step 1: 写失败测试——vllm docker 部署走 docker stop，conda 部署走 stop_vllm**

```python
def test_vllm_adapter_stop_conda_calls_stop_vllm(monkeypatch):
    """conda 部署的 vllm：stop 调 ProcessManager.stop_vllm（进程组 kill）。"""
    from inferfabric.engine_adapter.vllm import VLLMAdapter
    adapter = VLLMAdapter()
    pm = MagicMock(); adapter.set_process_manager(pm)
    model = _make_vllm_model(deployment="")  # 推导 conda
    adapter.stop(model)
    pm.stop_vllm.assert_called_once()
    pm.stop_sglang.assert_not_called()

def test_vllm_adapter_stop_docker_calls_docker_stop(monkeypatch):
    """docker 部署的 vllm：stop 调 docker stop <container>，不走 stop_vllm。"""
    import inferfabric.engine_adapter.vllm as vmod
    adapter = vmod.VLLMAdapter()
    pm = MagicMock(); adapter.set_process_manager(pm)
    model = _make_vllm_model(deployment="docker")
    model.container_name = "vllm-foo"  # 或经 property
    docker_calls = []
    monkeypatch.setattr(vmod.subprocess, "run", lambda *a, **k: docker_calls.append(a) or MagicMock(returncode=0, stderr=b""))
    adapter.stop(model)
    assert any("docker" in str(c) and "stop" in str(c) for c in docker_calls), "应调 docker stop"
    pm.stop_vllm.assert_not_called()
```

- [ ] **Step 2: 运行确认失败** → **Step 3: 实现 stop/validate/_stop_docker** → **Step 4: 跑 adapter 套件** → **Step 5: Commit**

```bash
git commit -m "feat(adapter): VLLMAdapter.stop dispatches by deployment (docker|conda)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.2: SGLangAdapter.stop 用统一 container_name（去除隐含）

**Files:**
- Modify: `inferfabric/engine_adapter/sglang.py:55-60`（`stop`）

**当前**：`self._proc.stop_sglang(port=cfg.port)`——stop_sglang 底层自己扫描 `sglang-*` 容器名（硬编码前缀）。

**改动**：`stop` 改为传 `model.container_name`（Task 1.2）给 `stop_sglang`，不再依赖底层扫描：
```python
def stop(self, model: ModelConfig) -> dict:
    if self._proc is None:
        raise RuntimeError("ProcessManager not set")
    cfg = getattr(model, 'sglang')
    return self._proc.stop_sglang(port=cfg.port, container_name=model.container_name)
```

- [ ] **Step 1-4**: 测试（sglang stop 传 container_name）→ 实现 → 跑套件 → Commit

```bash
git commit -m "feat(adapter): SGLangAdapter.stop uses unified container_name (no implicit scan)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.3: NInferAdapter.stop 确认就绪 + 用统一 container_name

**Files:**
- Modify: `inferfabric/engine_adapter/ninfer.py:126-142`（`stop`，已是 docker stop）

**当前**：`NInferAdapter.stop` 已正确做 `docker stop <cfg.container_name or ninfer-{port}>`。只需确认它用 `model.container_name`（Task 1.2）而非自己重推一遍，消除最后一份重复推导。

**改动**：`container = model.container_name`（替代 `cfg.container_name or f"ninfer-{cfg.port}"`）。

- [ ] **Step 1-4**: 测试 → 实现 → Commit

```bash
git commit -m "refactor(adapter): NInferAdapter.stop uses unified ModelConfig.container_name

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

### Task 3.4: base.py 加 `_stop_docker_container` 辅助方法（复用）

**Files:**
- Modify: `inferfabric/engine_adapter/base.py`

**动机**：vllm-docker、ninfer、sglang 三者 docker stop 逻辑同构（`docker stop <name>` + fallback）。抽到基类避免三份重复。

**接口**：
```python
def _stop_docker_container(self, model: ModelConfig, timeout: int = 30) -> dict:
    """docker stop <model.container_name>。docker 部署的 adapter 复用。"""
    import subprocess
    name = model.container_name
    if not name:
        return {"status": "warning", "message": "No container_name for docker stop"}
    r = subprocess.run(["docker", "stop", name], timeout=timeout, capture_output=True, check=False)
    if r.returncode == 0:
        return {"status": "ok", "message": f"Container {name} stopped"}
    return {"status": "warning", "message": f"docker stop exit {r.returncode}: {r.stderr.decode()[:200]}"}
```

- [ ] **Step 1-4**: 测试 → 实现 → vllm/ninfer/sglang adapter 改调它 → Commit

```bash
git commit -m "feat(adapter): base _stop_docker_container helper (shared by docker adapters)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 最终验证（全量回归）

- [ ] **全量 unit + integration 套件**

Run: `python3 -m pytest tests/unit/ tests/integration/ -v --tb=short`
Expected: ALL PASS

- [ ] **smoke：import + 核心端点**

Run: `python3 -c "import inferfabric"` && `./iff status`
Expected: 无 import 错误；status 正常返回（Qwen38-27B-TXT 健康）

- [ ] **活体验证（需用户在 sandbox/或确认后）**：
  - `./iff stop Qwen38-27B-TXT` → 容器真停 + GPU 转 idle（B 方案生效）
  - dashboard Stop 按钮 → 真停 + "已停止" toast（A+B 生效）
  - `./iff switch Qwen38-27B-TXT` 重新拉起 → 健康恢复
  - `./iff reset` → 全停（Step 2 force_reset 路径）

- [ ] **未来组合自适应性验证**（无实际 docker+vllm 镜像时用单测证明）：
  - 构造 `ModelConfig(type="vllm", deployment="docker")` → `resolved_deployment=="docker"`、`VLLMAdapter.stop` 走 docker 分支（Task 3.1 测试已覆盖）→ **无需改任何代码**即支持 docker+vllm

---

## 自检（Self-Review 已执行）

1. **Spec 覆盖**：用户三步要求（元数据/统一入口/adapter 持逻辑）→ Step 1/2/3 一一对应；A+B bugfix → Step 0。未来 docker+vllm 零改代码 → Task 3.1 + Task 1.1 显式 deployment 覆盖。force_reset 第三调用方 → Task 2.3/2.4 覆盖（避免漏 force_reset 仍硬编码）。
2. **无占位符**：每个 Step 含具体文件:行号、接口签名、测试代码、commit 消息。
3. **类型一致**：`resolved_deployment`/`container_name` property 在 config.py 定义后，adapter/lifecycle 测试中引用名一致；`_stop_all_active`/`_stop_docker_container` 命名贯穿。
4. **向后兼容**：Global Constraints 明示 YAML 不改即兼容（推导规则）；stop_all 保留 legacy 路径。
5. **不破坏既有行为**：vllm 进程组 kill / sglang docker kill / ninfer docker stop 底层方法体不动，只改调度层。
