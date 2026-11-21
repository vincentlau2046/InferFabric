# Mac 受管能力包试点证据（#23 Mac 侧，Task 3 真机取证）

日期：2026-11-21。基线：main@fcdfaad + 本地合并 fix/llama-cpp-chat-route、
fix/llama-cpp-stop-macos（两修复的验证即在本轮完成）。环境：干跑沙盒
（APFS COW 克隆，IFF_STATE_DIR/IFF_MODELS_DIR 副本、:8997、模型端口
58401→58411 / 52360→52361）；**生产 :8999 与 ~/.inferfabric 全程零改动**。

## 一、四项证据（spec §5 阶段 1 Mac 试点）

### ① 三维准入

- packs status（管理 API）：两包 installed，install_path 指向包目录（沙盒域）。
- 平台维：模板 `llama-cpp-macos-arm64-v1`（darwin-arm）；隔离维：degraded
  （模板 `allow_degraded=True` 显式允许，未声称 SBPL verified）；资源维：
  darwin-arm 统一内存语义（#23 裁决 B1′），受管启动走 memory-ledger 准入
  （本轮干跑无拒绝发生；拒绝路径由单测钉死）。
- GPU idle-only 真机实证：qwen3 运行中尝试切 bge → `resource_busy`
  （fail-closed 正确拒绝、零挤占）；/stop 释放后切换成功。

### ② 两启动路径 provider 效应对照

受管路径（本试点）：
- 引擎 = 包内二进制（`packs/<id>/1.0.0/runtime/engine`），cmd 含
  `-ngl 99`（chat，审核 YAML Metal 全卸载）/ `-ngl 0`（embedding 强制 CPU），
  `--host 127.0.0.1`（loopback），chat 包不带 `--embeddings`（Ruling：
  该 flag 语义为 restrict-to-embedding）。
- 探针分派：chat 包 openai_chat_ready_v1 → POST /v1/chat/completions
  （max_tokens=1，校验 choices）；embedding 包 → /v1/embeddings。
- 冷启动实测：qwen3 首启 6.1s、复启 3.1s（Metal shader 缓存暖）→
  **Nit 实测结论：/health 200 即模型加载完成，chat 探针 2s 内通过，
  无误杀**（探针在 health 之后运行，2s 维持）。
- legacy 路径对照：managed=None 行为逐字节不变（0.0.0.0、-ngl 透传、
  恒 --embeddings、无机制探针）——哨兵测试钉死。

### ③ 真实 API 请求

- chat（经 :8997 代理 → 受管 58411）：「1+1等于2。」，usage
  prompt=18/completion=7，延迟 314ms（request_log 落账行在案）。
- embeddings（受管 52361）：bge-m3 真实向量返回（1024 维 OpenAI 形态），
  20ms。
- 反例（正确拒绝）：受管切占用 GPU → 503 resource_busy（request_log
  503 行在案）。

### ④ 卸载所有权

owned 包卸载（confirmed）：删包目录 + 受控 YAML + store 记录，全部干净；
**生产 ~/.inferfabric 原包目录分毫未动**（沙盒克隆隔离实证）。借用产物
（external）不删除由单测钉死（test_uninstall_managed_pack_never_deletes_
borrowed_weights）。

## 二、真机首发现 → 修复 PR（全部走 PR 流程）

| # | 发现 | 定性 | PR |
|---|---|---|---|
| 1 | P1 旧 YAML `mode: on_demand` 被 loader 当 gpu_role 别名覆盖 → 撞启动策略读回校验 | 迁移配方（产品内配置调整）：删 `mode:` 行 + bge 对齐核准策略（本沙盒已验证配方） | 运维（无代码） |
| 2 | llama_cpp chat 被路由进 Ollama 原生 /api/chat → 必 404（fork P0 补丁未上游） | 产品缺陷 | **#33**（RED→GREEN，Ollama 行为不变） |
| 3 | /stop 返回 stopped 但引擎仍存活：macOS 无 fuser、pkill 回退只匹配 vllm → 停止恒空转 | 产品缺陷（Linux 被 fuser 掩盖） | **#34**（pid 文件进程组收割 + lsof 回退；真机复验：引擎消失、端口干净） |
| 4 | catalog sha256 为下载压缩包哈希，解压后 engine 二进制哈希不同（685941b8…） | 事实记录：回填校验强度=模型字节级 + engine 构建身份/可执行；来源链=P1 安装期压缩包校验 | 记录（安装器后续可存解压哈希） |

## 三、回填 vs 重装（迁移前置裁决项，待用户裁决）

生产两包缺 manifest.yaml + ownership（P1 安装事务早于持久化机制）→
受管 gate 会拒。回填（本沙盒已验证）：sha256 校验模型字节 → 写
manifest.yaml（catalog 权威）+ ownership 记录 + 最小 YAML 编辑（删
mode:、bge 策略对齐）。重装：下载链完整但 qwen3 需重下 2.5GB。

## 四、生产一致性核对清单（:8999 放行门）

| 项 | 结果 |
|---|---|
| migrations user_version（state=5 / log=11） | ✅ 与生产一致 |
| IFF_MODELS_DIR 覆盖后核心与 packs 同源 | ✅ 本轮实证（packs root 随 IFF_STATE_DIR） |
| memory-ledger 准入（受管 chat 路径） | ✅ 无拒绝发生；拒绝路径单测钉死 |
| auth 语义与生产一致 | ✅ API key 认证生效（Bearer 校验、401/200 落账） |
| secrets.env / admin token 复用 | ✅ 快照直用 |
| request_log / token-stats 读写 | ✅ 请求行落账正常 |
| plist diff 审阅（仅加 IFF_MODELS_DIR） | ✅ 演练实测 diff：venv/WorkDir/PYTHONPATH 三处指回仓库 + 新增 IFF_MODELS_DIR，无其他差异 |
| 回滚演练（plist .bak → launchd reload → 旧版健康） | ✅ 2026-11-21 生产实演双半场全绿：切新后受管 /switch 3.4s healthy（包内 engine、loopback、-ngl 99、无 --embeddings、chat 探针过）、/stop 收割干净（#34 生产复验）；回滚后旧 MacFabric 网关 health 200、模型清单一致（回填 YAML 与旧 loader 兼容） |

## 五、放行状态

四项证据 ✅；一致性清单 8 项 8 ✅；回滚演练 ✅（双半场：切换动作与回滚路径均在生产实证）。
回填已落生产（备份 `~/.inferfabric/backups/backfill-20261121-165022/` + plist
`.bak-20261121-165434`）。

**Task 4 完成（2026-11-21 17:34:18）：:8999 已切至 InferFabric main@1f74758**
（新 pid 16663，/health 200 idle、两包模型 + 云目录、packs installed；回滚保留
`.bak-20261121-165434` + ~/Library/MacFabric/prod 未删）。迁移链收官，遗留：
#36 darwin 复测（随修复推送）、A1 隔离分级 wiring PR。
