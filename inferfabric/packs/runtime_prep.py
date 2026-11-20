"""能力包运行时准备 + 隔离健康检查 — PK4 真包链路（2026-11-16）。

取证背景：installer 的 prepare_runtime / run_healthcheck 钩子此前生产未
接线（默认 noop），真包装出的 runtime/engine 是未解包的 gzip，健康检查
形同虚设。本模块提供真实实现，由 PackManager 接线。

契约：
- prepare_runtime(pack_dir, manifest)：按 manifest.runtime_template 分发；
  未知模板 RuntimeError("runtime_template_not_implemented:…")（安装事务在
  preparing_runtime 阶段诚实失败，零残留由 installer fail() 兜底）。
- llama-cpp-macos-arm64-v1：runtime/engine 处的发行包 tar.gz 安全解包
  （拒绝绝对路径 / .. 成员）、摊平单层顶层目录、llama-server →
  runtime/engine（chmod 755）。rpath=@loader_path → 二进制必须与 dylib
  同目录，故全部成员摊平进 runtime/。解包后删除压缩包。
- healthcheck_llama_cpp(pack_dir, manifest, timeout_s=…)：真拉起 engine
  （-m 包内模型 --alias model_id），轮询 /health 至 200，任何退出路径
  都收割子进程；超时 RuntimeError。

安全纪律：tarfile 成员逐一校验（absolute/.. 拒绝）；解包先落
runtime/_prep-<pid> 临时目录，全部成员校验通过后再搬入 runtime/。
"""

import io
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ENGINE_NAME = "llama-server"

# 已实现运行时模板（与 config_gen.KNOWN_TEMPLATES 的模型模板正交：
# 那张表管 model_config_template，这里管 runtime_template）
# llama-cpp-linux-cpu-v1（Task 5 BGE CPU 试点）：与 Mac 模板同一解包路径
# —— 发行包 tar.gz → runtime/ 摊平；平台差异只影响二进制本身，不在解包。
_TEMPLATES = {"llama-cpp-macos-arm64-v1", "llama-cpp-linux-cpu-v1"}


def prepare_runtime(pack_dir, manifest) -> None:
    tpl = manifest.runtime_template
    if tpl not in _TEMPLATES:
        raise RuntimeError(f"runtime_template_not_implemented:{tpl}")
    _prep_llama_cpp(Path(pack_dir))


def _prep_llama_cpp(pack_dir: Path) -> None:
    gz = pack_dir / "runtime" / "engine"
    with open(gz, "rb") as f:
        magic = f.read(2)
    if magic != b"\x1f\x8b":
        raise RuntimeError("runtime artifact is not a gzip tarball "
                           f"({gz})")
    work = pack_dir / "runtime" / f"_prep-{os_getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        with tarfile.open(gz, "r:gz") as tf:
            members = tf.getmembers()
            _validate_members(members)
            # 成员已逐一校验（_validate_members）；filter="data" 钉死安全
            # 语义（py3.14 起默认，显式声明消 DeprecationWarning）
            tf.extractall(work, filter="data")
        gz.unlink()   # 数据已进 work；entry 二进制随后会占据 runtime/engine
        _flatten_and_place(work, pack_dir / "runtime")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _validate_members(members) -> None:
    for m in members:
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"unsafe tar member rejected: {name!r}")


def _flatten_and_place(work: Path, runtime: Path) -> None:
    entries = list(work.iterdir())
    src = entries[0] if len(entries) == 1 and entries[0].is_dir() else work
    files = [p for p in src.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError("runtime tarball contains no files")
    servers = [p for p in files if p.name == ENGINE_NAME]
    if not servers:
        raise RuntimeError(f"{ENGINE_NAME} not found in runtime tarball")
    for p in files:
        if p == servers[0]:
            continue
        dest = runtime / p.relative_to(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 发行包内 dylib 常为符号链接（libggml.1.dylib → …1.0.0.dylib）：
        # 链接移动后不 chmod（真实目标可能尚未搬入；链接权限无意义）
        if p.is_symlink():
            shutil.move(str(p), str(dest))
            continue
        is_exec = bool(p.stat().st_mode & 0o111)   # move 前取 mode（move 后源不存在）
        shutil.move(str(p), str(dest))
        dest.chmod(0o755 if is_exec else 0o644)
    engine = runtime / "engine"
    shutil.move(str(servers[0]), str(engine))
    engine.chmod(0o755)


def healthcheck_llama_cpp(pack_dir, manifest, timeout_s: int = 300) -> None:
    pack_dir = Path(pack_dir)
    engine = pack_dir / "runtime" / "engine"
    model_art = next(a for a in manifest.artifacts if a.kind == "model")
    model_path = pack_dir / "models" / model_art.id
    port = _free_port()
    proc = subprocess.Popen(
        [str(engine), "-m", str(model_path),
         "--host", "127.0.0.1", "--port", str(port),
         "--alias", manifest.model_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        deadline = time.time() + timeout_s
        url = f"http://127.0.0.1:{port}/health"
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"pack engine exited during healthcheck "
                    f"(code={proc.returncode})")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(
            f"pack engine healthcheck timeout after {timeout_s}s")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def os_getpid() -> int:
    import os
    return os.getpid()
