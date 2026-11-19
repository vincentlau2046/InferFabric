"""能力包测试共享 fixture — MINIMAL_MANIFEST_YAML（合法最小 Manifest）。"""

_A64 = "a" * 64
_B64 = "b" * 64

MINIMAL_MANIFEST_YAML = f"""
pack_id: llm.qwen3-4b-instruct-2507
pack_version: 1.0.0
capability: llm
model: {{id: qwen3-4b-instruct-2507, parameter_count_b: 4, quantization: q4_k_m, license: apache-2.0}}
runtime: {{engine: llama_cpp, runtime_template: llama-cpp-macos-arm64-v1}}
resources: {{download_mb: 3200, disk_mb: 3400, peak_memory_mb: 4800, gpu_role: shared}}
lifecycle: {{mode: on_demand}}
network: {{listen: loopback, allowed_upstream: []}}
artifacts:
  - {{id: model, kind: model, source: "https://example.com/m.gguf", sha256: "{_A64}"}}
  - {{id: engine, kind: runtime, source: "https://example.com/llama.tar.gz", sha256: "{_B64}"}}
execution: {{model_config_template: llama_cpp_chat_v1, healthcheck: openai_chat_ready_v1}}
sandbox: {{policy: verified-local-pack-v1, degraded_install_allowed: true}}
"""

A64 = _A64
B64 = _B64
