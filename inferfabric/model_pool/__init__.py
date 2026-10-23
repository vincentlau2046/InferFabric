"""inferfabric.model_pool — 逻辑模型池（iff/fast 等稳定逻辑名 → 优先级候选映射）。

设计文档：docs/superpowers/specs/2026-10-23-logical-model-pools-design.md
- config.py    配置数据模型 + 静态校验（纯函数）
- evidence.py  协议/功能证据存储（五态 + 依赖指纹）
- registry.py  不可变快照 / revision / CAS 热更新
- selector.py  协议过滤 + 优先级选择 + dry-run 解释
- dispatch.py  OpenAI/Anthropic 入口接线
- pool_forward.py  P2 受控故障回退
"""

from inferfabric.model_pool.config import (
    CandidateConfig,
    PoolConfig,
    PoolsFile,
    load_pools_dict,
    load_pools_file,
)

__all__ = [
    "CandidateConfig",
    "PoolConfig",
    "PoolsFile",
    "load_pools_dict",
    "load_pools_file",
]
