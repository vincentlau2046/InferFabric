# PR-25 能力归模型 / 池归优先级 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 设计 | ✅ 2026-10-31 | 用户裁决：能力归模型（业界 LiteLLM Router / OpenRouter 模式）、vision 唯一请求感知维度、无「跳过 VL」规则（VL 排最前就全接）；方案批准即 7.9.0 minor 授权 |
| 后端实现 | ✅ commit 8c51dc0 | selector 视觉过滤（`_vision_capable` 三态）替换候选级 features 排除；dispatch 请求感知提取（openai `image_url` / anthropic `image`）+ 400 自解释（候选 id + `feature_unsupported` + 固定码 + 模型侧声明指引）；catalog vision 字段（本地 model_type=vl / 云 supports_vision is True）；admin dry-run `vision` 参数 + runtime 端点每候选 `vision` 布尔 |
| 安全加固 | ✅ commit c2368fa | 自动化安全评审 MEDIUM（fail-open 路由/数据披露）：`RuntimeView.local_vision: frozenset \| None` 三态化 — 元数据源不可用（mgr 缺入口/列举异常）≠ 确认无视觉，选择器不做静态视觉判定（按优先级路由，上游非回退 4xx 终止，不泄云）；镜像既有云未发现 None 语义。默认配置本就 fail-closed（400 自解释）已在修复时核实并记录于 commit |
| 前端实现 | ✅ commit 2657f68 | 编辑器能力列/复选框/全能力徽章/解除约束全退役；候选卡「视觉」小标（`.mp-feat-vision`）；云面板收敛为单「视觉」声明 chip（两态，.x 排除态退役）；添加表单仅视觉复选框 |
| 浏览器门禁 | ✅ commit 3fc52ee | test_08 重写三段契约（纯文本 200 / 带图 TXT-only 池 400 自解释 / 带图 VL 池 200 + 卡片视觉 chip + runtime 数据源复核 + 无跳过 VL）；scratch models.d 注入视觉靶 Qwen38-27B-STUB-VL（model_type=vl、同桩端口）；test_cloud_caps_browser 重写单 chip 两态 + 点灭不落 false + 双主题对比度。双门禁 12/12 |
| TDD | ✅ | 单测逐条 RED→GREEN；门禁重写首跑揭出真 bug：VL 靶名是原名超集时链式 replace 连环命中 served_name/model_id 造 '-VL-VL' 双后缀（catalog 实证）→ 名字改非超集 |
| 全量回归 | ✅ | 1923 passed（unit+integration；基线 1923 持平 — 7.8.0 能力控件测试组随功能退役删除，7.9.0 新增组补齐） |
| import 冒烟 | ✅ | `python3 -c "import inferfabric"` → 7.9.0 |
| JS 语法门 | ✅ | `node --check` model-pools.js + cloud.js |
| cross-review | ✅ | fresh-context subagent（独立复跑全量 1923 passed；评审范围 f44e96a..22a04e3）：**无 Critical/Important**。三项 Minor 全修（M-1 测试卫生 / M-2 local_vision 双收 name+served_name / M-3 声明指引后缀只挂 no_compatible_target），各 RED→GREEN；设计决策核实记录见下 |
| 版本 | ✅ commit 22a04e3 | 7.9.0（minor，用户随方案批准）；README 历史表已加行 |
| 合并/部署 | ✅ 2026-10-31 | ff-merge f44e96a→2c98d52 + BatchMode push 完成（pre-push 钩子全量绿 + import 冒烟 v7.9.0 OK）；**生产重启仍需用户单独授权**（与未部署的 7.8.0 一次重启合并上线）；重启后一次性运维：云面板为 3 个云视觉模型点亮 supports_vision（cand-1a18c915094s/p、cand-mvs52l4h）+ dry-run `{"pool":"premium","vision":true}` 验证 |

## 执行裁决记录

### cross-review（2026-10-31，fresh-context subagent）

- **无 Critical/Important**。三项 Minor 全修（commit 见合并记录）：
  - **M-1（测试卫生）**：test_admin `test_get_raw_is_json_serializable_with_yaml_dates`
    注释误吞 `known_providers={"prov"},` 实参 → registry 云候选变悬空但仍
    绿；恢复实参。
  - **M-2（死胡同指引）**：`runtime_view` local_vision 只收 served_name，
    手写 YAML 用内部 name 引用的 VL 候选带图请求被误判
    feature_unsupported 且指引指向已声明的 models.d → 双收
    name+served_name（同 active_services 策略）。
    RED：`test_runtime_view_local_vision_includes_internal_name`。
  - **M-3（事实错误后缀）**：`_no_eligible_message` 的「池内无视觉候选」
    声明指引在 no_ready_target（503，池内有 VL 候选只是未就绪）时也追
    加 → 后缀改挂 `sel.error == "no_compatible_target"`。
    RED：`test_no_ready_target_message_has_no_vision_suffix`。
- 评审核实为刻意设计（非缺陷）：local_vision=None 元数据故障期 runtime
  端点 vision=False（纯展示，admin.py 注释明示）；候选 features 三处
  读时容忍（config/registry/admin 视图）；supports_tools 字段格式兼容；
  omni/ocr 不进目录也不算 vision-capable（spec 只绑定 model_type=vl）。
- 评审确认安全修复密闭：runtime_view 全部异常路径降级 None、selector 只在
  `is False` 时排除，两条测试在 None 被降级为空集时会红。

- **视觉小标数据源 = runtime 端点**（spec 原文为 catalog）：catalog 进渲染路径
  会破坏 A2 懒预热裁决（Task 1：catalog 仅手势拉取），且需要跨模块缓存
  失效钩子、首绘滞后；runtime 端点本就 5s 轮询、已流入 candCard 的 `rt`
  参数、判定源与 selector 同一（local_vision / supports_vision）。spec.md
  实现表已随此裁决修订（commit 2657f68）。
- **`_candidate_view` 保留 `features` 透传**：GET /admin/model-pools 的候选
  视图仍回传 legacy features 字段 — 纯读、前端已不消费、编辑器以 raw 为
  基底读-改-写不受影响；删掉反而动 7.8.0 遗留测试锚点，收益为零。
- **`supports_tools` 字段保留、UI 退役**：cloud_provider.yaml 格式兼容（老
  配置零迁移），catalog/路由不再消费；云面板无入口（7.9.0 语义下 tools
  恒真不建模，声明通道无意义）。
- **I-2 残留（PR-24 遗留，未裁决）**：多候选池多轮 agent 流量的会话亲和
  仍开放 — 本 PR 不触及（纯路由语义变更，守卫行为不变）。

## 验证记录（截至 commit 22a04e3）

- 浏览器门禁：test_model_pools_browser 10/10 + test_cloud_caps_browser 2/2
  （console 零 error；双主题对比度全过）。
- 全量 unit+integration 1923 passed（22a04e3 复跑）。
- RED 实录：
  - test_admin 视觉端点用例首跑 `duplicate priority 2`（vlloc 与 cld 撞
    优先级）→ cld 升 priority 3。
  - test_08 首跑 select 超时 — 已配置卡的行内选择器是
    `data-mp-inline-replace`（test_04 模式）而非 `data-mp-config-pool`。
  - test_08 二跑「did not find some options」— VL 靶名超集连环 replace
    bug（见上 TDD 行），catalog 复现 `Qwen38-27B-TXT-VL-VL` 后修正。
