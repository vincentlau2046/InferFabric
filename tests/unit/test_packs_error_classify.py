"""统一错误分类目录（issue #49）— reason 枚举 → kind + 人话 + 建议动作。

契约：
- hint_for(code) → {"kind", "message", "action"}；kind ∈ KINDS；
  未知 code 回退 kind=unknown（message 原样透传 code）。
- parse_error_prefix("download_error:URLError") → "download_error"；
  无前缀原文 → None。
- 目录覆盖三面 reason 源：admission.evaluate_pack / installer.fail 前缀 /
  PackRuntimeError.reason_code —— 三面的每个现实 code 在目录中都有条目
  （防新增 reason 漏登记：各源测试逐一枚举）。
"""

import pytest

from inferfabric.packs import error_classify as ec
from inferfabric.packs.admission import evaluate_pack  # noqa: F401  (源存在)
from inferfabric.packs.managed_runtime import PackRuntimeError
from inferfabric.packs.store import PackRecord


def test_hint_for_shape_and_kinds():
    for code in ("resource_busy", "download_error", "unknown_pack_id"):
        h = ec.hint_for(code)
        assert h["kind"] in ec.KINDS
        assert h["message"] and h["action"]


def test_hint_for_unknown_code_falls_back():
    h = ec.hint_for("some_future_code")
    assert h["kind"] == "unknown"
    assert "some_future_code" in h["message"]
    assert h["action"]           # 通用排查建议仍在


def test_parse_error_prefix():
    assert ec.parse_error_prefix("download_error:URLError") == "download_error"
    assert ec.parse_error_prefix("internal_error:KeyError") == "internal_error"
    assert ec.parse_error_prefix("no prefix here") is None
    assert ec.parse_error_prefix("") is None


def test_classify_stage_error_download():
    h = ec.classify_error("download_error:URLError")
    assert h["kind"] == "download"
    assert h["message"] and h["action"]


def test_classify_stage_error_raw_text_falls_back_with_original():
    h = ec.classify_error("parameter_count_b 40 > max 8")
    assert h["kind"] == "unknown"
    assert "parameter_count_b 40 > max 8" in h["message"]   # 原文不丢


def test_classify_none_and_empty():
    assert ec.classify_error(None) is None
    assert ec.classify_error("") is None


@pytest.mark.parametrize("code", [
    "resource_busy", "resource_unknown", "resource_insufficient",
    "platform_mismatch", "platform_unverified", "runtime_unavailable",
    "isolation_unverified", "isolation_degraded_not_allowed",
])
def test_admission_reason_codes_all_covered(code):
    """admission.evaluate_pack 的每个现实 reason_code 都有非 unknown 条目。"""
    assert ec.hint_for(code)["kind"] == "admission"


@pytest.mark.parametrize("code", [
    "download_error", "artifact_sha256_mismatch", "builtin_hook_error",
    "runtime_error", "config_error", "healthcheck_error", "reload_error",
    "internal_error",
])
def test_installer_stage_error_prefixes_all_covered(code):
    """installer.fail() 的每个现实错误码前缀都有非 unknown 条目。"""
    assert ec.hint_for(code)["kind"] in ("download", "runtime")


def test_managed_runtime_error_codes_all_covered():
    """PackRuntimeError 现实 reason_code 全覆盖（构造实例取证）。"""
    seen = set()
    for args in (("resource_busy", "x"), ("ownership_conflict", "x"),
                 ("runtime_identity_mismatch", "x"),
                 ("runtime_unavailable", "x"), ("resource_unknown", "x")):
        e = PackRuntimeError(*args)
        seen.add(e.reason_code)
        assert ec.hint_for(e.reason_code)["kind"] in ("admission", "conflict")
    assert seen == {"resource_busy", "ownership_conflict",
                    "runtime_identity_mismatch", "runtime_unavailable",
                    "resource_unknown"}


def test_route_codes_covered():
    assert ec.hint_for("unknown_pack_id")["kind"] == "not_found"


def test_key_hint_copy_pins():
    """issue 验收的人话钉住：GPU busy 的 message/action 明确下一步。"""
    h = ec.hint_for("resource_busy")
    assert "GPU 忙" in h["message"] and "idle" in h["message"]
    assert "idle" in h["action"]
    d = ec.hint_for("download_error")
    assert "下载失败" in d["message"]
