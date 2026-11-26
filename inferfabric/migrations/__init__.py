"""Migration registry. Import all migration modules to register them."""
from inferfabric.db import IFFDB

# v001: state table
from inferfabric.migrations import v001_init_state
IFFDB.register_migration(1, "state", "init_state")

# v002: request_log table
from inferfabric.migrations import v002_init_request_log
IFFDB.register_migration(2, "request_log", "init_request_log")

# v003: manual_stops table (v5.2)
from inferfabric.migrations import v003_manual_stops
IFFDB.register_migration(3, "state", "manual_stops")

# v004: sleep_state table (v5.2)
from inferfabric.migrations import v004_sleep_state
IFFDB.register_migration(4, "state", "sleep_state")

# v005: reconcile manual_stops KV → table (v5.x)
from inferfabric.migrations import v005_reconcile_manual_stops
IFFDB.register_migration(5, "state", "reconcile_manual_stops")

# v006: request_log.tokens_in_cached (v6.1 缓存命中率统计)
from inferfabric.migrations import v006_add_tokens_in_cached
IFFDB.register_migration(6, "request_log", "add_tokens_in_cached")

# v007: gpu_power_samples (v6.2 功耗/电费时间序列)
from inferfabric.migrations import v007_gpu_power_series
IFFDB.register_migration(7, "request_log", "gpu_power_series")

# v008: request_log.agent/ua (v6.5 客户端 Agent 识别)
from inferfabric.migrations import v008_add_agent_ua
IFFDB.register_migration(8, "request_log", "add_agent_ua")

# v009: 历史行 agent='' 回填 claude-code（v6.6）
from inferfabric.migrations import v009_backfill_agent
IFFDB.register_migration(9, "request_log", "backfill_agent")

# v010: 诚实化历史数据 + 预留 x_app 列（v6.6 架构修正）
from inferfabric.migrations import v010_honestize_and_x_app
IFFDB.register_migration(10, "request_log", "honestize_and_x_app")

# v011: device_power_samples（DeviceTelemetry T4 平台中立功耗序列）
from inferfabric.migrations import v011_device_power_samples
IFFDB.register_migration(11, "request_log", "device_power_samples")

# v012: request_log.cost/metadata 补迁移（v002 事后补列，老库无 ALTER → flush 全量失败）
from inferfabric.migrations import v012_add_cost_metadata
IFFDB.register_migration(12, "request_log", "add_cost_metadata")
