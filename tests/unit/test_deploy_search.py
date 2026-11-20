"""deploy_wizard.search_models — ModelScope 搜索代理（部署向导计划 Task 3）。

契约：transport 注入 `(url, payload) -> dict`；归一化输出
{"models": [{id,name,downloads,capability_guess,pipeline}], "total"}；
错误归一 {"error", "code": "search_failed"|"invalid_request"}。
线上校准（2026-11-21）：PUT /api/v1/dolphin/models {"Name","PageSize","PageNumber"}
→ Data.Model.Models[]{Path,Name,Downloads,Pipelines} + TotalCount。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest

from inferfabric.proxy import deploy_wizard as dw


def _fake_transport(models, total=42):
    def transport(url, payload):
        return {"Code": 200, "Data": {"Model": {"Models": models,
                                                "TotalCount": total}}}
    return transport


_OK_MODELS = [
    {"Path": "Qwen", "Name": "Qwen2.5-7B-Instruct-GGUF",
     "Downloads": 123, "Pipelines": ["text-generation"]},
    {"Path": "BAAI", "Name": "bge-m3-embedding-GGUF", "Downloads": 5},
]


class TestNormalization:
    def test_normalizes_entries(self):
        r = dw.search_models(_fake_transport(_OK_MODELS), "qwen gguf")
        assert r["total"] == 42
        assert len(r["models"]) == 2
        m = r["models"][0]
        assert m["id"] == "Qwen/Qwen2.5-7B-Instruct-GGUF"
        assert m["name"] == "Qwen2.5-7B-Instruct-GGUF"
        assert m["downloads"] == 123
        assert m["pipeline"] == "text-generation"
        assert set(m) == {"id", "name", "downloads", "capability_guess", "pipeline"}

    def test_capability_guess_embedding(self):
        r = dw.search_models(_fake_transport(_OK_MODELS), "bge")
        assert r["models"][1]["capability_guess"] == "embedding"
        assert r["models"][0]["capability_guess"] == "llm"

    def test_skips_traversal_and_malformed_entries(self):
        # Review Focus 4：模型名含路径/特殊字符 — id 不合 pull_model 白名单
        # 形态的条目跳过，不透传给前端
        models = _OK_MODELS + [
            {"Path": "..", "Name": "evil", "Downloads": 1},
            {"Name": "no-path", "Downloads": 1},
            {"Path": "a/b", "Name": "deep", "Downloads": 1},
        ]
        r = dw.search_models(_fake_transport(models), "x")
        assert [m["id"] for m in r["models"]] == [
            "Qwen/Qwen2.5-7B-Instruct-GGUF", "BAAI/bge-m3-embedding-GGUF"]

    def test_payload_matches_calibrated_api(self):
        # 线上校准钉住：PUT dolphin 端点 + Name/PageSize/PageNumber 三键
        seen = {}
        def transport(url, payload):
            seen["url"], seen["payload"] = url, payload
            return {"Code": 200, "Data": {"Model": {"Models": [], "TotalCount": 0}}}
        dw.search_models(transport, "qwen", page=2)
        assert seen["url"] == dw.MODELSCOPE_SEARCH_URL
        assert seen["payload"] == {"Name": "qwen", "PageSize": 20, "PageNumber": 2}


class TestErrorNormalization:
    def test_transport_raises_is_search_failed(self):
        def transport(url, payload):
            raise TimeoutError("boom")
        r = dw.search_models(transport, "qwen")
        assert r["code"] == "search_failed"
        assert "error" in r
        assert "models" not in r

    def test_malformed_response_is_search_failed(self):
        for garbage in ({"Data": None}, {"Data": "x"}, {}, [], None,
                        {"Data": {"Model": {"Models": "notalist"}}}):
            r = dw.search_models(lambda u, p: garbage, "qwen")
            assert r["code"] == "search_failed", garbage

    def test_empty_q_is_invalid_request(self):
        r = dw.search_models(_fake_transport([]), "  ")
        assert r["code"] == "invalid_request"

    def test_dotdot_q_is_invalid_request(self):
        r = dw.search_models(_fake_transport([]), "../etc")
        assert r["code"] == "invalid_request"

    def test_page_out_of_range_is_invalid_request(self):
        for page in (0, -1, 101):
            r = dw.search_models(_fake_transport([]), "qwen", page=page)
            assert r["code"] == "invalid_request", page


class TestRouteWiring:
    def test_route_registered_with_admin_gate(self):
        from inferfabric.proxy import handler
        assert "/admin/deploy/search" in handler._GET_ROUTES
