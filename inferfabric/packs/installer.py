"""安装事务 — PK1（Task 5）。

状态机（spec §7.1）：
  preflight → staging → downloading → verifying → preparing_runtime
  → writing_config → isolated_healthcheck → gateway_reload → installed

事务规则：
- 所有下载/构建先放 <root>/_staging/<pack_id>/，.part 永不执行或注册。
- sha256 在 staging 内逐 artifact 校验后才移入正式目录（TOCTOU 安全：
  下载→校验→启用之间无窗口）。
- hash/运行时准备/健康检查/配置写入/reload 全部成功才 commit。
- 任一阶段失败：删 staging（及未提交的 pack_dir 与本次写入的受控 YAML），
  不动已安装包，不写半成品 models.d；错误为阶段化固定码，原文不带上游内容。
- 事务开始前已存在的正式目录在下载/校验失败阶段不得触碰；校验通过后才由
  本次事务接管（终审 F1）。
- 预检内存门禁（spec §9）：可用内存 - system_reserved_memory_mb <
  peak_memory_mb → 拒绝（终审 F3）。
- 上次崩溃残留的 staging 在下次安装开始时清理。
- 沙箱准入（spec §8.3）：探测 ≠ full 且 degraded_install_allowed=False
  → 拒绝；允许降级 → 安装继续并显式标记 sandbox_degraded。
"""

import hashlib
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from inferfabric.packs import policy as pack_policy_mod
from inferfabric.packs.config_gen import KNOWN_TEMPLATES

log = logging.getLogger(__name__)

INSTALL_STAGES = ("preflight", "staging", "downloading", "verifying",
                  "preparing_runtime", "writing_config",
                  "isolated_healthcheck", "gateway_reload", "installed")

# 升级回退窗口：新版本切换成功后保留的旧版本数（spec §7.4 裁决）
UPGRADE_KEEP_PREVIOUS = 1


@dataclass
class InstallResult:
    ok: bool
    stage: str
    error: str | None = None
    pack_dir: Path | None = None
    sandbox_degraded: bool = False


def _noop(*a, **k) -> None:
    return None


def system_available_memory_mb() -> int | None:
    """当前可用内存（MiB，MemAvailable 口径）；探不到返回 None（跳过门禁）。

    评审 PR#16 B1：委托平台层 probe.mem_available_mb—— packs 不直接碰
    OS 差异（SC_AVPHYS_PAGES 在 Linux ≈MemFree 不含可回收 page cache，
    生产机 preflight 必失败；F7 manifest「上游 core 不直接碰 OS 差异」）。
    """
    from inferfabric.platform.probe import mem_available_mb
    return mem_available_mb()


class PackInstaller:
    """安装事务执行器。root 由调用方注入（生产 ~/.inferfabric/packs，
    测试 tmp_path）；真实 fetcher/hook 在 Task 8/11/12 接线。"""

    def __init__(self, root: Path, fetcher, policy, sandbox_provider,
                 catalog_probe=None, prepare_runtime=None,
                 write_model_config=None, run_healthcheck=None,
                 reload_gateway=None, memory_available_mb=None,
                 on_stage=None):
        self._root = Path(root)
        self._fetcher = fetcher
        self._policy = policy
        self._sandbox_provider = sandbox_provider
        self._catalog_probe = catalog_probe
        self._prepare_runtime = prepare_runtime or _noop
        self._write_model_config = write_model_config or _noop
        self._run_healthcheck = run_healthcheck or _noop
        self._reload_gateway = reload_gateway or _noop
        # 进度上报：on_stage(pack_id, stage) 在各阶段起点触发（PK4 —
        # App 阶段文案数据源；异常吞掉，进度失败不得影响安装事务）
        self._on_stage = on_stage
        # 可用内存探测（callable → MiB 或 None）；None=门禁关闭（仅测试）。
        # 生产由 PackManager 注入 system_available_memory_mb（终审 F3）。
        self._memory_available_mb = memory_available_mb
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, pack_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(pack_id, threading.Lock())

    def _report(self, pack_id: str, stage: str) -> None:
        if self._on_stage is None:
            return
        try:
            self._on_stage(pack_id, stage)
        except Exception:
            log.exception("on_stage callback failed for %s", pack_id)

    # ── 目录布局 ──

    def _staging_dir(self, pack_id: str) -> Path:
        return self._root / "_staging" / pack_id

    def pack_dir_for(self, manifest) -> Path:
        return self._root / manifest.pack_id / manifest.pack_version

    def _artifact_rel(self, artifact) -> Path:
        if artifact.kind == "model":
            return Path("models") / artifact.id
        return Path("runtime") / artifact.id

    # ── 阶段 ──

    def _preflight(self, entry) -> tuple[bool, str | None, bool]:
        manifest = entry.manifest
        self._root.mkdir(parents=True, exist_ok=True)
        policy = self._policy.load()
        # 参数门禁复用 policy.check_param_gate（单一事实源，deferred #7）
        param_err = pack_policy_mod.check_param_gate(manifest, policy)
        if param_err:
            # 阶段 1 Task 3：同一准入路径的唯一例外是审核登记（精确
            # pack_id/version/模板/产物标识；manifest/自定义包/force 均不可触达）
            from inferfabric.packs.admission import reviewed_profile_for
            if reviewed_profile_for(manifest) is None:
                return False, param_err, False
        # 运行时模板门禁：未实现模板 preflight 即拒，早于下载（deferred #5）
        if manifest.model_config_template not in KNOWN_TEMPLATES:
            return False, (f"model_config_template_not_implemented:"
                           f"{manifest.model_config_template}"), False
        # 内存预算门禁（spec §7.1/§9）：可用内存 - 系统保留 < 峰值 → 拒绝
        if self._memory_available_mb is not None:
            avail = self._memory_available_mb()
            if avail is not None:
                budget = avail - policy.system_reserved_memory_mb
                if budget < manifest.peak_memory_mb:
                    return False, (f"peak_memory_mb {manifest.peak_memory_mb} "
                                   f"exceeds memory budget {budget}MB "
                                   f"(available {avail}MB - reserved "
                                   f"{policy.system_reserved_memory_mb}MB)"), False
        if not entry.builtin:
            free = shutil.disk_usage(self._root).free
            if free < manifest.disk_mb * 1024 * 1024:
                return False, (f"disk free {free // (1024*1024)}MB < "
                               f"required {manifest.disk_mb}MB"), False
        probe = (self._catalog_probe or _noop)()
        if probe is not None and probe.mode.value != "full" and \
                not manifest.degraded_install_allowed:
            return False, (f"sandbox mode {probe.mode.value} not allowed for "
                           f"degraded_install_allowed=False"), False
        degraded = probe is not None and probe.mode.value != "full"
        return True, None, degraded

    def _verify_hashes(self, manifest, base: Path) -> str | None:
        for artifact in manifest.artifacts:
            path = base / self._artifact_rel(artifact)
            if not path.exists():
                return f"artifact_missing:{artifact.id}"
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != artifact.sha256:
                return f"artifact_sha256_mismatch:{artifact.id}"
        return None

    def _move_into_place(self, manifest, staging: Path, pack_dir: Path):
        for artifact in manifest.artifacts:
            rel = self._artifact_rel(artifact)
            src = staging / rel
            dst = pack_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    # ── 主事务 ──

    def install(self, entry) -> InstallResult:
        manifest = entry.manifest
        with self._lock_for(manifest.pack_id):
            return self._install_locked(entry)

    def upgrade(self, entry) -> InstallResult:
        """升级 = 新版本目录走完整事务（含健康检查），全部成功才切换。

        spec §7.4 裁决：不做原地替换；失败时已装旧版不动、staging 清理；
        成功后仅保留 UPGRADE_KEEP_PREVIOUS 个旧版本作回退窗口。
        """
        manifest = entry.manifest
        with self._lock_for(manifest.pack_id):
            res = self._install_locked(entry)
            if not res.ok:
                return res
            pack_root = self._root / manifest.pack_id
            old_versions = sorted(
                d.name for d in pack_root.glob("*")
                if d.is_dir() and d.name != manifest.pack_version)
            excess = old_versions[:-UPGRADE_KEEP_PREVIOUS] if \
                len(old_versions) > UPGRADE_KEEP_PREVIOUS else []
            for old in excess:
                shutil.rmtree(pack_root / old, ignore_errors=True)
            return res

    def _install_locked(self, entry) -> InstallResult:
        manifest = entry.manifest
        staging = self._staging_dir(manifest.pack_id)
        pack_dir = self.pack_dir_for(manifest)
        sandbox_degraded = False
        written_configs: list[Path] = []   # 本次事务写入的受控 YAML（终审 F2）
        # 事务开始前已存在的正式目录（已装/unknown_origin 恢复）：下载/校验
        # 失败阶段不得触碰（终审 F1）；校验通过后由本次事务接管。
        pre_existing = pack_dir.exists()

        def remember_written(ret) -> None:
            if ret is None:
                return
            written_configs.extend(
                ret if isinstance(ret, (list, tuple)) else [ret])

        def fail(stage: str, error: str) -> InstallResult:
            for p in written_configs:
                try:
                    Path(p).unlink()
                except OSError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            if stage not in ("preflight",) and pack_dir.exists() and \
                    stage != "installed" and not pre_existing:
                # hook 失败：未提交，回滚未成形的正式目录（零残留：连空父目录）
                shutil.rmtree(pack_dir, ignore_errors=True)
                try:
                    pack_dir.parent.rmdir()  # 仅目录为空时成功
                except OSError:
                    pass
            return InstallResult(ok=False, stage=stage, error=error,
                                 sandbox_degraded=sandbox_degraded)

        # preflight
        self._report(manifest.pack_id, "preflight")
        ok, err, sandbox_degraded = self._preflight(entry)
        if not ok:
            return fail("preflight", err)
        if entry.builtin:
            shutil.rmtree(staging, ignore_errors=True)
            pack_dir.mkdir(parents=True, exist_ok=True)
            # spec §4「持久化解析后的模板/产物标识」：manifest.yaml 随安装
            # 事务写入正式包目录 —— ManagedRuntime 读回校验的数据源；随
            # fail() 的整目录回滚一起清理
            (pack_dir / "manifest.yaml").write_text(
                manifest.to_dict_yaml(), encoding="utf-8")
            # builtin 走同一阶段机（spec §7.1）：每阶段独立失败码，回滚
            # 语义与非 builtin 一致，gateway_reload 失败不得标 preparing_runtime
            self._report(manifest.pack_id, "preparing_runtime")
            try:
                self._prepare_runtime(pack_dir, manifest)
            except Exception as exc:
                return fail("preparing_runtime", f"builtin_hook_error:{exc}")
            self._report(manifest.pack_id, "writing_config")
            try:
                remember_written(self._write_model_config(pack_dir, manifest))
            except Exception as exc:
                return fail("writing_config", f"builtin_hook_error:{exc}")
            self._report(manifest.pack_id, "isolated_healthcheck")
            try:
                self._run_healthcheck(pack_dir, manifest)
            except Exception as exc:
                return fail("isolated_healthcheck", f"builtin_hook_error:{exc}")
            self._report(manifest.pack_id, "gateway_reload")
            try:
                self._reload_gateway()
            except Exception as exc:
                return fail("gateway_reload", f"builtin_hook_error:{exc}")
            return InstallResult(ok=True, stage="installed",
                                 pack_dir=pack_dir,
                                 sandbox_degraded=sandbox_degraded)

        # staging：先清上次崩溃残留
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        # downloading
        self._report(manifest.pack_id, "downloading")
        try:
            for artifact in manifest.artifacts:
                dest = staging / self._artifact_rel(artifact)
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_suffix(dest.suffix + ".part")
                self._fetcher.fetch(artifact, part)
                part.rename(dest)
        except Exception as exc:
            return fail("downloading", f"download_error:{type(exc).__name__}")

        # verifying（staging 内校验，通过后才移动）
        self._report(manifest.pack_id, "verifying")
        err = self._verify_hashes(manifest, staging)
        if err:
            return fail("verifying", err)

        # 移入正式目录（未提交：失败仍整体回滚）
        if pre_existing:
            # 校验已通过，本次事务接管该目录：先清旧内容再重建。此后回滚
            # 语义恢复"删未提交目录"；接管之前的失败不触碰旧目录。
            shutil.rmtree(pack_dir, ignore_errors=True)
            pre_existing = False
        pack_dir.mkdir(parents=True, exist_ok=True)
        self._move_into_place(manifest, staging, pack_dir)
        shutil.rmtree(staging, ignore_errors=True)
        # spec §4「持久化解析后的模板/产物标识」：manifest.yaml 随安装事务
        # 写入正式包目录（受管启动读回校验的数据源）；位于 pack_dir 内，
        # 随事务回滚与卸载一起清理
        (pack_dir / "manifest.yaml").write_text(manifest.to_dict_yaml(),
                                                encoding="utf-8")

        # preparing_runtime
        self._report(manifest.pack_id, "preparing_runtime")
        try:
            self._prepare_runtime(pack_dir, manifest)
        except Exception as exc:
            return fail("preparing_runtime", f"runtime_error:{exc}")

        # writing_config
        self._report(manifest.pack_id, "writing_config")
        try:
            remember_written(self._write_model_config(pack_dir, manifest))
        except Exception as exc:
            return fail("writing_config", f"config_error:{exc}")

        # isolated_healthcheck
        self._report(manifest.pack_id, "isolated_healthcheck")
        try:
            self._run_healthcheck(pack_dir, manifest)
        except Exception as exc:
            return fail("isolated_healthcheck", f"healthcheck_error:{exc}")

        # gateway_reload
        self._report(manifest.pack_id, "gateway_reload")
        try:
            self._reload_gateway()
        except Exception as exc:
            return fail("gateway_reload", f"reload_error:{exc}")

        # installed（commit）
        return InstallResult(ok=True, stage="installed", pack_dir=pack_dir,
                             sandbox_degraded=sandbox_degraded)
