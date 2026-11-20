"""outbound_credentials — 出站云账号选择（F6）占位接线（M3 Task 5）。

2026-11-19 用户裁决：本期只落设计文档 + 可跑但不改变任何行为的接口 stub。
分层口径（详见 docs/superpowers/specs/2026-11-19-outbound-account-pool-design.md）：
- F7（security/key_store）：入站客户端鉴权 — 本模块与之互不感知；
- 模型池（model_pool）：选模型/物理目标；
- F6（本模块）：出站云账号选择 — 未来多账号/配额路由/会话亲和的单一接入点。

当前实现：恒等透传。不读取会话/配额，不改变出站 key、请求路径、重试
次数或空 key 语义；同一次转发的重试沿用首次选定值（forwarder 只调一次）。
responses_bridge 原生探测/Chat 兜底、discovery、管理探测的接线本期不做。
"""
from __future__ import annotations

__all__ = ["select_outbound_api_key"]


def select_outbound_api_key(*, provider_name: str, configured_api_key: str) -> str:
    """返回该 provider 本次转发应使用的出站 key。

    默认实现原样返回 configured_api_key（含空字符串 — 空 key 的既有
    header 语义不变）。future: 多账号配额路由/会话亲和在此单一接入点
    演进，需另立评审，本期不预先冻结。
    """
    return configured_api_key
