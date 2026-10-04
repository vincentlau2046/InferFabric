"""unit/proxy/test_agent_endpoints.py — /api/agent-* 端点测试（SimpleNamespace 桩）。"""
from types import SimpleNamespace
from inferfabric.proxy.handler import ProxyHandler


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h.headers = {}  # BaseHTTPRequestHandler 恒有；snapshot 读 If-None-Match
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_agent_stats_invalid_gran_400():
    h = _handler("/api/agent-stats?granularity=bogus")
    pm = SimpleNamespace()
    h._handle_agent_stats(pm)
    assert h._sent[-1][0] == 400

def _mk_pm(tmp_path):
    from inferfabric.agent_registry import AgentRegistry
    reg = AgentRegistry(tmp_path / "builtin", tmp_path / "user")
    events = []
    pm = SimpleNamespace(agent_registry=reg,
                         telemetry=SimpleNamespace(
                             query_request_log=lambda since, limit=1: [],
                             reclassify_request_log=lambda classify_fn, known_ids=None: (
                                 events.append(("reclassify", tuple(sorted(known_ids or ())))), 2)[1]),
                         metrics=SimpleNamespace(price_config={}),
                         _events=events)
    return pm


def test_agents_get_lists(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    h._handle_agents(pm)
    code, data = h._sent[-1]
    assert code == 200 and data == {"agents": []}


def test_agents_post_creates(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    import json
    h._read_body = lambda: {"id": "curl-cli", "name": "curl-cli",
                            "header": "user-agent", "pattern": "^curl", "color": "#94a3b8"}
    h._handle_post_agents(pm)
    code, data = h._sent[-1]
    assert code == 200 and data["agent"]["id"] == "curl-cli"
    # reclassify 在响应前完成（前端拿到 200 即刷新，须读到重分类后的新数据），
    # 且行数回传供前端 toast 反馈。builtin 目录不存在 → known_ids 仅含新建 user def。
    assert pm._events == [("reclassify", ("curl-cli",))]
    assert data["reclassified"] == 2
    # 已落盘并热重载 → classify 命中
    assert pm.agent_registry.classify("openai", {"User-Agent": "curl/8"}).agent == "curl-cli"


def test_agents_post_duplicate_409(tmp_path):
    pm = _mk_pm(tmp_path)
    pm.agent_registry.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
    h = _handler("/api/agents")
    h._read_body = lambda: {"id": "curl-cli", "name": "x", "header": "user-agent", "pattern": "^x", "color": "#000"}
    h._handle_post_agents(pm)
    assert h._sent[-1][0] == 409


def test_agents_post_invalid_400(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    h._read_body = lambda: {"id": "Bad ID!", "name": "x", "header": "user-agent", "pattern": "^x", "color": "#000"}
    h._handle_post_agents(pm)
    assert h._sent[-1][0] == 400


def test_agents_delete_user_ok(tmp_path):
    pm = _mk_pm(tmp_path)
    pm.agent_registry.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
    h = _handler("/api/agents?id=curl-cli")
    h._handle_delete_agent(pm)
    assert h._sent[-1][0] == 200
    assert pm.agent_registry.classify("openai", {"User-Agent": "curl/8"}).agent == "unknown"


def test_agents_delete_builtin_400(tmp_path):
    from inferfabric.agent_registry import AgentRegistry
    b = tmp_path / "builtin"; b.mkdir()
    (b / "cc.yaml").write_text("id: claude-code\nname: CC\ncolor: '#000'\nmatch:\n  - { header: x-app, value: cli }\n", encoding="utf-8")
    pm = SimpleNamespace(agent_registry=AgentRegistry(b, tmp_path / "user"))
    h = _handler("/api/agents?id=claude-code")
    h._handle_delete_agent(pm)
    assert h._sent[-1][0] == 400

def test_request_log_carries_agent_ua():
    h = _handler("/api/request_log")
    rows = [{"timestamp": 1.0, "model": "m", "status": 200, "tokens_in": 1,
             "tokens_in_cached": 0, "tokens_out": 1, "ttft_ms": 1.0,
             "duration_ms": 2.0, "route": "local", "key_name": "k", "error": "",
             "agent": "claude-code", "ua": "claude-cli/2.0"}]
    pm = SimpleNamespace(telemetry=SimpleNamespace(
        query_request_log=lambda since, limit=1: rows))
    h._handle_request_log(pm)
    code, data = h._sent[-1]
    assert code == 200 and data["logs"][0]["agent"] == "claude-code"
    assert data["logs"][0]["ua"] == "claude-cli/2.0"


def test_snapshot_carries_agent_ua():
    h = _handler("/api/snapshot")
    # snapshot 走真实 send_response/_safe_write 而非 _send_json 桩
    h.send_response = lambda code, message=None: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    bodies = []
    h._safe_write = lambda b: bodies.append(b)
    rows = [{"timestamp": 1.0, "model": "m", "status": 200, "tokens_in": 1,
             "tokens_in_cached": 0, "tokens_out": 1, "ttft_ms": None,
             "duration_ms": None, "route": "local", "key_name": "k", "error": None,
             "agent": "", "ua": "curl/8"}]
    pm = SimpleNamespace(telemetry=SimpleNamespace(query_request_log=lambda since, limit=50: rows),
                         mgr=SimpleNamespace(state=SimpleNamespace(get_history=lambda n: []),
                                             list_models=lambda: [], _models={}),
                         _snap_exp_cache=None)
    h._handle_snapshot(pm)
    import json
    data = json.loads(bodies[-1])
    rl = data["request_log"]
    assert rl[0]["agent"] == "" and rl[0]["ua"] == "curl/8"


def test_agents_post_alias_to_builtin(tmp_path):
    """POST /api/agents 带 parent_id → 走 add_alias，子进程 UA 归入已有 builtin agent。"""
    from inferfabric.agent_registry import AgentRegistry
    b = tmp_path / "builtin"; u = tmp_path / "user"
    b.mkdir(); u.mkdir()
    (b / "cc.yaml").write_text(
        "id: claude-code\nname: Claude Code\ncolor: \"#d97757\"\nmatch:\n  - { header: x-app, value: cli }\n",
        encoding="utf-8")
    pm = _mk_pm(tmp_path)
    pm.agent_registry = AgentRegistry(b, u)
    h = _handler("/api/agents")
    h._read_body = lambda: {"parent_id": "claude-code", "header": "user-agent", "pattern": "^curl"}
    h._handle_post_agents(pm)
    code, data = h._sent[-1]
    assert code == 200 and data["agent"]["id"] == "claude-code"
    # curl 现在归入 claude-code
    assert pm.agent_registry.classify("openai", {"User-Agent": "curl/8.5.2"}).agent == "claude-code"
    # builtin 原规则不丢
    assert pm.agent_registry.classify("anthropic", {"x-app": "cli"}).agent == "claude-code"


def test_agents_post_alias_not_found_400(tmp_path):
    pm = _mk_pm(tmp_path)
    h = _handler("/api/agents")
    h._read_body = lambda: {"parent_id": "ghost", "header": "user-agent", "pattern": "^x"}
    h._handle_post_agents(pm)
    assert h._sent[-1][0] == 400


def test_reclassify_updates_unknown_rows(tmp_path):
    """POST /api/agents/reclassify — 对 agent='unknown' 且有 ua 的行用当前
    registry 重新 classify，命中的行 UPDATE 为新 agent。"""
    from inferfabric.agent_registry import AgentRegistry
    import time
    b = tmp_path / "builtin"; u = tmp_path / "user"
    b.mkdir(); u.mkdir()
    # registry 有 curl alias 归入 claude-code
    (b / "cc.yaml").write_text(
        "id: claude-code\nname: Claude Code\ncolor: \"#d97757\"\nmatch:\n  - { header: x-app, value: cli }\n",
        encoding="utf-8")
    (u / "claude-code.yaml").write_text(
        "id: claude-code\naliases:\n  - { header: user-agent, regex: \"^curl\" }\n", encoding="utf-8")
    reg = AgentRegistry(b, u)
    # 建 DB 带未知行
    import sqlite3
    from inferfabric.db import IFFDB, REQUEST_LOG_DB
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa
    db._run_migrations()
    with db.connect(REQUEST_LOG_DB) as conn:
        conn.execute("INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
                     "VALUES ('r1','m',200,?,'unknown','curl/8.5.2')", (time.time(),))
        conn.execute("INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
                     "VALUES ('r2','m',200,?,'unknown','Python-urllib/3.13')", (time.time(),))
        conn.commit()
    pm = _mk_pm(tmp_path)
    pm.agent_registry = reg
    pm.telemetry = db
    # reclassify
    h = _handler("/api/agents/reclassify")
    h._handle_reclassify(pm)
    code, data = h._sent[-1]
    assert code == 200
    assert data["reclassified"] >= 1  # curl 命中 claude-code
    # DB 验证
    with db.connect(REQUEST_LOG_DB) as conn:
        r1 = conn.execute("SELECT agent FROM request_log WHERE req_id='r1'").fetchone()
        r2 = conn.execute("SELECT agent FROM request_log WHERE req_id='r2'").fetchone()
    assert r1[0] == "claude-code"   # curl 命中 alias
    assert r2[0] == "unknown"       # Python-urllib 未命中（无 alias）
    db.close()


def test_reclassify_also_handles_observed_residual(tmp_path):
    """reclassify 也处理 observed 残留（agent='smoke-tmp' 等无 def 的 id），
    不仅 agent='unknown'。认领后这类行也应被重新分类。"""
    from inferfabric.agent_registry import AgentRegistry
    import time
    b = tmp_path / "builtin"; u = tmp_path / "user"
    b.mkdir(); u.mkdir()
    (b / "cc.yaml").write_text(
        "id: claude-code\nname: Claude Code\ncolor: \"#d97757\"\nmatch:\n  - { header: x-app, value: cli }\n",
        encoding="utf-8")
    # claude-code 的 alias：curl 归入
    (u / "claude-code.yaml").write_text(
        "id: claude-code\naliases:\n  - { header: user-agent, regex: \"^curl\" }\n",
        encoding="utf-8")
    # 用户新建 smoke-test agent（match ^smoke）
    (u / "smoke-test.yaml").write_text(
        "id: smoke-test\nname: smoke test\nmatch:\n  - { header: user-agent, regex: \"^smoke\" }\n",
        encoding="utf-8")
    reg = AgentRegistry(b, u)
    import sqlite3
    from inferfabric.db import IFFDB, REQUEST_LOG_DB
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa
    db._run_migrations()
    ts = time.time()
    with db.connect(REQUEST_LOG_DB) as conn:
        # unknown 行 + observed 残留（agent='smoke-tmp'，无 def）
        conn.execute("INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
                     "VALUES ('r1','m',200,?,'unknown','curl/8.5.2')", (ts,))
        conn.execute("INSERT INTO request_log (req_id, model, status, timestamp, agent, ua) "
                     "VALUES ('r2','m',200,?,'smoke-tmp','smoke-tmp/1.0')", (ts,))
        conn.commit()
    known = {d.id for d in reg.all()}
    n = db.reclassify_request_log(reg.classify, known)
    with db.connect(REQUEST_LOG_DB) as conn:
        r1 = conn.execute("SELECT agent FROM request_log WHERE req_id='r1'").fetchone()
        r2 = conn.execute("SELECT agent FROM request_log WHERE req_id='r2'").fetchone()
    assert r1[0] == "claude-code"   # curl alias 命中
    assert r2[0] == "smoke-test"    # smoke-tmp UA 被 ^smoke 命中 → 重新归类
    db.close()
