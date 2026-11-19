"""PackStore 状态持久化与磁盘对账 — PK1（Task 6）。

契约（计划 Interfaces）：
- PackRecord(pack_id, pack_version, status, stage, error, sandbox_degraded,
  installed_at)。
- PackStore(path)：set/get/all/remove；原子写 JSON。
- reconcile(store, root)：状态-磁盘对账 — installed 但目录缺失 → 修正
  failed(stale)；目录存在但无记录 → failed(unknown_origin)；返回修正列表。
"""

import pytest

from inferfabric.packs.store import PackRecord, PackStore, reconcile


def rec(pid="llm.a", status="installed", version="1.0.0", **kw):
    base = dict(stage="installed", error=None, sandbox_degraded=False,
                installed_at=1700000000)
    base.update(kw)
    return PackRecord(pack_id=pid, pack_version=version, status=status, **base)


def test_store_roundtrip(tmp_path):
    st = PackStore(tmp_path / "state.json")
    st.set(rec(status="failed", stage="verifying",
               error="artifact_sha256_mismatch:model"))
    assert PackStore(tmp_path / "state.json").get("llm.a").status == "failed"
    assert PackStore(tmp_path / "state.json").get("llm.a").stage == "verifying"


def test_store_missing_file_returns_none_and_empty(tmp_path):
    st = PackStore(tmp_path / "state.json")
    assert st.get("llm.a") is None
    assert st.all() == []


def test_store_remove(tmp_path):
    st = PackStore(tmp_path / "state.json")
    st.set(rec())
    st.remove("llm.a")
    assert st.get("llm.a") is None
    assert PackStore(tmp_path / "state.json").get("llm.a") is None


def test_store_corrupt_file_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{broken")
    st = PackStore(p)
    assert st.all() == []
    st.set(rec())   # 可继续写入
    assert st.get("llm.a") is not None


def test_store_atomic_no_part_leftover(tmp_path):
    st = PackStore(tmp_path / "state.json")
    st.set(rec())
    assert list(tmp_path.glob("*.part")) == []


def test_reconcile_installed_but_dir_missing(tmp_path):
    st = PackStore(tmp_path / "state.json")
    st.set(rec("llm.a"))
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    fixed = reconcile(st, packs_root)
    assert fixed == ["llm.a"]
    assert st.get("llm.a").status == "failed"
    assert "stale" in st.get("llm.a").error


def test_reconcile_dir_without_record(tmp_path):
    st = PackStore(tmp_path / "state.json")
    (tmp_path / "packs" / "llm.b" / "1.0.0").mkdir(parents=True)
    fixed = reconcile(st, tmp_path / "packs")
    assert fixed == ["llm.b"]
    assert st.get("llm.b").status == "failed"
    assert "unknown_origin" in st.get("llm.b").error


def test_reconcile_consistent_state_no_changes(tmp_path):
    st = PackStore(tmp_path / "state.json")
    st.set(rec("llm.a"))
    (tmp_path / "packs" / "llm.a" / "1.0.0").mkdir(parents=True)
    st.set(rec("llm.b", status="failed", stage="verifying"))
    assert reconcile(st, tmp_path / "packs") == []
    assert st.get("llm.a").status == "installed"
    assert st.get("llm.b").status == "failed"


def test_reconcile_staging_dir_ignored(tmp_path):
    # _staging/ 是事务工作区，不是包目录；有残留不判 unknown_origin
    st = PackStore(tmp_path / "state.json")
    (tmp_path / "packs" / "_staging" / "llm.x").mkdir(parents=True)
    assert reconcile(st, tmp_path / "packs") == []


def test_reconcile_unknown_origin_error_actionable(tmp_path):
    # 终审 deferred #3：unknown_origin 保留供诊断（spec §7.3），错误信息可行动
    root = tmp_path / "packs"
    (root / "llm.mystery" / "1.0.0").mkdir(parents=True)
    store = PackStore(tmp_path / "state.json")
    fixed = reconcile(store, root)
    assert fixed == ["llm.mystery"]
    rec = store.get("llm.mystery")
    assert rec is not None and rec.status == "failed"
    assert "uninstall" in rec.error   # 指引清理路径
