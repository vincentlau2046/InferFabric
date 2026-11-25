"""父进程消亡自退（issue #50）— 验收实例孤儿进程防护。

契约：
- IFF_PARENT_WATCH != "1" → start_parent_watch() 返回 None（生产 launchd
  网关不设该 env，行为不受影响）。
- IFF_PARENT_WATCH=1 → 返回 daemon 线程，轮询 getppid()。
- _watch 循环：ppid 变化（父死 → reparent）→ _exit(0)；稳定 → 不退出。
"""

import os
import threading

import pytest

from inferfabric import parent_watch as pw


class _Exit(Exception):
    def __init__(self, code):
        self.code = code


class _ExitRecorder:
    """模拟 os._exit：真实 _exit 不返回，故首次调用即抛出终止循环。"""

    def __init__(self):
        self.called = None

    def __call__(self, code):
        self.called = code
        raise _Exit(code)


def test_start_gate_requires_env(monkeypatch):
    monkeypatch.delenv("IFF_PARENT_WATCH", raising=False)
    assert pw.start_parent_watch() is None


def test_start_gate_env_on_returns_daemon_thread(monkeypatch):
    monkeypatch.setenv("IFF_PARENT_WATCH", "1")
    try:
        t = pw.start_parent_watch(interval=3600)   # 不让它真的轮到退出
        assert isinstance(t, threading.Thread)
        assert t.daemon
        assert pw.is_armed() is True               # /health 报实况（issue #50）
    finally:
        pw.stop_parent_watch()
    assert pw.is_armed() is False


def test_watch_exits_when_ppid_changes():
    """父进程死后 ppid reparent（如 → 1）→ _exit(0)。"""
    rec = _ExitRecorder()
    ppids = iter([4242, 4242, 1])
    with pytest.raises(_Exit):
        pw._watch(initial_ppid=4242, interval=0.0,
                  _sleep=lambda s: None,
                  _ppid=lambda: next(ppids),
                  _exit=rec)
    assert rec.called == 0


def test_watch_survives_stable_ppid():
    """父进程健在（ppid 稳定）→ N 轮后仍不退出。"""
    rec = _ExitRecorder()
    ticks = []
    pw._watch(initial_ppid=4242, interval=0.0,
              _sleep=lambda s: ticks.append(1),
              _ppid=lambda: 4242,
              _exit=rec, _max_ticks=5)
    assert rec.called is None
    assert len(ticks) == 5   # 轮询在进行（循环未提前退出）


def test_watch_initial_read_is_caller_responsibility():
    """initial_ppid 由 start 时读取 — 模块文档钉住语义（防把「当前 ppid」
    每轮重读导致永远不触发）。"""
    import inspect
    src = inspect.getsource(pw.start_parent_watch)
    assert "os.getppid()" in src


def test_instance_banner_production_port_none():
    assert pw.instance_banner(8999) is None


def test_instance_banner_nonproduction_mentions_identity():
    b = pw.instance_banner(8990)
    assert b is not None
    assert "8990" in b and "8999" in b
    assert "非生产实例" in b
    assert "IFF_PARENT_WATCH" in b
