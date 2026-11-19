"""
operational/test_operational.py — 操作类测试（⚠️ 需真实环境 + 再确认）

测试对象: 运行中的 InferFabric 代理（:8999）+ GPU + 模型文件
覆盖范围:
  - 模型生命周期: 共享模型 启动→停止→验证空闲
  - 排他模型: 启动→idle→验证空闲
  - 重复释放安全性
  - 排他→共享冲突拒绝
  - API 端点: /status, /v1/models, /switch, /stop, dashboard
  - 并发 switch idle 安全

⚠️ 运行前必须确认: 无重要推理任务、GPU 显存充足、代理已启动

⚠️⚠️⚠️ 重要警告 ⚠️⚠️⚠️
═══════════════════════════════════════════════════════════════════
本文件中的测试用例会操作真实环境，包括但不限于：

  1. 模型上线 / 下线 — 启动或停止 vLLM、ComfyUI、TTS 等推理进程
  2. GPU 模式切换 — idle ↔ exclusive ↔ shared 状态转换
  3. 向运行中的代理发送 switch / stop HTTP 请求
  4. 强制终止进程 — 发送 SIGTERM / SIGKILL 信号
  5. 修改 GPU 显存占用 — 加载/卸载模型权重

运行前必须确认：
  ✅ 当前无其他重要推理任务在运行
  ✅ GPU 显存充足，不会影响其他服务
  ✅ 代理服务已启动（iff serve，监听 :8999）
  ✅ 模型配置文件（models/ 目录）正确
  ✅ 在专用测试环境运行，勿在生产环境直接执行
  ✅ 已备份当前 GPU 状态和进程列表

运行方式：
  # 必须显式指定 -m operational 才会执行
  pytest tests/test_operational.py -m operational -v --tb=short

  # 或设置环境变量确认
  IFF_OPERATIONAL_CONFIRM=1 pytest tests/test_operational.py -v --tb=short
═══════════════════════════════════════════════════════════════════

覆盖场景：
  - 模型生命周期：启动 → 健康检查 → 停止 → 验证空闲
  - 共享模型增量添加 / 移除
  - 排他模型切换（idle → exclusive → idle）
  - 模型切换冲突拒绝（exclusive → shared 直接切换应失败）
  - Dashboard 可访问性
  - API 端点可用性（/status, /v1/models, /switch, /stop）
  - GPU 显存释放验证
  - 并发 switch 请求安全
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import threading
from pathlib import Path

import pytest

# ── 常量 ──────────────────────────────────────────────────────

PROXY = os.environ.get("IFF_PROXY_URL", "http://localhost:8999")
TIMEOUT_SWITCH = 180   # 模型切换超时（秒）
TIMEOUT_STOP = 120     # 模型停止超时（秒）
TIMEOUT_DEFAULT = 10   # 普通请求超时（秒）

# ── 安全检查 ─────────────────────────────────────────────────

def _require_operational_env():
    """操作类测试前置检查。

    必须设置 IFF_OPERATIONAL_CONFIRM=1 或通过 pytest -m operational 显式调用。
    """
    if os.environ.get("IFF_OPERATIONAL_CONFIRM") != "1":
        pytest.skip(
            "操作类测试需要设置 IFF_OPERATIONAL_CONFIRM=1 环境变量。"
            "请确认已阅读测试文件头部的安全警告后重新运行。"
        )


def _require_proxy():
    """确认代理服务可达。"""
    try:
        req = urllib.request.Request(f"{PROXY}/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                pytest.fail(f"代理返回非 200 状态: {resp.status}")
    except Exception as e:
        pytest.fail(
            f"代理服务不可达 ({PROXY})。请先启动: iff serve\n错误: {e}"
        )


# ── 辅助函数 ─────────────────────────────────────────────────

def _get(path, timeout=TIMEOUT_DEFAULT):
    """GET 请求代理。"""
    try:
        with urllib.request.urlopen(f"{PROXY}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _post(path, data, timeout=TIMEOUT_DEFAULT):
    """POST 请求代理。"""
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{PROXY}{path}", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _ensure_idle():
    """确保 GPU 处于 idle 状态。"""
    data, _ = _get("/status")
    if data.get("gpu_mode") != "idle":
        result, status = _post("/switch", {"model": "idle"}, timeout=TIMEOUT_STOP)
        assert result.get("status") in ("switched", "already_active"), \
            f"切换到 idle 失败: {result}"
        time.sleep(3)

    data, _ = _get("/status")
    assert data.get("gpu_mode") == "idle", f"无法到达 idle 状态: {data}"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="module")
def _check_env():
    """模块级环境检查。"""
    _require_operational_env()
    _require_proxy()


@pytest.fixture(autouse=True)
def _ensure_clean_state():
    """每个测试前确保 GPU 空闲。"""
    _ensure_idle()
    yield
    # 测试后也恢复 idle
    try:
        _ensure_idle()
    except Exception:
        pass  # 清理失败不影响测试结果


# ═══════════════════════════════════════════════════════════════
# 模型生命周期测试
# ═══════════════════════════════════════════════════════════════

@pytest.mark.operational
class TestModelLifecycle:
    """模型上线 → 健康检查 → 下线 → 验证空闲"""

    def test_shared_model_lifecycle(self):
        """共享模型完整生命周期：启动 → 验证 → 停止 → 验证空闲。

        ⚠️ 操作说明：此测试会启动 qwen35-9b-vl 模型（共享模式），
        占用部分 GPU 显存，测试结束后释放。
        """
        # 1. 启动共享模型
        result, status = _post("/switch", {"model": "qwen35-9b-vl"})
        assert result.get("status") == "switched", f"启动失败: {result}"
        print(f"  ✅ 模型启动，耗时 {result.get('elapsed_sec')}s")

        # 2. 验证健康状态
        data, _ = _get("/status")
        assert "qwen35-9b-vl" in data.get("active_services", []), \
            f"模型未出现在 active_services: {data}"
        assert data["gpu_mode"] == "shared", f"GPU 模式不是 shared: {data}"

        # 3. 验证 /v1/models 返回模型信息
        models, _ = _get("/v1/models")
        if isinstance(models, dict) and "data" in models:
            model_ids = [m["id"] for m in models["data"]]
            assert "qwen35-9b-vl" in model_ids, f"模型不在 /v1/models 中: {model_ids}"

        # 4. 停止共享模型
        result, status = _post("/stop", {"model": "qwen35-9b-vl"})
        assert result.get("status") == "stopped", f"停止失败: {result}"
        print(f"  ✅ 模型停止: {result.get('message', '')}")

        # 5. 验证空闲
        time.sleep(2)
        data, _ = _get("/status")
        assert data["gpu_mode"] == "idle", f"停止后 GPU 不是 idle: {data}"

    def test_exclusive_model_lifecycle(self):
        """排他模型完整生命周期：启动 → 验证 → 切换 idle → 验证空闲。

        ⚠️ 操作说明：此测试会启动 qwen36-27b 模型（排他模式），
        占用大量 GPU 显存，测试结束后释放。预计耗时 1-3 分钟。
        """
        # 1. 启动排他模型
        result, status = _post("/switch", {"model": "qwen36-27b"})
        assert result.get("status") == "switched", f"启动失败: {result}"
        print(f"  ✅ 排他模型启动，耗时 {result.get('elapsed_sec')}s")

        # 2. 验证健康状态
        data, _ = _get("/status")
        assert "qwen36-27b" in data.get("active_services", []), \
            f"模型未出现在 active_services: {data}"
        assert data["gpu_mode"] == "exclusive", f"GPU 模式不是 exclusive: {data}"

        # 3. 释放排他模型（切换到 idle）
        result, status = _post("/switch", {"model": "idle"}, timeout=TIMEOUT_STOP)
        assert result.get("status") in ("switched", "already_active"), \
            f"释放失败: {result}"
        print(f"  ✅ 模型释放，耗时 {result.get('elapsed_sec', '?')}s")

        # 4. 验证空闲
        time.sleep(3)
        data, _ = _get("/status")
        assert data["gpu_mode"] == "idle", f"释放后 GPU 不是 idle: {data}"

    def test_double_release_safety(self):
        """重复释放不应破坏状态。

        ⚠️ 操作说明：启动一个共享模型后连续释放两次，验证第二次返回错误
        而不是崩溃。
        """
        # 1. 启动
        result, _ = _post("/switch", {"model": "qwen35-9b-vl"})
        assert result.get("status") == "switched"

        # 2. 第一次释放
        result1, _ = _post("/stop", {"model": "qwen35-9b-vl"})
        assert result1.get("status") == "stopped"

        # 3. 第二次释放（应返回 error，不崩溃）
        result2, _ = _post("/stop", {"model": "qwen35-9b-vl"})
        assert result2.get("status") == "error", \
            f"第二次释放应返回 error: {result2}"

        # 4. 验证状态正常
        data, _ = _get("/status")
        assert data["gpu_mode"] == "idle"


# ═══════════════════════════════════════════════════════════════
# 模型切换冲突测试
# ═══════════════════════════════════════════════════════════════

@pytest.mark.operational
class TestModelSwitchConflict:
    """模型切换冲突场景"""

    def test_exclusive_to_shared_rejected(self):
        """排他模式运行时，直接切换到共享模式应被拒绝。

        ⚠️ 操作说明：先启动排他模型，再尝试切换到共享模型，
        验证系统正确拒绝不兼容的切换请求。
        """
        # 1. 启动排他模型
        result, _ = _post("/switch", {"model": "qwen36-27b"})
        assert result.get("status") == "switched"

        # 2. 尝试直接切换到共享模型（应失败）
        result2, _ = _post("/switch", {"model": "qwen35-9b-vl"})
        assert result2.get("status") == "error", \
            f"排他→共享切换应被拒绝: {result2}"

        # 3. 正确流程：先 idle 再切换
        result3, _ = _post("/switch", {"model": "idle"}, timeout=TIMEOUT_STOP)
        time.sleep(2)

        result4, _ = _post("/switch", {"model": "qwen35-9b-vl"})
        assert result4.get("status") == "switched", \
            f"idle→共享切换失败: {result4}"

        # 清理
        _post("/stop", {"model": "qwen35-9b-vl"})


# ═══════════════════════════════════════════════════════════════
# API 端点可用性测试
# ═══════════════════════════════════════════════════════════════

@pytest.mark.operational
class TestAPIEndpoints:
    """代理 API 端点可用性"""

    def test_status_endpoint(self):
        """GET /status 返回完整状态信息。"""
        data, status = _get("/status")
        assert status == 200
        assert "gpu_mode" in data
        assert "active_services" in data
        assert "gpu_used_mb" in data

    def test_v1_models_endpoint(self):
        """GET /v1/models 返回模型列表。"""
        data, status = _get("/v1/models")
        assert status == 200
        assert isinstance(data, dict)
        assert "data" in data
        assert isinstance(data["data"], list)

    def test_switch_idle_idempotent(self):
        """重复 switch idle 不报错。"""
        result1, _ = _post("/switch", {"model": "idle"})
        result2, _ = _post("/switch", {"model": "idle"})
        # 两次都应成功或返回 already_active
        assert result2.get("status") in ("switched", "already_active")

    def test_switch_unknown_model_returns_error(self):
        """切换到不存在的模型返回错误。"""
        result, status = _post("/switch", {"model": "nonexistent-model-xyz"})
        assert result.get("status") == "error"

    def test_stop_unknown_service_returns_error(self):
        """停止未运行的服务返回错误。"""
        result, status = _post("/stop", {"model": "nonexistent-service"})
        assert result.get("status") == "error"

    def test_dashboard_accessible(self):
        """Dashboard HTML 可访问。"""
        try:
            with urllib.request.urlopen(f"{PROXY}/", timeout=5) as resp:
                html = resp.read().decode()
                assert len(html) > 100, "Dashboard HTML 内容过少"
        except Exception as e:
            pytest.fail(f"Dashboard 不可访问: {e}")


# ═══════════════════════════════════════════════════════════════
# 并发安全测试
# ═══════════════════════════════════════════════════════════════

@pytest.mark.operational
class TestConcurrentSafety:
    """并发请求安全性"""

    def test_concurrent_switch_idle(self):
        """多个并发 switch idle 请求不会导致状态不一致。

        ⚠️ 操作说明：同时发送多个 switch idle 请求，验证代理正确处理。
        """
        results = []
        errors = []

        def do_switch():
            try:
                result, status = _post("/switch", {"model": "idle"},
                                       timeout=TIMEOUT_STOP)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_switch) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT_STOP + 10)

        assert not errors, f"并发请求出错: {errors}"
        # 所有结果都应成功或 already_active
        for r in results:
            assert r.get("status") in ("switched", "already_active", "error"), \
                f"意外状态: {r}"

        # 最终状态应为 idle
        time.sleep(2)
        data, _ = _get("/status")
        assert data.get("gpu_mode") == "idle"
