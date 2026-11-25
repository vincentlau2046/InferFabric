"""统一错误分类目录（issue #49）：reason 枚举 → kind + 人话 + 建议动作。

三面共用一套目录（单一事实源）：
- 安装准入拒绝：PackDecision.reason_code（admission.evaluate_pack）
- 安装 stage 失败：installer.fail() 错误串的前缀（"download_error:URLError"）
- 启动准入拒绝：PackRuntimeError.reason_code（managed_runtime.check_start）

kind 语义（前端按 kind 决定呈现形态）：
- admission  准入/预检拒绝 — 动作在环境侧（切 idle、释放资源、修运行时）
- download   下载/校验失败 — 可重试（重新发起安装）
- runtime    运行时/钩子失败 — 看网关日志修复后重试
- conflict   状态冲突 — 需先卸载或重装修复受管状态
- not_found  未知包 — 确认名称
- unknown    未登记 — 展示原文 + 通用排查建议（绝不吞掉/改写原文）

前端镜像目录在 dashboard/js/packs.js（STAGE_LABELS 同款镜像模式）；
Python 侧条目变化时须同步镜像并更新两侧测试。
"""

KINDS = ("admission", "download", "runtime", "conflict",
         "not_found", "unknown")

_GENERIC_ACTION = ("查看网关日志（~/.inferfabric/logs/proxy.log）定位，"
                   "修复后重试；或在本机用 CLI 重试")

# code -> (kind, message, action)
_HINTS = {
    # ── 准入（admission.evaluate_pack / installer preflight / PackRuntimeError）──
    "resource_busy": (
        "admission",
        "GPU 忙 — 托管能力包仅允许 GPU idle 时启动，不挤占现役模型",
        "先把 GPU 切回 idle（控制台「回到 idle」或 ./iff reset），"
        "再安装/启动该包"),
    "resource_insufficient": (
        "admission",
        "资源不足 — 磁盘/内存/显存低于包要求，或超过装机策略上限",
        "释放资源后在装机策略中核对上限；确有把握可调整策略上限重试"),
    "resource_unknown": (
        "admission",
        "资源证据缺失 — 磁盘/内存/显存遥测不可用，GPU 包不盲放行",
        "恢复资源遥测（/api/snapshot 的 device 证据）后重试"),
    "platform_mismatch": (
        "admission",
        "平台不符 — 当前主机不满足该包声明的平台/算力要求",
        "查看包详情的平台要求；如确需使用，联系包维护者放宽声明"),
    "platform_unverified": (
        "admission",
        "平台无法核实 — 主机缺少算力版本证据",
        "确认 nvidia-smi/驱动可用后重试"),
    "runtime_unavailable": (
        "admission",
        "运行时隔离不可用 — 沙箱运行时未安装或未接线",
        "安装/修复沙箱运行时后重试"),
    "isolation_unverified": (
        "admission",
        "隔离未验证 — 运行时存在但尚未验证隔离能力",
        "运行时验证通过后重试"),
    "isolation_degraded_not_allowed": (
        "admission",
        "隔离降级且该包不允许降级安装",
        "恢复完整沙箱隔离，或选择允许降级（degraded）安装的包"),
    # ── 安装 stage 失败（installer.fail() 错误码前缀）──
    "download_error": (
        "download",
        "下载失败 — 网络/下载源不可达",
        "点击重试重新发起安装（断点产物不重复校验已通过的分卷）"),
    "artifact_sha256_mismatch": (
        "download",
        "产物校验失败 — 下载内容与清单 sha256 不符",
        "重新下载重试；多次失败说明源产物被改动，勿强行绕过校验"),
    "model_config_template_not_implemented": (
        "runtime",
        "配置模板未实现 — 该包声明的 model_config_template 不受支持",
        "升级网关或更换使用受支持模板的包版本"),
    "builtin_hook_error": (
        "runtime",
        "内置钩子失败 — 包安装的钩子阶段出错",
        _GENERIC_ACTION),
    "runtime_error": (
        "runtime",
        "运行时准备失败 — 沙箱/运行时装配出错",
        _GENERIC_ACTION),
    "config_error": (
        "runtime",
        "受管配置写入失败",
        "检查 models.d 目录的磁盘空间/权限后重试"),
    "healthcheck_error": (
        "runtime",
        "隔离健康检查未通过 — 包在本机自检失败",
        _GENERIC_ACTION),
    "reload_error": (
        "runtime",
        "网关热加载失败 — 包已就位但未进入 /models",
        "执行 ./iff reconcile 或重启网关后确认 /models 出现该模型"),
    "internal_error": (
        "runtime",
        "内部错误 — 安装线程出现未预期异常",
        _GENERIC_ACTION),
    # ── 启动复核（managed_runtime.check_start / PackRuntimeError）──
    "ownership_conflict": (
        "conflict",
        "所有权冲突 — 模型目录由能力包受管，与磁盘实际状态不符",
        "重新安装该包以恢复一致的受管状态；勿手工改动受管目录"),
    "runtime_identity_mismatch": (
        "conflict",
        "运行时身份不匹配 — 受管包的运行时与清单不符",
        "重新安装该包以恢复清单一致的运行时"),
    # ── 路由 ──
    "unknown_pack_id": (
        "not_found",
        "未知能力包 — 目录中没有该 pack_id",
        "在能力包目录中确认名称（注意大小写与拼写）"),
}


def hint_for(code) -> dict:
    """reason code → {"kind", "message", "action"}；未知 code 回退 unknown。"""
    entry = _HINTS.get(code)
    if entry is None:
        return {"kind": "unknown",
                "message": f"未登记的失败原因：{code}",
                "action": _GENERIC_ACTION}
    kind, message, action = entry
    return {"kind": kind, "message": message, "action": action}


def parse_error_prefix(err) -> str | None:
    """installer 错误串前缀："download_error:URLError" → "download_error"。

    无 ":" 前缀形态（自然语句如参数门禁原文）→ None。
    """
    if not err:
        return None
    text = str(err)
    if ":" not in text:
        return None
    head = text.split(":", 1)[0].strip()
    return head or None


def classify_error(err) -> dict | None:
    """installer stage 失败错误串（或裸 reason code）→ hint；空 → None。

    前缀登记过 → 前缀的 hint；否则 unknown 回退并保留原文。
    """
    if not err:
        return None
    prefix = parse_error_prefix(err)
    if prefix is not None and prefix in _HINTS:
        return hint_for(prefix)
    return hint_for(str(err))
