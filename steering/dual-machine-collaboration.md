# 双机协同约定（vince-pc ⇄ Mac）

状态：2026-11-24 用户裁决（方案 A · 轻约定；交替使用形态）。本文件是两端 Claude session
的开工/收工执行清单，修改须经用户确认。

## 1. 角色与拓扑

- 唯一共享枢纽：`origin = github.com/vincentlau2046/InferFabric`。两端地位对称，
  **交替使用**：同一时间只有一端在开发，换机即交接。
- vince-pc：Linux + RTX 5090D，生产 :8999（NInfer/vLLM/SGLang 路线）；代码改动沿用
  sandbox-first（见 constitution.md）。
- Mac：darwin-arm，生产 :8999（launchd `com.macfabric.gateway`，llama.cpp/Metal +
  packs 路线）；feature-branch + PR 直做（已实践：PR #37/#38/#39）。
- 机器私有状态永不入库：`.state.db`、`_deps/`、`sandbox/`、`~/.inferfabric`、日志
  （.gitignore 已覆盖，勿加白名单）。

## 2. 交接协议（核心，交替形态下每次换机都走一遍）

**收工（一端停止开发时）—— 五件事：**
1. push 所有 WIP 分支（没写完也推，标题标 `WIP:` 或开 draft PR）；
2. 计划/spec/STATUS 等文档随代码入库，不留 untracked；
3. 更新对应 `specs/*/STATUS.md` 当前行（做了什么 / 停在哪 / 下一步）；
4. 未完成事项开 issue 或在既有 issue 留言，写明交接说明；
5. 不留"只有本地知道"的分支——本地分支要么已推远端、要么已合并删除。

**开工（另一端接手时）—— 四件事：**
1. `git fetch --prune` → 切 main → pull 对齐（**绝不基于旧 main 开新分支**）；
2. 查远端分支 + issues + 相关 STATUS.md，确认对端停在哪、有无 WIP 可续；
3. 新任务从最新 main 开新分支；续作则 checkout 对端推上来的分支；
4. 跑受影响模块的 unit+integration 冒烟，确认基线绿再动手。

## 3. 集成与分支卫生

- 一切进 main 走 PR（docs 也走 PR，成本就是自己点 merge，换来历史干净、对端可见）；
- 合并即删远端分支；存量 `pick/*` 与已合并分支做一次清理归档；
- commit message 沿用 conventional commits（feat/fix/docs/chore + scope）；
- **版本迭代（2026-11-24 用户裁定）**：patch 位（`0.0.x` 的 x）由任一端 Claude 自主升级，**每轮修改迭代一个 patch**（合并前自升 + README 版本历史表加行）；minor / major 须经用户裁决。版本位置：`inferfabric/__init__.py` + README 版本历史表。

## 4. 评审与验证

- 代码 PR 按 constitution 走 LLM cross-review（GLM-5.2）；另一端 session 可选复审；
- 测试基线：unit + integration（两端都必须绿）；operational（真机 GPU）只在 vince-pc 跑；
- 无 CI（方案 A 裁决暂不加 Actions）：**谁最后 push，谁负责全量绿**。

## 5. 缓议（不阻塞）

- CI 兜底（方案 B）：交替纪律失效或回到"同时并行"时再升级；
- 双平台遥测注入、custom 包准入等：见 packs-platform-adaptation spec STATUS，与本约定无关。
