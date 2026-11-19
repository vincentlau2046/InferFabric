"""能力包（Capability Packs）— 本地能力包后端。

模块职责（spec: docs/superpowers/specs/2026-11-15-capability-packs-design.md）：
- schema: Manifest 严格解析（PK0）
- policy: 4B/8GB 策略设置项与参数门禁（PK0）
- sandbox: 沙箱能力探测与 Provider（PK0）
- catalog: 捆绑能力目录加载（PK0）
- installer/store: 安装事务与状态持久化（PK1）
- config_gen: Manifest → ModelConfig 生成（PK3）
- supervisor/uninstaller: 进程组隔离与卸载（PK2）
"""
