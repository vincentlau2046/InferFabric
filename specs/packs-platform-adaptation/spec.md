# 能力包平台适配 — 平台框架 + 底层隔离底座（spec）

状态：草案（2026-11-20，待用户终审后进入 PR-A 实现）

## 0. 裁决记录

- 2026-11-20 用户裁决：**混合方案**（manifest 平台字段 + 校验过滤 + 能力包 tab 内平台分组展示）。
- 2026-11-20 用户裁决：**平台框架与底层沙箱/隔离方案先定**，asr/tts 首批预置随后；Linux 端产出并向 Mac 端提案（跨平台协作总线）。
- 事实修正记录：最初调查误判「ninfer 已迁 conda」；运行态证实 ninfer/sglang 均为 **docker 现役**（PM launcher 即 Docker container lifecycle），vLLM 双形态（conda + docker）。

## 1. 现状事实（已核实）

| 层 | Mac（darwin-arm64） | Linux x86+NVIDIA（本机现役） |
|---|---|---|
| 隔离底座 | SBPL / sandbox-exec（`packs/sandbox.py` PK0 冻结：probe full/degraded/unavailable，降级不冒充） | **docker**（PM.ninfer / PM.sglang = container lifecycle + `ensure_image_pulled`；vLLM `start_vllm_docker`）；conda 原生为 legacy/degraded |
| runtime_template | `llama-cpp-macos-arm64-v1`（唯一已实现） | 无 pack 模板（引擎直跑，YAML 直接声明 docker_image/container_name） |
| 平台感知 | `platform/probe.py` IS_MACOS 门禁；pack schema **无 platform 字段** | 同左 |

四种目标组合（docker+ninfer / docker+sglang / docker+vllm / llama.cpp+模型）**launcher 层零缺口**，缺口仅在 pack 体系（schema、模板注册、准入、呈现、预置 catalog）。

## 2. 设计

### §1 manifest 平台字段（schema.py）

```yaml
pack_id: llm.qwen38-ninfer
platforms: [linux-nvidia]        # 新增；缺省 [any]（向后兼容，旧 manifest 视为 any）
platforms: [darwin-arm, linux-nvidia]
```

- 平台键枚举（与 `platform/probe.py` 对齐）：`linux-nvidia` / `linux-cpu` / `darwin-arm` / `any`。
- 解析规则：manifest `platforms` 与 probe 结果求交；交集空 → **不可安装**（UI 只读展示，理由文案给出平台不匹配）。
- 校验：未知平台键 = ManifestError（严格，不做静默映射）。

### §2 隔离底座矩阵（substrate）

每个 runtime_template 声明其隔离底座，准入时对照平台探测结果：

| substrate | 平台 | 隔离边界 | 健康检查 | 降级语义 |
|---|---|---|---|---|
| `docker` | linux-nvidia（darwin 不支持 GPU 直通场景不列） | 容器（image + container_name 单一事实源） | `docker inspect` 运行态 + 端口 health | docker 不可用 → 拒绝安装（不降级到裸跑） |
| `sbpl` | darwin-arm | sandbox-exec profile（PK0 已冻结接口） | probe full/degraded + 现有 healthcheck | degraded 保留诊断（沿用 PK0 纪律） |
| `process-group` | 任意（legacy/degraded） | 进程组 + 目录隔离 + 资源监控（sandbox.py degraded 兜底语义） | 现有 healthcheck | 显式标注「非完整隔离」，不冒充 |

**纪律**：Linux 侧 docker 是一等底座（容器即隔离边界），conda 原生模板一律标 `process-group` 并在 UI/日志显式披露降级；与 PK0「不把降级冒充完整隔离」同一原则。

### §3 runtime_template 注册表

命名 `<engine>-<substrate>-v<N>`（platform 编码进 substrate/校验，不再进模板名后缀堆叠）：

- 首批新增：`docker-ninfer-v1`、`docker-sglang-v1`、`docker-vllm-v1`、`native-llama-cpp-v1`（darwin-arm 由 SBPL/二进制来源字段区分，保留旧名 `llama-cpp-macos-arm64-v1` 兼容映射）。
- `docker-*` 模板必填字段：`image`（建议 digest pin）、`container_name`、`port`——全部映射进 ModelConfig 既有字段（`docker_image` / `container_name`），**不改 config.py 结构**。
- `runtime_prep.py`：`docker-*` 模板 prepare_runtime = 校验 image 可用（`ensure_image_pulled` 语义复用）+ 校验字段齐全，无解包动作。

### §4 准入流程（installer 接线）

install 时序：平台求交（§1）→ policy 门禁（既有 4B/8GB）→ runtime_template 在平台注册表 → substrate 可用性探测（docker/ sbpl probe）→ 装载。任一步失败 = 诚实失败 + 明确错误码（沿用 installer 事务零残留纪律）。

### §5 Dashboard 呈现（能力包 tab 平台分组）

- 当前平台可用包置顶正常态；其他平台包折叠分组、只读、标注「不适用于本机（darwin-arm / linux-nvidia）」。
- 不做双份存储：同一 catalog，渲染层按 `platforms` 过滤分组（跨端同 catalog 各自过滤，服务 Mac 端同构）。

### §6 首批预置包（现役反向抽象）

| pack_id | 模板 | 来源（现役事实） |
|---|---|---|
| llm.qwen38-ninfer | docker-ninfer-v1 | Qwen38-27B-TXT.yaml（ninfer:accept-map-v2, :8007） |
| vl.qwen38-ninfer | docker-ninfer-v1 | Qwen38-27B-VL.yaml（:8009） |
| llm.muse-glimmer | docker-sglang-v1 | muse-glimmer-vl.yaml（sglang:dev-muse-glimmer） |
| embedding.bge-m3 / rerank.bge-reranker | native-llama-cpp-v1 | bge-m3.yaml / bge-reranker-v2-m3.yaml |
| asr.sensevoice / tts.qwen3 | conda process-group 模板（第二批，随 §3 扩表） | asr-sensevoice.yaml / tts-qwen3.yaml |
| t2i.comfyui | conda process-group 模板（第二批） | comfyui.yaml |

qwen36-35b-ninfer 待其 serve 验证后入表（依 qwen36-35b-ninfer-migration 状态）。

## 3. 分批

- **PR-A**（本 spec 主体）：schema `platforms` 字段 + 平台键校验 + 准入接线 + substrate 矩阵常量 + 单测。
- **PR-B**：docker-* 模板族 + 首批预置包（llm/vl/embedding/rerank）+ catalog。
- **PR-C**：dashboard 平台分组呈现 + 浏览器门禁（真实浏览器交互测试，非文本断言）。

## 4. 测试门禁

- 单测：平台求交（空交集拒绝/any 兼容）、未知平台键 ManifestError、docker 模板字段校验、准入序列各失败点诚实错误码。
- 每批全量 pytest + import 冒烟 + 干净 worktree 复验；PR-C 加 Playwright 真实交互门禁。
- cross-review：fresh-context subagent 一轮。

## 5. Mac 提案（跨平台总线）

Linux 端将本 spec §1–§2 作为提案发 GitHub issue 征询 Mac 端意见（darwin-arm 平台键命名、SBPL substrate 与本框架的对接方式、Mac 首批包清单由 Mac 端谁提谁建）。审阅性意见不阻塞 PR-A 落地（bidirectional flow：main 归 Linux 端）。
