"""能力包 admin API 门面 — PK1（Task 8）。

PackManager 聚合 Catalog/PolicyStore/PackStore/PackInstaller：App 只见
HTTP 契约，URL/hash 由后端提供（Catalog-only 安装入口，spec §6.1 裁决）。

HTTP 契约（handler.py 薄委托）：
  GET  /admin/packs/catalog / status / policy
  POST /admin/packs/install {"pack_id"} / policy
  旧 /admin/packs/download 原样保留（用户导入模型路径）。

安装事务在后台线程执行；同 pack_id 进行中 → 409（互斥由 installer 的
per-pack Lock + 本门面 in_progress 表共同保证）。
"""

import hashlib
import http.client
import json
import logging
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from inferfabric.packs.catalog import Catalog, load_catalog
from inferfabric.packs.custom import (
    SimpleArtifact, build_custom_manifest, find_reusable_engine,
    validate_custom_body, validate_gguf)
from inferfabric.packs.config_gen import (
    ConfigExistsError,
    build_model_config,
    write_managed_yaml,
)
from inferfabric.packs.installer import PackInstaller, system_available_memory_mb
from inferfabric.packs.policy import PackPolicy, PolicyStore
from inferfabric.packs.sandbox import NoopProvider, probe_sandbox
from inferfabric.packs.store import PackRecord, PackStore

log = logging.getLogger("inferfabric.packs")

POLICY_MEM_RANGE = (1024, 65536)

# 捆绑目录：pack_handlers.py 在 <repo>/inferfabric/proxy/，上溯三级到仓库根
# （生产冒烟回归：parents[1] 曾指错层级）
BUNDLED_CATALOG = Path(__file__).resolve().parents[2] / "packs_catalog.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DEFAULT_MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024 * 1024   # 32 GiB 硬上限


class _NoDowngradeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝 https→http 降级重定向（deferred #6b）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origin = str(getattr(req, "full_url", "") or "")
        if origin.startswith("https://") and \
                not str(newurl).startswith("https://"):
            raise urllib.error.URLError(
                f"refusing https->http redirect: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpArtifactFetcher:
    """生产 fetcher：HTTPS 流式下载到 dest（调用方保证 dest 为 .part）。

    尺寸硬上限防超预检 disk_mb 耗尽磁盘（deferred #6a）；Catalog-only 下
    URL/hash 后端冻结，完整性由下载后 sha256 校验兜底。
    """

    def __init__(self, chunk=1024 * 1024,
                 max_bytes=DEFAULT_MAX_DOWNLOAD_BYTES):
        self._chunk = chunk
        self._max_bytes = max_bytes
        self._opener = urllib.request.build_opener(
            _NoDowngradeRedirectHandler())

    def fetch(self, artifact, dest) -> None:
        # 断点续传（2026-11-16 生产取证）：ModelScope CDN 掐长连接，
        # ~2GB 级文件两连败于 TimeoutError。中断后带 Range 重试续传，
        # 上限 5 次；协议级错误（HTTPError）不重试。
        for attempt in range(6):
            try:
                self._fetch_once(artifact, dest)
                return
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException):
                # OSError 覆盖 socket.timeout(=TimeoutError)/ConnectionError/
                # URLError(网络层)；HTTPError 已在上方放行
                if attempt >= 5:
                    raise
                continue

    def _fetch_once(self, artifact, dest) -> None:
        offset = dest.stat().st_size if dest.exists() else 0
        req = urllib.request.Request(artifact.source, method="GET")
        if offset:
            req.add_header("Range", f"bytes={offset}-")
        written = offset
        with self._opener.open(req, timeout=60) as resp, \
                open(dest, "ab") as f:
            if offset and getattr(resp, "status", 206) != 206:
                # 服务器不支持 Range（返回 200 全量）→ 丢弃半截从头写
                written = 0
                f.seek(0)
                f.truncate()
            while True:
                chunk = resp.read(self._chunk)
                if not chunk:
                    break
                written += len(chunk)
                if written > self._max_bytes:
                    raise ValueError(
                        f"download exceeds limit {self._max_bytes} bytes")
                f.write(chunk)


class PackManager:
    def __init__(self, root, models_d, catalog_path=None, fetcher=None,
                 reload_gateway=None, prepare_runtime=None,
                 run_healthcheck=None):
        self.root = Path(root)
        self.models_d = Path(models_d)
        self.catalog = Catalog(load_catalog(catalog_path or BUNDLED_CATALOG))
        self.store = PackStore(self.root / "state.json")
        self.policy = PolicyStore(self.root / "policy.json")
        self._reload_gateway = reload_gateway
        self._fetcher = fetcher or HttpArtifactFetcher()
        self._in_progress: dict[str, str] = {}   # pack_id -> stage
        self._progress_lock = threading.Lock()
        from inferfabric.packs.runtime_prep import (
            healthcheck_llama_cpp as _hc_default,
            prepare_runtime as _prep_default)
        from inferfabric.packs.custom import CustomPackRegistry
        self._custom = CustomPackRegistry(self.root / "user_custom.json")
        self.installer = PackInstaller(
            root=self.root, fetcher=self._fetcher, policy=self.policy,
            sandbox_provider=NoopProvider(), catalog_probe=probe_sandbox,
            # 真实运行时准备/健康检查（2026-11-16 接线，fake 测试可注入替身）
            prepare_runtime=prepare_runtime or _prep_default,
            run_healthcheck=run_healthcheck or _hc_default,
            write_model_config=self._write_model_config,
            reload_gateway=self._reload_gateway,
            memory_available_mb=system_available_memory_mb,
            on_stage=self._on_install_stage)
        reconcile_on_boot(self.store, self.root)
        self._uninstaller = None    # 惰性：attach_uninstaller 注入网关侧回调
        self._disabled_models: set[str] = set()

    def attach_uninstaller(self, registry, disable_route_fn, stop_pack_fn,
                           is_alive_fn, pgid_fn=None, reload_fn=None) -> None:
        """网关侧回调注入（Task 12 集成时由 pm 侧装配）。

        registry 需 iter_candidates/purge_candidates（PoolRegistry 现有契约）。
        """
        from inferfabric.packs.uninstaller import PackUninstaller
        self._uninstaller = PackUninstaller(
            root=self.root, store=self.store, models_d=self.models_d,
            manifest_for=self._manifest_for, registry=registry,
            disable_route_fn=lambda name: (self._disabled_models.add(name),
                                           disable_route_fn(name))[-1],
            stop_pack_fn=stop_pack_fn, is_alive_fn=is_alive_fn,
            pgid_fn=pgid_fn, reload_fn=reload_fn or self._reload)

    def _manifest_for(self, pack_id: str):
        entry = self.catalog.find(pack_id)
        if entry is not None:
            return entry.manifest
        # 统一架构：自定义包也在能力级目录里（真 Manifest，可卸载可重试）
        rec = self._custom.get(pack_id)
        if rec is not None and rec.get("manifest"):
            from inferfabric.packs.schema import parse_manifest
            return parse_manifest(rec["manifest"])
        return None

    def preview_uninstall(self, pack_id: str) -> tuple[int, dict]:
        if self._uninstaller is None or self._manifest_for(pack_id) is None:
            return 404, {"error": f"unknown pack {pack_id!r}"}
        return 200, self._uninstaller.preview_uninstall(pack_id)

    def uninstall(self, pack_id: str, confirmed: bool = False) -> tuple[int, dict]:
        if self._uninstaller is None or self._manifest_for(pack_id) is None:
            return 404, {"error": f"unknown pack {pack_id!r}"}
        code, payload = self._uninstaller.uninstall(pack_id, confirmed=confirmed)
        if code == 200:
            self._disabled_models.discard(
                self._manifest_for(pack_id).model_id)
            # 自定义包：注册表同步移除（catalog 合并项随之消失）
            if self._custom.get(pack_id) is not None:
                self._custom.remove(pack_id)
        return code, payload

    def is_model_disabled(self, model_name: str) -> bool:
        """包卸载中/已完成的路由禁用查询 — 新请求 503 的判定依据。

        已接线 uninstaller 时委托之（单一事实源，deferred #2b）；
        未接线时退回本门面集合。
        """
        if self._uninstaller is not None:
            return self._uninstaller.is_route_disabled(model_name)
        return model_name in self._disabled_models

    # ── 钩子 ──

    def _write_model_config(self, pack_dir, manifest) -> None:
        port = _free_port()
        cfg = build_model_config(manifest, pack_dir, port=port)
        try:
            write_managed_yaml(cfg, self.models_d, manifest)
        except ConfigExistsError:
            log.info("pack config already exists: %s", manifest.pack_id)

    def _reload(self) -> None:
        if self._reload_gateway is not None:
            self._reload_gateway()

    def set_reload_gateway(self, fn) -> None:
        """网关装配时注入安装路径热加载回调。

        get_pack_manager 单例创建时不知网关回调（构造参数无从传入），
        必须由 attach_pack_uninstaller 在启动装配点注入 — 回归钉住
        2026-11-16 沙箱 e2e：单例从未接线 → 装包后 /models 无新模型。
        installer 持有独立引用，需同步更新。
        """
        self._reload_gateway = fn
        self.installer.reload_gateway = fn

    # ── 查询 ──

    def catalog_payload(self) -> dict:
        packs = [{
            "pack_id": e.manifest.pack_id,
            "display_name": e.display_name,
            "summary": e.summary,
            "tags": list(e.tags),
            "capability": e.manifest.capability,
            "builtin": e.builtin,
            "risk_level": e.risk_level,
            "manifest": e.manifest.to_dict(),
        } for e in self.catalog.entries]
        # 统一架构（2026-11-16）：用户自定义包与官方包同目录展示；
        # 校验链未走完的失败残项合成占位 manifest（可重试，错误在卡上可见）
        for m in self._custom.all().values():
            if m.get("manifest"):
                manifest = m["manifest"]
            else:
                manifest = {
                    "pack_id": m["pack_id"], "pack_version": "1.0.0",
                    "capability": m["capability"],
                    "model": {"id": m["name"], "parameter_count_b": 0,
                              "quantization": "", "license": "user-provided"},
                    "runtime": {"engine": "llama_cpp",
                                "runtime_template": "llama-cpp-macos-arm64-v1"},
                    "resources": {"download_mb": 0, "disk_mb": 0,
                                  "peak_memory_mb": 0, "gpu_role": "shared"},
                    "lifecycle": {"mode": "on_demand"},
                    "network": {"listen": "loopback",
                                "allowed_upstream": []},
                    "artifacts": [],
                    "execution": {"model_config_template": CUSTOM_TEMPLATE,
                                  "healthcheck": "openai_chat_ready_v1"},
                    "sandbox": {"policy": "verified-local-pack-v1",
                                "degraded_install_allowed": True}}
            packs.append({
                "pack_id": m["pack_id"],
                "display_name": m["name"],
                "summary": ("用户自定义能力包（自带下载链接，已过校验链）"
                            if m.get("manifest")
                            else "自定义包校验未通过 — 修正链接后重试"),
                "tags": ["custom"],
                "capability": m["capability"],
                "builtin": False,
                "risk_level": "experimental",
                "user_custom": True,
                "manifest": manifest,
            })
        return {"packs": packs}

    def status_payload(self) -> dict:
        packs = [self._rec_payload(r) for r in self.store.all()]
        with self._progress_lock:
            for pack_id, stage in self._in_progress.items():
                for p in packs:
                    if p["pack_id"] == pack_id:
                        # 进行中：阶段以实时进度为准；上次失败的 error/stage
                        # 不得泄漏（PK4 生产验收发现）
                        p["status"] = stage
                        p["stage"] = stage
                        p["error"] = None
                if all(p["pack_id"] != pack_id for p in packs):
                    packs.append({"pack_id": pack_id, "status": stage,
                                  "stage": stage, "error": None,
                                  "sandbox_degraded": False,
                                  "install_path": None,
                                  "pack_version": "", "installed_at": None})
        return {"packs": packs}

    def _rec_payload(self, rec) -> dict:
        # 安装路径可见性（2026-11-16 用户裁决）：已装包展示预设安装位置
        # root/<pack_id>/<version>（与 installer.pack_dir_for 同一拼法）
        install_path = None
        if rec.status == "installed":
            install_path = str(self.root / rec.pack_id / rec.pack_version)
        d = {"pack_id": rec.pack_id, "pack_version": rec.pack_version,
             "status": rec.status, "stage": rec.stage, "error": rec.error,
             "sandbox_degraded": rec.sandbox_degraded,
             "install_path": install_path,
             "installed_at": rec.installed_at, "affected_pools": []}
        return d

    # ── 安装 ──

    def _on_install_stage(self, pack_id: str, stage: str) -> None:
        """installer 进度回调：实时推进 _in_progress（App 阶段文案数据源）。
        不在进行中的包不复活条目（卸载/完成后回调迟到）。"""
        with self._progress_lock:
            if pack_id in self._in_progress:
                self._in_progress[pack_id] = stage

    def start_install(self, pack_id: str) -> tuple[int, dict]:
        entry = self.catalog.find(pack_id)
        if entry is None:
            # 自定义包失败重试：从注册表重启下载（同一 URL，同一校验链）
            rec = self._custom.get(pack_id)
            if rec is not None:
                return self._retry_custom(rec)
            return 404, {"error": f"unknown pack_id {pack_id!r}"}
        rec = self.store.get(pack_id)
        if rec is not None and rec.status == "installed":
            # 终审 F1：失败的重装不得摧毁已装工作包 — 已装包重装一律拒绝
            return 409, {"error": f"pack {pack_id!r} already installed; "
                                  f"uninstall first to reinstall"}
        with self._progress_lock:
            if pack_id in self._in_progress:
                return 409, {"error": f"install already in progress for "
                                      f"{pack_id!r}"}
            self._in_progress[pack_id] = "preflight"
        t = threading.Thread(target=self._run_install, args=(entry,),
                             daemon=True, name=f"pack-install-{pack_id}")
        t.start()
        return 202, {"pack_id": pack_id, "status": "preflight"}

    def _run_install(self, entry) -> None:
        pack_id = entry.manifest.pack_id
        try:
            res = self.installer.install(entry)
        except Exception as exc:   # 不让线程带异常消失
            res_ok, stage, err = False, "failed", \
                f"internal_error:{type(exc).__name__}"
        else:
            res_ok, stage, err = res.ok, res.stage, res.error
        import time
        now = int(time.time())
        try:
            if res_ok:
                self.store.set(PackRecord(
                    pack_id=pack_id, pack_version=entry.manifest.pack_version,
                    status="installed", stage="installed", error=None,
                    sandbox_degraded=res.sandbox_degraded, installed_at=now))
            else:
                self.store.set(PackRecord(
                    pack_id=pack_id, pack_version=entry.manifest.pack_version,
                    status="failed", stage=stage, error=err,
                    sandbox_degraded=False, installed_at=None))
        except Exception:
            # 终审 F4：簿记崩溃不得让线程带异常消失、pack_id 永久 409
            log.exception("pack install bookkeeping failed for %s", pack_id)
        finally:
            with self._progress_lock:
                self._in_progress.pop(pack_id, None)
        self._reload()

    # ── 用户扩展能力包（统一架构裁决 2026-11-16） ──

    def start_custom_install(self, body) -> tuple[int, dict]:
        """用户只提交 name/url/capability/sha256?；校验链在后台线程执行。"""
        try:
            name, url, capability, sha256 = validate_custom_body(body)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        pack_id = f"{capability}.{name}"   # schema 硬约束：前缀==capability
        existing = self._custom.get(pack_id)
        if self.catalog.find(pack_id) is not None \
                or (existing is not None and existing.get("manifest")) \
                or (self.store.get(pack_id) is not None
                    and self.store.get(pack_id).status == "installed"):
            return 409, {"error": f"pack {pack_id!r} already exists; "
                                  f"uninstall first to reinstall"}
        # existing 无 manifest = 上次校验链未走完（失败残留）→ 允许覆盖重装
        # （修 URL/参数后直接重新提交，无需先卸载）
        engine = find_reusable_engine(self.root)
        if engine is None:
            return 400, {"error": "no_llama_cpp_engine: 请先安装一个 "
                                  "llama.cpp 框架能力包（如轻量对话/嵌入），"
                                  "自定义包复用其引擎做试加载"}
        with self._progress_lock:
            if pack_id in self._in_progress:
                return 409, {"error": f"install already in progress for "
                                      f"{pack_id!r}"}
            self._in_progress[pack_id] = "downloading"
        t = threading.Thread(target=self._run_custom_install, args=(
            {"pack_id": pack_id, "name": name, "url": url,
             "capability": capability, "sha256": sha256}, None),
            daemon=True, name=f"pack-custom-{pack_id}")
        t.start()
        return 202, {"pack_id": pack_id, "status": "downloading"}

    def _fail_custom(self, rec: dict, stage: str, err: str,
                     manifest=None) -> None:
        self.store.set(PackRecord(
            pack_id=rec["pack_id"],
            pack_version=manifest.pack_version if manifest else "1.0.0",
            status="failed", stage=stage, error=err,
            sandbox_degraded=False, installed_at=None))
        # 失败也要可重试：注册表先记用户输入（manifest 可为 None，重试时重建）
        if self._custom.get(rec["pack_id"]) is None:
            self._custom.add({k: rec.get(k) for k in
                              ("pack_id", "name", "url", "sha256",
                               "capability")} | {"manifest": None,
                                                "installed_at": None})
        # 零残留：staging 与包目录全清
        shutil.rmtree(self.root / "_staging" / rec["pack_id"],
                      ignore_errors=True)
        if manifest is not None:
            shutil.rmtree(self.root / manifest.pack_id / manifest.pack_version,
                          ignore_errors=True)

    def _retry_custom(self, rec: dict) -> tuple[int, dict]:
        pack_id = rec["pack_id"]
        cur = self.store.get(pack_id)
        if cur is not None and cur.status == "installed":
            return 409, {"error": f"pack {pack_id!r} already installed; "
                                  f"uninstall first to reinstall"}
        if find_reusable_engine(self.root) is None:
            return 400, {"error": "no_llama_cpp_engine: 无可用 llama.cpp 引擎"}
        with self._progress_lock:
            if pack_id in self._in_progress:
                return 409, {"error": f"install already in progress for "
                                      f"{pack_id!r}"}
            self._in_progress[pack_id] = "downloading"
        threading.Thread(target=self._run_custom_install,
                         args=(rec, rec.get("manifest")), daemon=True,
                         name=f"pack-custom-retry-{pack_id}").start()
        return 202, {"pack_id": pack_id, "status": "downloading"}

    def _run_custom_install(self, rec: dict, manifest_dict: dict | None):
        """校验链：下载→sha256/GGUF→磁盘门禁→真引擎试加载→受控 YAML→注册。

        manifest_dict 非空 = 失败重试（manifest 已知，跳过重建）。
        """
        pack_id = rec["pack_id"]
        manifest = None
        try:
            staging = self.root / "_staging" / pack_id
            staging.mkdir(parents=True, exist_ok=True)
            part = staging / "model.gguf.part"

            self._on_install_stage(pack_id, "downloading")
            self._fetcher.fetch(SimpleArtifact("model", "model", rec["url"]),
                                part)

            self._on_install_stage(pack_id, "verifying")
            size_mb = part.stat().st_size // (1024 * 1024)
            actual = hashlib.sha256(part.read_bytes()).hexdigest()
            if rec.get("sha256") and rec["sha256"] != actual:
                raise ValueError(f"sha256_mismatch: 期望 {rec['sha256']}，"
                                 f"实际 {actual} — 下载源内容不符")
            validate_gguf(part)
            if manifest_dict is not None:
                from inferfabric.packs.schema import parse_manifest
                manifest = parse_manifest(manifest_dict)
            else:
                manifest = build_custom_manifest(
                    rec["name"], rec["capability"], rec["url"],
                    rec.get("sha256"), size_mb)
            free = shutil.disk_usage(self.root).free
            if free < part.stat().st_size * 1.2:
                raise ValueError(
                    f"insufficient_disk: 需要约 "
                    f"{part.stat().st_size // (1024*1024)}MB，"
                    f"磁盘仅余 {free // (1024*1024)}MB")

            pack_dir = self.root / manifest.pack_id / manifest.pack_version
            (pack_dir / "models").mkdir(parents=True, exist_ok=True)
            shutil.move(str(part), str(pack_dir / "models" / "model"))
            # 自定义包无引擎 artifact：复用已装包引擎（runtime 整目录符号链接
            # — rpath @loader_path 要求 engine 与 dylib 同目录，链接同时
            # 满足健康检查与服务期 binary_path）
            runtime_dir = pack_dir / "runtime"
            if not runtime_dir.exists():
                engine = find_reusable_engine(self.root)
                if engine is None:
                    raise ValueError("no_llama_cpp_engine: 引擎在安装中途消失")
                runtime_dir.symlink_to(engine.parent,
                                       target_is_directory=True)

            self._on_install_stage(pack_id, "isolated_healthcheck")
            # 真引擎试加载 = 框架可运行性的最终裁决（内存不够/格式坏在此暴露）
            self.installer._run_healthcheck(pack_dir, manifest)

            self._on_install_stage(pack_id, "writing_config")
            cfg = build_model_config(manifest, pack_dir, port=_free_port())
            write_managed_yaml(cfg, self.models_d, manifest)

            self._custom.add({
                "pack_id": pack_id, "name": rec["name"], "url": rec["url"],
                "sha256": actual, "capability": rec["capability"],
                "manifest": manifest.to_dict(),
                "installed_at": int(time.time())})
            self.store.set(PackRecord(
                pack_id=pack_id, pack_version=manifest.pack_version,
                status="installed", stage="installed", error=None,
                sandbox_degraded=False, installed_at=int(time.time())))
            shutil.rmtree(staging, ignore_errors=True)
        except Exception as exc:
            import time as _t
            try:
                self._fail_custom(rec, "failed",
                                  str(exc) or type(exc).__name__,
                                  manifest)
            except Exception:
                log.exception("custom pack failure bookkeeping: %s", pack_id)
        finally:
            with self._progress_lock:
                self._in_progress.pop(pack_id, None)
        self._reload()

    # ── 策略 ──

    def policy_get(self) -> tuple[int, dict]:
        p = self.policy.load()
        return 200, {"max_model_parameters_b": p.max_model_parameters_b,
                     "system_reserved_memory_mb": p.system_reserved_memory_mb}

    def policy_put(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "body must be an object"}
        try:
            cur = self.policy.load()
            p = PackPolicy(
                max_model_parameters_b=int(body.get(
                    "max_model_parameters_b", cur.max_model_parameters_b)),
                system_reserved_memory_mb=int(body.get(
                    "system_reserved_memory_mb",
                    cur.system_reserved_memory_mb)))
        except (TypeError, ValueError):
            return 400, {"error": "policy values must be integers"}
        if p.max_model_parameters_b < 1:
            return 400, {"error": "max_model_parameters_b must be >= 1"}
        lo, hi = POLICY_MEM_RANGE
        if not lo <= p.system_reserved_memory_mb <= hi:
            return 400, {"error": f"system_reserved_memory_mb must be in "
                                  f"[{lo}, {hi}] MiB"}
        self.policy.save(p)
        return 200, {"max_model_parameters_b": p.max_model_parameters_b,
                     "system_reserved_memory_mb": p.system_reserved_memory_mb}


def attach_pack_uninstaller(pm) -> None:
    """生产接线（PK4 T1，闭合终审 F5）：网关启动时注入真实回调。

    - registry: pm.pool_registry（iter_candidates/purge_candidates 现有契约）
    - stop_pack_fn: 模型名 → 先 stop_independent 后 stop_service
    - is_alive_fn(names): 与 pm.mgr.status()["active_services"] 求交集；
      status 异常吞掉返回 False（诚实降级：宁可不 drain，不可谎报）
    - pgid_fn: None — 生产未建 pgid 追踪，drain-only，绝不盲杀
    - reload_fn: pm.mgr.reload_models()
    同时挂 pm.pack_manager 供请求路径 503 检查（is_model_disabled）。
    """
    mgr = get_pack_manager()
    # 安装路径热加载同源注入（卸载链已有 reload_fn；安装链此前静默空转）
    mgr.set_reload_gateway(lambda: pm.mgr.reload_models())

    def stop_pack(name: str) -> None:
        try:
            pm.mgr.stop_independent(name)
        except Exception:
            pm.mgr.stop_service(name)

    def is_alive(names) -> bool:
        try:
            active = set(pm.mgr.status().get("active_services", []))
        except Exception:
            return False
        return bool(active & set(names))

    mgr.attach_uninstaller(
        registry=getattr(pm, "pool_registry", None),
        disable_route_fn=lambda name: None,   # 禁用状态在 PackManager 禁用集
        stop_pack_fn=stop_pack, is_alive_fn=is_alive,
        pgid_fn=None, reload_fn=lambda: pm.mgr.reload_models())
    pm.pack_manager = mgr


def reconcile_on_boot(store: PackStore, root) -> list[str]:
    from inferfabric.packs.store import reconcile
    fixed = reconcile(store, root)
    if fixed:
        log.warning("pack store reconciled on boot: %s", fixed)
    return fixed


# ── 进程级单例（handler 使用；测试经 reset_pack_manager 重置） ──

_manager: PackManager | None = None
_manager_lock = threading.Lock()


def get_pack_manager(catalog_path=None) -> PackManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            import os
            from inferfabric.config import IFF_DATA_DIR, MODELS_DIR
            state_dir = Path(os.environ.get(
                "IFF_STATE_DIR",
                os.environ.get("MACFABRIC_STATE_DIR", str(IFF_DATA_DIR))))
            models_dir = Path(os.environ.get(
                "IFF_MODELS_DIR",
                os.environ.get("MACFABRIC_MODELS_DIR", str(MODELS_DIR))))
            _manager = PackManager(root=state_dir / "packs",
                                   models_d=models_dir,
                                   catalog_path=catalog_path)
        return _manager


def reset_pack_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None
