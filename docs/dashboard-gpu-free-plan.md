# Dashboard GPU-FREE 分类方案

**状态**：方案分析（未改代码）
**日期**：2026-07-07
**范围**：`inferfabric/dashboard.py`（模型推理 TAB）

---

## 1. Dashboard 当前结构分析

### 1.1 数据传递链（已确认）

```
models.d/*.yaml  (gpu_role: exclusive | shared | none)
        ↓  load_models()  config.py
manager.py  list_models()  →  dict["mode"] = m.gpu_role        (L101)
        ↓  filter: m.type not in ("alias_map","ollama_daemon")  (L109)
GET /models  →  [{name, description, mode, type, active, model_type, quantization, context_window}, ...]
        ↓  dashboard.py  loadModels()  j('/models')             (L969)
JS:  const excl = models.filter(m=>m.mode==='exclusive');       (L970)
     const shrd = models.filter(m=>m.mode==='shared');           (L971)
```

`"mode"` key 直接承载 `gpu_role` 值，后端无需任何改动即可区分三种角色。`ollama_daemon` 与 `alias_map` 已在 `manager.py` L109 被排除，不会进入前端列表。

### 1.2 当前两栏实现位置（三处）

| 部位 | 行号 | 内容 |
|---|---|---|
| HTML 结构 | L623–L638 | `<div class="panels">` 内两个 `<div class="panel">`，id `exclList` / `shrdList` |
| CSS 样式 | L131–137 | `.panel-icon.excl`（red-g）、`.panel-icon.shrd`（green-g）<br>L214–215 `.model-badge.excl` / `.model-badge.shrd` |
| JS 过滤+渲染 | L970–971 | `filter(m=>m.mode===...)` 分两组<br>L1026–1027 `exclList.innerHTML` / `shrdList.innerHTML`<br>L1006 `modeLabel={excl:'独占',shrd:'共享'}` |

### 1.3 三栏分类现状（YAML 实测）

| gpu_role | 模型 | type |
|---|---|---|
| `exclusive` | qwen36-27b, qwen36-27b-vl, gemma4-26b | vllm |
| `shared` | qwen35-9b, comfyui | vllm / comfyui |
| `none` | llama3-1b, phi3-mini, qwen25-omni-3b | llama_cpp |

`none` 类三个均为 `llama_cpp` 框架（CPU/轻量推理，不占独占 GPU 锁），与「GPU-FREE」语义一致。

### 1.4 现有渲染逻辑关键点（影响新增栏）

- `renderCard(m, modeBadge)`（L975）接受第二个参数作 badge 类名，目前仅传 `'excl'`/`'shrd'`。
- `modeLabel`（L1006）只映射 `excl`/`shrd`，未传则 badge 文字为类名本身（L1019 `modeLabel[modeBadge]||modeBadge`）—— 故新增类必须补 `modeLabel`。
- 卡片按钮逻辑（L987–996）：`active`/`sleeping`/`idle` 三态，`doRelease` 的 `isExcl` 由 `m.mode==='exclusive'` 决定（L989/L992）。`none` 类 `isExcl=false`，走 `/stop` 分支，行为与 `shared` 相同 —— **无需改动按钮逻辑**。
- `isVllm` 控制「休眠/唤醒」按钮是否出现（L990/L993）；`none` 类非 vllm，自然不显示休眠按钮 —— **无需改动**。

---

## 2. 方案设计

### 2.1 HTML：新增第三栏（L636 后插入）

```html
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon free">🔓</div>
        <span class="panel-title">GPU-FREE</span>
      </div>
      <div id="freeList" class="model-grid"></div>
    </div>
```

放在 `shrdList` panel 之后、`</div><!-- /panels -->` 之前。`panels` 容器为 `flex-direction:column`（L111），垂直堆叠，新增栏自动纵向排列，无需改布局。

### 2.2 CSS：新增 `.free` 视觉样式

复用现有 `panel-icon` + `model-badge` 双轨。`none` 类语义为「不占 GPU 资源 / 常驻可用」，与现有 red（独占重）/ green（共享）区分。选用 **orange**（系统已定义 `--orange`/`--orange-s`/`--orange-g`，L25），呼应 CPU 类资源的暖色调，且与 stat-icon.cpu（L96）一致。

```css
.panel-icon.free { background:var(--orange-g); box-shadow:0 2px 6px rgba(255,159,10,.2); }
.model-badge.free { background:var(--orange-s); color:var(--orange); }
```

插入位置：紧跟 L137（`.panel-icon.shrd`）与 L215（`.model-badge.shrd`）之后，保持分组。

图标选用 `🔓`（与任务约定一致；语义为「无 GPU 锁」，与独占 `🔒` 形成对照）。`<`/`>` 在 Python raw string `r"""..."""`（L14）内无需转义。

### 2.3 JS：三栏过滤与渲染

**L970–971** 改为三组：

```js
  const excl = models.filter(m=>m.mode==='exclusive');
  const shrd = models.filter(m=>m.mode==='shared');
  const free = models.filter(m=>m.mode==='none');
```

**L1006** `modeLabel` 补第三键：

```js
    const modeLabel = { excl:'独占', shrd:'共享', free:'GPU-FREE' };
```

**L1026–1027** 后追加：

```js
  document.getElementById('freeList').innerHTML = free.map(m=>renderCard(m,'free')).join('');
```

### 2.4 后端改动评估

**无需改动 `manager.py`。** `list_models()` 返回的 `mode` 字段已直接等于 `gpu_role`，前端按 `m.mode==='none'` 过滤即可。约束条件（不改 API 返回值结构）满足。

---

## 3. 改动清单

| 文件 | 行号（约） | 改动类型 | 说明 |
|---|---|---|---|
| `inferfabric/dashboard.py` | L137 后 | CSS +1 行 | `.panel-icon.free` |
| `inferfabric/dashboard.py` | L215 后 | CSS +1 行 | `.model-badge.free` |
| `inferfabric/dashboard.py` | L636 后 | HTML +5 行 | 第三栏 panel 结构 |
| `inferfabric/dashboard.py` | L971 后 | JS +1 行 | `const free=...` 过滤 |
| `inferfabric/dashboard.py` | L1006 | JS 改 | `modeLabel` 补 `free:'GPU-FREE'` |
| `inferfabric/dashboard.py` | L1027 后 | JS +1 行 | `freeList.innerHTML` 渲染 |

**总计**：单文件，约 9 行新增 + 1 行修改。

**向后兼容性**：

- 旧版本若无 `gpu_role: none` 模型 → `free` 数组为空 → `freeList.innerHTML=''` → 第三栏 grid 内空。需处理空栏占位（见 §4.2）。
- `mode` 字段值 `'none'` 是字符串，前端 `===` 比较安全；后端未来若新增其他 `gpu_role` 值不会误归 `free`。
- `.free` CSS 类为新增，不冲突任何现有选择器。
- `ollama_daemon`/`alias_map` 已在后端排除，不会因新增栏意外显示。

---

## 4. 验证计划

### 4.1 三栏渲染正确性

1. 启动 dashboard，打开「模型推理」TAB。
2. 预期三栏自上而下：独占模型（3 卡，红 badge「独占」）、共享服务（2 卡，绿 badge「共享」）、GPU-FREE（3 卡，橙 badge「GPU-FREE」）。
3. 每栏图标：🔒 红 / 🔓 绿 / 🔓 橙。
4. 卡片内框架标签：GPU-FREE 三卡应显示 `📦 llama.cpp`（L999–1000 `fwIcons`/`fwLabels` 已支持 `llama_cpp`）。
5. 卡片按钮：idle 状态显示「启动」按钮（L995）；active 显示「释放」（无休眠，因 `isVllm=false`）。

### 4.2 空栏占位处理

**问题**：当前两栏无空栏占位逻辑 —— `exclList`/`shrdList` 即使空也直接 `innerHTML=''`，留空 grid（`min-height:80px`，L123）。

**方案**：复用同款处理。建议在 L1026–1027 渲染后，对三个 list 统一加空态提示，与「活跃服务」栏的 `svc-empty`（L874「无活跃服务」）风格一致：

```js
  function fillList(id, arr, modeBadge){
    const el = document.getElementById(id);
    if(arr.length===0){ el.innerHTML='<span class="svc-empty">无模型</span>'; return; }
    el.innerHTML = arr.map(m=>renderCard(m,modeBadge)).join('');
  }
  fillList('exclList', excl, 'excl');
  fillList('shrdList', shrd, 'shrd');
  fillList('freeList', free, 'free');
```

> 注：此为可选增强。若严格遵循「不改超出任务范围」原则，可仅对 `freeList` 加空态判断，不动 excl/shrd。但三栏统一处理更一致，建议采用。

### 4.3 交互回归

- 点击 GPU-FREE 卡片「启动」→ 走 `/switch` → 后端部署 llama_cpp 模型（CPU/轻量），不触 GPU 锁切换。验证 dashboard 状态栏 `gpu_mode` 保持 `idle`（因 `is_cpu_only` 服务不计入 gpu_mode，manager.py L181）。
- 「释放」按钮 → `isExcl=false`（L989，因 `m.mode==='none'`）→ `/stop` 分支 → 停止后若无其他活跃服务则转 idle（L1042–1045）。

### 4.4 自动化检查

- `python -c "import inferfabric.dashboard"` 确认无语法错误（HTML 在 raw string 内，Python 不解析）。
- 浏览器 DevTools Console 无报错（`freeList` 元素必须存在，§2.1 HTML 已提供）。
- `grep -c 'freeList' inferfabric/dashboard.py` 应为 3（HTML 定义 1 + JS getElementById 1 + innerHTML 赋值 1）。

---

## 5. 风险与边界

| 风险 | 评估 |
|---|---|
| `gpu_role: none` 模型未来出现 vllm 类型 | `isVllm` 会为真，显示「休眠/唤醒」按钮 —— vLLM 休眠 API 仅对独占/共享 vllm 有意义。**当前 YAML 无此组合**，暂不处理；若后续出现需在后端或前端按 `mode` 限定休眠按钮可见性。 |
| orange 与 CPU stat-icon 撞色 | 视觉上可接受（CPU/GPU-FREE 同属「非 GPU 重资源」语义族）；若需更强区分可改用 `--teal`（L27，但无 `-g` 渐变变体，需补）。 |
| 第三栏过长导致页面纵向滚动 | 当前独占 3 + 共享 2 = 5 卡，新增 3 卡共 8 卡，每栏 grid 3 列 → 各栏 1–2 行，纵向增量可忽略。 |

---

## 6. 实施顺序建议

1. CSS 两行（L137、L215 后）。
2. HTML 一段（L636 后）。
3. JS 三处（L971 后 filter、L1006 modeLabel、L1027 后 render）。
4. （可选）空态统一处理替换 L1026–1027。
5. 启动 dashboard 走 §4.1 验收。
