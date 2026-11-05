# InferFabric（IFF）× DSH 插件化 + 推广计划

> 关联文档：《API 规范文档计划》(docs/API_SPEC_PLAN.md) — OpenAPI 3.1 规范已实现（37 端点 / 46 schemas，GET /api/openapi.json）。

## 0. 现状核查（实测 2026-07-22）

- IFF proxy（:8999）健康：{"status":"ok","gpu_mode":"exclusive"}
- 当前活跃服务：qwen38-27b-abliterated（vLLM :8002，NVFP4，max_model_len=131072）+ bge-m3（llama.cpp :11441）
- 聊天自测通过：POST /v1/chat/completions（model=qwen38-27b-abliterated，用户消息 "？"）→ 200；
  由于 max_tokens=32，finish_reason=length，模型只生成了 reasoning 片段
- DSH Web GUI（http://127.0.0.1:3080）当前由预构建 launcher 服务（apps/cli/lib/bin.js --profile web --port 3080）；
  客户端插件的 HMR 热更新仅在 pnpm run dev:web（dev-web.ts）运行期间生效
- IFF 仓库：github.com/vincentlau2046/InferFabric（MIT）；尚无 PyPI 打包（无 pyproject.toml / setup.py）

## 1. DSH 插件化架构

### 1.1 两层设计

| 层 | 包 | 职责 |
|---|---|---|
| Host（Node） | packages/iff/iff（新建） | LLM adapter + IFF 工具 + admin 钩子 |
| Client（浏览器） | packages/client/ui-iff（新建） | 状态 slot + 命令 + 设置卡片 |

### 1.2 Host 层：dsh-iff cordis 插件

- LLM adapter（深度集成的核心）：
  - ctx.llm.registerAdapter(['iff'], new IFFAdapter(config))
  - IFF 暴露 OpenAI 兼容的 /v1/chat/completions，DSH agent 的 LLM 调用经 IFF :8999 路由
  - Config：baseUrl（默认 http://127.0.0.1:8999），adminToken 经 !js process.env.IFF_ADMIN_TOKEN（对应 IFF 的 ENV_VAR 密钥模式）
  - 流式契约（照 DSH LLM adapter guide）：usage 在 finish 之前发出，finish 之后不再发出；
    tool-call arguments 全程保持原始 JSON 字符串；尊重 options.signal
- 工具（ctx.tools.register / defineTool）：
  - iff_status → GET /status（gpu_mode / active_services / services_health / gpu_used_mb）
  - iff_switch_model → POST /switch {model}（model 或 "idle" 停止全部服务）
  - iff_cloud_providers → GET /admin/cloud/providers
  - iff_openapi → GET /api/openapi.json
- 钩子：ctx.on('tools/pre-execute') 门禁 — 当 IFF_ADMIN_TOKEN 已配置时，admin 操作必须携带 X-Admin-Token
- 组合（cordis.yml 增加条目）：
    - id: iff
      name: '@deepseek-ai/dsh-iff'
      config:
        baseUrl: 'http://127.0.0.1:8999'
        adminToken: '!!js process.env.IFF_ADMIN_TOKEN'

### 1.3 Client 层：packages/client/ui-iff

- package.json（参照 ui-model-selection 等现有客户端包）：
  - name: @deepseek-ai/dsh-client-ui-iff
  - main: lib/index.js；exports 含 ./client → lib/client.js
  - dsh.client 声明：
    "dsh": { "client": {
      "inject": [
        "@deepseek-ai/dsh-client-locale",
        "@deepseek-ai/dsh-client-runtime",
        "@deepseek-ai/dsh-client-ui-commands"
      ],
      "platform": "web"
    } }
- UI slot（ui-slots）：侧栏 "InferFabric" 卡片，实时展示：
  - GPU 模式、活跃服务 + 健康状态、GPU 显存用量
  - 轮询 GET /status：ctx.effect() + setInterval（HMR 卸载时 disposer 清理定时器）
  - 按钮：模型切换（POST /switch）、云 provider 列表（GET /admin/cloud/providers）
- 命令（ui-commands）：/iff status、/iff switch <model>、/iff cloud、/iff spec
- 设置卡片（ui-settings）：base URL + admin token（env 变量密钥，chmod 600）

### 1.4 构建与 HMR 流程

- 当前 :3080 由预构建 launcher 服务；客户端插件改动需 pnpm run build:web（vite build）+ 页面刷新
- HMR 模式：运行 pnpm run dev:web（dev-web.ts 三阶段 watch：tsc client types → tsdown lib → vite build）；
  宿主 webserver 对 lib/client.js 产物做 stat 轮询并广播 rebuilt 帧 — 无需刷新即热更新
- 注意：dev:web 与 pnpm run build 不可并发运行（都写 lib/ 与 apps/web/dist/）

## 2. 工作量估计

| 工作项 | 估计 |
|---|---|
| Host LLM adapter（参照 packages/llm/llm-deepseek 参考实现） | 1 天 |
| IFF 工具 + pre-execute 门禁 | 0.5 天 |
| Client ui-iff（slot / 命令 / 设置卡片 / 轮询） | 1–2 天 |
| vitest 测试（客户端包 tests/ 目录，遵循 DSH 测试政策） | 0.5 天 |

## 3. IFF 推广计划

1. **PyPI 打包**：创建 pyproject.toml，发布 inferfabric 到 PyPI（当前无打包）
2. **社区发布**：Show HN 帖子（"Show HN: InferFabric — 单卡 LLM 推理网关"）
   + Reddit r/LocalLLaMA + X 帖子
3. **DSH 网站集成页**：在 DSH website/（VitePress）的 Integrations 章节新增 "InferFabric" 集成文档
4. **旗舰演示**：博客/视频 "DeepSeek Harness 的 agent 跑在 InferFabric 上"（DSH agent 经 IFF 路由 LLM 调用）
5. **GitHub Release Notes**：为 v5.5.x 写发布说明（README 已更新到 v5.5.1：SVG 架构图 + Dashboard 截图）

## 4. 行动清单（按顺序）

1. （Host）创建 packages/iff/iff cordis 插件（LLM adapter + 工具 + 钩子）
2. （Client）创建 packages/client/ui-iff（slot / 命令 / 设置卡片 / 轮询）
3. 后台启动 pnpm run dev:web（managed job）→ 在 :3080 验证 HMR 热更新
4. 更新 IFF README（版本历史 5.4.0 → 5.5.x；新增 "DSH 集成" 章节）
5. 推广材料：Show HN 帖子 + 博客/视频脚本 + PyPI 打包