# model-pools（逻辑模型池）

设计文档：`docs/superpowers/specs/2026-10-23-logical-model-pools-design.md`（权威）
实施计划：`docs/superpowers/plans/2026-10-23-logical-model-pools.md`
评估报告：`docs/reviews/2026-10-23-model-pools-evaluation.md`

## 功能一句话

稳定逻辑名（`iff/fast` 等）映射到优先级候选池；同一 :8999 端口按入站协议
（OpenAI / Anthropic）过滤兼容候选，P2 预算内故障回退；热更新（revision CAS）；
不自动启动/抢占本地模型；池请求旁路响应缓存；完整逻辑名鉴权。

## 实现组成

| 模块 | 职责 |
|---|---|
| `inferfabric/model_pool/config.py` | 池/候选数据模型 + 静态校验 |
| `inferfabric/model_pool/evidence.py` | 协议/功能证据存储 + 端点指纹 |
| `inferfabric/model_pool/registry.py` | 不可变快照 / revision CAS 热更新 / flip_pool_enabled |
| `inferfabric/model_pool/selector.py` | 协议/能力过滤 + 优先级选择 + 排除原因 |
| `inferfabric/model_pool/dispatch.py` | 双协议入口调度（鉴权→选择→限流→传输→日志） |
| `inferfabric/model_pool/pool_forward.py` | P2 回退循环 + CooldownStore（spec §6.2–§6.4） |
| `inferfabric/model_pool/admin.py` | 管理 API：GET/PUT(CAS)/validate/status/dry-run/toggle |
| `inferfabric/forwarder.py`（增量） | `suppress_client_errors` 传输边界（PR-14 加参数，默认行为不变） |
| `inferfabric/dashboard/`（新增 TAB） | 模型池只读面板 + enabled 开关 |

## 用户确认过的业务边界（2026-10-23）

- 跨本地/云回退：显式开关（`allowed_locations` + `allow_cross_location`），
  保守默认关闭、逐池显式开启。
- 本地候选 **只使用已启动端口**（ready_only），**绝不自动启动本地模型**
  （池调度永不调用 `ensure_service`），防止异常调度引发系统故障。
- VL 模型限制不因池化解除（证据需显式声明，未声明 vision 不投递）。

## 测试

`tests/unit/model_pool/`（138 例）+ `tests/unit/dashboard/test_model_pools_panel.py`
（Node VM 行为测试 6 例）+ forwarder 既有套件回归。
