"""Catalog 加载与校验 — PK0（Task 4）。

契约（计划 Interfaces）：
- CatalogEntry(manifest, display_name, summary, tags, risk_level, builtin)。
- load_catalog(path)：每条目内嵌 manifest: 块走 parse_manifest 严格校验；
  任一条目非法 → CatalogError 并指明 pack_id。
- Catalog.find / list_capability。
- 捆绑目录首批 4 项（2026-11-16 用户裁决换 ModelScope 源后修订）：Qwen3-4B-Instruct-2507 /
  BGE-M3 / Kokoro ONNX / macOS Vision；Gemma 不在目录中（许可证复核前移出）；
  whisper ggml bin 无 ModelScope 源 → 移出。
"""

from pathlib import Path

import pytest

from inferfabric.packs.catalog import Catalog, CatalogError, load_catalog

REPO = Path(__file__).resolve().parents[2]


def test_bundled_catalog_loads_four_entries():
    entries = load_catalog(REPO / "packs_catalog.yaml")
    ids = {e.manifest.pack_id for e in entries}
    assert ids == {
        "llm.qwen3-4b-instruct-2507", "embedding.bge-m3",
        "tts.kokoro-82m-onnx", "ocr.macos-vision"}


def test_bundled_catalog_first_batch_invariants():
    entries = load_catalog(REPO / "packs_catalog.yaml")
    by_id = {e.manifest.pack_id: e for e in entries}
    # 首个完整试点
    qwen = by_id["llm.qwen3-4b-instruct-2507"]
    assert qwen.manifest.capability == "llm"
    assert qwen.manifest.parameter_count_b == 4
    assert qwen.manifest.license == "apache-2.0"
    assert qwen.manifest.lifecycle_mode == "on_demand"
    assert qwen.manifest.listen == "loopback"
    # macOS Vision：系统内置，无下载 artifact
    vision = by_id["ocr.macos-vision"]
    assert vision.builtin is True
    assert vision.manifest.artifacts == ()
    # 其余三项非内置，均有 artifact
    for pid in ("embedding.bge-m3", "tts.kokoro-82m-onnx"):
        e = by_id[pid]
        assert e.builtin is False and e.manifest.artifacts
    # Gemma 许可证复核前不得进入目录（spec §11 裁决）
    assert not any("gemma" in pid for pid in by_id)


def test_catalog_entry_bad_manifest_named():
    bad = """
packs:
  - pack_id: llm.bad-entry
    display_name: Bad
    summary: s
    tags: []
    risk_level: verified
    builtin: false
    manifest:
      pack_id: llm.bad-entry
      pack_version: 1.0.0
      capability: video_gen
"""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(bad)
        p = Path(f.name)
    with pytest.raises(CatalogError) as e:
        load_catalog(p)
    assert e.value.pack_id == "llm.bad-entry"
    p.unlink(missing_ok=True)


def test_catalog_duplicate_pack_id_rejected(tmp_path):
    base = (REPO / "packs_catalog.yaml").read_text()
    # 复制首个条目形成重复 pack_id
    parts = base.split("\n  - pack_id:")
    dup = "\n  - pack_id:" + parts[1]
    p = tmp_path / "cat.yaml"
    p.write_text(base + dup)
    with pytest.raises(CatalogError) as e:
        load_catalog(p)
    assert "duplicate" in str(e.value)


def test_find_and_list_capability():
    cat = Catalog(load_catalog(REPO / "packs_catalog.yaml"))
    assert cat.find("llm.qwen3-4b-instruct-2507") is not None
    assert cat.find("llm.nope") is None
    assert [e.manifest.pack_id for e in cat.list_capability("llm")] == \
        ["llm.qwen3-4b-instruct-2507"]
    assert [e.manifest.pack_id for e in cat.list_capability("ocr")] == \
        ["ocr.macos-vision"]
