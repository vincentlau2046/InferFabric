# STATUS — dashboard-ux-v3

- **阶段**：设计草案 + 样机已落盘；builder check 已完成（3 项裁决记入 design.md：读侧 API 扩展边界、缓存命中口径、dark on-accent 方案；协议列取消）。二轮评审改模型页分层；**三轮评审（2026-11-27）：模型池独立为导航项「路由」，导航 5→6**（推翻二轮"池置顶于模型页"）；模型页只留本地/云端清单与在池提示。等待用户复看样机后定稿。
- **样机验收方式**：浏览器打开 `specs/dashboard-ux-v3/preview.html`（本会话 Artifact 托管发布不可用：会话为 ANTHROPIC_AUTH_TOKEN 认证，非 claude.ai 登录）。
- **裁决链**：2026-11-27 用户四项裁决（范围=结构重构·保栈；IA=5 区任务导向；视觉=暖浅色微调；不满=层级/深度/页面/观感/聚类全项）。推翻 console-ux-batch WS4 十项导航与 v2-console 六 TAB 定位，详见 design.md 开头。
- **产物**：`design.md`（设计）、`preview.html`（可点击样机，示例数据）。
- **下一步**：样机验收 → 按 design.md §6 分期 A–F 出 PR 计划（superpowers:writing-plans）→ sandbox-first 实施。
- **未开始**：任何生产代码改动。
