# Tech Plan: NInfer V3 升级执行手册

> 配套 spec.md ｜ 每阶段含验证点与回滚路径 ｜ 生产影响仅 P3（~40min GPU 窗口）与 P4（分钟级）

## 三条铁律

1. **不碰内核驱动 / DKMS / 内核**——全程无 sudo、无重启（615.71.09 已满足一切要求）
2. **旧镜像与旧 artifact 在 P5 之前永不删除**——任意阶段可两条命令回滚
3. **一次只动一个变量**——先引擎+格式（本计划），MTP3/DFlash2/官方 artifact 全部延后到 P6

---

## P0 — 驱动确认（无操作，用户自证 30 秒）

- [ ] 用户在自己终端跑 `nvidia-smi`，预期正常显示（agent 沙箱内失败是 landlock 拦 `/dev/nvidiactl` 写打开，非主机问题）
- [ ] （可选卫生项，与升级无关，用户自行决定）清理 610 残留 userspace：
  ```bash
  sudo apt -s purge 'nvidia-driver-610-open' 'nvidia-dkms-610-open' 'nvidia-compute-utils-610' \
    'nvidia-utils-610' 'libnvidia-compute-610' 'libnvidia-cfg1-610' 'libnvidia-common-610' \
    'libnvidia-decode-610' 'libnvidia-encode-610'   # -s 先模拟，确认不含内核正在用的模块
  ```

## P1 — 构建 v3 引擎镜像（不占 GPU，VL 可继续服务，~20-30 分钟）

```bash
cd ~/ninfer && git fetch upstream
git worktree add /tmp/ninfer-v3-build upstream/master   # 干净树，不碰本地脏文件
docker build -t ninfer:v3 /tmp/ninfer-v3-build          # upstream Dockerfile，CUDA 13.1.2 基镜像已缓存
```

- [ ] 验证：`docker run --rm ninfer:v3 ninfer-serve --help | head -20`（新旗标 `--spec dflash|dflash2`、`--thinking-budget`、`--chat-template` 应出现）
- [ ] 回滚：`docker rmi ninfer:v3`（不影响任何现有 tag）

## P2 — TXT artifact 离线升级 v2→v3（纯磁盘操作，~10 分钟）

```bash
python3 /tmp/ninfer-v3-build/tools/upgrade_ninfer_v2_to_v3.py \
  ~/models/ninfer-nvfp4/qwen3_8_27b_nvfp4.ninfer \
  ~/models/ninfer-nvfp4/qwen3_8_27b_nvfp4_v3.ninfer
xxd -l 8 ~/models/ninfer-nvfp4/qwen3_8_27b_nvfp4_v3.ninfer   # 预期 NINFER\x00\x03
```

- [ ] 工具自校验对象数（KNOWN_COUNTS 允许 qwen3.8-27b/nvfp4 = 1124 或 1190 个对象）；权重字节不变 + 内置维护版 chat template
- [ ] 原 v2 文件不动（回滚即改回 YAML 路径）
- [ ] **回退分支**：若工具拒绝社区 artifact → 下载官方 `neroued/Qwen3.8-27B-nvfp4-NInfer`（~20GB，含 DFlash2+Vision），后续同流程

## P3 — 平行验证（需 GPU 空闲窗口 ~40 分钟）

```bash
./iff sleep    # 或 switch idle，释放 GPU
```

**测试 A（TXT 路径，新 artifact）**——参数与 adapter 实际生成保持一致（执行前从 `process_manager/ninfer.py` 复核）：

```bash
docker run --gpus all --rm -v ~/models/ninfer-nvfp4:/workspace -p 8017:8080 \
  --name ninfer-v3-test-txt -e NVIDIA_DISABLE_REQUIRE=1 ninfer:v3 \
  ninfer-serve /workspace/qwen3_8_27b_nvfp4_v3.ninfer --model-id Qwen38-27B-TXT \
  --host 0.0.0.0 --port 8080 --max-concurrency 4 --max-context 155000 \
  --kv-capacity 632000 --kv-dtype nvfp4 --prefill-chunk 2048 \
  --spec mtp --draft-tokens 1 --media-cache-mib 0 --media-live-mib 384
```

**测试 B（VL 路径，现有 v3 artifact + master loader 兼容性确认）**：

```bash
docker run --gpus all --rm -v ~/models/ninfer-qwen38-27b-vl:/workspace -p 8019:8080 \
  --name ninfer-v3-test-vl -e NVIDIA_DISABLE_REQUIRE=1 ninfer:v3 \
  ninfer-serve /workspace/qwen3_8_27b_vl_nvfp4.ninfer --model-id Qwen38-27B-VL \
  --host 0.0.0.0 --port 8080 --max-context 204800 --kv-capacity 410000 \
  --kv-dtype nvfp4 --spec mtp --draft-tokens 1 --vision \
  --media-cache-mib 0 --media-live-mib 1024
```

- [ ] A：`curl -s localhost:8017/v1/models` → `python3 benchmarks/bench_ninfer.py` 与 09-14 基线对比（判定：解码吞吐 ±10% 内且 MTP 接受率不降）→ 抽查 3-5 条输出质量
- [ ] B：健康检查 → 红方块视觉用例 → Anthropic `/v1/messages` 小请求（Claude Code 路由路径）
- [ ] 启动日志核对：KV pages、`free` 余量与旧引擎同水位（新引擎预留若有变化需重调 kv_capacity）
- [ ] 恢复现场：`./iff switch Qwen38-27B-VL`（或用户指定模型）
- [ ] **分支**：B 失败（09-22 v3 容器与 master loader 不兼容）→ VL 暂留 `vl-pre-v3` 镜像仅切 TXT；或启用官方 artifact 回退分支

## P4 — 切换（分钟级窗口，P3 全部通过后）

```bash
# 1) 备份现 tag（不覆盖既有 *-rollback）
docker tag ninfer:auto ninfer:auto-pre-v3
docker tag ninfer:vl  ninfer:vl-pre-v3
# 2) 新镜像接管
docker tag ninfer:v3 ninfer:auto && docker tag ninfer:v3 ninfer:vl
# 3) YAML 变更（sandbox-first）：Qwen38-27B-TXT.yaml 的 weight_path
#    ~/models/ninfer-nvfp4/qwen3_8_27b_nvfp4.ninfer
#      → ~/models/ninfer-nvfp4/qwen3_8_27b_nvfp4_v3.ninfer
#    sandbox/ 内修改 → pytest tests/unit/ → smoke → diff review → 合并
# 4) 切换验证
./iff switch Qwen38-27B-TXT   # 或用户指定
curl -s localhost:8007/v1/models && curl -s localhost:8007/metrics | head -5
```

- [ ] 双模型 metrics 端点恢复一致（TXT 此前没有的直方图也有了）
- [ ] **回滚**：`docker tag ninfer:auto-pre-v3 ninfer:auto`、`docker tag ninfer:vl-pre-v3 ninfer:vl` + YAML weight_path 改回 + `./iff switch`——三步皆可独立执行

## P5 — 收尾（稳定运行 1-2 天后）

- [ ] `git worktree remove /tmp/ninfer-v3-build`
- [ ] ninfer 本地仓库归零：`git diff > ~/ninfer-local-mods-20261022.patch`（留档 Dockerfile 13.0.1 改动等）→ `git checkout -- .` → `git reset --hard upstream/master` → push fork（自有提交 beae4840 已在上游；package.cpp 过时 patch 随之消亡）
- [ ] 归档旧 v2 TXT artifact（移 `~/models/archive/` 或确认后删除，-18.3GB）
- [ ] 文档：`models.d/README.md` 版本注记；CLAUDE.md 性能注记修正（"MTP 4-token" → 与 YAML draft_tokens:1 一致）；bench 新基线入 `bench_results/`
- [ ] vault 决策日志（01-InferFabric）

## P6 — 后续 track（独立排期，不阻塞本计划）

1. 官方 artifact（~20GB，DFlash2+Vision+MTP 统一）下载 bench → 若采用：TXT/VL 共用一个 weight_path，退役社区双 artifact（-22GB）
2. adapter 加 `spec_mode` 字段（dflash2，~10 行 + 单测，sandbox-first）→ scenarios 重调（draft 7 / MTP3）→ 长上下文 bench 对比（现 MTP 接受率 0.86@1K → 0.26@100K 是否改善）
3. qwen4-exp（Qwen3.8-Flash-Next）评估

---

## 风险清单

| 风险 | 概率 | 缓解 / 回退 |
|---|---|---|
| v2→v3 工具拒绝社区 artifact | 中 | P2 内置回退分支（官方 artifact 下载） |
| 09-22 v3 artifact 与 master loader 不兼容 | 低 | P3-B 先行验证；失败则 VL 暂留旧镜像 |
| CUDA 13.1.2 构建产物在 615 驱动异常 | 很低 | 基镜像退回 13.0.1（本机已验证组合） |
| bench 劣化超阈值 | 低 | 不切换，全链路保留 |
| 新引擎显存预留变化挤压 KV 池 | 低 | 显式 kv_capacity 不变 + P3 核对 pages/free；必要时微调 |
| 服务中断 | 计划内 | 仅 P3/P4 两处短窗口，均有回滚 |
