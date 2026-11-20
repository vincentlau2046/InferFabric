# tests/unit/test_packs_config_gen.py
"""能力包 config_gen / runtime_prep — Task 5（BGE CPU embedding 试点）。

- llama_cpp_embedding_v1 模板：embedding 能力 → CPU（gpu_layers=0）、
  无 chat 专用 --jinja、model_type=embedding、产物路径钉在包目录内。
- runtime_prep：Linux CPU 模板与 Mac 模板同一解包路径（保留旧模板，
  不删不改其行为）；未知模板仍拒绝。
"""
import io
import tarfile

import pytest

from inferfabric.packs.config_gen import (
    KNOWN_TEMPLATES,
    build_model_config,
    pack_model_name,
)


def _embedding_manifest_yaml(template="llama-cpp-linux-cpu-v1"):
    return f"""
pack_id: embedding.bge-cpu
pack_version: 1.0.0
capability: embedding
model: {{id: bge-m3, parameter_count_b: 1, quantization: q8_0, license: mit}}
runtime: {{engine: llama_cpp, runtime_template: {template}}}
resources: {{download_mb: 1200, disk_mb: 1500, peak_memory_mb: 1500, gpu_role: none}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/bge.gguf", sha256: "{"a"*64}"}}
  - {{id: engine, kind: runtime, source: "https://example.com/llama.tar.gz", sha256: "{"b"*64}"}}
execution: {{model_config_template: llama_cpp_embedding_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: true}}
"""


def _parse(yaml_text):
    from inferfabric.packs.schema import parse_manifest
    return parse_manifest(yaml_text)


class TestEmbeddingConfigTemplate:

    def test_embedding_template_registered(self):
        assert "llama_cpp_embedding_v1" in KNOWN_TEMPLATES

    def test_builds_cpu_embedding_config(self, tmp_path):
        from pathlib import Path
        manifest = _parse(_embedding_manifest_yaml())
        pack_dir = tmp_path / "packs_root" / "embedding.bge-cpu" / "1.0.0"
        cfg = build_model_config(manifest, pack_dir, port=8123)
        assert cfg.name == pack_model_name(manifest)
        assert cfg.model_type == "embedding"
        assert cfg.type == "llama_cpp"
        assert cfg.gpu_role == "none"
        # CPU 试点：受管 embedding 必然 gpu_layers=0（PM 层再强制）
        assert cfg.llama_cpp.gpu_layers == 0
        assert cfg.llama_cpp.served_name == "bge-m3"
        assert cfg.llama_cpp.model_path == \
            str(pack_dir / "models" / "model")
        assert cfg.llama_cpp.binary_path == \
            str(pack_dir / "runtime" / "engine")
        # chat 专用 --jinja 不进 embedding 配置
        assert "--jinja" not in cfg.llama_cpp.extra_flags

    def test_chat_template_unchanged(self, tmp_path):
        from tests.unit.tests_fixtures_packs import MINIMAL_MANIFEST_YAML
        manifest = _parse(MINIMAL_MANIFEST_YAML)
        cfg = build_model_config(manifest, tmp_path, port=8124)
        assert cfg.model_type == "llm"
        assert cfg.llama_cpp.gpu_layers == 99   # chat 模板行为不变


class TestRuntimePrepLinuxTemplate:

    @staticmethod
    def _make_pack(tmp_path):
        """runtime/engine 放一个含 llama-server 的 tar.gz（发行包形态）。"""
        pack_dir = tmp_path / "packs_root" / "embedding.bge-cpu" / "1.0.0"
        (pack_dir / "runtime").mkdir(parents=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            import time as _t
            for name, data in (("dist/llama-server", b"bin"),
                               ("dist/libggml.so", b"lib")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mtime = int(_t.time())
                tf.addfile(ti, io.BytesIO(data))
        (pack_dir / "runtime" / "engine").write_bytes(buf.getvalue())
        return pack_dir

    def test_linux_cpu_template_prepares_runtime(self, tmp_path):
        from inferfabric.packs.runtime_prep import prepare_runtime
        pack_dir = self._make_pack(tmp_path)
        manifest = _parse(_embedding_manifest_yaml())
        prepare_runtime(pack_dir, manifest)
        runtime = pack_dir / "runtime"
        # llama-server 摊平后落位 runtime/engine（= config_gen 的 binary_path）
        assert (runtime / "engine").exists()
        assert (runtime / "libggml.so").exists()
        assert not (runtime / "engine.tar.gz").exists()

    def test_unknown_template_still_rejected(self, tmp_path):
        from inferfabric.packs.runtime_prep import prepare_runtime
        with pytest.raises(RuntimeError, match="runtime_template_not_implemented"):
            prepare_runtime(tmp_path, _parse(_embedding_manifest_yaml(
                template="llama-cpp-future-v1")))
