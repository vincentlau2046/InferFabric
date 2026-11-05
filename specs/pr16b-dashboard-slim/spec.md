# PR-16b: Dashboard 瘦身

## 已完成
- `model_discovery.py`: 删 discovered 项的 path/files 字段（vllm/ollama/llama_cpp/comfyui）
- 前端 payload 减少 ~40% (无 path 字符串 + files 数组)
- dashboard.py 内嵌 JS 无 path/files 引用 → 零影响

## 推迟至 v4.3+1
- Dashboard 布局重设计 (1500行内嵌 JS, 高风险, 需专门 review)
