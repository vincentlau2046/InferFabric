# 云端出站代理事故修复（#78）

## 本轮范围

macOS 系统代理退出后，已运行网关可能继续使用 `urllib` 默认 opener 缓存的代理地址，导致推理与模型发现连接失败。保留 DSH 会话验证过的最小修复：三个模块共用 `ProxyHandler({})` 构造的直连 opener，不替换 Python 全局 opener，不改变路由、鉴权、重试或流式交付。

不改 Ubuntu 入站反向代理／MaaS 隧道。显式出站代理配置另议：默认直连、开启时显式配置、loopback 仍直连、代理故障不静默回退。此次版本不提前加入该开关。

## 验收

- 隔离回归：无外网、假上游与不可达代理均使用临时 loopback 端口；云转发、Responses 原生、Responses 的 chat 兜底出站、模型发现均须成功。原实现四例 RED（连接超时）；合入前验证 GREEN。
- Mac / Ubuntu：受影响用例、unit + integration 基线、进程与端口清退。PR 上记录各端所测 SHA 与签收；结果不包含原始日志或凭证。
- 本轮仅入版本，不操作生产网关或 GPU。
