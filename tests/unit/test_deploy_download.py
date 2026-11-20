"""deploy_download — ModelScope GGUF 下载器（部署向导计划 Task 3b，用户裁决 B）。

契约：
- normalize_model_id：org/name 白名单，拒路径穿越（Review Focus 4 同源）
- list_gguf_files(transport, model_id)：files API 归一化，只留 .gguf blob
- start_download(model_id, ws_root, ...) → daemon 线程落盘
  <ws_root>/models/<name>/*.gguf（匹配 install_yaml 的 **/{name}/*.gguf glob）
- download_status(download_id)：{stage, files_total, files_done, current_file, error}
- 多量化变体 repo 必须选文件（path）；split 分片按家族归并（-NNNNN-of-NNNNN）
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest

from inferfabric.proxy import deploy_download as dd


_SHA_A = "aa" * 32
_PAYLOAD = b"GGUF-payload"      # Gate fetcher 落盘内容
_SHA_PAYLOAD = __import__("hashlib").sha256(_PAYLOAD).hexdigest()


def _files_transport(files=None):
    if files is None:
        files = [
            {"Type": "blob", "Path": ".gitattributes", "Size": 3081,
             "Sha256": "11" * 32},
            {"Type": "blob", "Path": "README.md", "Size": 5633,
             "Sha256": "22" * 32},
            {"Type": "blob", "Path": "model-q4_k_m.gguf",
             "Size": 4000, "Sha256": _SHA_A},
            {"Type": "blob", "Path": "model-q8_0.gguf",
             "Size": 8000, "Sha256": "33" * 32},
        ]
    def transport(url):
        assert "repo/files" in url and "Revision=master" in url
        return {"Code": 200, "Data": {"Files": files}}
    return transport


class _FakeFetcher:
    """fetcher 契约：fetch(artifact, dest)，artifact 只读 .source。"""
    def __init__(self, payload=b"GGUF-fake"):
        self.payload = payload
        self.sources = []
    def fetch(self, artifact, dest):
        self.sources.append(artifact.source)
        dest.write_bytes(self.payload)


def _wait_done(download_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        st = dd.download_status(download_id)
        if st["stage"] in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError("download did not finish in time")


class TestNormalizeModelId:
    def test_accepts_org_name(self):
        assert dd.normalize_model_id("Qwen/Qwen2.5-7B-Instruct-GGUF") == \
            "Qwen/Qwen2.5-7B-Instruct-GGUF"

    def test_rejects_traversal_and_garbage(self):
        for bad in ("../x", "a/..", "..", "nodir", "a/b/c", "", "a b/c",
                    "a/b c", "/abs", "x/"):
            with pytest.raises(ValueError):
                dd.normalize_model_id(bad)


class TestListGgufFiles:
    def test_keeps_only_gguf_blobs(self):
        files = dd.list_gguf_files(_files_transport(), "Qwen/Qwen2.5-7B-GGUF")
        assert [f["path"] for f in files] == ["model-q4_k_m.gguf",
                                              "model-q8_0.gguf"]
        assert files[0]["size"] == 4000
        assert files[0]["sha256"] == _SHA_A

    def test_rejects_pathed_entries(self):
        files = [
            {"Type": "blob", "Path": "model-q4_k_m.gguf", "Size": 4000,
             "Sha256": _SHA_A},
            {"Type": "blob", "Path": "sub/dir/x.gguf", "Size": 1,
             "Sha256": "44" * 32}]
        out = dd.list_gguf_files(_files_transport(files), "a/b")
        assert [f["path"] for f in out] == ["model-q4_k_m.gguf"]

    def test_malformed_response_raises(self):
        for garbage in ({}, {"Data": None}, {"Data": {"Files": "x"}}, None):
            with pytest.raises(ValueError):
                dd.list_gguf_files(lambda u: garbage, "a/b")


class TestSelectFiles:
    def test_single_variant_no_choice_needed(self):
        files = [{"path": "only.gguf", "size": 1, "sha256": _SHA_A}]
        assert dd.select_files(files, None) == ["only.gguf"]

    def test_multiple_variants_require_path(self):
        files = [{"path": "m-q4.gguf", "size": 1, "sha256": ""},
                 {"path": "m-q8.gguf", "size": 1, "sha256": ""}]
        with pytest.raises(ValueError) as ei:
            dd.select_files(files, None)
        assert "path" in str(ei.value)

    def test_split_family_merged(self):
        # 选中 split 第一分片 → 同量化家族全部分片（llama.cpp 需要整套）
        files = [{"path": f"m-q4_k_m-{i:05d}-of-{2:05d}.gguf", "size": 1,
                  "sha256": ""} for i in (1, 2)]
        files.append({"path": "m-q8_0.gguf", "size": 1, "sha256": ""})
        assert dd.select_files(files, "m-q4_k_m-00001-of-00002.gguf") == [
            "m-q4_k_m-00001-of-00002.gguf", "m-q4_k_m-00002-of-00002.gguf"]

    def test_incomplete_split_family_not_offered(self):
        # 评审 F9：截断清单只有分片 1/2 → 不得作为变体选项报 done
        files = [{"path": "m-q4_k_m-00001-of-00002.gguf", "size": 1,
                  "sha256": ""}]
        with pytest.raises(ValueError) as ei:
            dd.select_files(files, None)
        assert "incomplete_split_family" in str(ei.value)

    def test_chosen_split_requires_full_family(self):
        # 显式选中分片 1 但清单缺分片 2 → 拒绝整套下载
        files = [{"path": "m-q4_k_m-00001-of-00002.gguf", "size": 1,
                  "sha256": ""},
                 {"path": "m-q8_0.gguf", "size": 1, "sha256": ""}]
        with pytest.raises(ValueError) as ei:
            dd.select_files(files, "m-q4_k_m-00001-of-00002.gguf")
        assert "incomplete_split_family" in str(ei.value)

    def test_unknown_path_rejected(self):
        files = [{"path": "m-q4.gguf", "size": 1, "sha256": ""}]
        with pytest.raises(ValueError):
            dd.select_files(files, "nope.gguf")

    def test_multi_variant_error_carries_choices(self):
        # 前端变体 picker 数据源：结构化 choices，不解析错误文案
        files = [{"path": "m-q4.gguf", "size": 1, "sha256": ""},
                 {"path": "m-q8.gguf", "size": 1, "sha256": ""}]
        with pytest.raises(dd.MultiVariantError) as ei:
            dd.select_files(files, None)
        assert ei.value.choices == ["m-q4.gguf", "m-q8.gguf"]


class TestDownloadFlow:
    def test_requires_path_when_multiple_variants(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            dd.start_download("Qwen/Qwen2.5-7B-GGUF", tmp_path,
                              fetcher=_FakeFetcher(),
                              transport=_files_transport())
        assert "path" in str(ei.value)

    def test_selected_file_lands_in_ws_models(self, tmp_path):
        import hashlib
        payload = b"GGUF-fake"
        files = [
            {"Type": "blob", "Path": "model-q4_k_m.gguf",
             "Size": len(payload),
             "Sha256": hashlib.sha256(payload).hexdigest()},
            {"Type": "blob", "Path": "model-q8_0.gguf", "Size": 8000,
             "Sha256": "33" * 32}]
        dl = dd.start_download("Qwen/Qwen2.5-7B-GGUF", tmp_path,
                               fetcher=_FakeFetcher(payload),
                               transport=_files_transport(files),
                               path="model-q4_k_m.gguf")
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "done"
        assert st["files_done"] == 1 and st["files_total"] == 1
        dest = (tmp_path / "models" / "Qwen__Qwen2.5-7B-GGUF"
                / "model-q4_k_m.gguf")
        assert dest.exists()
        assert dest.read_bytes() == payload
        # 源 URL 形态 = 线上校准的 resolve/master
        assert dl["files"][0]["source"].startswith(
            "https://modelscope.cn/models/Qwen/Qwen2.5-7B-GGUF/resolve/master/")
        assert dl["total_bytes"] == len(payload)

    def test_fetcher_error_is_visible(self, tmp_path):
        class Boom:
            def fetch(self, artifact, dest):
                raise TimeoutError("cdn cut")
        dl = dd.start_download("a/m", tmp_path, fetcher=Boom(),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": 1, "Sha256": _SHA_A}]))
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "error"
        assert "cdn cut" in st["error"]

    def test_sha_mismatch_is_error(self, tmp_path):
        dl = dd.start_download("a/m", tmp_path,
                               fetcher=_FakeFetcher(b"GGUF-other"),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": 1, "Sha256": _SHA_A}]))
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "error"
        assert "sha256" in st["error"].lower()

    def test_sha_match_is_done(self, tmp_path):
        import hashlib
        payload = b"GGUF-real"
        good = hashlib.sha256(payload).hexdigest()
        dl = dd.start_download("a/m", tmp_path,
                               fetcher=_FakeFetcher(payload),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": len(payload), "Sha256": good}]))
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "done"

    def test_no_gguf_files_is_actionable_error(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            dd.start_download("a/m", tmp_path, fetcher=_FakeFetcher(),
                              transport=_files_transport(files=[]))
        assert "gguf" in str(ei.value).lower()

    def test_status_unknown_id(self):
        assert dd.download_status("nope") is None

    def test_duplicate_dest_rejected(self, tmp_path):
        # 评审 F5：并发同目标下载会互相追加同一 .part → 拒绝第二个
        import threading
        gate = threading.Event()

        class Gate:
            def fetch(self, artifact, dest):
                gate.wait(5)
                dest.write_bytes(_PAYLOAD)

        dl = dd.start_download("a/m", tmp_path, fetcher=Gate(),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": 1, "Sha256": _SHA_PAYLOAD}]))
        time.sleep(0.1)
        try:
            with pytest.raises(ValueError) as ei:
                dd.start_download("a/m", tmp_path, fetcher=_FakeFetcher(),
                                  transport=_files_transport(
                                      [{"Type": "blob", "Path": "m.gguf",
                                        "Size": 1, "Sha256": _SHA_A}]))
            assert "download_in_progress" in str(ei.value)
        finally:
            gate.set()
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "done"

    def test_symlink_staging_rejected(self, tmp_path):
        # 评审 F5：.part 为既有符号链接 → 追加写会穿透目标文件
        # F2 后落盘目录为 a__m（model_id "a/m" → org__name）；符号链接指向
        # 非敏感路径，断言其不被穿透写入
        dest_dir = tmp_path / "models" / "a__m"
        dest_dir.mkdir(parents=True)
        target = tmp_path / "symlink-victim"
        target.write_bytes(b"victim")
        (dest_dir / "m.gguf.part").symlink_to(target)
        dl = dd.start_download("a/m", tmp_path,
                               fetcher=_FakeFetcher(b"GGUF-real"),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": 1, "Sha256": _SHA_A}]))
        st = _wait_done(dl["download_id"])
        assert st["stage"] == "error"
        assert "staging" in st["error"].lower()
        assert target.read_bytes() == b"victim", "符号链接目标不得被穿透写入"

    def test_dest_dir_includes_publisher(self, tmp_path):
        # 评审 F2：两名发布者同名 repo 共享落盘目录 → org__name 隔离
        dl = dd.start_download("org/m", tmp_path, fetcher=_FakeFetcher(),
                               transport=_files_transport(
                                   [{"Type": "blob", "Path": "m.gguf",
                                     "Size": 1, "Sha256": _SHA_A}]))
        assert dl["dest_dir"] == str(tmp_path / "models" / "org__m")


class TestHandlerBodyContract:
    def test_body_none_sends_no_second_response(self, tmp_path):
        # 评审 F6：read_body 413/400 已发送响应后返回 None → 不得二次发送
        h = SimpleNamespace(_read_body=lambda: None, _sent=[])
        h._send_json = lambda data, status=200, extra_headers=None: \
            h._sent.append((data, status))
        pm = SimpleNamespace(mgr=SimpleNamespace(models_dir=str(tmp_path)))
        dd.handle_deploy_download(h, pm)
        assert h._sent == [], "read_body 已响应后不得二次发送"


class TestRouteWiring:
    def test_routes_registered_with_admin_gate(self):
        from inferfabric.proxy import handler
        assert "/admin/deploy/download" in handler._POST_ROUTES
        assert "/admin/deploy/download/status" in handler._GET_ROUTES
