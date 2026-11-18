"""
unit/proxy/test_system_info.py — _system_info GPU 字段投影测试

DeviceTelemetry T5 重构后：_system_info 不再自行跑 nvidia-smi 子进程，
GPU 读数来自调用方传入的共享 DeviceSnapshot（TelemetryHub 15s 观测缓存，
一个探针喂 device/system/status 三面）。本测试断言快照投影契约：

- snapshot 各字段投影到 info["gpu_*"]（含温度）；
- 不可用读数 = None（JSON null，前端渲染 —），绝不伪造 0；
- snapshot=None（skeleton 路径）→ gpu_* 全 None、无 device 键。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest

from inferfabric.platform.telemetry import DeviceCapabilities, DeviceSnapshot


def _make_handler():
    """ProxyHandler with minimal mocked state."""
    import inferfabric.proxy.handler as handler_module
    ProxyHandler = handler_module.ProxyHandler
    h = ProxyHandler.__new__(ProxyHandler)
    return h, handler_module


def _snapshot(**overrides):
    """Full-reading NVIDIA-style snapshot; per-field None via overrides."""
    fields = dict(platform="linux", accelerator="nvidia", memory_kind="vram",
                  memory_used_mb=1024, memory_total_mb=32607,
                  memory_budget_mb=31628, utilization_pct=100.0,
                  power_w=496.4, temperature_c=55.0, clock_mhz=2857,
                  pressure_level=None, timestamp=1000.0,
                  sources={"temperature_c": "nvidia-smi"})
    fields.update(overrides)
    return DeviceSnapshot(**fields)


class TestSystemInfoGpuTemp:
    def test_gpu_temp_c_projected_from_snapshot(self):
        """snapshot.temperature_c → info["gpu_temp_c"]（round 由后端负责）。"""
        h, _ = _make_handler()
        snap = _snapshot(temperature_c=55.0, utilization_pct=100.0,
                         clock_mhz=2857, power_w=496.4)

        info = h._system_info(snap, DeviceCapabilities(
            memory=True, utilization=True, power=True, temperature=True, clock=True))

        assert info["gpu_temp_c"] == 55.0, f"温度投影错误: {info['gpu_temp_c']}"
        # 既有字段不受影响
        assert info["gpu_util_pct"] == 100.0
        assert info["gpu_clock_mhz"] == 2857
        assert info["gpu_power_w"] == 496.4

    def test_gpu_temp_c_rounds_decimal_passthrough(self):
        """快照已按源精度取整；投影原样透传（不再二次加工）。"""
        h, _ = _make_handler()
        snap = _snapshot(temperature_c=67.5)

        info = h._system_info(snap, DeviceCapabilities(temperature=True))
        assert info["gpu_temp_c"] == 67.5

    def test_gpu_temp_c_none_when_unavailable(self):
        """读数不可用 = None（JSON null，前端渲染 —），绝不伪造 0。"""
        h, _ = _make_handler()
        snap = _snapshot(temperature_c=None, utilization_pct=None,
                         clock_mhz=None, power_w=None, sources={})

        info = h._system_info(snap, DeviceCapabilities(memory=True))
        assert info["gpu_temp_c"] is None
        assert info["gpu_util_pct"] is None
        assert info["gpu_clock_mhz"] is None
        assert info["gpu_power_w"] is None

    def test_device_envelope_embedded(self):
        """_system_info 同时携带 device 包络（snapshot + capabilities）。"""
        h, _ = _make_handler()
        snap = _snapshot()
        caps = DeviceCapabilities(memory=True, temperature=True)

        info = h._system_info(snap, caps)
        assert info["device"]["snapshot"]["temperature_c"] == 55.0
        assert info["device"]["capabilities"]["temperature"] is True

    def test_skeleton_without_snapshot(self):
        """snapshot=None（skeleton 路径）→ gpu_* 全 None、无 device 键。"""
        h, _ = _make_handler()

        info = h._system_info()
        assert info["gpu_temp_c"] is None
        assert info["gpu_util_pct"] is None
        assert "device" not in info

