# 控制台易用性批次 — STATUS

| 项 | 状态 | 备注 |
|---|---|---|
| 需求与裁决 | ✅ 2026-11-24 | 四项裁决见 [spec.md](spec.md) §0；两项既有裁决被推翻（向导挂点、key 一次性展示），实施时在对应文档记录 |
| UX 落地性审视 | ✅ 2026-11-24 | document-ux-review 技能动线推演：82/100，1 高 2 中 3 低；ISSUE-01/02/03/04/05 已修入 spec（轮换动作、失败可读性、入口层级、密码恢复指引、吊销文案）；HTML 报告在会话产物目录 |
| PR1 · IA（向导迁移+导航重组） | ✅ 已实施（#43） | 异常并入状态组已含；fresh-context 评审 2 important + 4 nit 全部修入同分支 |
| PR2 · 组级回退开关 | ✅ 已实施（#45） | strategy 严格枚举（未知值拒载）；selector first_only 截断（排序后 [:1]，决策 reason 不动）；新错误码 `fallback_disabled`（双协议 503 可区分）；`POST /admin/model-pools/strategy` CAS 字段级翻转（registry `_flip_pool_field` 共享路径，toggle 重构复用）；UI 回退开关 + tooltip + fallback_disabled 专用横幅。fresh-context 评审 3 important 全修入：①建池骨架旧值 `strategy:'priority'` 改现行值 + 解析层 legacy 别名归一化（防存量 YAML 升级整份拒载）；②strategy 出 structSig（跨端翻转触发重建，开关不陈旧）；③first_only 降级横幅误报消除，ISSUE-02 由 fallback_disabled 横幅真正落地面。23 例 RED→GREEN，全量 2766 passed（3 例 connect_clients[codex] 为 #44 既有 macOS 失败） |
| PR3 · 授权设置页（原「设置页+密码 UI」） | ✅ 已实施（目标 7.12.2，PR 评审中） | 实施修订见 spec §4 两条（2026-11-25）：①首次网页设密管理员凭证恒校验 + `iff keys init` 语义（不借 enable 路径，拒绝路径零落盘）；②改密 = 控制台会话 + CSRF + 旧密码（SecurityAdmin `password` 动作 + `verify_current`，旧会话随口令记录自动失效）；③忘记密码健康库走 `iff keys set-password`（非 recover）；④授权体系审计后收敛为「授权设置」三卡：本机零门槛边界（永不过密码，不设全站锁）+ 推理 API 鉴权开关（写前 confirm）+ Key 保护密码（仅保护敏感操作，非控制台登录）；status 增 `password_set`；nav 9→10。34 例新增 RED→GREEN（+2 nav/模块列表钉同步） |
| PR4 · key 重看+吊销硬删 | ⬜ 未开始 | 依赖 PR3；存量旧 key 不可重看；一次性清理现存 revoked 行 |
| 验收 | ⬜ | 每 PR 全量 unit+integration + fresh-context cross-review；WS3 需真机验证 reveal 全链路（设密码→建 key→重看→轮换→吊销→列表无痕→同名可重建）；动线级用例（ISSUE-06）：①新装模型全程走能力包页向导 ②关回退→P1 故障→失败原因可读 ③key 全生命周期 |
