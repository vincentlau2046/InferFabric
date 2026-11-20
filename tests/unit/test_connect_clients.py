"""Task 1 — platform.clients.ConnectManager 配置事务（F10 Connect 页地基）。

契约（计划 Task 1 Interfaces）：
- ConnectManager(*, home, state_dir, gateway_origin, codex_home=None)
- inspect(client) -> {client, state, target, revision}；state ∈
  missing_directory|missing_file|unmanaged|managed|conflict；target 仅分类
  （this_gateway|other|unset|unknown），绝不回显原 URL/凭据。
- apply(client, *, expected_revision, api_key) / rollback(client, *, expected_revision)
  成功返回新 inspect 状态。
- ConnectError.code ∈ {invalid_request, unavailable, conflict, io_error}，
  错误消息不得拼接配置内容。
- Claude: ~/.claude/settings.json env.ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
- Codex: $CODEX_HOME/config.toml model_provider + model_providers.inferfabric
  （name/base_url/wire_api/requires_openai_auth/experimental_bearer_token）
- 文件安全：symlink 拒绝、revision CAS、同目录临时文件 + 原子替换、
  备份先于替换持久化（0700 目录 / 0600 文件）、回退还原原始字节。

本文件只测事务语义；HTTP 门禁在 Task 2（test_connect_admin.py），
Dashboard 组装在 Task 3（test_dashboard_connect.py）。
"""
from __future__ import annotations

import json
import os
import stat

import pytest

ORIGIN = "http://127.0.0.1:8998"
KEY = "sk-f7-test-000"

CLAUDE_ORIGINAL = json.dumps({
    "env": {"OTHER_VAR": "keep-me"},
    "plugins": {"enabled": ["a"]},
    "includeCoAuthoredBy": False,
}, indent=2) + "\n"

CODEX_ORIGINAL = """# user comment kept
model = "gpt-5.1-codex"

[history]
persistence = "save-all"
"""


def _mgr(tmp_path, client, tag=""):
    from inferfabric.platform.clients import ConnectManager
    home = tmp_path / f"home{tag}"
    codex_home = tmp_path / f"codex{tag}" if client == "codex" else None
    if client == "claude":
        (home / ".claude").mkdir(parents=True)
    else:
        codex_home.mkdir(parents=True)
        home.mkdir(exist_ok=True)
    return ConnectManager(home=home, state_dir=tmp_path / f"state{tag}",
                          gateway_origin=ORIGIN, codex_home=codex_home)


def _target(tmp_path, client, tag=""):
    if client == "claude":
        return tmp_path / f"home{tag}" / ".claude" / "settings.json"
    return tmp_path / f"codex{tag}" / "config.toml"


def _seed(tmp_path, client, text, tag=""):
    p = _target(tmp_path, client, tag)
    p.write_text(text, encoding="utf-8")
    return p


# ── 1. 托管 → 回退：字节还原 + 无关设置保留 ──────────────────────

@pytest.mark.parametrize("client", ["claude", "codex"])
def test_apply_then_rollback_restores_bytes_and_unrelated_settings(tmp_path, client):
    from inferfabric.platform.clients import ConnectManager  # noqa: F401
    m = _mgr(tmp_path, client)
    original = CLAUDE_ORIGINAL if client == "claude" else CODEX_ORIGINAL
    p = _seed(tmp_path, client, original)

    before = m.inspect(client)
    assert before["client"] == client
    assert before["state"] == "unmanaged"

    applied = m.apply(client, expected_revision=before["revision"], api_key=KEY)
    assert applied["state"] == "managed"
    assert applied["target"] == "this_gateway"

    raw = p.read_text(encoding="utf-8")
    if client == "claude":
        data = json.loads(raw)
        assert data["env"]["ANTHROPIC_BASE_URL"] == ORIGIN
        assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY
        assert data["env"]["OTHER_VAR"] == "keep-me"           # 无关 env 保留
        assert data["plugins"] == {"enabled": ["a"]}           # 无关顶层键保留
        assert data["includeCoAuthoredBy"] is False
    else:
        import tomlkit
        data = tomlkit.parse(raw)
        assert data["model_provider"] == "inferfabric"
        prov = data["model_providers"]["inferfabric"]
        assert prov["name"] == "InferFabric"
        assert prov["base_url"] == ORIGIN + "/v1"
        assert prov["wire_api"] == "responses"
        assert prov["requires_openai_auth"] is False
        assert prov["experimental_bearer_token"] == KEY
        assert data["model"] == "gpt-5.1-codex"                # 默认模型不动
        assert data["history"]["persistence"] == "save-all"    # 无关表保留
        assert "# user comment kept" in raw                    # 注释保留
    assert stat.S_IMODE(p.stat().st_mode) <= 0o600             # 凭据文件不宽于 0600

    cur = m.inspect(client)
    assert cur["state"] == "managed"
    rolled = m.rollback(client, expected_revision=cur["revision"])
    assert rolled["state"] == "unmanaged"
    assert p.read_bytes() == original.encode()                 # 原始字节一致
    after = m.inspect(client)
    assert after["state"] == "unmanaged"
    assert after["revision"] == before["revision"]


# ── 2. 原文件缺失：只创建托管文件，回退只移除它 ──────────────────

@pytest.mark.parametrize("client", ["claude", "codex"])
def test_missing_file_rollback_removes_only_managed_file(tmp_path, client):
    m = _mgr(tmp_path, client)
    p = _target(tmp_path, client)
    sibling = p.parent / "unrelated.txt"
    sibling.write_text("keep", encoding="utf-8")

    before = m.inspect(client)
    assert before["state"] == "missing_file"
    assert before["revision"] == "absent"

    applied = m.apply(client, expected_revision="absent", api_key=KEY)
    assert applied["state"] == "managed"
    assert p.exists()
    assert sibling.read_text() == "keep"

    rolled = m.rollback(client, expected_revision=applied["revision"])
    assert not p.exists()                                      # 只移除本次创建的文件
    assert sibling.read_text() == "keep"
    assert rolled["state"] == "missing_file"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_apply_missing_client_directory_rejected_no_side_effects(tmp_path, client):
    """评审 I4：计划 = 只在已存在的客户端目录下创建配置；缺目录即拒绝，
    且拒绝（含 stale revision）不产生任何文件系统副作用。"""
    from inferfabric.platform.clients import ConnectError, ConnectManager
    home = tmp_path / "home-md"
    home.mkdir()
    codex_home = tmp_path / "codex-md"
    m = ConnectManager(home=home, state_dir=tmp_path / "state-md",
                       gateway_origin=ORIGIN,
                       codex_home=codex_home if client == "codex" else None)
    before = m.inspect(client)
    assert before["state"] == "missing_directory"

    # 正确 revision（absent）也拒绝：缺目录不是可自动修复的状态
    with pytest.raises(ConnectError) as ei:
        m.apply(client, expected_revision="absent", api_key=KEY)
    assert ei.value.code == "conflict"
    # stale revision 同样拒绝，且两次拒绝都不建目录/不写状态
    with pytest.raises(ConnectError):
        m.apply(client, expected_revision="stale", api_key=KEY)
    anchor = (tmp_path / "home-md" / ".claude") if client == "claude" \
        else codex_home
    assert not anchor.exists()                                 # 零副作用
    assert not (tmp_path / "state-md" / "connect").exists()    # 无备份目录


# ── 3. stale revision / 外部编辑 / symlink：绝不写入 ─────────────

def test_stale_revision_post_apply_edit_or_symlink_never_written(tmp_path):
    from inferfabric.platform.clients import ConnectError
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]

    # 预览后目标变了：外部编辑后按旧 revision apply → 拒绝且文件不变
    data = json.loads(CLAUDE_ORIGINAL)
    data["includeCoAuthoredBy"] = True
    edited = json.dumps(data, indent=2) + "\n"
    p.write_text(edited, encoding="utf-8")
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=rev0, api_key=KEY)
    assert ei.value.code == "conflict"
    assert p.read_text() == edited

    # 回退前用户另有修改：按旧 managed revision rollback → 拒绝且文件不变
    m.apply("claude",
            expected_revision=m.inspect("claude")["revision"], api_key=KEY)
    managed_rev = m.inspect("claude")["revision"]
    data2 = json.loads(p.read_text())
    data2["plugins"] = {"tampered": True}
    tampered = json.dumps(data2, indent=2) + "\n"
    p.write_text(tampered, encoding="utf-8")
    with pytest.raises(ConnectError) as ei:
        m.rollback("claude", expected_revision=managed_rev)
    assert ei.value.code == "conflict"
    assert p.read_text() == tampered

    # 目标文件是符号链接 → 拒绝且链接另一端不变
    m2 = _mgr(tmp_path, "claude", tag="2")
    p2 = _target(tmp_path, "claude", tag="2")
    outside = tmp_path / "outside2.json"
    outside.write_text('{"keep": true}\n', encoding="utf-8")
    p2.symlink_to(outside)
    with pytest.raises(ConnectError) as ei:
        m2.apply("claude", expected_revision="absent", api_key=KEY)
    assert ei.value.code == "conflict"
    assert outside.read_bytes() == b'{"keep": true}\n'
    assert p2.is_symlink()

    # 中间目录是符号链接 → 拒绝且真实目录不被写入
    m3 = _mgr(tmp_path, "claude", tag="3")
    real_dir = tmp_path / "real3"
    real_dir.mkdir()
    dot_claude = tmp_path / "home3" / ".claude"
    dot_claude.rmdir()                       # _mgr 已建真实目录，换成 symlink
    dot_claude.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ConnectError) as ei:
        m3.apply("claude", expected_revision="absent", api_key=KEY)
    assert ei.value.code == "conflict"
    assert not (real_dir / "settings.json").exists()
    assert dot_claude.is_symlink()


# ── 4. 畸形/冲突配置：原文件不动 ─────────────────────────────────

def test_malformed_or_conflicting_config_leaves_original_untouched(tmp_path):
    from inferfabric.platform.clients import ConnectError

    # claude 无效 JSON
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", "{not json")
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=m.inspect("claude")["revision"],
                api_key=KEY)
    assert ei.value.code == "conflict"
    assert p.read_text() == "{not json"

    # claude 重复 JSON 键（标准 json.loads 会静默取后者，必须显式拒绝）
    dup = '{"env": {"A": "1"}, "env": {"B": "2"}}\n'
    p.write_text(dup, encoding="utf-8")
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=m.inspect("claude")["revision"],
                api_key=KEY)
    assert ei.value.code == "conflict"
    assert p.read_text() == dup

    # codex 无效 TOML
    mc = _mgr(tmp_path, "codex")
    pc = _seed(tmp_path, "codex", "[broken\n")
    with pytest.raises(ConnectError) as ei:
        mc.apply("codex", expected_revision=mc.inspect("codex")["revision"],
                 api_key=KEY)
    assert ei.value.code == "conflict"
    assert pc.read_text() == "[broken\n"

    # codex 保留 provider 名冲突：inferfabric 已被占用且指向别处
    occupied = '[model_providers.inferfabric]\nbase_url = "http://example.com:1/v1"\n'
    pc.write_text(occupied, encoding="utf-8")
    with pytest.raises(ConnectError) as ei:
        mc.apply("codex", expected_revision=mc.inspect("codex")["revision"],
                 api_key=KEY)
    assert ei.value.code == "conflict"
    assert pc.read_text() == occupied

    # claude 既有 ANTHROPIC_API_KEY（用户既有凭据语义）→ 拒绝
    withkey = json.dumps({"env": {"ANTHROPIC_API_KEY": "user-key"}}, indent=2) + "\n"
    p.write_text(withkey, encoding="utf-8")
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=m.inspect("claude")["revision"],
                api_key=KEY)
    assert ei.value.code == "conflict"
    assert p.read_text() == withkey


# ── 5. 原子替换失败：原文件完整、不宣称托管、可恢复、私有模式 ────

def test_atomic_failure_retains_recoverable_original_and_private_modes(
        tmp_path, monkeypatch):
    from inferfabric.platform.clients import ConnectError
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=rev0, api_key=KEY)
    assert ei.value.code == "io_error"
    monkeypatch.undo()

    assert p.read_bytes() == CLAUDE_ORIGINAL.encode()          # 原文件完整
    assert m.inspect("claude")["state"] == "unmanaged"         # 不宣称托管成功
    leftovers = [q.name for q in p.parent.iterdir()
                 if "connect-tmp" in q.name]
    assert leftovers == []                                     # 临时文件已清理

    # 备份已先于替换持久化 → 直接重试成功，回退还原首次原件
    applied = m.apply("claude", expected_revision=rev0, api_key=KEY)
    assert applied["state"] == "managed"
    rolled = m.rollback("claude", expected_revision=applied["revision"])
    assert p.read_bytes() == CLAUDE_ORIGINAL.encode()
    assert rolled["state"] == "unmanaged"

    # 私有模式：state_dir 下所有文件 ≤0600，目录 ≤0700
    state_root = tmp_path / "state"
    for root, dirs, files in os.walk(state_root):
        for d in dirs:
            path = os.path.join(root, d)
            if not os.path.islink(path):
                assert stat.S_IMODE(os.stat(path).st_mode) <= 0o700, path
        for f in files:
            path = os.path.join(root, f)
            if not os.path.islink(path):
                assert stat.S_IMODE(os.stat(path).st_mode) <= 0o600, path


# ── 6. 评审修复：事务内重验 / 跨实例串行 / 备份完整性 / 重应用崩溃可回退 ──

def test_external_edit_during_transaction_rejected_no_overwrite(
        tmp_path, monkeypatch):
    """评审 C1：构建/备份期间外部编辑 → 提交前重验必须拒绝，绝不覆盖。"""
    from inferfabric.platform.clients import ConnectError
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]

    real_build = m._build

    def _sneaky_build(client, current, api_key):
        # 模拟外部编辑器在事务中段保存
        data = json.loads(current.decode())
        data["plugins"] = {"external": True}
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return real_build(client, current, api_key)

    monkeypatch.setattr(m, "_build", _sneaky_build)
    with pytest.raises(ConnectError) as ei:
        m.apply("claude", expected_revision=rev0, api_key=KEY)
    assert ei.value.code == "conflict"
    monkeypatch.undo()

    raw = p.read_text()
    assert json.loads(raw)["plugins"] == {"external": True}    # 外部编辑保留
    assert not (tmp_path / "state" / "connect").exists()       # 未落任何备份


def test_concurrent_apply_from_two_managers_second_conflicts(tmp_path):
    """评审 C1：每请求新建 manager（threaded/async 各自实例）也必须串行 —
    同一 revision 的第二个 apply 冲突，不得双双通过校验。"""
    import threading
    from inferfabric.platform.clients import ConnectError, ConnectManager
    m1 = _mgr(tmp_path, "claude")
    # 第二个请求的新实例：同 home/state（threaded 与 async server 各自 new）
    m2 = ConnectManager(home=tmp_path / "home", state_dir=tmp_path / "state",
                        gateway_origin=ORIGIN, codex_home=None)
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m1.inspect("claude")["revision"]

    inside = threading.Event()
    release = threading.Event()
    real_build = m1._build

    def _gated_build(client, current, api_key):
        inside.set()
        release.wait(timeout=5)
        return real_build(client, current, api_key)

    results = {}

    def _first():
        m1._build = _gated_build
        try:
            results["first"] = m1.apply("claude",
                                        expected_revision=rev0, api_key=KEY)
        except ConnectError as exc:
            results["first"] = exc.code

    def _second():
        try:
            m2.apply("claude", expected_revision=rev0, api_key=KEY)
            results["second"] = "ok"
        except ConnectError as exc:
            results["second"] = exc.code

    t1 = threading.Thread(target=_first)
    t1.start()
    assert inside.wait(timeout=5)                              # 第一个已持锁
    t2 = threading.Thread(target=_second)
    t2.start()                                                 # 在锁上排队
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert results["first"] and results["first"] != "conflict"  # 第一个成功
    assert results["second"] == "conflict"                     # 串行等待后冲突
    assert json.loads(p.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY


def test_reapply_interrupted_still_rollbackable_to_original(tmp_path):
    """评审 C2：重应用在替换前中断 → 记录必须保留先前托管 revision，
    一键回退到首次原件仍然可用（不得出现三不认状态）。"""
    from inferfabric.platform.clients import ConnectError
    import inferfabric.platform.clients as clients_mod
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]
    m.apply("claude", expected_revision=rev0, api_key=KEY)
    rev1 = m.inspect("claude")["revision"]

    real_replace = clients_mod.os.replace

    def _boom(src, dst):
        raise OSError("simulated replace failure on reapply")

    clients_mod.os.replace = _boom
    try:
        with pytest.raises(ConnectError) as ei:
            m.apply("claude", expected_revision=rev1, api_key=KEY + "2")
        assert ei.value.code == "io_error"
    finally:
        clients_mod.os.replace = real_replace

    # 中断后：文件仍是旧托管内容，inspect 报 managed（可回退）
    cur = m.inspect("claude")
    assert cur["state"] == "managed", cur
    assert cur["revision"] == rev1
    rolled = m.rollback("claude", expected_revision=rev1)
    assert rolled["state"] == "unmanaged"
    assert p.read_bytes() == CLAUDE_ORIGINAL.encode()          # 回到首次原件


def test_rollback_rejects_corrupt_backup_target_unchanged(tmp_path):
    """评审 C2：备份字节损坏 → 完整性校验拒绝回退，目标不被垃圾覆盖。"""
    from inferfabric.platform.clients import ConnectError
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]
    applied = m.apply("claude", expected_revision=rev0, api_key=KEY)

    backup = tmp_path / "state" / "connect" / "claude" / "original.bin"
    backup.write_bytes(b"CORRUPTED-BACKUP-BYTES")              # 篡改备份

    with pytest.raises(ConnectError) as ei:
        m.rollback("claude", expected_revision=applied["revision"])
    assert ei.value.code == "conflict"
    managed = p.read_text()
    assert json.loads(managed)["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY  # 未被覆盖


def test_reapply_never_truncates_first_original_backup(tmp_path):
    """评审 C2：重复托管不覆盖首次原件（含权限不放宽）。"""
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]
    m.apply("claude", expected_revision=rev0, api_key=KEY)

    backup = tmp_path / "state" / "connect" / "claude" / "original.bin"
    first = backup.read_bytes()
    os.chmod(backup, 0o644)                                    # 人为放宽

    m.apply("claude", expected_revision=m.inspect("claude")["revision"],
            api_key=KEY + "2")
    assert backup.read_bytes() == first                        # 首次原件原样
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600        # 权限已收紧


def test_rejected_rollback_leaves_target_and_backup_untouched(tmp_path):
    """评审 Minor：被拒绝的 rollback 后目标字节与备份记录均不变。"""
    from inferfabric.platform.clients import ConnectError
    m = _mgr(tmp_path, "claude")
    p = _seed(tmp_path, "claude", CLAUDE_ORIGINAL)
    rev0 = m.inspect("claude")["revision"]
    m.apply("claude", expected_revision=rev0, api_key=KEY)
    rev1 = m.inspect("claude")["revision"]

    record = tmp_path / "state" / "connect" / "claude" / "record.json"
    backup = tmp_path / "state" / "connect" / "claude" / "original.bin"
    rec_before = record.read_bytes()
    bak_before = backup.read_bytes()
    target_before = p.read_bytes()

    with pytest.raises(ConnectError):
        m.rollback("claude", expected_revision="stale-revision")
    assert p.read_bytes() == target_before                     # 目标字节未动
    target_now = m.inspect("claude")["revision"]
    assert target_now == rev1                                  # 目标未动
    assert record.read_bytes() == rec_before                   # 记录未动
    assert backup.read_bytes() == bak_before                   # 备份未动
