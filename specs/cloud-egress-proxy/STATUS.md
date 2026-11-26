# 云端出站代理事故修复（#78）

## 本轮范围

macOS 系统代理退出后，已运行网关可能继续使用 `urllib` 默认 opener 缓存的代理地址，导致推理与模型发现连接失败。保留 DSH 会话验证过的最小修复：三个模块共用 `ProxyHandler({})` 构造的直连 opener，不替换 Python 全局 opener，不改变路由、鉴权、重试或流式交付。

不改 Ubuntu 入站反向代理／MaaS 隧道。显式出站代理配置另议：默认直连、开启时显式配置、loopback 仍直连、代理故障不静默回退。此次版本不提前加入该开关。

## 验收

- 隔离回归：无外网、假上游与不可达代理均使用临时 loopback 端口；云转发、Responses 原生、Responses 的 chat 兜底出站、模型发现均须成功。原实现四例 RED（连接超时）；合入前验证 GREEN。
- Mac / Ubuntu：受影响用例、unit + integration 基线、进程与端口清退。PR 上记录各端所测 SHA 与签收；结果不包含原始日志或凭证。
- 本轮仅入版本，不操作生产网关或 GPU。

## 已知残留（同根因，阶段一不做 — 评审 I-1）

仍走 urllib 默认 opener 的本机出网点（同根因：默认 opener 快照环境代理；
本机 `http_proxy=127.0.0.1:7890` 环境，代理退出后以下能力哑到网关重启，
勿误判「#78 未修完」）：

- loopback metrics 抓取：`prometheus.py:210`、`token_stats.py:86`、`engine_adapter/{vllm,sglang,ninfer}.py` metrics
- 引擎 sleep/wake 控制：`process_manager/vllm.py:343,371`、`process_manager/llama_cpp.py:38,63`
- 网关内 pack 下载：`proxy/handler.py:2114,2136`

处置：与显式代理开关（Phase 2）一并收敛到统一 egress 入口。

## Phase 2 显式代理开关 — 待办与兼容条件（评审 I-2）

默认直连；显式配置才走代理；绝不隐式读环境。上线兼容条件：

1. 过渡：阶段一硬切直连后，依赖 `HTTP(S)_PROXY` 出网的部署升级即断云路由（本双机无此依赖，README 已告知）
2. 配置面：全局 egress 段/env 显式才生效；明确与 config_reloader/指纹的关系
3. 可观测：egress 模式暴露到 /health 与请求日志；代理失败接 AnomalyEvent + Prometheus 计数；「不静默回退」落到具体发射点
4. 显式代理启用时 loopback 恒直连需单独验收
5. 代理 URL 凭证进 secrets.env、不进日志

## 测试边界（评审 I-3）

- 回归夹具只设 `http_proxy`、上游为 `http://loopback`：https 出网未直接钉（机制同源，被 `ProxyHandler({})` 覆盖；真实云上游全 https）
- macOS scutil 系统代理无法进程内自动测；Linux/Windows 环境代理路径未单测
- RED 模式随 OS 不同：macOS 死代理表现为超时（bind 不 listen 吞 SYN），Linux 为即时 ECONNREFUSED——均必 RED

## Deferred minors（独立评审 2026-11-26）

- `health.py` 与 `egress.py` 双 no-proxy opener 收敛单例
- grep 式护栏测试：防新增云出站点再引默认 urlopen
- `egress.py` docstring 懒构建措辞（已随本轮修正）

## 独立评审（iff-reviewer，2026-11-26）

Mac 端主评审 + fresh-context 对抗交叉，全程只读：**无 Critical、无代码级阻塞，
verdict「With fixes」**。机制语义比对（= 默认 handler 链去掉 ProxyHandler，其余
逐项一致）、四例 GREEN、全量 2933 passed、旧代码必 RED 实证均在评审会话留存。

## 后续裁决（2026-11-26，用户）

- **方案 B（发版门禁补强）随 v7.12.12 落地**：`scripts/iff-post-restart-check.sh`
  重启后金丝雀（/health 版本一致 + 模型注册表非空 + 重启窗口内云端 discovery
  成功行 + 可选 `IFF_SMOKE_KEY_FILE` 微云请求；journalctl / 日志文件双源），
  与 constitution 新增「测试/出站卫生」「重启门禁」两条。
- **方案 C（显式出站代理开关）用户裁定为伪诉求，关闭**：出站恒直连为终态设计，
  I-2 Phase 2 五条兼容条件不再推进；I-1 残留点维持现状（本机出网、代理失效时
  哑到重启，已知可接受）。若未来出现必须经代理出网的部署环境，重新立项评审。

