"""model_pool.evidence — 协议/功能证据存储（五态 + 依赖指纹 + JSON 持久化）。"""

import json

from inferfabric.model_pool.evidence import (
    Evidence,
    EvidenceState,
    endpoint_fingerprint,
)


def _probed(fp="fp1", protocol="openai"):
    return Evidence(
        state=EvidenceState.PROBED,
        protocol=protocol,
        features=("tools",),
        fingerprint=fp,
        recorded_at="2026-10-23T00:00:00+00:00",
        source="admin-probe",
    )


class TestFingerprint:
    def test_endpoint_fingerprint_sensitivities(self):
        a = endpoint_fingerprint("cloud", "provider_a", "https://a.example/v1", "model_a")
        assert a != endpoint_fingerprint("cloud", "provider_a", "https://b.example/v1", "model_a")
        assert a != endpoint_fingerprint("cloud", "provider_b", "https://a.example/v1", "model_a")
        assert a != endpoint_fingerprint("cloud", "provider_a", "https://a.example/v1", "model_b")
        assert a != endpoint_fingerprint("local", "provider_a", "https://a.example/v1", "model_a")

    def test_fingerprint_is_deterministic(self):
        args = ("cloud", "p", "https://x/v1", "m")
        assert endpoint_fingerprint(*args) == endpoint_fingerprint(*args)


class TestEvidenceStore:
    def test_record_and_get_roundtrip(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        store = EvidenceStore(tmp_path / "evidence.json")
        store.record("cloud:p:m:openai", _probed())

        got = store.get("cloud:p:m:openai")
        assert got is not None
        assert got.state == EvidenceState.PROBED
        assert got.protocol == "openai"
        assert got.features == ("tools",)
        assert got.fingerprint == "fp1"

    def test_get_missing_key_returns_none(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        store = EvidenceStore(tmp_path / "evidence.json")
        assert store.get("nope") is None

    def test_persistence_reload(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        path = tmp_path / "evidence.json"
        store = EvidenceStore(path)
        store.record("k1", _probed())
        store.save()

        store2 = EvidenceStore(path)
        store2.load()
        got = store2.get("k1")
        assert got is not None and got.state == EvidenceState.PROBED

    def test_broken_json_keeps_in_memory_state(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        path = tmp_path / "evidence.json"
        path.write_text("{not json", encoding="utf-8")
        store = EvidenceStore(path)
        store.load()  # 损坏 → 保留内存旧态（空）
        store.record("k1", _probed())

        path.write_text("{broken again", encoding="utf-8")
        store3 = EvidenceStore(path)
        store3.load()
        assert store3.get("k1") is None  # 无崩溃，仅清空
        # 原内存对象不受影响
        assert store.get("k1") is not None

    def test_invalidate_by_fingerprint_marks_stale(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        store = EvidenceStore(tmp_path / "evidence.json")
        store.record("k1", _probed(fp="fpA"))
        store.record("k2", _probed(fp="fpB"))

        n = store.invalidate_by_fingerprint("fpA")

        assert n == 1
        assert store.get("k1").state == EvidenceState.STALE
        assert store.get("k2").state == EvidenceState.PROBED

    def test_invalidate_does_not_touch_non_probed(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        store = EvidenceStore(tmp_path / "evidence.json")
        store.record("k1", Evidence(
            state=EvidenceState.DECLARED, protocol="openai", features=(),
            fingerprint="fpA", recorded_at="t", source="admin-declare"))
        n = store.invalidate_by_fingerprint("fpA")
        assert n == 0
        assert store.get("k1").state == EvidenceState.DECLARED

    def test_save_is_atomic_no_partial_file(self, tmp_path):
        from inferfabric.model_pool.evidence import EvidenceStore
        path = tmp_path / "evidence.json"
        store = EvidenceStore(path)
        store.record("k1", _probed())
        store.save()
        # 保存后文件是完整 JSON，无残留 tmp 文件
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "k1" in data
        leftovers = [p for p in tmp_path.iterdir() if p.name != "evidence.json"]
        assert leftovers == []
