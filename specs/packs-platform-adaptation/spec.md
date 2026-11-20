# 能力包平台适配：先验证隔离与生命周期，再扩目录

状态：2026-11-20，用户确认修订方向；本文件取代原「先上 platforms 字段」方案。当前只完成设计和实施计划，尚未恢复代码实现。

## 1. 目标与边界

InferFabric 把已验证的本地「运行时 + 引擎 + 模型产物 + 性能配置」变成可重复装载、启停和卸载的单元。**统一目录、准入结果和用户操作；不统一 Linux 与 Mac 的隔离实现。** Dashboard 默认显示本机可用项；其他平台可查看、不可执行。ASR/TTS 是两端的正式预设能力，不是自定义链接的替代品，随底座验证后进入目录。

考虑过另做统一启动器，但会复制已有引擎生命周期和资源编排。保留既有 EngineAdapter / ProcessManager，仅为受管包附加可验证的启动约束。仅靠页面分栏或 manifest 字段不能执行准入；不把现役配置直接批量转换为包。非目标：换主网关路由、升级既有 vLLM 0.24、切换 Qwen38-27B-VL、接管或覆盖现役部署、增加任意镜像/脚本/新能力类别的自助准入。vLLM-Omni 两模型试点独立于本计划，不阻塞本计划，也不自动入正式目录。

## 2. 已核实的起点

- NInfer 与 SGLang 已有 Docker 生命周期，vLLM 支持 Docker 和 conda。NInfer 现役路径有只读权重挂载，但发布端口未限定宿主回环；SGLang/vLLM 现有 Docker 命令还使用宿主 IPC 和共享缓存。它们是复用对象，不是新包隔离的验收证据。
- Mac 的 `probe_sandbox()` 探测 SBPL 符号，现有 `PackManager` 装配的是 `NoopProvider`，`healthcheck_llama_cpp()` 直接启动子进程。符号可用不等于隔离已施加。
- 现有 llama.cpp Mac 运行时模板具有明确平台；Linux BGE embedding 使用 `gpu_layers: 0`。4B/8GiB 是现有通用策略，不能把 27B NInfer 包直接套进默认对话门槛。现有策略不得全局放宽。

## 3. 平台、隔离、资源是三个不同判定

| 维度 | 结果 | 规则 |
|---|---|---|
| 平台 | `compatible / incompatible / unknown` | 主机事实来自 `inferfabric.platform`：OS、架构、加速器；NInfer 旗舰另要求已验证的 SM 12.x / GPU 规格。命令在 PATH 不代表设备可用，探测失败不是 CPU 正常态。|
| 隔离 | `verified / degraded / unverified / unavailable` | 只有审核过的运行时配置及生效证据才可称 `verified`。进程组仅负责进程树管理，最多 `degraded`；须模板、包均显式允许，默认不把不可用的 Docker 或 Mac 沙箱降级成裸跑。Linux BGE CPU 试点允许显式降级，不代表完整沙箱。|
| 资源 | `ready / busy / insufficient / unknown` | 区分装载所需磁盘、验证时所需资源、正式启动时所需 RAM/VRAM。Linux 独立显存和 Mac 统一内存不可互换。缺遥测不为旗舰包盲放行。|

共享 wire 契约：三个维度各自返回枚举；操作返回 `allowed`、稳定的 `reason_code`，可附人类可读详情。首轮 reason 包括 `platform_mismatch`、`platform_unverified`、`runtime_unavailable`、`isolation_unverified`、`isolation_degraded_not_allowed`、`resource_busy`、`resource_insufficient`、`resource_unknown`、`ownership_conflict`、`runtime_identity_mismatch`。成功 `reason_code=null`；显式降级成功仍保留 `isolation=degraded`。相同判定规则服务目录和执行闸；UI 用 API 结果展示，不能成为权限闸。并发/重读时的执行结果可比目录快照更严格。

### 目录与旧包

- 可选 `platforms` 申明只**收窄**审核过的模板范围，不能扩大；旧包未声明时按模板推断，已知 Mac 模板绝不变成跨平台，未知模板为 `unknown`、不可新装。禁止新明确通配；需要多平台时列出对应平台，运行时仍需匹配各自模板。平台键首轮提议 `linux-nvidia` / `linux-cpu` / `darwin-arm`；CPU BGE 可以在 Linux NVIDIA 主机跑，不要求没有 GPU。Mac wire 名称需 #23 对齐后冻结。
- 已有包不被迁移、重装、自动停止或重新打隔离标签。旧包保留原启停、查询、卸载路径；新装/升级进入新版模板的旧包，必须明确满足该模板的准入条件。目录原有方式无法判定的旧条目只读，显示「未验证」；不能在新受管包启动入口借用 legacy 路径。

## 4. 受管包的执行约束

- **同一个审核过的运行时策略覆盖临时验证和正式启动**。两者实例名、端口可不同，但不能因验证是临时的就绕开隔离。试点需要从零取证：只有不进目录、明确授权的私有试点配置可进入「临时取证」动作；先检查可验证的运行配置，再检查实际实例并验证业务，失败不得发布配置。该动作不是对外装载或启动的例外，不允许未审核的策略或产物。持久化解析后的模板/产物标识与包所有权；正式启动读回并校验。坏引用、缺失记录、模板或镜像不匹配均拒绝，不能默认为 legacy。发现旧实例时只认属于自己的已验证身份，不按端口或名字收养/停止现役实例。
- **Linux 容器**：用审核镜像的不可变身份（本地构建可用匹配的本地 image ID，发布时用固定 digest）；端口只发布到宿主 `127.0.0.1`，权重只读、可写目录限包专用路径。不得使用 privileged、宿主 IPC/网络、宿主 Docker socket、全局可写缓存或任意透传 flags/env；GPU 只给获准设备。默认不需要联网的运行时不得访问外部网络；入站回环与出站限制分别验证。涉及额外权限或无法证明的限制时，停止试点并记录，不把默认 Docker 等同于沙箱。
- **Linux 原生**：BGE embedding 走 CPU，包内/审核的 llama-server；显式回环监听、进程组收割。仅当模板和 manifest 同意时以 `degraded` 运行；不声称限制了宿主文件访问或出站网络。
- **Mac 原生**：由 Mac 端选定可执行的 provider，反馈对同一包验证与正式启动均生效的证据；仅 API 存在或 health 200 不足以标记 `verified`。无证据只能按显式允许的降级策略运行，或不可执行。
- **事务与 GPU**：装载不切换现役模型、不改路由、不启动常驻包。GPU 临时验证只在已有 GPU 管理锁下且确认 GPU 可用时运行；占用时返回 `resource_busy`，不挤占现役服务、不执行 GPU reset。验证实例使用独立包身份和端口；先完成验证/停止，再发布受管配置并热加载；失败只清理本次拥有的配置、实例和 staging。包安装初期不自动收养共享产物；试点借用明确批准的现有只读权重时，所有权标为外部，卸载行为不得删除其数据或改变现用容器。正式试点启动继续使用现有资源编排，准入拒绝不得先停旧服务。
- **资源政策**：保留旧 LLM/VL 默认 4B 和系统预留 8192 MiB。Linux NInfer 工作站例外只用于审核登记的包版本/模板/产物，取真实设备能力、RAM/VRAM/磁盘预算，不允许 manifest、自定义包、`force` 或篡改 YAML 提升权限。内存报价先实测后冻结；不假设名义 32GB 等于可分配预算。CPU 包不触碰 GPU 锁/重置。

## 5. 分阶段与退出标准

| 阶段 | 范围 | 退出标准 |
|---|---|---|
| 0 契约 | 本文、#23、阶段 1 计划；对齐枚举与权威证据 | Linux 可做本地验证准备；Mac 未给证据时不冻结双平台能力声明。|
| 1 双路径试点 | Linux：NInfer TXT + Docker、llama.cpp + BGE embedding；Mac 并行用一个原生包验证相同契约 | 非生产实例完整走装载、临时检查、启动、停止、回滚、卸载；验证业务端点、端口范围和运行时身份。NInfer 在授权的 GPU 窗口，同镜像、权重、NVFP4/上下文/MTP 配置同负载比对 TTFT、吞吐、显存峰值。未核实的性能标未验证，明显退化不得晋级。Mac 同样用真实执行证据，不用探针替代。|
| 2 Dashboard | 共用目录，当前平台可用项置顶，其他平台只读；显示缺 runtime、隔离降级、资源不足和操作说明 | Linux/Mac 隔离代理上的真实浏览器复验装载、不匹配、失败恢复，后端与界面一致；不拿生产代理做交互。|
| 3 扩目录 | SGLang、vLLM、BGE rerank；SenseVoice/Qwen3-TTS 与 Mac 对应首批音频包 | 模板先验证再纳入；ASR 用真实短音频确认文本，TTS 用文本确认可解码音频；端口 200 不算业务验收。未知链接/品类是后续扩展，只能进入审核过的模板，不接受可执行钩子。|

各代码批次：Python 3.10+，不升级既有 vLLM 0.24，不改代理转发核心；sandbox-first，unit+integration、`import inferfabric`、隔离代理核心 API 冒烟、fresh-context cross-review、干净提交树复验。Dashboard 阶段另加 Playwright 门禁。未经单独授权不重启生产，不启动真实 GPU 试点。

阶段 1 的可执行任务见 [实施计划](../../docs/superpowers/plans/2026-11-20-packs-runtime-pilot.md)。阶段 2/3 只列依赖与发布门槛，不提前锁定尚未由试点验证的模板/实现。
