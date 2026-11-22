"""insert_request_log entry normalization (mac prod upsource ①).

main 的 INSERT 绑定 :ttft_ms，但条目预处理不补 ttft_ms 缺省——dict 直传
路径（旧 fixture / 外部构造的 entry）缺键时 sqlite3.ProgrammingError。
prod（MacFabric 7.10.x）用 ttft_ms=e.get("ttft_ms") 归一，本测试钉死该契约。
"""

import pytest

from inferfabric.db import IFFDB


def _minimal_entry(req_id: str, **overrides) -> dict:
    """调用方最小 entry：只带 INSERT 必需身份列，不带 ttft_ms。"""
    entry = {
        "req_id": req_id,
        "key_name": "k1",
        "model": "m1",
        "status": 200,
        "tokens_in": 3,
        "tokens_out": 5,
        "duration_ms": 12.0,
        "route": "local",
        "error": "",
        "timestamp": 1700000000,
        "ts": 1700000000.0,
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def db(tmp_path):
    return IFFDB(data_dir=tmp_path)


def test_insert_entry_without_ttft_ms_does_not_raise(db):
    """缺 ttft_ms 键的 entry 不崩，ttft 落 NULL（prod 归一契约）。"""
    db.insert_request_log([_minimal_entry("r1")])


def test_insert_entry_without_ttft_ms_row_has_null_ttft(db):
    db.insert_request_log([_minimal_entry("r2")])
    rows = db.query_request_log(since=0, limit=10)
    row = next(r for r in rows if r.get("req_id") == "r2")
    assert row.get("ttft_ms") is None


def test_insert_entry_with_ttft_ms_preserved(db):
    db.insert_request_log([_minimal_entry("r3", ttft_ms=123.5)])
    rows = db.query_request_log(since=0, limit=10)
    row = next(r for r in rows if r.get("req_id") == "r3")
    assert row.get("ttft_ms") == pytest.approx(123.5)


def test_insert_entry_metadata_dict_serialized(db):
    """metadata dict → JSON 文本落列（schema v002 本有该列，prod 契约）。"""
    db.insert_request_log([_minimal_entry(
        "r4", metadata={"pool_summary": {"pool": "p1"}})])
    rows = db.query_request_log(since=0, limit=10)
    row = next(r for r in rows if r.get("req_id") == "r4")
    assert row.get("metadata") == '{"pool_summary": {"pool": "p1"}}'


def test_insert_entry_route_none_falls_back_local(db):
    """route 显式 None 兜底 'local'，防 NOT NULL 静默丢行（prod 契约）。"""
    db.insert_request_log([_minimal_entry("r5", route=None)])
    rows = db.query_request_log(since=0, limit=10)
    row = next(r for r in rows if r.get("req_id") == "r5")
    assert row.get("route") == "local"
