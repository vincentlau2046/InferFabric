# Agent 感知（Client as Plugin）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 InferFabric 按请求头指纹识别调用它的客户端 Agent，在监控 TAB 里按 Agent 统计用量，并支持 dashboard 一键认领未知 Agent（落盘 `agents.d/` 立即生效）；同时补 api_keys 命名规则文档（仅文档，鉴权保持默认关闭）。

**Architecture:** 复用 IFF「模型即插件」范式——`agents.d/` 目录自发现（内置 + 用户 drop-in 双目录，用户覆盖内置）；`agent_registry.py` 做 `classify(protocol, headers) → AgentHit`，每请求 O(1)，热重载 fail-closed；`request_log` 加 `agent/ua` 两列（迁移 v008）作为统一漏斗的客户端维度；`/api/agent-stats` 与 `/api/agents`（GET/POST/DELETE）为控制面/观测端点；监控 TAB 新增 col-12「客户端 Agent 用量」卡（图左表右）。

**Tech Stack:** Python 3.10+（dataclasses / re / yaml）、SQLite（request_log）、aiohttp + http.server（双协议代理）、ECharts + 原生 JS 仪表板、pytest。

**Spec:** `docs/superpowers/specs/2026-09-28-agent-aware-clients-design.md`（本计划从 spec 论证；执行者两份一起读）

## Global Constraints

- Python 3.10+；**vLLM 0.24 不升级**；**不修改代理转发核心路径（PR-14 划界）**——采集只读请求头，不改请求体、不改路由决定
- 测试从 repo 根运行（`python3 -m pytest ...`，imports 经 CWD 解析）；`_deps` 已 vendored，**不新增任何 pip 依赖**（yaml 已随 auth 使用）
- 迁移编号顺位 **v008**（当前 v007）；迁移必须幂等（`PRAGMA table_info` 判列再 ALTER）
- `regex` 匹配语义 = `re.search`（子串匹配，需锚点显式写 `^`）；`value` 全等比较**大小写不敏感**
- 分类规范化值恒为 `"unknown"`；聚合层 `agent = agent or 'unknown'` 归一
- 一键认领 = **新建语义**（id 撞已有 → 409）；保留字 `unknown` 拒绝；只写 `~/.inferfabric/agents.d/`
- 用户可见文案用中文（沿用 dashboard / 现有 docstring 惯例）

## Review Focus

spec 声明「必须做什么」，但这些输入也一样不能坏（每行都有对应任务的测试钉住）：

1. **agents.d 双目录都不存在 / 内置目录损坏** → `classify` 永不抛异常，全部归 `unknown`（Task 2 空目录测试）
2. **单文件 YAML 损坏或含坏 regex** → 跳过该文件/该条并告警，其余规则照常，不炸请求路径（Task 1 `MatchRule.build` + Task 2 坏文件测试）
3. **历史行 `agent=''` 与分类器 `'unknown'` 混存** → 聚合成同一个「未识别」桶，不裂成两行（Task 6 混存测试）
4. **请求缺 User-Agent 头 / header 名大小写不同** → 正常分类、`ua=''`、不抛（Task 1 no-UA 与大小写测试、Task 5 采集）
5. **重复一键认领同一 id / 并发写** → 409 且不残留 `.tmp` 半文件（Task 3 `add_from_ui` + 原子写测试）

---

### Task 1: AgentRegistry 分类引擎（核心）

**Files:**
- Create: `inferfabric/agent_registry.py`
- Test: `tests/unit/proxy/test_agent_registry.py`

**Interfaces:**
- Consumes: 无（纯新模块；依赖 stdlib `re`/`yaml` + `inferfabric.agents.d` 目录（Task 2 提供））
- Produces:
  - `MatchRule.build(raw: dict) -> MatchRule | None`（坏规则返回 None）
  - `MatchRule.matches(protocol: str, headers: Mapping) -> bool`
  - `AgentDef(id, name, color, rules, source)`（frozen dataclass）
  - `AgentHit(agent, name, color, ua)`（frozen dataclass）
  - `request_protocol(path: str) -> str`（`"/v1/messages"` → `"anthropic"`，其余 → `"openai"`）
  - `class AgentRegistry: __init__(builtin_dir, user_dir) / load() / reload() -> bool / classify(protocol, headers) -> AgentHit / all() -> list[AgentDef] / policy(agent_id) -> None`
  - 常量 `_HEADER_WHITELIST = ("x-app", "user-agent", "x-client-version")`、`_UNKNOWN_ID = "unknown"`、`_ROOT_ID_RE`

- [ ] **Step 1: 写失败测试（分类规则矩阵）**

```python
"""unit/proxy/test_agent_registry.py — Agent 识别引擎测试

覆盖 classify 规则矩阵：header 全等(value)/正则(regex)/协议条件/first-match
顺序/unknown 兜底/大小写不敏感/请求无 UA 头。
"""
from pathlib import Path
import pytest
from inferfabric.agent_registry import (
    AgentRegistry, MatchRule, AgentDef, AgentHit, request_protocol, _UNKNOWN_ID,
)

BUILTIN = None  # 用例内用 tmp_path 自建

def _mk(tmp_path, builtin=(), user=()):
    b = tmp_path / "builtin"; u = tmp_path / "user"
    for d in (b, u): d.mkdir(exist_ok=True)
    for name, body in builtin:
        (b / name).write_text(body, encoding="utf-8")
    for name, body in user:
        (u / name).write_text(body, encoding="utf-8")
    return AgentRegistry(b, u)

CC = """
id: claude-code
name: Claude Code
color: "#d97757"
match:
  - { header: x-app, value: cli }
  - { header: user-agent, regex: "^claude-cli/" }
"""
CURSOR = """
id: cursor
name: Cursor
color: "#000000"
match:
  - { header: user-agent, regex: "Cursor" }
"""

class TestClassify:
    def test_xapp_value_hit(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("anthropic", {"x-app": "cli"})
        assert hit.agent == "claude-code" and hit.name == "Claude Code"

    def test_xapp_value_case_insensitive(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("anthropic", {"X-APP": "CLI"})
        assert hit.agent == "claude-code"

    def test_ua_regex_search_hit(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {"User-Agent": "claude-cli/2.0.1"})
        assert hit.agent == "claude-code"

    def test_ua_regex_substring_not_anchor(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cursor.yaml", CURSOR)])
        hit = r.classify("openai", {"User-Agent": "MyCursorApp/1.2"})
        assert hit.agent == "cursor"

    def test_protocol_condition_gates(self, tmp_path):
        only_openai = """
id: codex
name: Codex
color: "#000000"
match:
  - { protocol: openai }
"""
        r = _mk(tmp_path, builtin=[("codex.yaml", only_openai)])
        assert r.classify("openai", {}).agent == "codex"
        assert r.classify("anthropic", {}).agent == _UNKNOWN_ID

    def test_first_match_wins(self, tmp_path):
        # 两个 def 都命中同一条 UA → 排序在前者胜
        r = _mk(tmp_path, builtin=[("aa.yaml", CC), ("bb.yaml", CURSOR)])
        hit = r.classify("openai", {"User-Agent": "claude-cli/1.0 Cursor/2.0"})
        assert hit.agent == "claude-code"  # aa.yaml 排序在先

    def test_unknown_fallback_captures_ua(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {"User-Agent": "curl/8.5.2"})
        assert hit.agent == _UNKNOWN_ID and hit.ua == "curl/8.5.2"

    def test_no_ua_header(self, tmp_path):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        hit = r.classify("openai", {})
        assert hit.agent == _UNKNOWN_ID and hit.ua == ""

    def test_empty_dirs_all_unknown(self, tmp_path):
        r = _mk(tmp_path)
        hit = r.classify("openai", {"User-Agent": "curl/8"})
        assert hit.agent == _UNKNOWN_ID and hit.name == _UNKNOWN_ID

class TestMatchRuleBuild:
    def test_bad_regex_skipped(self):
        assert MatchRule.build({"header": "user-agent", "regex": "("}) is None

    def test_unknown_header_skipped(self):
        assert MatchRule.build({"header": "x-custom", "value": "x"}) is None

    def test_invalid_protocol_skipped(self):
        assert MatchRule.build({"protocol": "grpc"}) is None

    def test_empty_rule_skipped(self):
        assert MatchRule.build({"header": "user-agent"}) is None

    def test_valid_rule_built(self):
        r = MatchRule.build({"header": "x-app", "value": "cli"})
        assert r is not None and r.matches("anthropic", {"x-app": "cli"})

class TestRequestProtocol:
    def test_messages_anthropic(self):
        assert request_protocol("/v1/messages") == "anthropic"
    def test_chat_openai(self):
        assert request_protocol("/v1/chat/completions") == "openai"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_registry.py -v --tb=short`
Expected: FAIL，`ModuleNotFoundError: No module named 'inferfabric.agent_registry'`

- [ ] **Step 3: 实现 agent_registry.py**

```python
"""inferfabric/agent_registry.py — 客户端 Agent 识别引擎（Client as Plugin）。

与 cloud_discovery.py 对称：读 agents.d YAML → 归一化为 AgentHit。
Agent = 用请求头指纹识别的工具（Claude Code/Cursor/…），与 key_name（身份）
和 client IP（来源）是三个正交维度。

匹配语义（设计文档 §1）：
  - 文件按文件名排序，跨文件 first-match，任一 match 命中即归类
  - header 白名单 3 个信号；value 全等比较大小写不敏感；regex 用 re.search
  - protocol 可选条件（anthropic/openai），缺省不限
  - 单条规则坏（坏 regex / 未知 header / 非法 protocol / 空规则）→ 跳过该条
  - 单文件解析失败 → 跳过该文件（fail-open 于单文件）；reload 时意外异常 → 保留旧规则
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import yaml

log = logging.getLogger("inferfabric.agent_registry")

_HEADER_WHITELIST = ("x-app", "user-agent", "x-client-version")
_UNKNOWN_ID = "unknown"
_UNKNOWN_NAME = "未识别"
_UNKNOWN_COLOR = "#9ca3af"
_ROOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROTOCOLS = ("anthropic", "openai")


def request_protocol(path: str) -> str:
    """从请求路径判定协议先验：/v1/messages → anthropic，其余 → openai。"""
    return "anthropic" if (path or "").startswith("/v1/messages") else "openai"


@dataclass(frozen=True)
class MatchRule:
    header: str | None = None
    value: str | None = None
    regex: str | None = None
    protocol: str | None = None
    _rx: re.Pattern | None = field(default=None, repr=False)

    @classmethod
    def build(cls, raw: dict) -> MatchRule | None:
        """从 YAML dict 构建；非法输入返回 None（调用方跳过该条）。"""
        header = raw.get("header")
        if header is not None and header.lower() not in _HEADER_WHITELIST:
            log.warning("agents.d: unknown header signal '%s' skipped", header)
            return None
        proto = raw.get("protocol")
        if proto is not None and proto not in _PROTOCOLS:
            log.warning("agents.d: invalid protocol '%s' skipped", proto)
            return None
        rx = None
        if raw.get("regex"):
            try:
                rx = re.compile(raw["regex"])
            except re.error as e:
                log.warning("agents.d: bad regex '%s' skipped (%s)", raw["regex"], e)
                return None
        rule = cls(
            header=header.lower() if header else None,
            value=raw.get("value"),
            regex=raw.get("regex"),
            protocol=proto,
            _rx=rx,
        )
        if rule.value is None and rule._rx is None and rule.protocol is None:
            return None
        return rule

    def matches(self, protocol: str, headers: Mapping) -> bool:
        if self.protocol is not None and self.protocol != protocol:
            return False
        if self.header is None:
            return True
        hv = headers.get(self.header, "")
        if self.value is not None:
            return hv.lower() == self.value.lower()
        if self._rx is not None:
            return self._rx.search(hv) is not None
        return False


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    color: str
    rules: tuple[MatchRule, ...]
    source: str  # "builtin" | "user"


@dataclass(frozen=True)
class _Snapshot:
    defs: tuple[AgentDef, ...]


@dataclass(frozen=True)
class AgentHit:
    agent: str
    name: str
    color: str
    ua: str


class AgentRegistry:
    """Agent 识别引擎。每请求 classify O(1)；热重载 fail-closed。"""

    def __init__(self, builtin_dir: Path, user_dir: Path):
        self._builtin_dir = Path(builtin_dir)
        self._user_dir = Path(user_dir)
        self._defs: list[AgentDef] = []
        self._snapshot: _Snapshot = _Snapshot(tuple())
        self.load()

    def load(self) -> None:
        try:
            self._defs = self._collect_defs()
        except Exception as e:
            log.error("AgentRegistry initial load failed — running with empty rules: %s", e)
            self._defs = []
        self._snapshot = _Snapshot(tuple(self._defs))
        log.info("AgentRegistry loaded: %d agents (builtin=%s user=%s)",
                 len(self._defs), self._builtin_dir, self._user_dir)

    def reload(self) -> bool:
        """热重载。收集过程抛异常（如权限错误）→ 保留旧快照，返回 False。"""
        try:
            new_defs = self._collect_defs()
        except Exception as e:
            log.error("AgentRegistry reload FAILED — keeping previous rules: %s", e)
            return False
        self._defs = new_defs
        self._snapshot = _Snapshot(tuple(new_defs))
        log.info("AgentRegistry reloaded: %d agents", len(new_defs))
        return True

    def classify(self, protocol: str, headers: Mapping) -> AgentHit:
        snap = self._snapshot
        ua = str(headers.get("user-agent", "") or "")[:512]
        for d in snap.defs:
            for rule in d.rules:
                if rule.matches(protocol, headers):
                    return AgentHit(d.id, d.name, d.color, ua)
        return AgentHit(_UNKNOWN_ID, _UNKNOWN_NAME, _UNKNOWN_COLOR, ua)

    def all(self) -> list[AgentDef]:
        return list(self._defs)

    def policy(self, agent_id: str):  # noqa: ARG002 — 预留策略接缝，恒 None
        return None

    # ── internal ──

    def _collect_defs(self) -> list[AgentDef]:
        defs: list[AgentDef] = []
        index: dict[str, int] = {}
        for base, source in ((self._builtin_dir, "builtin"), (self._user_dir, "user")):
            if not base.is_dir():
                continue
            for path in sorted(base.glob("*.yaml")):
                d = self._read_def(path, source)
                if d is None:
                    continue
                idx = index.get(d.id)
                if idx is not None:
                    defs[idx] = d  # 用户目录覆盖内置（同 id）；保持内置原位置（排序稳定）
                else:
                    index[d.id] = len(defs)
                    defs.append(d)
        return defs

    def _read_def(self, path: Path, source: str) -> AgentDef | None:
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as e:
            log.error("agents.d: skip unreadable file %s (%s)", path, e)
            return None
        if not isinstance(raw, dict):
            log.warning("agents.d: %s is not a mapping, skipped", path)
            return None
        aid = raw.get("id")
        if not isinstance(aid, str) or not _ROOT_ID_RE.match(aid) or aid == _UNKNOWN_ID:
            log.warning("agents.d: %s has invalid/reserved id %r, skipped", path, aid)
            return None
        rules: list[MatchRule] = []
        for m in raw.get("match") or []:
            if isinstance(m, dict):
                rule = MatchRule.build(m)
                if rule is not None:
                    rules.append(rule)
        if not rules:
            log.warning("agents.d: %s has no usable rules, skipped", path)
            return None
        return AgentDef(
            id=aid,
            name=str(raw.get("name") or aid)[:40],
            color=str(raw.get("color") or _UNKNOWN_COLOR),
            rules=tuple(rules),
            source=source,
        )

    def add_from_ui(self, id: str, name: str, header: str,
                    pattern: str, color: str) -> AgentDef:
        """一键认领：校验 → 原子落盘用户目录 → reload → 返回新 def。

        Raises:
            ValueError: id/header/pattern 非法或为保留字
            KeyError:   id 已存在（新建语义）
        """
        if not _ROOT_ID_RE.match(id) or id == _UNKNOWN_ID:
            raise ValueError(f"invalid agent id: {id!r}")
        if header not in _HEADER_WHITELIST:
            raise ValueError(f"invalid header signal: {header!r}")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"bad regex: {pattern!r} ({e})") from e
        if any(d.id == id for d in self._defs):
            raise KeyError(f"agent id already exists: {id}")
        self._user_dir.mkdir(parents=True, exist_ok=True)
        path = self._user_dir / f"{id}.yaml"
        agent = {"id": id, "name": name, "color": color,
                 "match": [{"header": header, "regex": pattern}]}
        comment = "# auto-generated {ts} (dashboard one-click) — 编辑后 iff reload 生效\n".format(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(comment + yaml.safe_dump(agent, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        os.replace(tmp, path)  # 原子替换：不残留 .tmp，并发写最后者胜
        self.reload()
        return self._find(id)

    def remove(self, id: str) -> None:
        """删除用户目录 agent（管理模式）。内置/保留字/不存在 → 抛 ValueError。"""
        d = self._find(id)
        if d is None:
            raise ValueError(f"agent not found: {id}")
        if d.source != "user":
            raise ValueError(f"builtin agent cannot be removed: {id}")
        path = self._user_dir / f"{id}.yaml"
        if path.exists():
            path.unlink()
        self.reload()

    def _find(self, id: str) -> AgentDef | None:
        return next((d for d in self._defs if d.id == id), None)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_agent_registry.py -v --tb=short`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add inferfabric/agent_registry.py tests/unit/proxy/test_agent_registry.py
git commit -m "feat(agents): AgentRegistry 识别引擎 — classify/first-match/unknown 兜底/热重载/一键认领落盘

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 内置 agents.d 默认规则 + 双目录合并覆盖

**Files:**
- Create: `inferfabric/agents.d/claude-code.yaml`（及同目录 claude-desktop / cursor / codex）
- Modify: `tests/unit/proxy/test_agent_registry.py`（追加合并/覆盖/坏文件用例）

**Interfaces:**
- Consumes: `AgentRegistry(builtin_dir, user_dir)`（Task 1）
- Produces: 出厂 4 个内置 Agent matcher（`id/name/color/match` 齐全）；测试钉死内置可加载、用户目录可覆盖内置

- [ ] **Step 1: 写内置规则文件**

`inferfabric/agents.d/claude-code.yaml`（其余三个同构，见下表）：

```yaml
# claude-code.yaml — 出厂默认（内置目录，git 管理；用户覆盖同 id 放 ~/.inferfabric/agents.d/）
id: claude-code
name: Claude Code
color: "#d97757"
match:
  - { header: x-app, value: cli }
  - { header: user-agent, regex: "^claude-cli/" }
```

| 文件 | id | name | color | match |
|---|---|---|---|---|
| claude-code.yaml | claude-code | Claude Code | #d97757 | `x-app: cli`；`user-agent: ^claude-cli/` |
| claude-desktop.yaml | claude-desktop | Claude Desktop | #8b5cf6 | `x-app: desktop` |
| cursor.yaml | cursor | Cursor | #38bdf8 | `user-agent: Cursor\|Windsurf` |
| codex.yaml | codex | Codex CLI | #34d399 | `user-agent: codex\|openai` |

- [ ] **Step 2: 写失败测试（内置加载 + 覆盖 + 坏文件不炸）**

在 `tests/unit/proxy/test_agent_registry.py` 追加：

```python
class TestMergeAndBuiltins:
    """双目录合并：内置可加载、用户覆盖内置、坏文件跳过、reload fail-closed。"""

    def test_builtin_defaults_load(self, tmp_path):
        from inferfabric.agent_registry import AgentRegistry, _UNKNOWN_ID
        import inferfabric.agent_registry as ar
        builtin = Path(ar.__file__).parent / "agents.d"
        reg = AgentRegistry(builtin, tmp_path / "user")
        ids = {d.id for d in reg.all()}
        assert {"claude-code", "claude-desktop", "cursor", "codex"} <= ids

    def test_user_overrides_builtin_by_id(self, tmp_path):
        ov = """
id: claude-code
name: 我的自用
color: "#111111"
match:
  - { header: user-agent, regex: "^my-cc" }
"""
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        u = tmp_path / "user"; u.mkdir()
        (u / "cc.yaml").write_text(ov, encoding="utf-8")
        reg = AgentRegistry(b, u)
        cc = [d for d in reg.all() if d.id == "claude-code"][0]
        assert cc.name == "我的自用" and cc.source == "user"
        # 覆盖后按新规则分类
        assert reg.classify("openai", {"User-Agent": "my-cc/1"}).agent == "claude-code"

    def test_broken_user_file_skipped_others_ok(self, tmp_path):
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        u = tmp_path / "user"; u.mkdir()
        (u / "broken.yaml").write_text("{{{{ not yaml", encoding="utf-8")
        (u / "ok.yaml").write_text(CURSOR, encoding="utf-8")
        reg = AgentRegistry(b, u)
        ids = {d.id for d in reg.all()}
        assert "claude-code" in ids and "cursor" in ids   # 坏文件不影响好文件
        assert reg.classify("openai", {"User-Agent": "C/1"}).agent == _UNKNOWN_ID
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_registry.py -k "MergeAndBuiltins" -v --tb=short`
Expected: FAIL（`inferfabric/agents.d` 不存在 → `test_builtin_defaults_load` 断言失败；其余因 Task 1 `_collect_defs` 已实现可能部分通过——失败即可）

- [ ] **Step 4: 创建 4 个内置文件并让测试通过**

Run: 创建上述 4 个 YAML 后执行
`python3 -m pytest tests/unit/proxy/test_agent_registry.py -v --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add inferfabric/agents.d/ tests/unit/proxy/test_agent_registry.py
git commit -m "feat(agents): 内置 agents.d 默认 matcher（Claude Code/Desktop/Cursor/Codex）+ 双目录覆盖

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: add_from_ui 校验/原子写 + remove + reload fail-closed 测试

**Files:**
- Modify: `tests/unit/proxy/test_agent_registry.py`（追加管理/校验用例）
- 无产品代码改动（Task 1 已实现 add_from_ui/remove/reload；本任务负责把它们的行为钉死）

**Interfaces:**
- Consumes: `AgentRegistry.add_from_ui(id, name, header, pattern, color) / remove(id) / reload()`（Task 1）
- Produces: 钉死约定——`ValueError`（非法 id/保留字/header/regex）、`KeyError`（重名 409）、原子写无 `.tmp` 残留、deploy 后 classify 立即命中、`remove` 拒绝内置、reload 抛异常保留旧规则

- [ ] **Step 1: 写失败测试**

```python
class TestClaimAndManage:
    def test_claim_creates_file_and_reloads(self, tmp_path):
        r = _mk(tmp_path)
        d = r.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#94a3b8")
        assert d.id == "curl-cli" and d.source == "user"
        # 落盘文件可读且内容吻合
        ua_file = tmp_path / "user" / "curl-cli.yaml"
        assert ua_file.exists()
        hit = r.classify("openai", {"User-Agent": "curl/8.5.2"})
        assert hit.agent == "curl-cli"

    def test_claim_no_tmp_leftover(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")
        assert not list((tmp_path / "user").glob("*.tmp"))

    def test_claim_duplicate_raises_keyerror(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")
        with pytest.raises(KeyError):
            r.add_from_ui("a1", "a1", "user-agent", "^a1", "#000")

    def test_claim_invalid_inputs(self, tmp_path):
        r = _mk(tmp_path)
        with pytest.raises(ValueError):
            r.add_from_ui("Bad ID!", "x", "user-agent", "^x", "#000")
        with pytest.raises(ValueError):
            r.add_from_ui("unknown", "x", "user-agent", "^x", "#000")   # 保留字
        with pytest.raises(ValueError):
            r.add_from_ui("ok", "x", "x-custom", "^x", "#000")          # header 白名单外
        with pytest.raises(ValueError):
            r.add_from_ui("ok", "x", "user-agent", "(", "#000")         # 坏 regex

    def test_remove_user_agent_allowed(self, tmp_path):
        r = _mk(tmp_path)
        r.add_from_ui("curl-cli", "curl-cli", "user-agent", "^curl", "#000")
        r.remove("curl-cli")
        assert r.classify("openai", {"User-Agent": "curl/8"}).agent == _UNKNOWN_ID
        assert not (tmp_path / "user" / "curl-cli.yaml").exists()

    def test_remove_builtin_rejected(self, tmp_path):
        b = tmp_path / "builtin"; b.mkdir()
        (b / "cc.yaml").write_text(CC, encoding="utf-8")
        r = AgentRegistry(b, tmp_path / "user")
        with pytest.raises(ValueError):
            r.remove("claude-code")

    def test_reload_failure_keeps_old(self, tmp_path, monkeypatch):
        r = _mk(tmp_path, builtin=[("cc.yaml", CC)])
        before = r.classify("anthropic", {"x-app": "cli"}).agent
        def boom():
            raise OSError("disk lost")
        monkeypatch.setattr(r, "_collect_defs", boom)
        assert r.reload() is False
        assert r.classify("anthropic", {"x-app": "cli"}).agent == before
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_registry.py -k "ClaimAndManage" -v --tb=short`
Expected: 部分失败（`test_claim_no_tmp_leftover` 等；失败说明行为未钉住——本任务如实现已可过则全部 PASS 也接受，但必须至少有一次红→绿循环：先跑确认现状）

说明：`add_from_ui/remove` 已在 Task 1 Step 3 实现。若本步全绿，则把它当作「行为回归钉死」而非红→绿；若个别失败（如设计遗漏），修实现直至全绿。

- [ ] **Step 3: 修复实现（如出现红）** — 按失败点修 `agent_registry.py`，不引入新依赖
- [ ] **Step 4: 全绿**

Run: `python3 -m pytest tests/unit/proxy/test_agent_registry.py -v --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add inferfabric/agent_registry.py tests/unit/proxy/test_agent_registry.py
git commit -m "test(agents): 一键认领/删除/reload fail-closed 行为钉死

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: v008 迁移 + RequestLog 字段 + DB 写入

**Files:**
- Create: `inferfabric/migrations/v008_add_agent_ua.py`
- Modify: `inferfabric/migrations/__init__.py`（注册 v008）
- Modify: `inferfabric/proxy/request_logger.py`（RequestLog 加 `agent/ua`；DB dict 带上）
- Modify: `inferfabric/db.py`（`insert_request_log` SQL 加两列；旧 entry 缺键兜底）
- Test: `tests/unit/config/test_migration_v008.py`、`tests/integration/test_request_log_db.py`（追加）

**Interfaces:**
- Consumes: `IFFDB` + 迁移注册机制（`inferfabric.migrations`）；`RequestLog` dataclass
- Produces: `RequestLog.agent: str = ""`、`RequestLog.ua: str = ""`；`request_log` 表新增 `agent`、`ua` 列；`insert_request_log` 幂等兼容旧 entry

- [ ] **Step 1: 写失败测试（迁移幂等 + 字段持久化）**

`tests/unit/config/test_migration_v008.py`：

```python
"""unit/config/test_migration_v008.py — request_log 增列 agent/ua 迁移测试。"""
import sqlite3
from inferfabric.db import IFFDB, REQUEST_LOG_DB


def _migrate(tmp_path):
    db = IFFDB(tmp_path)
    import inferfabric.migrations  # noqa: F401 — 注册迁移
    db._run_migrations()
    return db


def test_v008_adds_columns(tmp_path):
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    assert "agent" in cols and "ua" in cols
    db.close()


def test_v008_idempotent_on_existing(tmp_path):
    """旧库升级：已存在该列不应报错；历史行默认 ''。"""
    import sqlite3
    p = tmp_path / "request_log.db"
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE request_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL UNIQUE,
        key_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
        status INTEGER NOT NULL, ttft_ms REAL, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, duration_ms REAL NOT NULL DEFAULT 0.0,
        route TEXT NOT NULL DEFAULT 'local', cloud_provider TEXT, error TEXT,
        timestamp REAL NOT NULL, ts TEXT NOT NULL DEFAULT '',
        cost TEXT DEFAULT 'local', metadata TEXT DEFAULT '{}')""")
    conn.execute("PRAGMA user_version = 6")
    conn.execute("INSERT INTO request_log (req_id, model, status, timestamp) "
                 "VALUES ('r1', 'm', 200, 1.0)")
    conn.commit(); conn.close()
    db = _migrate(tmp_path)
    with db.connect(REQUEST_LOG_DB) as conn:
        row = conn.execute("SELECT agent, ua FROM request_log WHERE req_id='r1'").fetchone()
    assert row[0] == "" and row[1] == ""
    db.close()
```

`tests/integration/test_request_log_db.py` 追加：

```python
def test_insert_and_query_agent_fields(tmp_iffdb):
    """agent/ua 字段经 IFFDB 写入/读出往返一致。"""
    import inferfabric.migrations  # noqa: F401
    db = tmp_iffdb
    db.insert_request_log([{
        "req_id": "a1", "key_name": "primary", "model": "qwen38",
        "status": 200, "tokens_in": 10, "tokens_out": 5,
        "timestamp": 1000.0, "ts": "t", "route": "local",
        "agent": "claude-code", "ua": "claude-cli/2.0",
    }])
    rows = db.query_request_log(since=0)
    assert rows[0]["agent"] == "claude-code" and rows[0]["ua"] == "claude-cli/2.0"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/config/test_migration_v008.py tests/integration/test_request_log_db.py -v --tb=short`
Expected: FAIL（column agent/ua 不存在；`RequestLog` 无字段）

- [ ] **Step 3: 实现迁移 + 字段**

`inferfabric/migrations/v008_add_agent_ua.py`：

```python
"""Migration 008: request_log 增列 agent/ua — 客户端 Agent 识别（v6.5）。"""


def upgrade(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_log)").fetchall()]
    if "agent" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN agent TEXT NOT NULL DEFAULT ''")
    if "ua" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN ua TEXT NOT NULL DEFAULT ''")
    conn.commit()
```

`inferfabric/migrations/__init__.py`：在 v007 之后补

```python
from inferfabric.migrations import v008_add_agent_ua
# ...
IFFDB.register_migration(8, "request_log", "add_agent_ua")
```

`inferfabric/proxy/request_logger.py`：`RequestLog` 加两字段

```python
    tokens_out: int = 0
    agent: str = ""      # 客户端 Agent id（agents.d 归一化，unknown 兜底；v6.5）
    ua: str = ""         # 原始 User-Agent（认领学习回路原料）
    duration_ms: float = 0.0
```

`RequestLogger.log()` 的 SQLite dict（现 ~107-122 行）加两键：

```python
            d = {
                "req_id": entry.req_id,
                "key_name": entry.key_name,
                "model": entry.model,
                "status": entry.status,
                "ttft_ms": entry.ttft_ms,
                "tokens_in": entry.tokens_in,
                "tokens_in_cached": entry.tokens_in_cached,
                "tokens_out": entry.tokens_out,
                "agent": entry.agent,
                "ua": entry.ua,
                "duration_ms": entry.duration_ms,
                "route": entry.route,
                "cloud_provider": entry.cloud_provider,
                "error": entry.error,
                "timestamp": entry.timestamp,
                "ts": entry.ts,
            }
```

`inferfabric/db.py` `insert_request_log`（现 338-357 行）：SQL 加两列、entry 兜底

```python
        entries = [dict(e,
                        tokens_in_cached=e.get("tokens_in_cached", 0),
                        agent=e.get("agent", ""),
                        ua=e.get("ua", "")) for e in entries]
        with self._write_lock:
            with self.connect(REQUEST_LOG_DB) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO request_log "
                    "(req_id, key_name, model, status, ttft_ms, tokens_in, "
                    " tokens_in_cached, tokens_out, agent, ua, "
                    " duration_ms, route, cloud_provider, error, timestamp, ts) "
                    "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                    ":tokens_in_cached, :tokens_out, :agent, :ua, "
                    ":duration_ms, :route, :cloud_provider, :error, "
                    ":timestamp, :ts)",
                    entries,
                )
                conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/config/test_migration_v008.py tests/integration/test_request_log_db.py tests/unit/ -v --tb=short`
Expected: PASS（含既有测试——确认 SQL 改动不破坏旧路径）

- [ ] **Step 5: 提交**

```bash
git add inferfabric/migrations/ tests/unit/config/test_migration_v008.py tests/integration/test_request_log_db.py inferfabric/proxy/request_logger.py inferfabric/db.py
git commit -m "feat(agents): v008 request_log 增列 agent/ua + RequestLog 字段 + DB 写入

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 请求入口采集 + 全部 RequestLog 站点带 agent/ua

**Files:**
- Modify: `inferfabric/proxy_manager.py`（实例化 `AgentRegistry`，挂 `pm.agent_registry`）
- Modify: `inferfabric/proxy/handler.py`（消息入口 ~406-418 行、转发入口 ~1868-1878 行：capture + 所有 11 处 `RequestLog(` 加参）
- Modify: `inferfabric/proxy/chat_handlers.py`（入口 ~209-210 行 + 8 处 `RequestLog(` 加参）
- Test: `tests/unit/proxy/test_agent_capture.py`

**Interfaces:**
- Consumes: `AgentRegistry.classify` / `request_protocol`（Task 1）、`RequestLog.agent/ua`（Task 4）
- Produces: 约定 `handler._agent_hit`（classify 结果，每请求一次缓存）；所有 request_log 行携带 agent/ua

- [ ] **Step 1: 写失败测试（采集接线 + 协议先验）**

`tests/unit/proxy/test_agent_capture.py`：

```python
"""unit/proxy/test_agent_capture.py — 请求入口 agent 采集接线测试。

验证 proxy_manager 接线：pm.agent_registry 存在；入口按路径算协议并
调 classify 缓存到 handler._agent_hit；RequestLog 携带 agent/ua。
"""
from pathlib import Path
from types import SimpleNamespace
import inferfabric.agent_registry as ar
from inferfabric.agent_registry import AgentRegistry, request_protocol


def _reg(tmp_path):
    b = tmp_path / "builtin"; b.mkdir()
    (b / "cc.yaml").write_text("""
id: claude-code
name: Claude Code
color: "#d97757"
match:
  - { header: x-app, value: cli }
""", encoding="utf-8")
    return AgentRegistry(b, tmp_path / "user")


def test_proxy_manager_exposes_agent_registry():
    """agent_registry 挂为 pm.agent_registry（monkeypatch 建临 pm 验证属性布线）。"""
    # 预览：proxy_manager 将在 __init__ 增加 self.agent_registry；此处只验类存在
    assert hasattr(AgentRegistry, "classify")


def test_capture_sets_handler_agent_hit(tmp_path):
    reg = _reg(tmp_path)
    h = SimpleNamespace(path="/v1/messages", headers={"X-App": "cli"})
    # 与 handler.py 入口完全相同的语句（采集点接线）
    h._agent_hit = reg.classify(request_protocol(h.path), h.headers)
    assert h._agent_hit.agent == "claude-code"
    assert request_protocol("/v1/messages") == "anthropic"


def test_request_log_fields_flow_to_logger_dict():
    """RequestLog.agent/ua 进入 logger 的 DB dict（与 Task 4 持久化衔接）。"""
    from inferfabric.proxy.request_logger import RequestLog
    e = RequestLog(req_id="r", key_name="k", model="m", status=200,
                   agent="claude-code", ua="claude-cli/2.0")
    assert e.agent == "claude-code" and e.ua == "claude-cli/2.0"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_capture.py -v --tb=short`
Expected: FAIL（`pm.agent_registry` 未接线）

- [ ] **Step 3: proxy_manager 接线**

`inferfabric/proxy_manager.py`：顶部 import

```python
from inferfabric.agent_registry import AgentRegistry
```

在 `self.auth = AuthManager(...)`（~62 行）旁加：

```python
        # v6.5: 客户端 Agent 识别引擎（agents.d 双目录；用户目录 ~/.inferfabric/agents.d）
        self.agent_registry = AgentRegistry(
            builtin_dir=Path(__file__).parent / "agents.d",
            user_dir=IFF_DATA_DIR / "agents.d",
        )
```

确认 `IFF_DATA_DIR` 在该模块可用（proxy_manager 已用它建 auth）。

- [ ] **Step 4: handler.py 采集（两处入口）**

**入口 1：消息入口（~406-418 行，与 `key_name` 并列）**

```python
        key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
        # v6.5: 客户端 Agent 分类（每请求一次，缓存到 handler 供所有 RequestLog 站点取值）
        self._agent_hit = pm.agent_registry.classify(
            request_protocol(self.path), self.headers)
        # G-1b: 挂到 handler 上，供 _forward_local / forwarder 写 RequestLog
        self._req_id = req_id
        self._req_start = req_start
        self._key_name = key_name
```

（`request_protocol` 从 `inferfabric.agent_registry` import。）

**入口 2：转发入口（~1868-1878 行），在 `key_name = ...` 后加**

```python
        # v6.5: 客户端 Agent 分类（缓存到 handler）
        self._agent_hit = pm.agent_registry.classify(
            request_protocol(self.path), self.headers)
```

**全部 RequestLog 构造点加两个参数**（机械操作，覆盖下列行号；取 `self._agent_hit.agent / .ua`）：

- `handler.py`：428, 471, 509, 532, 554, 591, 612, 652, 1874, 2019, 2088
- `chat_handlers.py`：入口 ~210 行加 capture，构造点 222, 265, 284, 320, 333, 377, 393, 404

统一模式（对每个站点插入 `agent=..., ua=...`，取自已缓存的 hit）：

```python
                pm.logger.log(RequestLog(
                    req_id=req_id, key_name=key_name, model=model,
                    status=..., error=..., duration_ms=...,
                    agent=self._agent_hit.agent if hasattr(self, "_agent_hit") else "",
                    ua=self._agent_hit.ua if hasattr(self, "_agent_hit") else "",
                ))
```

`chat_handlers.py` 用 `handler` 而非 `self`，捕获入口在 ~209-210 行：

```python
    auth_header = handler.headers.get("Authorization", "") or handler.headers.get("x-api-key", "")
    key_name = pm.auth.key_name(auth_header) if pm.auth.enabled else "anonymous"
    # v6.5: 客户端 Agent 分类（每请求一次）
    handler._agent_hit = pm.agent_registry.classify(
        request_protocol(handler.path), handler.headers)
```

（`chat_handlers.py` 顶部 import `request_protocol`。）

**边缘注意**：某些失败路径 TypeError 前已 401/404 直接 `_send_json`——它们的 RequestLog 同样带上 agent/ua（同一 entry 风格）。

- [ ] **Step 5: 运行全部单测看回归**

Run: `python3 -m pytest tests/unit/ -v --tb=short`
Expected: PASS（新增用例全绿；既有 handler/chat 用例不受影响——`request_protocol`/`classify` 均为只读）

- [ ] **Step 6: 提交**

```bash
git add inferfabric/proxy_manager.py inferfabric/proxy/handler.py inferfabric/proxy/chat_handlers.py tests/unit/proxy/test_agent_capture.py
git commit -m "feat(agents): 请求入口采集 agent/ua（handler 缓存每请求一次，全站点带参）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: cost_of_row 抽取 + /api/agent-stats 聚合

**Files:**
- Modify: `inferfabric/metrics_aggregator.py`（模块级 `cost_of_row(prices, row)`）
- Modify: `inferfabric/proxy/handler.py`（`_handle_token_curve` 改用共享 helper；新增 `_handle_agent_stats`；`_GET_ROUTES` 注册 `/api/agent-stats`）
- Create: `inferfabric/agent_stats.py`（纯聚合函数 `aggregate_agent_stats` + 粒度规格）
- Test: `tests/unit/proxy/test_agent_stats.py`（纯函数）、`tests/unit/proxy/test_agent_endpoints.py`（HTTP 桩）

**Interfaces:**
- Consumes: `RequestLog` 表行（含 agent/ua，Task 4）、`pm.metrics.price_config`、`pm.agent_registry.all()`、`pm.telemetry.query_request_log(since, limit)`
- Produces:
  - `cost_of_row(prices: dict, row: Mapping) -> float`（¥，云路由按价入/价出；非云/未配价 → 0.0）
  - `AGENT_GRAN: dict[str, dict]`（minute/hour/day/week 规格，与 token-curve 同 since/n/width_s）
  - `aggregate_agent_stats(rows, gran, scope, prices, meta) -> dict`
    （返回 `{"n","width_s","window_requests","series","totals","unassigned"}`；`series[tid] = [{x, requests, tokens, cost}]`；`totals` 按 requests 排序、unknown 置顶，含 name/color/source/top_models/ttft/tokens/cost/pct；`unassigned` = unknown 去重 UA top20 每条截 120）

- [ ] **Step 1: 写失败测试（纯聚合；钉 Review Focus 3 混存）**

`tests/unit/proxy/test_agent_stats.py`：

```python
"""unit/proxy/test_agent_stats.py — /api/agent-stats 纯聚合函数测试。

钉 Review Focus 3：历史行 agent='' 与 'unknown' 混存 → 同一个未识别桶。
"""
from inferfabric.agent_stats import AGENT_GRAN, aggregate_agent_stats
from inferfabric.metrics_aggregator import CloudModelPrice, cost_of_row

PRICES = {"glm-5.1": CloudModelPrice(price_input=5.0, price_output=15.0)}


def _row(**kw):
    base = dict(req_id="r", key_name="k", model="glm-5.1", status=200,
                tokens_in=1_000_000, tokens_out=0, timestamp=0.0,
                cloud_provider="baidu", agent="codex", ua="codex/1")
    base.update(kw)
    return base


class TestCostOfRow:
    def test_cloud_priced(self):
        assert cost_of_row(PRICES, _row(cloud_provider="baidu")) == 5.0  # 1M in @ ¥5/1M

    def test_local_zero(self):
        assert cost_of_row(PRICES, _row(cloud_provider=None)) == 0.0

    def test_unpriced_zero(self):
        assert cost_of_row({}, _row(cloud_provider="baidu")) == 0.0


NOW = 1000.0  # 注入的墙钟右缘：bucket 窗口相对它，测试可断言

class TestAggregate:
    def test_basic_bucketing(self):
        rows = [
            _row(timestamp=NOW - 50, agent="claude-code", ua="cc"),
            _row(timestamp=NOW - 60, agent="codex", ua="cx"),
        ]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES,
                                    {"claude-code": {"name": "Claude Code", "color": "#1", "source": "builtin"}},
                                    now=NOW)
        assert out["window_requests"] == 2
        cc = next(t for t in out["totals"] if t["agent"] == "claude-code")
        assert cc["requests"] == 1 and cc["top_models"][0]["model"] == "glm-5.1"
        cx = next(t for t in out["totals"] if t["agent"] == "codex")
        assert cx["cost_yuan"] == 5.0
        # 两个请求都落在「最近」桶（idx = n-1）
        assert out["series"]["claude-code"][-1]["requests"] == 1

    def test_empty_and_unknown_normalized(self):
        """历史 '' 与 'unknown' 混存 → 单一未识别桶。"""
        rows = [_row(timestamp=NOW - 10, agent="", ua="curl/8"),
                _row(timestamp=NOW - 20, agent="unknown", ua="python-requests/2")]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        ids = [t["agent"] for t in out["totals"]]
        assert ids == ["unknown"]
        unk = out["totals"][0]
        assert unk["requests"] == 2
        assert {u["ua"] for u in out["unassigned"]} == {"curl/8", "python-requests/2"}

    def test_scope_local_filters_cloud(self):
        rows = [_row(timestamp=NOW - 10, cloud_provider=None),
                _row(timestamp=NOW - 10, cloud_provider="baidu")]
        lok = aggregate_agent_stats(rows, "hour", "local", PRICES, {}, now=NOW)
        assert lok["window_requests"] == 1
        clo = aggregate_agent_stats(rows, "hour", "cloud", PRICES, {}, now=NOW)
        assert clo["window_requests"] == 1

    def test_unassigned_top20_truncated(self):
        rows = [_row(timestamp=NOW - 10, agent="", ua="x" * 300 + "/1") for _ in range(25)]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        assert len(out["unassigned"]) <= 20
        assert all(len(u["ua"]) <= 120 for u in out["unassigned"])

    def test_unknown_row_kept_with_ua(self):
        rows = [_row(timestamp=NOW - 10, agent="unknown", ua="curl/8.5.2")]
        out = aggregate_agent_stats(rows, "hour", "all", PRICES, {}, now=NOW)
        assert out["totals"][0]["agent"] == "unknown"
```

`tests/unit/proxy/test_agent_endpoints.py`（HTTP 桩，仿 test_power_endpoint 范式）——先只写最小断言（后续 Task 7 复用同一桩）：

```python
"""unit/proxy/test_agent_endpoints.py — /api/agent-* 端点测试（SimpleNamespace 桩）。"""
from types import SimpleNamespace
from inferfabric.proxy.handler import ProxyHandler


def _handler(path):
    h = ProxyHandler.__new__(ProxyHandler)
    h.path = path
    h.command = "GET"
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def test_agent_stats_invalid_gran_400():
    h = _handler("/api/agent-stats?granularity=bogus")
    pm = SimpleNamespace()
    h._handle_agent_stats(pm)
    assert h._sent[-1][0] == 400
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_stats.py tests/unit/proxy/test_agent_endpoints.py -v --tb=short`
Expected: FAIL（`inferfabric.agent_stats` 不存在；`cost_of_row` 未定义；`_handle_agent_stats` 未定义）

- [ ] **Step 3: 实现 cost_of_row（metrics_aggregator.py 顶部）**

```python
def cost_of_row(prices: dict, row: Mapping) -> float:
    """单行请求费用（¥）：仅云端路由按价格表逐请求计；本地/未配价 → 0.0。

    token-curve 与 agent-stats 共用的唯一计价入口（口径防漂移，设计 §4）。
    """
    if not row.get("cloud_provider"):
        return 0.0
    try:
        tin = int(row.get("tokens_in") or 0)
        tout = int(row.get("tokens_out") or 0)
    except (ValueError, TypeError):
        return 0.0
    p = prices.get(row.get("model") or "")
    if p is None:
        return 0.0
    return (tin / 1e6) * p.price_input + (tout / 1e6) * p.price_output
```

（`Mapping` 从 `typing` import；`CloudModelPrice` 已在该模块定义。）

- [ ] **Step 4: 实现 agent_stats.py 聚合**

```python
"""inferfabric/agent_stats.py — 客户端 Agent 统计纯聚合（v6.5）。

从 request_log 行做时间分桶 × Agent 分组；只依赖本模块 + metrics_aggregator，
不依赖 HTTP/DB——便于单测。数据源与语义对齐 /api/token-curve。
"""
from __future__ import annotations

import time
from collections import defaultdict, Counter
from typing import Mapping

from inferfabric.metrics_aggregator import cost_of_row

AGENT_GRAN = {
    "minute": {"since": 3600,       "n": 12, "width_s": 5 * 60},
    "hour":   {"since": 24 * 3600,  "n": 24, "width_s": 3600},
    "day":    {"since": 30 * 86400, "n": 30, "width_s": 86400},
    "week":   {"since": 90 * 86400, "n": 13, "width_s": 7 * 86400},
}

_UNKNOWN_META = {"name": "未识别", "color": "#9ca3af", "source": "observed"}


def _empty_buckets(n):
    return [{"x": i, "requests": 0, "tokens": 0, "cost": 0.0} for i in range(n)]


def aggregate_agent_stats(rows, gran, scope, prices, meta,
                          now: float | None = None) -> dict:
    """rows: request_log dict 列表; gran/scope 见端点; meta: {agent: {name,color,source}};
    now: 墙钟右缘（缺省 time.time()；测试注入固定值保证分桶可断言）。"""
    spec = AGENT_GRAN[gran]
    n, w_s = spec["n"], spec["width_s"]
    now = time.time() if now is None else now

    def in_scope(r):
        cp = r.get("cloud_provider")
        if scope == "local":
            return not cp
        if scope == "cloud":
            return bool(cp)
        return True

    series: dict[str, list] = {}
    totals: dict[str, dict] = {}
    unk_counter: Counter = Counter()
    for r in rows:
        if not in_scope(r):
            continue
        ts = r.get("timestamp")
        if not ts:
            continue
        idx = n - 1 - int((now - ts) // w_s)
        if not (0 <= idx < n):
            continue
        agent = r.get("agent") or "unknown"
        ser = series.setdefault(agent, _empty_buckets(n))
        tin = int(r.get("tokens_in") or 0)
        tout = int(r.get("tokens_out") or 0)
        b = ser[idx]
        b["requests"] += 1
        b["tokens"] += tin + tout
        b["cost"] += cost_of_row(prices, r)
        agg = totals.setdefault(agent, {
            "agent": agent, "name": agent, "color": "#9ca3af", "source": "observed",
            "requests": 0, "success": 0, "errors": 0, "success_rate": 0.0,
            "tokens_in": 0, "tokens_out": 0, "cost_yuan": 0.0,
            "ttft": [], "top_models": Counter(),
        })
        agg["requests"] += 1
        agg["success"] += 1 if (r.get("status") or 0) < 400 and not r.get("error") else 0
        agg["errors"] += 0 if (r.get("status") or 0) < 400 and not r.get("error") else 1
        agg["tokens_in"] += tin
        agg["tokens_out"] += tout
        agg["cost_yuan"] += cost_of_row(prices, r)
        ttft = r.get("ttft_ms")
        if ttft and ttft > 0 and (r.get("status") or 0) < 400:
            agg["ttft"].append(float(ttft))
        agg["top_models"][r.get("model") or ""] += 1
        if agent == "unknown":
            ua = str(r.get("ua") or "")[:120]
            if ua:
                unk_counter[ua] += 1

    result: list[dict] = []
    for agent, agg in totals.items():
        m = meta.get(agent, _UNKNOWN_META)
        ttfts = sorted(agg["ttft"])
        def _q(q):
            if not ttfts:
                return None
            k = max(1, round(len(ttfts) * q)) - 1
            return round(ttfts[k], 1)
        top = [{"model": mname, "requests": c}
               for mname, c in agg["top_models"].most_common(3)]
        result.append({
            "agent": agent, "name": m.get("name", agent), "color": m.get("color", "#9ca3af"),
            "source": m.get("source", "observed"),
            "requests": agg["requests"], "success": agg["success"], "errors": agg["errors"],
            "success_rate": round(agg["success"] / max(agg["requests"], 1) * 100, 1),
            "tokens_in": agg["tokens_in"], "tokens_out": agg["tokens_out"],
            "cost_yuan": round(agg["cost_yuan"], 4),
            "ttft_p50": _q(0.50), "ttft_p95": _q(0.95), "top_models": top,
        })

    total_req = max(sum(t["requests"] for t in result), 1)
    for t in result:
        t["pct_of_requests"] = round(t["requests"] / total_req * 100, 1)
    result.sort(key=lambda t: -t["requests"])
    unk = next((t for t in result if t["agent"] == "unknown"), None)
    if unk:
        result.remove(unk)
        result.insert(0, unk)  # 未识别置顶（设计 §5）
    for _, ser in series.items():
        for b in ser:
            b["cost"] = round(b["cost"], 4)

    unassigned = [{"ua": u, "requests": c} for u, c in unk_counter.most_common(20)]
    return {
        "n": n, "width_s": w_s, "window_requests": sum(t["requests"] for t in result),
        "series": series, "totals": result, "unassigned": unassigned,
    }
```

- [ ] **Step 5: 实现 handler `_handle_agent_stats` + 路由 + token-curve 换 helper**

`handler.py` 顶部 import：

```python
from inferfabric.agent_stats import AGENT_GRAN, aggregate_agent_stats
from inferfabric.metrics_aggregator import cost_of_row
```

新增方法（放在 `_handle_token_curve` 之后）：

```python
    def _handle_agent_stats(self, pm):
        """GET /api/agent-stats?granularity=minute|hour|day|week&scope=all|local|cloud

        客户端 Agent 用量分桶（v6.5）。数据源 = request_log（SQLite），
        费用经 cost_of_row 与 /api/token-curve 同口径。"""
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(self.path).query or "")
            g = (qs.get("granularity", ["hour"])[0]).lower()
            if g not in AGENT_GRAN:
                self._send_json({"error": f"invalid granularity: {g}"}, 400)
                return
            scope = (qs.get("scope", ["all"])[0]).lower()
            if scope not in ("all", "local", "cloud"):
                self._send_json({"error": f"invalid scope: {scope}"}, 400)
                return
            spec = AGENT_GRAN[g]
            since = int(time.time() - spec["since"])
            rows = pm.telemetry.query_request_log(since=since, limit=100000)
            meta = {d.id: {"name": d.name, "color": d.color, "source": d.source}
                    for d in pm.agent_registry.all()}
            res = aggregate_agent_stats(rows, g, scope, pm.metrics.price_config, meta)
            res["granularity"] = g
            self._send_json(res, 200)
        except Exception as e:
            log.error("/api/agent-stats failed: %s", e)
            self._send_json({"error": "agent stats unavailable"}, 500)
```

`_GET_ROUTES` 加一行（~199 行 /api/snapshot 附近）：

```python
    "/api/agent-stats":        lambda h, pm: h._handle_agent_stats(pm),
```

`_handle_token_curve`（~1057-1099 行）：删掉内联计价循环，换成共享 helper：

```python
                if r.get("cloud_provider"):
                    target[idx]["cost"] += cost_of_row(prices, r)
```

（删除原 `p = prices.get(...)` 到 `...(p.price_output)` 的 5 行内联逻辑。）

- [ ] **Step 6: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_agent_stats.py tests/unit/proxy/test_agent_endpoints.py tests/unit/proxy/test_latency_endpoint.py tests/unit/proxy/test_r5_cache_parity.py -v --tb=short`
Expected: PASS（新聚合用例全绿；token-curve 相关既有测试证明 helper 口径未变）

- [ ] **Step 7: 提交**

```bash
git add inferfabric/agent_stats.py inferfabric/metrics_aggregator.py inferfabric/proxy/handler.py tests/unit/proxy/test_agent_stats.py tests/unit/proxy/test_agent_endpoints.py
git commit -m "feat(agents): /api/agent-stats 分桶聚合 + cost_of_row 共享计价

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: /api/agents GET / POST / DELETE（一键认领 + 管理）

**Files:**
- Modify: `inferfabric/proxy/handler.py`（`_handle_agents` GET / `_handle_post_agents` / `_handle_delete_agent`；`_GET_ROUTES`/`_POST_ROUTES`/`_DELETE_ROUTES` 注册）
- Modify: `tests/unit/proxy/test_agent_endpoints.py`（追加端点用例）

**Interfaces:**
- Consumes: `pm.agent_registry.add_from_ui / remove / all`（Task 1）、`_admin` 包装、`self._read_body()`
- Produces:
  - `GET /api/agents` → `{"agents": [{id,name,color,source,rules}]}`
  - `POST /api/agents`（admin）body `{id,name,header,pattern,color}` → 200 `{agent}` / 400 校验 / 409 重名
  - `DELETE /api/agents?id=<id>`（admin）→ 200 `{ok}` / 400（内置或不存在）/ 404（未知 id）

- [ ] **Step 1: 写失败测试**

`tests/unit/proxy/test_agent_endpoints.py` 追加：

```python
def _mk_pm(tmp_path):
    from inferfabric.agent_registry import AgentRegistry
    reg = AgentRegistry(tmp_path / "builtin", tmp_path / "user")
    pm = SimpleNamespace(agent_registry=reg,
                         telemetry=SimpleNamespace(query_request_log=lambda since, limit=1: []),
                         metrics=SimpleNamespace(price_config={}))
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
    assert h._sent[-1][0] == 200
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_endpoints.py -v --tb=short`
Expected: FAIL（`_handle_agents` 等未定义）

- [ ] **Step 3: 实现三个 handler 方法 + 路由**

```python
    def _handle_agents(self, pm):
        """GET /api/agents — 已知 Agent 清单（dashboard 图例/管理）。"""
        self._send_json({"agents": [{
            "id": d.id, "name": d.name, "color": d.color, "source": d.source,
            "rules": [{"header": r.header, "value": r.value, "regex": r.regex,
                       "protocol": r.protocol} for r in d.rules],
        } for d in pm.agent_registry.all()]}, 200)

    def _handle_post_agents(self, pm):
        """POST /api/agents — 一键认领（admin-token 保护）。"""
        try:
            data = self._read_body()
            if not isinstance(data, dict):
                self._send_json({"error": "body required"}, 400)
                return
            d = pm.agent_registry.add_from_ui(
                str(data.get("id") or ""), str(data.get("name") or ""),
                str(data.get("header") or "user-agent"),
                str(data.get("pattern") or ""),
                str(data.get("color") or "#94a3b8"),
            )
            self._send_json({"agent": {
                "id": d.id, "name": d.name, "color": d.color, "source": d.source,
            }}, 200)
        except KeyError:
            self._send_json({"error": "agent id already exists"}, 409)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            log.error("/api/agents POST failed: %s", e)
            self._send_json({"error": "claim failed"}, 500)

    def _handle_delete_agent(self, pm):
        """DELETE /api/agents?id=<id> — 管理模式删除（仅用户目录文件）。"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        aid = (qs.get("id") or [""])[0]
        try:
            pm.agent_registry.remove(aid)
            self._send_json({"ok": True}, 200)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            log.error("/api/agents DELETE failed: %s", e)
            self._send_json({"error": "delete failed"}, 500)
```

路由注册：

- `_GET_ROUTES`：`"/api/agents": lambda h, pm: h._handle_agents(pm),`
- `_POST_ROUTES`：`"/api/agents": _admin(lambda h, pm: h._handle_post_agents(pm)),`
- `_DELETE_ROUTES`：`"/api/agents": _admin(lambda h, pm: h._handle_delete_agent(pm)),`

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_agent_endpoints.py -v --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add inferfabric/proxy/handler.py tests/unit/proxy/test_agent_endpoints.py
git commit -m "feat(agents): /api/agents GET/POST/DELETE — 一键认领 + 管理删除

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 8: /api/request_log + /api/snapshot 带 agent/ua

**Files:**
- Modify: `inferfabric/proxy/handler.py`（`_handle_request_log` ~926-934 行、`_handle_snapshot` ~1148-1157 行各加两字段）
- Modify: `tests/unit/proxy/test_agent_endpoints.py`（追加用例）

**Interfaces:**
- Consumes: `request_log` 行（含 agent/ua，Task 4）
- Produces: 两个 GET 端点的行对象统一含 `agent`、`ua`（dashboard 明细表「客户端」列数据源）

- [ ] **Step 1: 写失败测试**

```python
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
    rows = [{"timestamp": 1.0, "model": "m", "status": 200, "tokens_in": 1,
             "tokens_in_cached": 0, "tokens_out": 1, "ttft_ms": None,
             "duration_ms": None, "route": "local", "key_name": "k", "error": None,
             "agent": "", "ua": "curl/8"}]
    pm = SimpleNamespace(telemetry=SimpleNamespace(query_request_log=lambda since, limit=50: rows),
                         mgr=SimpleNamespace(state=SimpleNamespace(get_history=lambda n: []),
                                             list_models=lambda: []),
                         _snap_exp_cache=None)
    h._handle_snapshot(pm)
    code, data = h._sent[-1]
    rl = data["request_log"]
    assert rl[0]["agent"] == "" and rl[0]["ua"] == "curl/8"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_endpoints.py -v --tb=short`
Expected: FAIL（缺 agent/ua 键）

- [ ] **Step 3: 实现两处字段透出**

`_handle_request_log` 的 logs dict（现 ~929-934 行）加：

```python
                    "agent": r.get("agent", ""),
                    "ua": r.get("ua", ""),
```

`_handle_snapshot` 的 request_log 推导（现 ~1148-1157 行）在 `"route": ..., "key_name": ...` 后加：

```python
                  "agent": r.get("agent", ""),
                  "ua": r.get("ua", ""),
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_agent_endpoints.py tests/unit/ -v --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add inferfabric/proxy/handler.py tests/unit/proxy/test_agent_endpoints.py
git commit -m "feat(agents): /api/request_log 与 /api/snapshot 透出 agent/ua

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 9: config_reloader 接入 agents 域 + proxy_manager 装配

**Files:**
- Modify: `inferfabric/config_reloader.py`（`__init__` 加 `registry` 参数；`reload_all` 加 agents 域）
- Modify: `inferfabric/proxy/handler.py`（~2225 行 `ConfigReloader(...)` 构造穿 registry）
- Test: `tests/unit/proxy/test_agent_capture.py`（追加 reloader 行为）+ 冒烟

**Interfaces:**
- Consumes: `AgentRegistry.reload() -> bool`（Task 1）、`ProxyManager.agent_registry`
- Produces: SIGHUP / `/reload-config` 会重载 agents.d（失败进 `failed` 列表）

- [ ] **Step 1: 写失败测试**

```python
def test_reloader_reloads_registry():
    import inferfabric.config_reloader as cr
    r = AgentRegistry.__new__(AgentRegistry)
    calls = []
    r.reload = lambda: (calls.append(1), True)[1]
    rel = cr.ConfigReloader(mgr=None, auth=None, cloud=None, registry=r)
    rel._last_reload = 0  # 跳过 5s 冷却
    failed = rel.reload_all()
    # reload_all 会先跑 mgr.reload_models（mgr=None → N/A），registry 必被调
    assert calls  # registry.reload 至少被调用一次
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/unit/proxy/test_agent_capture.py -k reloader -v --tb=short`
Expected: FAIL（TypeError: ConfigReloader 不接受 registry 参数）

- [ ] **Step 3: 实现**

`config_reloader.py`：

```python
    def __init__(self, mgr: "ModelManager", auth: "AuthManager" = None,
                 cloud: "CloudDiscovery" = None, registry=None):
        self._mgr = mgr
        self._auth = auth
        self._cloud = cloud
        self._registry = registry
        self._last_reload = 0.0
        self._cooldown = 5.0  # seconds
```

`reload_all()` 在 cloud 块之后加：

```python
        if self._registry:
            try:
                if not self._registry.reload():
                    failed.append("agents")
                    log.error("ConfigReloader: agent registry reload FAILED")
            except Exception as e:
                failed.append("agents")
                log.error("ConfigReloader: agent registry reload failed: %s", e)
```

`handler.py` ~2225 行 `ConfigReloader(mgr.mgr, auth=mgr.auth, cloud=mgr.cloud, registry=mgr.agent_registry)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/unit/proxy/test_agent_capture.py tests/unit/ -v --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add inferfabric/config_reloader.py inferfabric/proxy/handler.py tests/unit/proxy/test_agent_capture.py
git commit -m "feat(agents): config_reloader 接入 agents 域（SIGHUP//reload-config 热重载）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 10: api_keys 命名规则文档（仅文档，鉴权保持默认关闭）

**Files:**
- Modify: `inferfabric/proxy/auth.py`（模块 docstring 追加命名规则块）

**Interfaces:** 无（纯文档）

- [ ] **Step 1: 修改 docstring**

`auth.py` 顶部 docstring 追加：

```python
"""单用户鉴权 — Bearer token 校验 + 可选临时 key

配置文件 api_keys.yaml:
  primary: "sk-iff-xxx"          # 主 key，长期有效，全模型
  guests:                        # 可选
    - key: "***"
      name: "测试用"
      models: ["qwen35-9b-vl"]
      expires: "2026-08-30T00:00:00+08:00"

═══ 命名规则（约定，不强制；仅为可读性与未来 key_name↔agent 关联） ═══
  primary: sk-iff-<yyyymm>-v<n>          例: sk-iff-202609-v1
  guest  : sk-<agent-id>-<yyyymm>-v<n>   例: sk-claude-code-202609-v1
          ↑ 对象取 agents.d 的 id（Claude Code → claude-code）
  yyyymm = 发卡年月；v<n> = 换发版本（同对象重发递增）

文件不存在或为空 = 不开启鉴权（当前行为不变，默认保持关闭）。
"""
```

- [ ] **Step 2: 运行既有 auth 测试确认无回归**

Run: `python3 -m pytest tests/unit/proxy/test_auth.py -v --tb=short`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add inferfabric/proxy/auth.py
git commit -m "docs(auth): api_keys 命名规则（sk-<对象>-<yyyymm>-v<n>，仅文档不启用）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 11: Dashboard —— 客户端 Agent 用量卡（布局 A）+ 一键认领 + 请求日志客户端列

**Files:**
- Modify: `inferfabric/dashboard/fragments/monitor.html`（Token 行后插入 col-12 卡）
- Modify: `inferfabric/dashboard/js/monitor.js`（相对引入的 `fetchAgentStats/renderAgentCard/认领 modal/管理模式/请求日志列`）
- Modify: `inferfabric/dashboard/fragments/style.css`（卡专属样式）
- 验证：dev 代理 + curl + 浏览器手测（本仓库无 JS 单测基建）

**Interfaces:**
- Consumes: `GET /api/agent-stats`（Task 6）、`GET/POST/DELETE /api/agents`（Task 7）、snapshot.`request_log[].agent/ua`（Task 8）、现有 `UI.toast`/`UI.confirm`/`mon-seg`/`fmtNum`
- Produces: 监控 TAB「客户端 / Agent 用量」卡 + 请求日志表「客户端」列 + 认领/管理交互

- [ ] **Step 1: monitor.html 插入卡结构（Token 行之后、延迟行之前）**

在 `monitor.html` Token 用量双卡的 `</div></div>` 之后、`<!-- Row 3: 模型延迟趋势` 之前插入：

```html
  <!-- Row 2.5: 客户端 Agent 用量 — col-12 单卡（图左表右，布局 A；v6.5）。
       图源 GET /api/agent-stats（30s 节流）；未识别可一键认领落盘。 -->
  <div class="if-grid">
    <div class="col-12">
      <div class="if-card">
        <div class="if-card-hdr">
          <span class="if-card-title">客户端 / Agent 用量</span>
          <span class="if-card-sub" id="monAgentSub">按请求头识别 · 未识别可在下方认领</span>
          <div class="mon-seg" data-seg="agscope">
            <button type="button" class="mon-seg-btn active" data-scope="all">全部</button>
            <button type="button" class="mon-seg-btn" data-scope="local">本地</button>
            <button type="button" class="mon-seg-btn" data-scope="cloud">云端</button>
          </div>
          <div class="mon-seg" data-seg="aggran">
            <button type="button" class="mon-seg-btn" data-gran="minute">分钟</button>
            <button type="button" class="mon-seg-btn active" data-gran="hour">小时</button>
            <button type="button" class="mon-seg-btn" data-gran="day">天</button>
            <button type="button" class="mon-seg-btn" data-gran="week">周</button>
          </div>
          <span class="mon-claim-badge" id="monAgentUnkBadge" style="display:none">
            <svg><use href="#s-bolt"/></svg><span id="monAgentUnkNum">0</span>
          </span>
          <button type="button" class="mon-mini-btn" id="monAgentManage" title="管理：删除/改名">管理</button>
        </div>
        <div class="if-card-body if-agent-body">
          <div class="if-agent-left">
            <div class="mon-chart-wrap">
              <div id="monAgentChart" class="mon-chart"></div>
              <div id="monAgentEmpty" class="mon-chart-empty" style="display:none">暂无客户端数据——经代理发起请求后在此统计</div>
            </div>
          </div>
          <div class="if-agent-right">
            <div id="monAgentTable">
              <div class="skeleton"><div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div></div>
            </div>
            <div id="monAgentUnassigned" style="display:none"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: monitor.js 追加数据层（30s 节流 + seg 联动）**

在 IIFE 内、`renderMonitor` 之前追加（含顶部状态变量；`var` 风格与全文件一致）：

```js
  /* ── 客户端 / Agent 用量卡（v6.5） ──
   * 数据源 GET /api/agent-stats；30s TTL + inflight guard；不随 3s snapshot。
   * 认领：POST /api/agents（adminHeaders）；DELETE /api/agents?id= 管理。 */
  var _AGENT_TTL = 30000;
  var _agentCache = null, _agentLast = 0, _agentInflight = false, _agentChart = null;
  var _agentCfg = { gran: 'hour', scope: 'all' };
  var _claimSample = '';

  function getAgentStats() {
    if (_agentCache && Date.now() - _agentLast < _AGENT_TTL) { renderAgentCard(); return; }
    if (_agentInflight) return;
    _agentInflight = true;
    fetch('/api/agent-stats?granularity=' + _agentCfg.gran + '&scope=' + _agentCfg.scope,
          { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { _agentCache = d; _agentLast = Date.now(); renderAgentCard(); })
      .catch(function (e) { console.warn('[monitor] /api/agent-stats failed:', e); })
      .then(function () { _agentInflight = false; });
  }

  function _agentColor(idx) { return _paletteColor(idx); }

  function renderAgentTable(totals) {
    var el = $('monAgentTable'); if (!el) return;
    if (!totals || !totals.length) {
      el.innerHTML = '<div class="if-empty">暂无客户端数据——经代理发起请求后在此统计</div>';
      return;
    }
    var rows = '';
    for (var i = 0; i < totals.length; i++) {
      var t = totals[i];
      var isUnk = t.agent === 'unknown';
      rows += '<tr class="' + (isUnk ? 'agent-unk-row' : '') + '">' +
        '<td><span class="agent-dot" style="background:' + escHtml(t.color) + '"></span>' +
          escHtml(t.name) + (isUnk ? ' <span class="agent-unk-tag">未识别</span>' : '') + '</td>' +
        '<td class="mono num">' + UI.fmtNum(t.requests) + '</td>' +
        '<td class="mono num">' + escHtml(t.success_rate.toFixed(0)) + '%</td>' +
        '<td class="mono num">' + UI.fmtNum(t.tokens_in) + '/' + UI.fmtNum(t.tokens_out) + '</td>' +
        '<td class="mono num">' + (t.cost_yuan > 0 ? _yuanFmt(t.cost_yuan) : '—') + '</td>' +
        '<td class="mono num">' + (t.ttft_p50 != null ? escHtml(t.ttft_p50.toFixed(0)) + 'ms' : '—') + '</td>' +
        '<td class="mono">' + escHtml((t.top_models || []).slice(0, 1).map(function (m) { return shortName(m.model); }).join('')) + '</td>' +
        '<td>' + (isUnk ? '<button class="mon-mini-btn" data-claim="1" title="识别为新 Agent">识别</button>' : '') +
          (_agentManage && !isUnk && t.source !== 'builtin'
             ? '<button class="mon-mini-btn" data-del="' + escHtml(t.agent) + '" title="删除">✕</button>' : '') + '</td>' +
      '</tr>';
    }
    el.innerHTML = '<div class="cp-table-wrap">' +
      '<table class="if-table mon-tbl mon-agent-tbl">' +
        '<thead><tr><th>客户端</th><th>请求</th><th>成功</th><th>Tokens in/out</th>' +
        '<th>费用</th><th>TTFT P50</th><th>Top 模型</th><th></th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table></div>';
  }
```

- [ ] **Step 3: monitor.js 追加渲染主函数 + 认领/管理交互 + seg 绑定**

```js
  function _agentPatternFromUA(ua) {
    ua = ua || '';
    var pre = (ua.match(/^[A-Za-z0-9]+/) || [''])[0];
    return pre ? ('^' + pre) : '.*';
  }

  function renderAgentUnassigned(list) {
    var box = $('monAgentUnassigned'); if (!box) return;
    var badge = $('monAgentUnkBadge'), num = $('monAgentUnkNum');
    if (num) num.textContent = (list || []).length;
    if (badge) badge.style.display = (list && list.length) ? '' : 'none';
    if (!list || !list.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    var rows = '';
    for (var i = 0; i < list.length; i++) {
      var u = list[i];
      rows += '<div class="agent-unk-item"><span class="mono">' + escHtml(u.ua) +
        '</span><span class="agent-unk-count">' + UI.fmtNum(u.requests) + ' 次</span>' +
        '<button class="mon-mini-btn" data-claim-ua="' + escHtml(u.ua) + '">识别</button></div>';
    }
    box.innerHTML = '<div class="agent-unk-head">未识别来源（待认领）</div>' + rows;
    box.style.display = '';
  }

  function renderAgentCard() {
    if (!isMonitorActive()) return;
    var d = _agentCache; if (!d) { getAgentStats(); return; }
    // 空图清理 + 图表：堆叠柱（每 Agent 一色）
    if (_agentChart) { _agentChart.dispose(); _agentChart = null; }
    var chartEl = $('monAgentChart');
    var emptyEl = $('monAgentEmpty');
    var have = d.window_requests > 0;
    if (!have) {
      if (emptyEl) emptyEl.style.display = '';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    ensureCharts();
    _agentChart = echarts.init(chartEl);
    var names = [], series = [];
    for (var i = 0; i < d.totals.length; i++) {
      var t = d.totals[i];
      var pts = (d.series[t.agent] || []).map(function (b) { return b.requests; });
      series.push({ name: t.name, type: 'bar', stack: 'a', barWidth: '70%',
                    itemStyle: { color: t.color }, data: pts });
    }
    names = d.totals.map(function (t) { return t.name; });
    _agentChart.setOption({
      grid: { left: 40, right: 12, top: 24, bottom: 26 },
      tooltip: { trigger: 'axis' },
      legend: { show: true, top: 0, type: 'scroll', textStyle: { fontSize: 11 } },
      xAxis: { type: 'category', data: (d.series[d.totals.length ? d.totals[0].agent : ''] || [])
                 .map(function (b) { return 'x' + b.x; }), show: false },
      yAxis: { type: 'value', name: '请求' },
      series: series,
    });
    renderAgentTable(d.totals);
    renderAgentUnassigned(d.unassigned);
  }

  function bindAgentEvents() {
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('[data-claim], [data-claim-ua], [data-del]') : null;
      if (!btn) return;
      ev.preventDefault();
      if (btn.hasAttribute('data-claim-ua')) { openClaimModal(btn.getAttribute('data-claim-ua')); return; }
      if (btn.hasAttribute('data-claim')) {
        // 明细表 unknown 行：以当前窗口首个未识别 UA 预填
        var first = (_agentCache && _agentCache.unassigned && _agentCache.unassigned[0]) || {};
        openClaimModal(first.ua || ''); return;
      }
      if (btn.hasAttribute('data-del')) { delAgent(btn.getAttribute('data-del')); return; }
    });
  }

  function openClaimModal(ua) {
    _claimSample = ua || '';
    var rid = _agentPatternFromUA(_claimSample);
    var base = (rid.length > 1 ? rid.slice(1) : 'agent').toLowerCase()
        .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'agent';
    var holder = $('confirmModal');
    holder.innerHTML =
      '<div class="if-modal-backdrop">' +
        '<div class="if-modal" role="dialog" aria-modal="true">' +
          '<div class="if-modal-title">识别为新 Agent</div>' +
          '<div class="if-modal-body if-agent-claim">' +
            '<div class="agent-claim-sample mono">样本: ' + escHtml(_claimSample || '—') + '</div>' +
            '<label>名称 <input type="text" id="claimName" value="' + escHtml(base) + '" maxlength="40"></label>' +
            '<label>标识 id <input type="text" id="claimId" value="' + escHtml(base) + '" placeholder="小写字母数字连字符" pattern="[a-z0-9-]{1,64}"></label>' +
            '<label>匹配来源 <select id="claimHeader">' +
              '<option value="user-agent">User-Agent</option>' +
              '<option value="x-app">x-app</option></select></label>' +
            '<label>匹配规则 <input type="text" id="claimPattern" value="' + escHtml(rid) + '"></label>' +
            '<div class="agent-claim-preview" id="claimPreview"></div>' +
          '</div>' +
          '<div class="if-modal-actions">' +
            '<button type="button" class="mon-mini-btn" id="claimCancel">取消</button>' +
            '<button type="button" class="mon-mini-btn" id="claimSave" style="color:var(--ok,#22c55e)">创建并生效</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    holder.style.display = 'block';
    bindClaimEvents();
    updateClaimPreview();
  }

  function updateClaimPreview() {
    var pat = $('claimPattern'), pre = $('claimPreview');
    if (!pat || !pre) return;
    var msg, ok = false;
    try {
      ok = new RegExp(pat.value).test(_claimSample || '');
      msg = '实时预览: 「' + (_claimSample || '—') + '」 ' + (ok ? '→ ✓ 命中' : '→ ✗ 不命中');
    } catch (e) { msg = '实时预览: regex 无法编译'; }
    pre.textContent = msg;
    pre.style.color = ok ? 'var(--ok,#22c55e)' : 'var(--crit,#ef4444)';
  }

  function bindClaimEvents() {
    var c = $('claimCancel'), s = $('claimSave');
    if (c) c.addEventListener('click', closeClaimModal);
    if (s) s.addEventListener('click', submitClaim);
    var pat = $('claimPattern');
    if (pat) pat.addEventListener('input', updateClaimPreview);
    var idInput = $('claimId');
    if (idInput) idInput.addEventListener('input', function () { idInput.dataset.touched = '1'; });
    var nameInput = $('claimName');
    if (nameInput) nameInput.addEventListener('input', function () {
      var ipt = $('claimId');
      if (ipt && !ipt.dataset.touched) {
        ipt.value = nameInput.value.toLowerCase()
          .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'agent';
      }
    });
  }

  function closeClaimModal() {
    var holder = $('confirmModal');
    if (holder) { holder.innerHTML = ''; holder.style.display = 'none'; }
    _claimSample = '';
  }
```

```js
  function submitClaim() {
    var id = $('claimId').value, name = $('claimName').value,
        header = $('claimHeader').value, pattern = $('claimPattern').value;
    fetch('/api/agents', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, UI.adminHeaders()),
      body: JSON.stringify({ id: id, name: name, header: header, pattern: pattern, color: '#94a3b8' }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { UI.toast(res.d.error || '认领失败', 'error'); return; }
        UI.toast('已创建 Agent ' + id + '，热重载已生效', 'ok');
        closeClaimModal();
        _agentCache = null; getAgentStats();
      });
  }

  function delAgent(id) {
    UI.confirm({
      title: '删除 Agent ' + id,
      body: '仅删除 ~/.inferfabric/agents.d/' + id + '.yaml（内置不可删）。历史请求行不受影响。',
      okText: '删除', cancelText: '取消', danger: true,
      onOk: function () {
        fetch('/api/agents?id=' + encodeURIComponent(id), {
          method: 'DELETE', headers: UI.adminHeaders(),
        })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.ok) { UI.toast('已删除 ' + id, 'ok'); _agentCache = null; getAgentStats(); }
            else { UI.toast(d.error || '删除失败', 'error'); }
          });
      },
    });
  }
```

- [ ] **Step 4: seg/管理按钮绑定 + renderMonitor 挂载**

在 `renderMonitor()` 内（现 `renderTokenChart();` 之后）加：

```js
    getAgentStats();
```

并在 IIFE 初始化处（与其它 seg 绑定同区）加：

```js
    document.querySelectorAll('[data-seg="aggran"] .mon-seg-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var host = b.parentNode;
        host.querySelectorAll('.mon-seg-btn').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        _agentCfg.gran = b.getAttribute('data-gran');
        _agentCache = null; getAgentStats();
      });
    });
    document.querySelectorAll('[data-seg="agscope"] .mon-seg-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var host = b.parentNode;
        host.querySelectorAll('.mon-seg-btn').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        _agentCfg.scope = b.getAttribute('data-scope');
        _agentCache = null; getAgentStats();
      });
    });
    var mBtn = $('monAgentManage');
    if (mBtn) {
      mBtn.addEventListener('click', function () {
        _agentManage = !_agentManage;
        mBtn.classList.toggle('active', _agentManage);
        if (_agentCache) renderAgentTable(_agentCache.totals);
      });
    }
    bindAgentEvents();
```

> 注意：`UI.confirm` 是文本模态（title/body 字符串），删除确认用它；认领表单用自定义模态（Step 3 的 `openClaimModal` 渲染到 `#confirmModal` + 自主按钮）。落地后必做 Step 5 手测清单（本仓库无 JS 单测基建）。

- [ ] **Step 5: 手测清单（dev 代理跑通交互）**

Run:（新终端）`EDGE_PROXY_PORT=8901 python3 -m inferfabric.proxy.handler`

```
# 1) 造真实分类数据
curl -s http://127.0.0.1:8901/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'x-app: cli' -H 'User-Agent: claude-cli/2.0.1' \
  -d '{"model":"<任一已配置模型>","messages":[{"role":"user","content":"hi"}]}' || true
curl -s http://127.0.0.1:8901/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'User-Agent: curl/8.5.2' \
  -d '{"model":"<任一已配置模型>","messages":[{"role":"user","content":"hi"}]}' || true
# 2) 统计端点验证
curl -s 'http://127.0.0.1:8901/api/agent-stats?granularity=hour'
#   → totals 应含 claude-code（若模型路由成功）与 unknown；unassigned 含 curl/8.5.2
# 3) 一键认领
curl -s -X POST http://127.0.0.1:8901/api/agents -H 'Content-Type: application/json' \
  -d '{"id":"curl-cli","name":"curl-cli","header":"user-agent","pattern":"^curl","color":"#94a3b8"}'
#   → {"agent":{...}}；再发一次同 id → 409
# 4) 浏览器开 http://127.0.0.1:8901/ → 监控 TAB：
#   卡渲染、seg 切换、未识别 badge、认领 modal、预览、管理删除 curl-cli、请求日志「客户端」列
```

- [ ] **Step 6: style.css 增补**

在 `monitor.html` 相关样式区末尾追加：

```css
/* ── v6.5 客户端 Agent 卡 ── */
.if-agent-body{display:grid;grid-template-columns:minmax(0,60fr) minmax(0,40fr);gap:16px;align-items:start}
.if-agent-left{min-width:0}
.if-agent-right{min-width:0}
.mon-agent-tbl th,.mon-agent-tbl td{padding:4px 8px;font-size:12px}
.agent-row{}
.agent-unk-row{background:rgba(156,163,175,.06)}
.agent-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.agent-unk-tag{color:var(--crit,#ef4444);font-size:11px}
.agent-unk-head{font-size:12px;font-weight:600;margin:8px 0 4px;color:var(--txt-2,#64748b)}
.agent-unk-item{display:flex;gap:8px;align-items:center;font-size:12px;padding:3px 0}
.agent-unk-count{color:var(--txt-2,#64748b);margin-right:auto}
.mon-claim-badge{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--crit,#ef4444);cursor:pointer}
.mon-mini-btn{font-size:11px;padding:2px 8px;border:1px solid var(--line,#e2e8f0);border-radius:6px;background:transparent;cursor:pointer}
.mon-mini-btn:hover{background:rgba(100,116,139,.08)}
.if-agent-claim label{display:block;font-size:12px;margin:8px 0}
.if-agent-claim input,.if-agent-claim select{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--line,#e2e8f0);border-radius:6px;background:transparent;color:inherit}
.agent-claim-sample{font-size:12px;padding:6px 8px;background:rgba(100,116,139,.08);border-radius:6px;margin-bottom:8px}
.agent-claim-preview{font-size:12px;color:var(--txt-2,#64748b);min-height:18px}
@media (max-width:1100px){.if-agent-body{grid-template-columns:1fr}}
```

- [ ] **Step 7: 提交 + 生产服务重载**

```bash
git add inferfabric/dashboard/fragments/monitor.html inferfabric/dashboard/js/monitor.js inferfabric/dashboard/fragments/style.css
git commit -m "feat(dashboard): 监控 TAB 客户端 Agent 用量卡（布局 A）+ 一键认领 + 明细列

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

生产生效：`systemctl --user restart inferfabric-proxy.service`（记忆：dashboard 改动需重启服务方能上线）。

---

### Task 12: 全量回归 + 沙箱端到端冒烟

**Files:** 无新文件；跑测试 + 冒烟

- [ ] **Step 1: 全量单测 + 集成**

Run: `python3 -m pytest tests/unit/ tests/integration/ -v --tb=short`
Expected: 全绿

- [ ] **Step 2: import 冒烟**

Run: `python3 -c "import inferfabric"`
Expected: 无异常

- [ ] **Step 3: 沙箱端到端冒烟（含 Review Focus 3/4/5 实弹）**

Run:（新终端）`EDGE_PROXY_PORT=8902 python3 -m inferfabric.proxy.handler`

```
# 不带 UA 头 / 带 CLI 头 / 认领后归类
curl -s 'http://127.0.0.1:8902/api/agent-stats?granularity=hour&scope=all'
# POST 认领（重复 → 409）、GET /api/agents（source 字段）、DELETE? id= → 400（内置）
```

- [ ] **Step 4: 生产服务重启 + dashboard 人工过一遍（可选，由用户决定）**

```bash
systemctl --user restart inferfabric-proxy.service
```

- [ ] **Step 5: 交叉评审（质量门）**

按 steering/constitution.md：LLM 交叉评审（AtomCode GLM-5.2 via :8999，`max_tokens ≥ 8000`），重点看 Review Focus 五条与 PR-14 划界。

- [ ] **Step 6: 提交（如沙箱有修正）并收尾汇报**

```bash
git add -A
git commit -m "chore(agents): 全量回归 + 沙箱冒烟修正

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 执行顺序与依赖

```
Task1 → Task2 → Task3 ─┐
Task4 ─────────────────┼→ Task5 → Task6 → Task7 → Task8 → Task9
Task10（独立，任意位置）  │                ↑ Task7 依赖 Task3 的 add_from_ui/remove
Task11 依赖 Task6/7/8
Task12 收尾
```

## 非目标（本计划明确不做）

- 认领时「归入现有 Agent / 合并」、HOCON 复杂匹配多信号组合的 UI（仍手写 YAML）
- `policy()` 接线（限流/白名单/切换差异化/KV 预热）——只留接口恒 None
- AnomalyEvent 带 agent / 按客户端过滤异常
- agent × key 双维度下钻（等 guest keys 启用）
- dashboard「改名」视为「删除+重建」（不新增 PATCH 端点；spec §5 措辞保留）