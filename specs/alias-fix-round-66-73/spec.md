# 别名/云路由缺陷修复一轮（#66–#73）— spec

审视基线：`f7b3757`（main，PR #62 合并后）。8 个 issue 一轮修复，单分支单 PR，
每 issue 独立 RED→GREEN 提交。验收：Mac 全套 + vince-pc worktree 全套（路由受影响，
双平台跑满）；§4.3 清理照旧。

## 全局约束

- 不改 proxy 转发核心路径（PR-14 排除项）；forwarder.py 不动
- TDD：每个 issue 先看测试因正确原因失败，再实现
- 锁序约定沿用 `cloud_discovery.py:273`：`_save_lock → _models_lock`；
  `_models_lock` 是 RLock（272 行），handler 侧可嵌套进入
- 提交信息 `fix(alias): … (#NN)`，每 issue 一提交；PR 尾注 attribution

## 各 issue 设计

### #66 Responses 路径别名/规格模型解析不一致
- `responses_bridge.py:323`（回退 chat 探测）：`cloud_models.get(provider + "/" + c.model_id)`
  → `pm.cloud.resolve_cloud_model(c.provider, c.model_id)`
- `responses_bridge.py:330`、`:431`：`data["model"] = c.model_id` → `cm.model_id`
  （对齐 `forwarder.py:378` chat 路径：上游永远收注册表条目的原始 model_id）
- caller（`:734-749`）已把别名解析成上游 id 放进 `_DirectCandidate.model_id`，
  但 spec 键/带 `/` 的 id / 前缀键直呼仍会原样透传 —— resolve_cloud_model 统一归一
- 测试：别名 id、spec 键（model_id 与键不同）、`prov/glm` 前缀键直呼三形态，
  断言回退 chat 的请求体 `model` == 注册表 model_id 且探测不再 502

### #67 已退役别名静默路由到他处
- `resolve_route`（`cloud_discovery.py:684`）：入口先查 `_retired_aliases`，
  命中 → 返回 None（先于 local/短名回退 —— README 删除承诺「绝不回退到
  本地/另一 provider/池」）
- `explain_unknown`（`:745`）：`alias_target` 未命中时查 retired，命中给出
  「目标提供方 X 已删除」解释（既有分支由此可达）
- `validate_alias` 调用点（handler 端点 + #69 加载路径）：退役名并入撞名集合，
  同 provider 显式重设除外
- 新增小助手 `retired_alias_owner(alias) -> str | None`（锁内读）
- 测试：删除 provider 后旧别名 404 + explain 带删除语义；他 provider 抢设
  同名被拒；重建同 provider 显式重设仍放行

### #68 撞名校验漏本地模型 name
- `handler.py:3297` local_names 增加 `{m.name for m in mgr._models.values()}`
  （`find_model_by_served_name` 第二段按 `name` 匹配，先于 resolve_route）
- 测试：本地 `name: qwen-chat` / `served_name: Qwen3-27B`，设别名 `qwen-chat`
  → 400 alias_conflict

### #69 加载路径别名完整校验
- `_load_config` 别名校验循环（`cloud_discovery.py:1170-1185`）：非空检查之上
  复用 `validate_alias` 规则（`_ALIAS_RE`、`fabric-` 前缀、本地/注册表短名/池/
  其他别名撞名 —— 加载路径的撞名集合不含本地模型（无 mgr），为注册表短名 +
  其他 provider 别名 + 退役名）+ 跨 provider 唯一
- 违规 → 抛 ValueError 走既有 corrupt 保注册表路径（全有或全无，与坏条目
  行为一致；issue 给了 corrupt 或丢弃+warning 两选一，取 corrupt 保一致性）
- 同 provider 重声明退役别名 = 显式重设：应用后从 `_retired_aliases` 摘除
  （对齐端点 unretire 语义）；跨 provider 声明退役名 → corrupt
- 测试：双 provider 同别名 → corrupt；坏命名 → corrupt；同 provider 重声明
  退役名 → 激活且 retired 清空；跨 provider 抢退役名 → corrupt

### #70 /v1/models 与改别名并发 500
- handler 别名端点的原地 `del cfg.aliases[a]` / `cfg.aliases[alias] = model_id` /
  `cfg.aliases.clear()` 改为**构造新 dict 后整体重绑**（`cfg.aliases = dict(...)`,
  或统一加 `with pm.cloud._models_lock:`，RLock 可重入 —— 实现时二选一，
  取「锁 + 快照」双保险：写侧持锁，读侧 `list()`/`dict()` 快照）
- 读侧快照点：`_v1_models` 别名循环（`handler.py:1166-1175`）、GET providers
  `dict(cfg.aliases)`、`other_aliases` 构建循环
- `_retired_aliases` 写侧同样持锁（retire/unretire 内部已持锁，handler 回滚
  重绑处补快照语义即可 —— 重绑本身原子）
- 测试：并发压测（写线程循环 set/remove × 读线程循环 /v1/models 等价函数）
  无 RuntimeError；或以 GIL 交替语义构造 dict-mutation-during-iteration 用例

### #71 删除 provider 不回滚
- DELETE 分支（`handler.py:3453-3472`）：drop 前快照
  `{name: cfg}`（provider 配置对象）、注册表条目 `{k: m for k,m in
  cloud_models.items() if m.provider == name}`、`_candidates.get(name)`、
  `_retired_aliases` 全量；save_config 抛错 → 逆序恢复（cfg 回插、注册表
  条目回插、候选回插、retired 重绑），500 响应同现状
- 测试：save_config 抛错后 provider 仍可路由、别名仍激活、retired 未变

### #72 routable_caps/count 漏前缀键与带 / 的 id
- GET providers `routable_caps`/`routable_count`（`handler.py:3412-3418`）与
  `_routable_count`（`cloud_discovery.py:679`）：改为按 `m.provider == name`
  遍历**全部**注册表条目，键 = 条目 key 去掉 `name/` 前缀（provider 内 id），
  双键（短名 + 前缀名同模型）按 provider 内 id 去重计数
- 测试：双 provider 同短名（B 只有前缀键）→ B 行 routable_count=1 且 caps
  含该模型；model_id 带 `/`（`org/x`）→ 入 caps map 且计数

### #73 别名复制按钮不等剪贴板结果
- `cloud.js:1522-1529`：`navigator.clipboard.writeText(...).then(成功 toast,
  失败 toast)`；`navigator.clipboard` 不可用（局域网 http 非安全上下文）→
  `document.execCommand('copy')` 回退（隐藏 textarea + select + execCommand），
  仍失败 → 失败 toast
- 测试：dashboard 契约测试（源断言，照 `test_cloud_alias_attrs.py` 风格）：
  `writeText(...).then(` 存在、`execCommand('copy')` 回退存在、无「先 toast
  后 await」的旧形态

## 提交切分

每 issue 一个提交（8 个），消息 `fix(alias|responses|dashboard): … (#NN)`；
spec + STATUS.md 随首提交入库。

## 验收

1. Mac 全套 pytest（基线 2935 passed / 15 playwright 环境错误）
2. vince-pc worktree 全套（基线对齐，6 个 Ubuntu 基线失败集不变）
3. §4.3 清理：worktree remove + porcelain 前后对比 + 无孤儿进程/端口
