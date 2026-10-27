# tests/unit/state/test_statedb_seal.py
"""StateDB.seal — PR-20 ③ 重启零中断：垂死进程禁写状态。

P0 实测：旧进程关停期 reconcile 毒写 gpu_mode=idle + 清空 services，
把错误状态交给下一个进程（12:27:58 事件）。封印后所有写方法 no-op、
读不受影响、幂等——下一个进程从现实重新推导。
"""
import logging

import pytest

from inferfabric.state import StateDB, GPUMode


def _db(tmp_path):
    return StateDB(tmp_path / "state.db")


class TestSealBlocksAllWrites:
    def test_set_after_seal_is_noop(self, tmp_path):
        db = _db(tmp_path)
        db.set("profile_state", "healthy")
        db.seal("proxy shutdown")
        db.set("profile_state", "idle")
        db.set("brand_new_key", "x")
        assert db.get("profile_state") == "healthy"
        assert db.get("brand_new_key") is None

    def test_set_multi_after_seal_is_noop(self, tmp_path):
        db = _db(tmp_path)
        db.set_multi({"a": "1", "b": "2"})
        db.seal("x")
        db.set_multi({"a": "9", "c": "3"})
        assert db.get("a") == "1"
        assert db.get("b") == "2"
        assert db.get("c") is None

    def test_active_services_frozen(self, tmp_path):
        db = _db(tmp_path)
        db.set_active_services(["qwen", "bge"])
        db.seal("x")
        db.add_active_service("tts")
        db.remove_active_service("qwen")
        db.set_active_services(["z"])
        assert db.get_active_services() == ["qwen", "bge"]

    def test_gpu_mode_setter_noop_after_seal(self, tmp_path):
        db = _db(tmp_path)  # 无 models_lookup → property 落到 DB 存储值
        db.gpu_mode = GPUMode.SHARED
        db.seal("x")
        db.gpu_mode = GPUMode.IDLE  # 毒写场景（P0 12:27:58）→ 必须 no-op
        assert db.gpu_mode == GPUMode.SHARED

    def test_manual_stop_frozen(self, tmp_path):
        db = _db(tmp_path)
        db.record_manual_stop("m1")
        db.seal("x")
        db.clear_manual_stop("m1")
        db.record_manual_stop("m2")
        assert db.is_manually_stopped("m1") is True
        assert db.is_manually_stopped("m2") is False

    def test_sleep_state_frozen(self, tmp_path):
        db = _db(tmp_path)
        db.set_sleep_state("m", 2)
        before = db.get_sleep_state("m")
        db.seal("x")
        db.set_sleep_state("m", 1)
        db.set_sleep_state("m", None)
        assert db.get_sleep_state("m") == before

    def test_history_frozen(self, tmp_path):
        db = _db(tmp_path)
        db.add_history("idle", "shared", 1.5)
        db.seal("x")
        db.add_history("shared", "idle", 2.0)
        history = db.get_history(10)
        assert len(history) == 1
        assert history[0]["to"] == "shared"


class TestSealSemantics:
    def test_seal_idempotent_logs_once(self, tmp_path, caplog):
        db = _db(tmp_path)
        with caplog.at_level(logging.WARNING, logger="inferfabric"):
            db.seal("first")
            db.seal("second")
        seals = [r for r in caplog.records if "StateDB sealed" in r.getMessage()]
        assert len(seals) == 1
        assert "first" in seals[0].getMessage()

    def test_reads_unaffected_after_seal(self, tmp_path):
        db = _db(tmp_path)
        db.set("k", "v")
        db.set_active_services(["svc"])
        db.record_manual_stop("m")
        db.add_history("idle", "shared", 1.0)
        db.seal("x")
        # 读全部正常——封印只挡写
        assert db.get("k") == "v"
        assert db.get_active_services() == ["svc"]
        assert db.is_manually_stopped("m") is True
        assert len(db.get_history(5)) == 1
        assert db.gpu_mode in (GPUMode.IDLE, GPUMode.SHARED, GPUMode.EXCLUSIVE)

    def test_seal_from_writing_thread(self, tmp_path):
        """封印是线程安全标志：另一线程封印后，本线程写即刻 no-op。"""
        db = _db(tmp_path)
        db.set("k", "before")
        db.seal("from other thread")
        db.set("k", "after")
        assert db.get("k") == "before"
