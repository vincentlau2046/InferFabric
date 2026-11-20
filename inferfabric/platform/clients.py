"""platform.clients — Connect 配置事务（F10，M3 Task 1）。

把本机 CLI 客户端（claude / codex）的配置文件安全地指向本网关：
文件事务（revision CAS、备份、原子替换、回退）全部在本模块，HTTP 层
（Task 2 connect_handlers）只做门禁与转发，Dashboard（Task 3）只做展示。

设计裁决（计划 Task 1 + Review Focus 2）：
- 固定目标，无路径参数：claude = <home>/.claude/settings.json；
  codex = <codex_home 显式给定> / <home>/.codex 下的 config.toml。
  anchor 本身是可信服务输入；anchor 之下的每个路径成分都拒绝符号链接。
- revision = 目标文件字节 sha256（缺失为 "absent"）；apply/rollback 前核对
  expected_revision，外部编辑一律冲突拒绝，绝不覆盖。
- 备份先于替换持久化到 state_dir（0700 目录 / 0600 文件），记录
  pre/post revision；重启后按 revision 识别未完成操作，识别不了报冲突。
  重复托管保留首次原件，不生成备份链。
- 写入 = 同目录临时文件（0600）+ flush/fsync + os.replace + 目录 fsync。
- 错误消息只含 client 名与原因，绝不拼接配置内容或 key。
- 托管字段：claude env.ANTHROPIC_BASE_URL / env.ANTHROPIC_AUTH_TOKEN；
  codex model_provider="inferfabric" + model_providers.inferfabric 表。
  其余内容（无关 env、注释、默认 model、其他表）原样保留（codex 走
  tomlkit 保格式；JSON 重排由回退还原字节兜底）。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path

try:  # tomlkit 是 requirements 声明依赖；缺失只在 codex 操作时可见
    import tomlkit
except ImportError:  # pragma: no cover
    tomlkit = None

__all__ = ["ConnectManager", "ConnectError"]

CLIENTS = ("claude", "codex")
_ABSENT = "absent"
_NONREGULAR = "nonregular"
_ERROR_CODES = frozenset(
    {"invalid_request", "unavailable", "conflict", "io_error"})

_CODING_PROVIDER = "inferfabric"

# 评审 C1：threaded/async server 每请求各自 new 一个 manager，实例锁锁不住
# 跨实例并发 — 事务串行按 client 用模块级锁，进程内所有实例共享。
_MODULE_LOCKS = {c: threading.Lock() for c in CLIENTS}


class ConnectError(Exception):
    """固定 code 的 Connect 失败；消息不含配置内容。"""

    def __init__(self, code: str, message: str):
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown ConnectError code: {code}")
        super().__init__(message)
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError("duplicate key in JSON object")
        seen.add(key)
    return dict(pairs)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_new(path: Path, data: bytes) -> None:
    """创建新 0600 文件并 fsync（评审 C2：O_EXCL — 绝不截断既有文件；
    已存在时仅收紧权限，内容保持首次原件）。"""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_private(path: Path, data: bytes) -> None:
    """原子替换 0600 文件（评审 C2：record.json 不许原地截断重写）。"""
    tmp = path.parent / f".{path.name}.tmp-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConnectManager:
    """两种客户端的 inspect / apply / rollback 文件事务。"""

    def __init__(self, *, home: Path, state_dir: Path, gateway_origin: str,
                 codex_home: Path | None = None):
        self._home = Path(home)
        self._codex_home = Path(codex_home) if codex_home is not None else None
        self._origin = gateway_origin
        self._backup_root = Path(state_dir) / "connect"

    # ── 公共接口 ─────────────────────────────────────────────────

    def inspect(self, client: str) -> dict:
        self._check_client(client)
        with self._lock(client):
            return self._inspect_locked(client)

    def apply(self, client: str, *, expected_revision: str,
              api_key: str) -> dict:
        self._check_client(client)
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ConnectError("invalid_request", f"{client}: missing expected_revision")
        if not isinstance(api_key, str) or not api_key:
            raise ConnectError("invalid_request", f"{client}: missing api_key")
        with self._lock(client):
            return self._apply_locked(client, expected_revision, api_key)

    def rollback(self, client: str, *, expected_revision: str) -> dict:
        self._check_client(client)
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ConnectError("invalid_request", f"{client}: missing expected_revision")
        with self._lock(client):
            return self._rollback_locked(client, expected_revision)

    # ── 路径与安全检查 ───────────────────────────────────────────

    def _check_client(self, client: str) -> None:
        if client not in CLIENTS:
            raise ConnectError("invalid_request", f"unknown client: {client!r}")

    def _lock(self, client: str) -> threading.Lock:
        # 模块级锁：跨实例（threaded/async 各自 new 的 manager）同 client 串行
        return _MODULE_LOCKS[client]

    def _paths(self, client: str) -> tuple[Path, tuple[str, ...]]:
        """返回 (anchor, 相对成分)；anchor 是可信服务输入，不检查自身符号链接。"""
        if client == "claude":
            return self._home, (".claude", "settings.json")
        if self._codex_home is not None:
            return self._codex_home, ("config.toml",)
        return self._home, (".codex", "config.toml")

    def _target(self, client: str) -> Path:
        anchor, parts = self._paths(client)
        return anchor.joinpath(*parts)

    def _walk_dirs(self, client: str):
        """逐个 lstat anchor 与中间目录成分。

        返回现有目录列表；遇到缺失成分返回（现有列表, 缺失起点, None）。
        符号链接 / 非目录 / 非本服务用户属主 → conflict。
        """
        anchor, parts = self._paths(client)
        existing: list[Path] = []
        cur = anchor
        euid = os.geteuid()
        chain = (anchor,) + tuple(anchor.joinpath(*parts[:i + 1])
                                  for i in range(len(parts) - 1))
        for i, d in enumerate(chain):
            try:
                st = d.lstat()
            except FileNotFoundError:
                return existing, i, None
            except OSError as exc:
                raise ConnectError("io_error", f"{client}: cannot inspect path") from exc
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ConnectError(
                    "conflict", f"{client}: configuration directory is not a safe directory")
            if st.st_uid != euid:
                raise ConnectError(
                    "conflict", f"{client}: configuration directory not owned by service user")
            existing.append(d)
            cur = d
        return existing, None, cur

    def _require_dirs(self, client: str) -> None:
        """评审 I4：只在已存在的客户端目录下写配置 — 目录链缺失即 conflict，
        拒绝路径零副作用（不建目录、不写状态）。"""
        _, missing_from, _ = self._walk_dirs(client)
        if missing_from is not None:
            raise ConnectError(
                "conflict",
                f"{client}: client directory does not exist — start the "
                f"client once, then connect")

    def _read_target(self, client: str, target: Path) -> tuple[str, bytes | None]:
        """评审 C1：一次 lstat + read 同时取 revision 与字节（同一份已验证
        文件）；缺失 → ("absent", None)；符号链接/非常规 → ("nonregular", None)。"""
        try:
            st = target.lstat()
        except FileNotFoundError:
            return _ABSENT, None
        except OSError:
            return _NONREGULAR, None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return _NONREGULAR, None
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ConnectError(
                "io_error", f"{client}: cannot read configuration") from exc
        return _sha256(data), data

    def _recheck(self, client: str, target: Path, cur: str) -> None:
        """评审 C1：提交前重验 — 构建/备份期间目标被外部改动 → conflict，
        绝不覆盖（合同不承诺锁住外部编辑器，但检测到即拒绝）。"""
        now = self._rev(target)
        if now != cur:
            raise ConnectError(
                "conflict",
                f"{client}: configuration changed during transaction, "
                f"re-check and retry")

    def _rev(self, target: Path) -> str:
        """目标文件 revision：缺失 "absent"；非常规文件 "nonregular"；否则 sha256。"""
        try:
            st = target.lstat()
        except FileNotFoundError:
            return _ABSENT
        except OSError:
            return _NONREGULAR
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return _NONREGULAR
        try:
            return _sha256(target.read_bytes())
        except OSError:
            return _NONREGULAR

    # ── 备份记录（state_dir 私有区）──────────────────────────────

    def _record_dir(self, client: str) -> Path:
        return self._backup_root / client

    def _load_record(self, client: str) -> dict | None:
        path = self._record_dir(client) / "record.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConnectError("io_error", f"{client}: cannot read backup record") from exc
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectError(
                "conflict", f"{client}: backup record unreadable") from exc
        for field in ("pre_revision", "post_revision", "original_exists"):
            if field not in rec:
                raise ConnectError(
                    "conflict", f"{client}: backup record incomplete")
        return rec

    def _original_bytes(self, client: str, rec: dict) -> bytes:
        if not rec.get("original_exists"):
            return b""
        try:
            data = (self._record_dir(client) / "original.bin").read_bytes()
        except OSError as exc:
            raise ConnectError("io_error", f"{client}: cannot read backup bytes") from exc
        # 评审 C2：回退前校验备份完整性 — 损坏备份绝不写回目标
        expect = rec.get("original_sha256")
        if expect and _sha256(data) != expect:
            raise ConnectError(
                "conflict", f"{client}: backup integrity check failed")
        return data

    def _save_record(self, client: str, *, pre: str, post: str,
                     prior_post: str | None, original_exists: bool,
                     original_bytes: bytes, original_mode: int | None,
                     write_original: bool) -> None:
        """备份先于替换持久化（评审 C2 修订）：
        - original.bin 只在首次创建（O_EXCL，绝不截断既有原件）；
        - record.json 原子替换，且带 prior_post（上一次托管 revision）—
          重应用在替换前中断时，cur == prior_post 仍识别为托管、可回退；
        - 新建目录时 fsync 父目录链，保证崩溃后目录项在场。"""
        d = self._record_dir(client)
        try:
            created = not d.exists()
            d.mkdir(parents=True, mode=0o700, exist_ok=True)
            # parents=True 中间层按 umask 生成，逐级收紧到 0700
            os.chmod(d, 0o700)
            os.chmod(self._backup_root, 0o700)
            if created:
                try:
                    _fsync_dir(self._backup_root.parent)
                    _fsync_dir(self._backup_root)
                except OSError:
                    pass  # 目录项 fsync 尽力；文件本体已 fsync
            if original_exists and write_original:
                _write_private_new(d / "original.bin", original_bytes)
            elif original_exists:
                try:
                    os.chmod(d / "original.bin", 0o600)
                except OSError:
                    pass
            rec = {
                "client": client,
                "pre_revision": pre,
                "post_revision": post,
                "prior_post": prior_post,
                "original_exists": original_exists,
                "original_mode": original_mode,
                "original_sha256": _sha256(original_bytes) if original_exists else None,
                "managed_origin": self._origin,
            }
            _replace_private(d / "record.json",
                             json.dumps(rec, indent=2).encode("utf-8"))
            _fsync_dir(d)
        except OSError as exc:
            raise ConnectError("io_error", f"{client}: cannot persist backup") from exc

    def _clear_record(self, client: str) -> None:
        d = self._record_dir(client)
        for name in ("record.json", "original.bin"):
            try:
                (d / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ConnectError("io_error", f"{client}: cannot clear backup") from exc
        try:
            d.rmdir()
        except OSError:
            pass

    # ── 原子写入 ─────────────────────────────────────────────────

    def _atomic_write(self, client: str, target: Path, data: bytes,
                      mode: int, expect_rev: str | None = None) -> None:
        tmp = target.parent / f".{target.name}.connect-tmp-{secrets.token_hex(6)}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, mode)
            if expect_rev is not None and self._rev(target) != expect_rev:
                # 评审 C1：替换前最后一刻重验（窗口收窄到一次 hash + replace）
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise ConnectError(
                    "conflict",
                    f"{client}: configuration changed during transaction, "
                    f"re-check and retry")
            os.replace(tmp, target)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ConnectError("io_error", f"{client}: cannot write configuration") from exc
        try:
            _fsync_dir(target.parent)
        except OSError as exc:
            raise ConnectError("io_error", f"{client}: cannot sync directory") from exc

    # ── 内容构造 ─────────────────────────────────────────────────

    def _read_current(self, client: str, target: Path) -> bytes | None:
        """当前文件字节；缺失返回 None。非常规文件在 _rev 已拦截。"""
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConnectError("io_error", f"{client}: cannot read configuration") from exc

    def _build_claude(self, client: str, current: bytes | None,
                      api_key: str) -> bytes:
        if current is None:
            data: dict = {}
        else:
            try:
                data = json.loads(current.decode("utf-8"),
                                  object_pairs_hook=_reject_duplicate_keys)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ConnectError(
                    "conflict", f"{client}: settings.json is not valid JSON") from exc
            if not isinstance(data, dict):
                raise ConnectError("conflict", f"{client}: settings.json is not an object")
        env = data.get("env")
        if env is None:
            env = {}
            data["env"] = env
        if not isinstance(env, dict):
            raise ConnectError("conflict", f"{client}: env is not an object")
        if "ANTHROPIC_API_KEY" in env:
            raise ConnectError(
                "conflict",
                f"{client}: existing ANTHROPIC_API_KEY must be removed first")
        env["ANTHROPIC_BASE_URL"] = self._origin
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def _build_codex(self, client: str, current: bytes | None,
                     api_key: str) -> bytes:
        if tomlkit is None:  # pragma: no cover
            raise ConnectError("unavailable", f"{client}: tomlkit not installed")
        if current is None:
            doc = tomlkit.document()
        else:
            try:
                doc = tomlkit.parse(current.decode("utf-8"))
            except Exception as exc:
                raise ConnectError(
                    "conflict", f"{client}: config.toml is not valid TOML") from exc
        providers = doc.get("model_providers")
        if providers is None:
            providers = tomlkit.table()
            doc["model_providers"] = providers
        if not isinstance(providers, dict):
            raise ConnectError(
                "conflict", f"{client}: model_providers is not a table")
        existing = providers.get(_CODING_PROVIDER)
        if existing is not None:
            base = existing.get("base_url") if isinstance(existing, dict) else None
            if base != self._origin + "/v1":
                raise ConnectError(
                    "conflict",
                    f"{client}: reserved provider name '{_CODING_PROVIDER}' already in use")
        prov = existing if isinstance(existing, dict) else tomlkit.table()
        prov["name"] = "InferFabric"
        prov["base_url"] = self._origin + "/v1"
        prov["wire_api"] = "responses"
        prov["requires_openai_auth"] = False
        prov["experimental_bearer_token"] = api_key
        if existing is None:
            providers[_CODING_PROVIDER] = prov
        doc["model_provider"] = _CODING_PROVIDER
        return tomlkit.dumps(doc).encode("utf-8")

    def _build(self, client: str, current: bytes | None, api_key: str) -> bytes:
        if client == "claude":
            return self._build_claude(client, current, api_key)
        return self._build_codex(client, current, api_key)

    # ── 事务本体 ─────────────────────────────────────────────────

    @staticmethod
    def _managed_revs(rec: dict) -> frozenset:
        """记录在案、可识别为托管内容的 revision 集（含 prior_post —
        评审 C2：重应用中断后旧托管内容仍可识别、可回退）。"""
        revs = {rec["post_revision"]}
        if rec.get("prior_post"):
            revs.add(rec["prior_post"])
        return frozenset(revs)

    def _inspect_locked(self, client: str) -> dict:
        target = self._target(client)
        rev = self._rev(target)
        rec = self._load_record(client)
        if rec is not None:
            if rev in self._managed_revs(rec):
                tgt = ("this_gateway"
                       if rec.get("managed_origin") == self._origin else "other")
                return {"client": client, "state": "managed",
                        "target": tgt, "revision": rev}
            if rev != rec["pre_revision"]:
                # 备份在但内容既非写入后也非原件：外部改动，识别不了 → 冲突
                return {"client": client, "state": "conflict",
                        "target": "unknown", "revision": rev}
            # rev == pre：apply 未落地或回退已写回 → 按原始状态报告
        # 原始状态分类（无记录，或记录对应操作未落地）
        try:
            self._walk_dirs(client)   # symlink/属主异常 → conflict
        except ConnectError:
            return {"client": client, "state": "conflict",
                    "target": "unknown", "revision": rev}
        if rev == _ABSENT:
            anchor, parts = self._paths(client)
            state = "missing_directory" if not anchor.exists() or not self._dir_exists(anchor, parts) else "missing_file"
            return {"client": client, "state": state,
                    "target": "unset", "revision": rev}
        if rev == _NONREGULAR:
            return {"client": client, "state": "conflict",
                    "target": "unknown", "revision": rev}
        current = self._read_current(client, target)
        return {"client": client, "state": "unmanaged",
                "target": self._classify(client, current), "revision": rev}

    @staticmethod
    def _dir_exists(anchor: Path, parts: tuple[str, ...]) -> bool:
        return anchor.joinpath(*parts[:-1]).is_dir()

    def _classify(self, client: str, current: bytes | None) -> str:
        """仅分类，不回显原值；解析失败按 unknown（状态仍是 unmanaged）。"""
        if current is None:
            return "unset"
        try:
            if client == "claude":
                data = json.loads(current.decode("utf-8"))
                env = data.get("env") if isinstance(data, dict) else None
                url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
                if not url:
                    return "unset"
                return "this_gateway" if url == self._origin else "other"
            if tomlkit is None:  # pragma: no cover
                return "unknown"
            doc = tomlkit.parse(current.decode("utf-8"))
            provider = doc.get("model_provider")
            if provider == _CODING_PROVIDER:
                providers = doc.get("model_providers")
                base = None
                if isinstance(providers, dict):
                    entry = providers.get(_CODING_PROVIDER)
                    if isinstance(entry, dict):
                        base = entry.get("base_url")
                return ("this_gateway"
                        if base == self._origin + "/v1" else "other")
            if provider:
                return "other"
            return "unset"
        except Exception:
            return "unknown"

    def _apply_locked(self, client: str, expected_revision: str,
                      api_key: str) -> dict:
        target = self._target(client)
        # 评审 I4：前置校验全部先行，拒绝路径零副作用（不建目录/不写状态）
        self._require_dirs(client)
        # 评审 C1：一次 lstat+read 同取 revision 与字节（同一份已验证文件）
        cur, current = self._read_target(client, target)
        if cur != expected_revision:
            raise ConnectError(
                "conflict",
                f"{client}: configuration changed since inspection, re-check and retry")
        rec = self._load_record(client)
        if rec is not None and (cur in self._managed_revs(rec)
                                or cur == rec["pre_revision"]):
            # 已托管（重复托管 / prior_post 中断态）或上次 apply 未落地：
            # 保留首次原件；prior_post 记录本次替换前的托管 revision
            original_exists = bool(rec["original_exists"])
            original_bytes = (self._original_bytes(client, rec)
                              if original_exists else b"")
            original_mode = rec.get("original_mode")
            pre = rec["pre_revision"]
            # prior_post = 当前磁盘上的托管 revision（评审 C2：重应用中断后
            # 再重试，中断态 revision 必须始终被下一份记录识别）
            prior_post = (cur if cur != rec["pre_revision"]
                          else rec.get("prior_post"))
            write_original = False
        else:
            if cur == _NONREGULAR:
                raise ConnectError(
                    "conflict", f"{client}: target is not a regular file")
            original_exists = cur != _ABSENT
            original_bytes = current or b""
            if original_exists:
                st = target.lstat()
                if st.st_uid != os.geteuid():
                    raise ConnectError(
                        "conflict",
                        f"{client}: configuration file not owned by service user")
                original_mode = stat.S_IMODE(st.st_mode)
            else:
                original_mode = None
            pre = cur
            prior_post = None
            write_original = original_exists
        new_bytes = self._build(client, current, api_key)
        post = _sha256(new_bytes)
        # 构建后重验 + 备份（含 pre/post/prior_post）先于替换持久化
        self._recheck(client, target, cur)
        self._save_record(client, pre=pre, post=post, prior_post=prior_post,
                          original_exists=original_exists,
                          original_bytes=original_bytes,
                          original_mode=original_mode,
                          write_original=write_original)
        self._atomic_write(client, target, new_bytes, 0o600, expect_rev=cur)
        return self._inspect_locked(client)

    def _rollback_locked(self, client: str, expected_revision: str) -> dict:
        target = self._target(client)
        rec = self._load_record(client)
        if rec is None:
            raise ConnectError(
                "invalid_request", f"{client}: no managed change to roll back")
        cur = self._rev(target)
        if cur != expected_revision:
            raise ConnectError(
                "conflict",
                f"{client}: configuration changed since inspection, re-check and retry")
        if cur in self._managed_revs(rec):
            if rec["original_exists"]:
                # 仅在确需写回原件时要求目录链在（拒绝路径零副作用）
                self._require_dirs(client)
                original = self._original_bytes(client, rec)  # 含完整性校验
                mode = rec.get("original_mode") or 0o600
                self._atomic_write(client, target, original, mode,
                                   expect_rev=cur)
            elif cur != _ABSENT:
                self._recheck(client, target, cur)
                try:
                    target.unlink()
                except OSError as exc:
                    raise ConnectError(
                        "io_error", f"{client}: cannot remove managed file") from exc
                try:
                    _fsync_dir(target.parent)
                except OSError as exc:
                    raise ConnectError(
                        "io_error", f"{client}: cannot sync directory") from exc
        elif cur != rec["pre_revision"]:
            raise ConnectError(
                "conflict",
                f"{client}: configuration is neither the managed nor the original state")
        # cur == pre：已在原始状态（上次 apply 未落地），仅清记录
        self._clear_record(client)
        return self._inspect_locked(client)
