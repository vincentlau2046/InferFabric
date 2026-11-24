# Deploy Wizard (五步收窄一键部署) Implementation Plan

> **⚠️ 2026-11-24 裁决推翻记录**：本计划「向导挂模型池 tab」的挂点已由用户裁决推翻——
> 向导迁至**能力包 tab**（主装载入口），详见 `specs/console-ux-batch/spec.md` WS1
> （PR #43 已实施）。本计划的五步交互/一键序列/评审修复契约仍然有效，仅挂点变更。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> (native, inline — user's standing choice for this line of work) to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模型池 tab 内的五步收窄部署向导：能力类型 → 框架（按平台收窄）→ 部署方式
→ ModelScope 搜索选模型 → 参数 → 一键装载（+ 可选自启），替代裸表单成为主部署入口。

**Architecture:** 前端 stepper（wizard.js，五步状态机，事件委托无 inline onclick）+
两个新后端只读端点（`/admin/deploy/options` 平台收窄选项、`/admin/deploy/search`
ModelScope 搜索代理），一键复用既有链路：`ModelManager.install_model`（本计划修复其
上游缺失）→ `POST /admin/models/set-autostart`（#19 已合入）→ 既有 switch。
packs 能力包体系只作引擎可用性探测与 deep-link，不扩范围。

**Tech Stack:** Python stdlib HTTP handler（既有一致）、urllib 搜索代理（无新依赖）、
vanilla JS stepper（dashboard fragment 体系）。

**Spec:** 2026-11-19 部署向导设计裁决（会话裁定，落记忆
`macfabric-unification-order.md`）：五步收窄：能力类型→框架→部署方式按平台收窄→
ModelScope/HF 搜索→参数→一键，挂模型池 tab；模型源统一 ModelScope（HF 直连被阻断）。

## Global Constraints

- 模型源统一 ModelScope：搜索代理不实现 HF 直连（被网络阻断，2026-11-16 裁决）。
- 无新第三方依赖；搜索代理用 urllib，超时 5s，失败降级为错误 toast 不崩页面。
- 平台收窄裁决：darwin/arm64 → llama_cpp（Metal 全卸载）；linux+NVIDIA →
  vllm/sglang/llama_cpp；无 NVIDIA → llama_cpp。收窄不可绕过（向导内不出被收窄掉的选项）。
- 一键语义与 Mac 语义一致（2026-11 裁决）：装载不启动服务——启动留给首请求唤醒或
  用户显式 switch；「自启」是显式勾选项，走 set-autostart，非默认。
- 新 admin 端点走 `_admin` 门（F7 语义）；搜索代理不回显任何凭据，响应 no-store。
- 注释中文与文件既有风格一致；前端禁 inline onclick（评审纪律沿用）。
- 引擎名与 YAML type 取值沿用上游：`vllm` / `llama_cpp` / `sglang`。

## Review Focus

1. ModelScope 搜索接口形态与响应结构（网络对面，可能变动/被限流）→ 代理层必须
   超时+错误归一（`{error, code}` 形态），前端不因半截 JSON 崩 —— Task 3 的
   transport 注入 + 畸形响应用例钉住。
2. 平台探测失真（无 nvidia-smi / apple 虚拟化 / 容器内）→ 探测失败按「无 NVIDIA」
   收窄，绝不出 vllm 选项 —— Task 2 用例钉住。
3. 一键后 YAML 已存在（重复部署同名）→ `already_configured` 必须表现为可继续
   （提示 + 允许 switch/自启），不是失败 —— Task 5 用例钉住。
4. 搜索结果模型名含路径/特殊字符（`org/name`、点、冒号）→ install 名校验沿用
   `pull_model` 的白名单正则，拒绝路径穿越 —— Task 3/5 用例钉住。
5. 五步中途切换 tab/重复点击 → stepper 状态不残留、写操作防双击（packs.js
   `_busy` 同款）—— Task 4 源码契约钉住。

---

### Task 1: 修复 `/deploy` 断链 — 移植 `ModelManager.install_model`

**Files:**
- Modify: `inferfabric/manager.py`（`pull_model` 方法后追加）
- Test: `tests/unit/test_install_model.py`（新建）

**Interfaces:**
- Consumes: `inferfabric.model_discovery.install_yaml(name, model_type, models_dir,
  existing_models, model_type_label="") -> dict`（上游已有，:141）
- Produces: `ModelManager.install_model(self, name: str, model_type: str,
  model_type_label: str = "") -> dict`；status ∈ installed/already_configured/error；
  installed 时已热重载（`self.reload_models()`）。

- [ ] **Step 1: 写失败测试**

```python
def test_install_model_writes_yaml_and_reloads(tmp_path, monkeypatch):
    # install_model("m1","llama_cpp") → models.d/m1.yaml 生成 + reload_models 调用 1 次
def test_install_model_existing_yaml_is_already_configured(tmp_path):
    # 预置同名 yaml → status == "already_configured"，不重写
```

- [ ] **Step 2: 跑测试确认失败**（`AttributeError: install_model`）
- [ ] **Step 3: 实现** — 从 fork `manager.py:764` 逐字移植 8 行包装（docstring 保留
  「Mac 语义」裁决说明），`_install_yaml` 引上游 `model_discovery.install_yaml`
- [ ] **Step 4: 测试通过** + `pytest tests/unit/proxy/test_provider_models.py -q` 不回归
- [ ] **Step 5: Commit** `fix(manager): port install_model — /deploy route latent break`

### Task 2: `GET /admin/deploy/options` — 能力×引擎×平台收窄

**Files:**
- Create: `inferfabric/proxy/deploy_wizard.py`（纯函数层 + handler 注册函数）
- Modify: `inferfabric/proxy/handler.py`（import + `_GET_ROUTES` 加
  `"/admin/deploy/options": _admin(...)`）
- Test: `tests/unit/test_deploy_options.py`

**Interfaces:**
- Consumes: `platform.probe`（sys/platform）、`shutil.which("nvidia-smi")`、
  packs `get_pack_manager()` 引擎探测（仅探测不安装）、packs_catalog capabilities
- Produces: `deploy_options() -> dict`：
  `{"capabilities": ["llm","embedding","tts","asr","ocr"],
    "engines": [{"id":"llama_cpp","label":"llama.cpp","available":true}, ...],
    "platform": {"os":"darwin","arch":"arm64","nvidia":false},
    "deployment_modes": {"llama_cpp": ["local-gguf","packs-catalog"], "vllm": ["local-dir"]}}`
  收窄在 Python 侧完成：darwin/arm64 只回 llama_cpp；nvidia=false 时剔除 vllm/sglang。

- [ ] **Step 1: 失败测试** — monkeypatch `sys.platform`/`shutil.which`：darwin+arm64
  断言 engines 恰为 `[llama_cpp]`；linux+nvidia-smi 断言含 vllm/sglang/llama_cpp；
  linux 无 nvidia-smi 断言剔除 vllm/sglang（Review Focus 2）
- [ ] **Step 2: 确认失败**（404 路由不存在）
- [ ] **Step 3: 实现** — `deploy_wizard.py` 纯函数 + handler 路由行；_admin 门
- [ ] **Step 4: 通过** + dashboard 全目录回归
- [ ] **Step 5: Commit** `feat(proxy): deploy options endpoint — capability×engine×platform`

### Task 3: `GET /admin/deploy/search` — ModelScope 搜索代理

**Files:**
- Modify: `inferfabric/proxy/deploy_wizard.py`
- Modify: `inferfabric/proxy/handler.py`（`_GET_ROUTES` 加 search 路由）
- Test: `tests/unit/test_deploy_search.py`

**Interfaces:**
- Consumes: urllib；`MODELSCOPE_SEARCH_URL = "https://modelscope.cn/api/v1/dolphin/models"`
  （实现时以一次真实请求校准端点/字段，契约以本测试的归一化层为准）
- Produces: `search_models(transport, q: str, capability: str | None, page: int = 1)
  -> dict`，transport 注入（`(url, payload) -> dict`），归一化输出
  `{"models": [{"id","name","downloads","capability_guess","pipeline"}],
    "total": int}`；错误归一 `{"error": str, "code": "search_failed"|"invalid_request"}`。
  名称校验复用 `pull_model` 白名单正则形态（Review Focus 4）。

- [ ] **Step 1: 失败测试** — fake transport：正常归一化、畸形 JSON → search_failed、
  超时异常 → search_failed、空 q → invalid_request、q 含 `..` → invalid_request
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — urllib POST JSON、5s timeout、响应 no-store、_admin 门；
  一次真实 `curl` 校准端点（记录进注释，网络不可达时以注释+单测契约交付并声明）
- [ ] **Step 4: 通过**
- [ ] **Step 5: Commit** `feat(proxy): modelscope search proxy for deploy wizard`

### Task 3b: ModelScope 下载器 — `POST /admin/deploy/download` + status（用户裁决 B）

**Files:**
- Create: `inferfabric/proxy/deploy_download.py`（后台线程下载 + 状态注册表）
- Modify: `inferfabric/proxy/handler.py`（POST download 路由 + GET status 路由，_admin 门）
- Test: `tests/unit/test_deploy_download.py`

**Interfaces:**
- Consumes: `HttpArtifactFetcher`（`inferfabric/proxy/pack_handlers.py:71`，构造可注入
  `fetcher`；fetch(artifact, dest) 只读 `artifact.source`——沿用 `packs/custom.py`
  `SimpleArtifact` 契约形态）；files API 每文件 `Sha256`（校准 2026-11-21）。
- **线上校准记录（2026-11-21，真实 curl 验证）**：
  - 搜索 `PUT https://modelscope.cn/api/v1/dolphin/models`
    `{"Name": q, "PageSize": N, "PageNumber": P}` →
    `Data.Model.Models[]{Path, Name, Downloads, ...}`、`Data.Model.TotalCount`
  - 文件列表 `GET https://modelscope.cn/api/v1/models/{org}/{name}/repo/files?Revision=master&PageSize=200`
    → `Data.Files[]`，blob 条目 `{Type:"blob", Path, Size, Sha256}`
  - 下载 `GET https://modelscope.cn/models/{org}/{name}/resolve/master/{Path}`
    → 302 至 CDN，支持 Range（fetcher 的 `_NoDowngradeRedirectHandler` 兜底）
- Produces: `start_download(model_id, ws_root, fetcher=None) -> dict`（含
  `download_id` + gguf 文件清单 + 总字节数；daemon 线程落盘
  `<ws_root>/models/<model_name>/*.gguf`——匹配 install_yaml 的
  `**/{name}/*.gguf` glob）；`download_status(download_id) -> dict`
  `{stage: listing|downloading|done|error, files_total, files_done,
  current_file, error}`；`normalize_model_id(raw) -> str`（正则
  `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`，拒绝路径穿越，Review Focus 4 同源）。
  完整性：files API 的 Sha256 下载后校验（复用 custom.py hashlib 形态）。

- [ ] **Step 1: 失败测试** — fake transport + fake fetcher：
  `normalize_model_id("../x")` → ValueError；files 归一化只留 `.gguf` blob、
  丢弃 `.gitattributes/README`；下载完成断言 dest 文件存在 + status→done +
  files_done==n；fetcher 抛错 → status.error 非空；sha 不符 → error
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — 校验 → 文件清单（files API，过滤 .gguf）→ daemon 线程
  逐文件 fetch（dest 为 `.part` 落盘后 rename，与 packs 语义一致）→ sha256 校验
  → done。状态注册表模块级 dict + threading.Lock（_PACK_DOWNLOADS 同款）。
  端点 POST `/admin/deploy/download` `{model_id}` 与 GET
  `/admin/deploy/download/status?download_id=`，均 no-store
- [ ] **Step 4: 通过** + tests/unit/proxy/ 回归
- [ ] **Step 5: Commit** `feat(proxy): modelscope gguf downloader for deploy wizard`

### Task 4: 前端 stepper — wizard.js + 模型池 tab 挂载

**Files:**
- Create: `inferfabric/dashboard/js/wizard.js`、`inferfabric/dashboard/fragments/wizard.html`
- Modify: `inferfabric/dashboard/__init__.py`（JS_MODULES/fragments += "wizard"）、
  `inferfabric/dashboard/fragments/model-pools.html`（向导卡容器挂载点，无 inline onclick）、
  `inferfabric/dashboard/base.html`（无需新 nav —— 挂模型池 tab 内）
- Test: `tests/unit/test_dashboard_wizard.py`（源码契约风格，沿 fork
  test_dashboard_security_page 模式）

**Interfaces:**
- Consumes: Task 2/3 两端点（`UI.adminHeaders()`）、`/deploy`（既有 POST）、
  `/admin/models/set-autostart`（#19）、store.on('tab_active')（切出停轮询/重置态）
- Produces: `window.Wizard.open(capability?)` 五步 stepper；步骤间状态
  `wiz = {capability, engine, mode, model:{id,name}, params:{name,port,quant,autostart}}`。

- [ ] **Step 1: 失败测试**（组装契约 + 源码契约）：get_html 含
  `id="tab-deploy-wizard"`；`wizard.js` 消费 `admin/deploy/options`、
  `admin/deploy/search`、`/deploy`、`set-autostart`；无 `onclick=`；防双击
  `_busy` 存在；Esc/切页重置在途态（Review Focus 5）；错误路径全部走 UI.toast
- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — 五步：①能力类型（options.capabilities）→ ②框架（收窄后
  engines，不可用引擎灰置并 deep-link packs tab）→ ③部署方式（deployment_modes）
  → ④搜索（防抖 300ms、分页「加载更多」、空态与失败态）→ ⑤参数+一键
  （名称默认取模型 id 尾段、端口留空由 install_yaml 分配、自启勾选默认关、
  一键序列=「下载 → 轮询 download/status → `POST /deploy` 装载 → 可选
  set-autostart」→ 结果卡含「去模型池查看」；下载轮询在切出 tab 时停止）
- [ ] **Step 4: 通过** + `tests/unit/dashboard/` 回归
- [ ] **Step 5: Commit** `feat(dashboard): five-step deploy wizard on model-pools tab`

### Task 5: 一键链路集成 + 收尾

**Files:**
- Modify: `inferfabric/dashboard/js/wizard.js`（一键提交序列）
- Test: `tests/unit/test_deploy_wizard_flow.py`（后端序列集成）

**Interfaces:**
- Consumes: Task 1 install_model（already_configured 可继续语义，Review Focus 3）、
  Task 3b downloader（start_download/download_status）、Task 4 stepper
- Produces: 完整用户流：搜索选中 → 参数 → 一键 = 下载（轮询状态）→ 装载
  → yaml 生成 + （可选）自启 → 状态卡。

- [ ] **Step 1: 失败测试** — fake handler 序列：`POST /admin/deploy/download`
  启动 + status 轮询至 done → `POST /deploy` installed →
  `set-autostart {name, enabled:true}` → 200；`already_configured` → 前端呈现
  「已配置，可直接切换」并允许继续（不发第二次 install）；下载 error →
  中止序列、错误 toast，不触发 install
- [ ] **Step 2: 确认失败** → **Step 3: 实现**（前端序列 + already_configured 分支）
- [ ] **Step 4: 全量回归** `pytest tests/ -q --ignore=tests/interactive` 全绿
- [ ] **Step 5: Commit** `feat(dashboard): wizard one-click flow — install + optional autostart`
- [ ] **Step 6: 浏览器验收**（隔离实例 :8990，HOME 级隔离，沿 #18 验收模式）：
  五步全流 + 平台收窄呈现 + 搜索失败降级；记录进 PR body

### 收尾

- [ ] 分支 `feat/deploy-wizard`（基于 main fc361ee），按栈节奏推 PR（base=main）
- [ ] PR body：五步截图/流程说明、ModelScope 端点校准记录、Review Focus 对照

## 自查记录

- Spec 覆盖：五步=Task 4；按平台收窄=Task 2；ModelScope 统一源=Task 3；
  一键+可选自启=Task 5；挂模型池 tab=Task 4。断链修复=Task 1（侦察新发现，
  属向导前置）。Task 3b=下载器（2026-11-21 用户裁决 B：install 语义不含权重
  下载，一键流必须先落盘 GGUF）；ModelScope 三端点已线上校准（记录于 Task 3b
  Interfaces）。
- 类型一致性：install_model 签名与 handler 既有三处调用一致（name, model_type,
  model_type_label）；search_models 归一化键与 Task 4 消费键一致。
- Review Focus 5 条均有钉住用例（Task 3×3、Task 2×1、Task 4×1、Task 5×1）。
