"""云端 Provider 自动发现与配置管理

从 cloud_provider.yaml 加载 provider 配置，
通过 GET <openai_base>/models 自动发现可用模型，
构建 CloudModel 注册表供路由决策使用。

v4.6.0: 模型能力属性 (context_window, max_output_tokens 等)
  - cloud_provider.yaml 的 models: 段可手动指定
  - 自动发现结果与手动配置合并（手动优先）
"""

import logging
import os
import re
import time
import threading
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
import urllib.request
import urllib.error

log = logging.getLogger("inferfabric.cloud_discovery")

# 发现请求显式 User-Agent：urllib 默认 "Python-urllib/3.x" 会被 Cloudflare
# 返回 403 error code 1010（浏览器签名封禁）→ 候选列表恒为空。
_CLOUD_UA = "inferfabric/6.1.6"

# 云模型能力标签键（L2b 打标唯一接受的键）：上游 /models 不提供能力元数据，
# 人工声明存 model_specs，经 _merge_model_spec 注入注册表。
_CAPABILITY_KEYS = ("supports_vision", "supports_tools")


@dataclass
class CloudModel:
    """云端模型条目。"""
    model_id: str
    provider: str
    openai_available: bool = True
    anthropic_available: bool = False
    discovered_at: float = 0.0
    # v4.6.0: 模型能力属性 — 对齐 OpenClaw model spec
    name: str = ""                   # 人类可读名称, e.g. "DeepSeek V4 Flash"
    context_window: int | None = None       # 模型理论最大上下文
    max_output_tokens: int | None = None    # 模型理论最大输出
    contextWindow: int | None = None        # 实际可用上下文 (考虑流控)
    maxTokens: int | None = None            # 实际建议最大输出 (考虑流控)
    input: list[str] = field(default_factory=lambda: ["text"])  # 输入模态
    reasoning: bool = False           # 是否支持思考模式
    supports_vision: bool = False
    supports_tools: bool = False
    # G-2: 价格字段 (¥/1M tokens)
    price_input: float = 0.0
    price_output: float = 0.0
    # 自定义扩展字段 (YAML 中的额外 key-value)
    extra: dict = field(default_factory=dict)

    def to_api_dict(self) -> dict:
        """转换为 OpenAI /v1/models 响应格式的 dict。
        
        字段对齐 OpenClaw openclaw.json model spec:
        - contextWindow / maxTokens: 实际可用值 (考虑流控)
        - context_window / max_output_tokens: 模型理论最大值
        """
        d = {
            "id": self.model_id,
            "object": "model",
            "owned_by": f"cloud:{self.provider}",
            "permission": [],
        }
        if self.name:
            d["name"] = self.name
        # 填充能力属性
        caps = {}
        # 实际可用值 (client 优先使用)
        if self.contextWindow is not None:
            caps["contextWindow"] = self.contextWindow
        if self.maxTokens is not None:
            caps["maxTokens"] = self.maxTokens
        # 模型理论最大值
        if self.context_window is not None:
            caps["context_window"] = self.context_window
        if self.max_output_tokens is not None:
            caps["max_output_tokens"] = self.max_output_tokens
        # 模态与能力
        if self.input:
            caps["input"] = self.input
        caps["reasoning"] = self.reasoning
        caps["supports_vision"] = self.supports_vision
        caps["supports_tools"] = self.supports_tools
        # 自定义扩展
        if self.extra:
            caps.update(self.extra)
        d["capabilities"] = caps
        return d


@dataclass
class ProviderConfig:
    """单个 cloud provider 的配置。"""
    name: str
    api_key: str = ""
    openai_base: str = ""
    anthropic_base: str = ""
    timeout: int = 600
    enabled: bool = True
    # Discovery
    discovery_enabled: bool = True
    discovery_endpoint: str = "/models"
    discovery_interval: int = 3600
    include_pattern: str = ""
    # Routing
    routing_default: str = "cloud_only"
    # v6.1 Phase 1: 发现模型白名单（勾选才可路由；未列出的发现候选仅作候选，不路由）
    enabled_models: list[str] = field(default_factory=list)
    # v4.7.0: ENV-based key management
    key_env_var: str = ""           # API Key 对应的 ENV 变量名
    preset_id: str = ""            # 来源预设 ID（可选）
    # v4.6.0: 模型能力属性手动覆盖
    model_specs: dict[str, dict] = field(default_factory=dict)


@dataclass
class CloudPreset:
    """v4.7.0: 云厂商预设模板。"""
    id: str
    display_name: str = ""
    icon: str = "📦"
    openai_base: str = ""
    anthropic_base: str = ""
    env_var: str = ""
    discovery: bool = True
    timeout: int = 60
    models: dict = field(default_factory=dict)  # 预置模型规格


class SecretsManager:
    """v4.7.0: 管理 ~/.inferfabric/secrets.env — API Key 安全存储。"""

    def __init__(self, path: Path | None = None):
        self._path = path or (Path.home() / ".inferfabric" / "secrets.env")
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def ensure_file(self):
        """创建 secrets.env（如不存在）并设置权限 600。"""
        if not self._path.exists():
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("# IFF Cloud Provider API Keys — auto-generated\n# Do not commit to git!\n")
                self._path.chmod(0o600)
                log.info("Created secrets.env at %s", self._path)
            except OSError as e:
                log.error("Failed to create secrets.env: %s", e)
                raise

    def load(self) -> dict[str, str]:
        """读取所有 key=value 对。"""
        result = {}
        if not self._path.exists():
            return result
        try:
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        except OSError as e:
            log.error("Failed to read secrets.env: %s", e)
        return result

    def write(self, key: str, value: str):
        """追加或更新单条 key=value（线程安全）。"""
        with self._lock:
            self.ensure_file()
            existing = self.load()
            existing[key] = value
            lines = ["# IFF Cloud Provider API Keys — auto-generated", "# Do not commit to git!"]
            for k, v in existing.items():
                lines.append(f"{k}={v}")
            try:
                self._path.write_text("\n".join(lines) + "\n")
                self._path.chmod(0o600)
            except OSError as e:
                log.error("Failed to write secrets.env: %s", e)
                raise

    def env_set(self, var_name: str) -> bool:
        """检查 ENV 变量是否已设置且非空。"""
        return bool(os.environ.get(var_name, ""))


class CloudDiscovery:
    """云端模型发现引擎。

    用法：
        cd = CloudDiscovery(Path("cloud_provider.yaml"))
        models = cd.discover_all()   # dict[str, CloudModel]
    """

    def __init__(self, config_path: Path | None = None):
        self._providers: dict[str, ProviderConfig] = {}
        self._cloud_models: dict[str, CloudModel] = {}
        # v6.1 Phase 1: 发现候选快照（白名单过滤前的上游 /models 结果，供面板勾选；
        # 不持久化 —— 重启后「刷新发现」/轮询自动补回）
        self._candidates: dict[str, list[CloudModel]] = {}
        self._models_lock = threading.RLock()  # protects _cloud_models + _providers reads/writes
        # 锁序约定: _save_lock → _models_lock (reload 嵌套时)。handler add/delete: _models_lock → save_config(_save_lock)，不嵌套。
        # G-2: price config cached for metrics_aggregator
        self._price_config: dict[str, tuple[float, float]] = {}
        self._last_discovery: float = 0.0
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._config_path: Path | None = config_path
        self._save_lock = threading.Lock()
        self._config_corrupt = False
        if config_path:
            self._load_config(config_path)
            # Register spec-only models on startup so they're visible before first discover
            self._register_spec_only_models(self._cloud_models)

    @property
    def providers(self) -> dict[str, ProviderConfig]:
        return self._providers

    @property
    def cloud_models(self) -> dict[str, CloudModel]:
        with self._models_lock:
            return dict(self._cloud_models)  # return snapshot, not mutable reference

    # ── v4.7.0: Preset Management ──────────────────────────────────

    @staticmethod
    def load_presets(presets_path: Path | None = None) -> dict[str, CloudPreset]:
        """加载预设模板库。"""
        if presets_path is None:
            presets_path = Path.home() / ".inferfabric" / "cloud_presets.yaml"
        # Fallback to bundled presets
        if not presets_path.exists():
            bundled = Path(__file__).parent / "cloud_presets.yaml"
            if bundled.exists():
                presets_path = bundled
            else:
                return {}
        try:
            with open(presets_path) as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            log.error("Failed to load cloud_presets.yaml: %s", e)
            return {}
        if not cfg or "presets" not in cfg:
            return {}
        result = {}
        for pid, pdata in cfg["presets"].items():
            if not isinstance(pdata, dict):
                continue
            result[pid] = CloudPreset(
                id=pid,
                display_name=pdata.get("display_name", pid),
                icon=pdata.get("icon", "📦"),
                openai_base=pdata.get("openai_base", ""),
                anthropic_base=pdata.get("anthropic_base", ""),
                env_var=pdata.get("env_var", ""),
                discovery=pdata.get("discovery", True),
                timeout=pdata.get("timeout", 600),
                models=pdata.get("models", {}),
            )
        return result

    @staticmethod
    def _env_key_for_provider(name: str) -> str:
        """根据 provider name 生成 ENV 变量名。"""
        return f"IFF_{name.upper().replace('-', '_')}_KEY"

    @staticmethod
    def _inject_secrets_env(secrets_path: Path | None = None):
        """v4.7.0: 从 secrets.env 注入环境变量（不覆盖已有）。"""
        sp = secrets_path or (Path.home() / ".inferfabric" / "secrets.env")
        if not sp.exists():
            return
        try:
            for line in sp.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k:
                        os.environ.setdefault(k, v)
        except Exception as e:
            log.warning("Failed to inject secrets.env: %s", e)

    # ── Secrets Manager ────────────────────────────────────────────

    @property
    def secrets(self) -> SecretsManager:
        if not hasattr(self, '_secrets_mgr'):
            self._secrets_mgr = SecretsManager()
        return self._secrets_mgr

    def discover_all(self) -> dict[str, CloudModel]:
        """对所有 enabled provider 执行模型发现。返回合并后的模型注册表（= 可路由集）。

        v6.1 Phase 1 白名单制：上游 /models 结果先进 candidates 快照（面板勾选用），
        只有 model_id 在 cfg.enabled_models 里的**才进注册表**（可路由）。specs
        恒进（含手填）。未勾选的发现模型 → None（404），不静默兜底。
        """
        merged: dict[str, CloudModel] = {}
        for name, cfg in self._providers.items():
            if not cfg.enabled or not cfg.discovery_enabled:
                continue
            try:
                models = self._discover_provider(cfg)
                # 候选快照（网络抖动时保留旧候选，不覆盖为空）
                if models:
                    self._candidates[name] = list(models)
                enabled_set = set(cfg.enabled_models)
                routed = 0
                for m in models:
                    if m.model_id not in enabled_set:
                        continue  # 非白名单：只作候选，不进可路由注册表
                    routed += 1
                    # 合并手动 model_specs
                    m = self._merge_model_spec(m, cfg)
                    # 同一 model_id 多 provider 时：短名 key 指向首个，
                    # 同时存 <provider>/<model_id> 形式的带前缀 key
                    if m.model_id not in merged:
                        merged[m.model_id] = m
                    merged[f"{m.provider}/{m.model_id}"] = m
                log.info("Provider %s: discovered %d models (%d routed)", name, len(models), routed)
            except Exception as e:
                log.warning("Provider %s discovery failed: %s", name, e)
        # 也注册仅有 model_specs 但未被发现的模型
        self._register_spec_only_models(merged)
        # Preserve old models if new discovery yields nothing (network blip protection)
        if not merged and self._cloud_models:
            log.warning("Discovery yielded 0 models — keeping previous %d models",
                        len(self._cloud_models))
            return dict(self._cloud_models)
        with self._models_lock:
            self._cloud_models = merged
        self._last_discovery = time.time()
        return merged

    def start_polling(self):
        """启动后台轮询（非阻塞）。"""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        intervals = [cfg.discovery_interval for cfg in self._providers.values()
                     if cfg.enabled and cfg.discovery_enabled and cfg.discovery_interval > 0]
        if not intervals:
            return
        interval = min(intervals)
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, args=(interval,), daemon=True)
        self._poll_thread.start()
        log.info("Cloud discovery polling started, interval=%ds", interval)

    def stop_polling(self):
        """停止后台轮询。"""
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
            self._poll_thread = None
        log.info("Cloud discovery polling stopped")

    # ── v6.1 Phase 1: 模型策展（勾选=可路由，取消=隐藏，手填=新增） ─────
    # 调用方（handler）负责 save_config() 持久化。

    def get_candidates(self, provider: str) -> list[CloudModel]:
        """发现候选快照（白名单过滤前的上游 /models 结果）。"""
        with self._models_lock:
            return list(self._candidates.get(provider, []))

    def enable_model(self, provider: str, model_id: str) -> int:
        """勾选发现候选 → 进白名单 + 注册表（可路由）。"""
        with self._models_lock:
            cfg = self._providers.get(provider)
            if cfg is None:
                raise KeyError(provider)
            if model_id not in cfg.enabled_models:
                cfg.enabled_models.append(model_id)
            self._rebuild_provider_registry(provider, cfg)
        return self._routable_count(provider)

    def disable_model(self, provider: str, model_id: str) -> int:
        """取消勾选 → 移出白名单 + 注册表（spec/manual 同名仍在 spec 侧）。"""
        with self._models_lock:
            cfg = self._providers.get(provider)
            if cfg is None:
                raise KeyError(provider)
            if model_id in cfg.enabled_models:
                cfg.enabled_models.remove(model_id)
            self._rebuild_provider_registry(provider, cfg)
        return self._routable_count(provider)

    def add_manual_model(self, provider: str, model_id: str,
                         caps: dict | None = None) -> int:
        """手填模型 → 空 spec 进 model_specs（复用现有管道，spec 恒可路由）。

        caps: 可选能力标签（supports_vision/supports_tools）—— 添加时即声明，
        面板手填表单的「视觉/工具调用」选择框直接传入。
        """
        with self._models_lock:
            cfg = self._providers.get(provider)
            if cfg is None:
                raise KeyError(provider)
            spec = cfg.model_specs.setdefault(model_id, {})
            if caps:
                spec.update({k: v for k, v in caps.items()
                             if k in _CAPABILITY_KEYS})
            self._rebuild_provider_registry(provider, cfg)
        return self._routable_count(provider)

    def update_model_spec(self, provider: str, model_id: str, caps: dict) -> int:
        """能力打标（L2b）：写 model_specs → 重建注册表（catalog 据此出 features）。

        云模型能力上游 /models 不提供（发现只取 id），model_specs 是唯一
        注入点。spec 不存在则创建 —— 打标即声明，语义与手填/配置声明一致。
        只接受 _CAPABILITY_KEYS；显式 False 持久化（关闭标签不靠删键复活）。
        """
        with self._models_lock:
            cfg = self._providers.get(provider)
            if cfg is None:
                raise KeyError(provider)
            spec = cfg.model_specs.setdefault(model_id, {})
            spec.update({k: v for k, v in caps.items()
                         if k in _CAPABILITY_KEYS})
            self._rebuild_provider_registry(provider, cfg)
        return self._routable_count(provider)

    def remove_manual_model(self, provider: str, model_id: str) -> int:
        """移除手填模型（不存在 → KeyError，handler 映射 404）。"""
        with self._models_lock:
            cfg = self._providers.get(provider)
            if cfg is None:
                raise KeyError(provider)
            if cfg.model_specs.pop(model_id, None) is None:
                raise KeyError(model_id)
            self._rebuild_provider_registry(provider, cfg)
        return self._routable_count(provider)

    def drop_provider(self, provider: str):
        """删除 provider：移除配置 + 注册表模型 + 候选快照。"""
        with self._models_lock:
            if provider not in self._providers:
                raise KeyError(provider)
            del self._providers[provider]
            self._cloud_models = {
                k: v for k, v in self._cloud_models.items()
                if v.provider != provider
            }
            self._candidates.pop(provider, None)

    def _rebuild_provider_registry(self, provider: str, cfg: ProviderConfig):
        """按 (候选∩白名单) ∪ model_specs 重建单个 provider 的可路由注册表条目。"""
        # 1. 清掉本 provider 的旧条目（短名 + provider/ 前缀双键）
        self._cloud_models = {
            k: v for k, v in self._cloud_models.items()
            if v.provider != provider
        }
        # 2. 勾选的发现候选（合并 spec 属性）
        enabled_set = set(cfg.enabled_models)
        for cand in self._candidates.get(provider, []):
            if cand.model_id in enabled_set:
                m = self._merge_model_spec(cand, cfg)
                self._cloud_models.setdefault(m.model_id, m)
                self._cloud_models[f"{provider}/{m.model_id}"] = m
        # 3. model_specs（含手填空 spec）恒注册 —— _register_spec_only_models 对
        #    已注册短名幂等跳过，直接重跑全量即可
        self._register_spec_only_models(self._cloud_models)

    def _routable_count(self, provider: str) -> int:
        """该 provider 可路由模型数（短名 key 计数，避免双键重复）。"""
        return sum(1 for k, v in self._cloud_models.items()
                   if "/" not in k and v.provider == provider)

    def resolve_route(self, model_name: str, local_models: set[str]) -> Optional[str]:
        """路由决策：给定模型名，返回 "local" / "cloud:<provider>" / None。"""
        short_name = model_name.split("/")[-1] if "/" in model_name else model_name
        if short_name in local_models or model_name in local_models:
            return "local"
        with self._models_lock:
            cm = self._cloud_models.get(short_name) or self._cloud_models.get(model_name)
        if cm:
            return f"cloud:{cm.provider}"
        return None

    def get_provider_config(self, provider_name: str) -> ProviderConfig | None:
        return self._providers.get(provider_name)

    def reload(self, config_path: Path | None = None):
        """热加载配置。

        config_path 缺省时回退到构造时的 self._config_path —— SIGHUP /
        /reload-config 的无参 reload() 因此能重载实例自身的配置文件，
        而非抛 TypeError 被吞（B2）。
        """
        path = Path(config_path) if config_path else self._config_path
        if path is None:
            log.warning("CloudDiscovery.reload: no config path (ctor 未提供且未传参) — skipped")
            return
        self.stop_polling()
        with self._save_lock:
            self._config_path = path
        with self._models_lock:
            self._providers = {}
        self._load_config(path)

    # ── internal ──

    def save_config(self):
        """原子写入当前 providers 到 cloud_provider.yaml

        流程: write-to-temp → 校验 → os.replace (POSIX 原子)
        """
        if not self._config_path:
            log.warning("No config path set, cannot save")
            return
        with self._save_lock:
            try:
                tmp = self._config_path.with_suffix('.yaml.tmp')
                data = self._serialize_providers()
                with open(tmp, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
                    f.flush()
                    os.fsync(f.fileno())
                # 校验：读回确认可解析
                with open(tmp) as f:
                    yaml.safe_load(f)
                # 原子替换
                os.replace(tmp, self._config_path)
                log.info("Saved cloud provider config to %s", self._config_path)
                self._config_corrupt = False  # 成功保存后重置
            except Exception as e:
                log.error("Failed to save cloud provider config: %s", e)
                # 清理 tmp
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise

    def _serialize_providers(self) -> dict:
        """序列化当前 providers 为 YAML-compatible dict

        v4.7.0: 明文 api_key 自动转为 ${ENV_VAR} 引用，明文写入 secrets.env。
        """
        result = {"providers": {}}
        # #12: Snapshot to avoid dict mutation during iteration
        for name, p in list(self._providers.items()):
            # ── API Key ENV 转换 ──
            api_key = p.api_key or ""
            env_var = p.key_env_var or self._env_key_for_provider(name)
            if api_key and not api_key.startswith("${"):
                # 明文 key → 写入 secrets.env，YAML 存引用
                self.secrets.write(env_var, api_key)
                api_key = f"${{{env_var}}}"

            pd = {
                "openai_base": p.openai_base or "",
                "api_key": api_key,
            }
            # v4.7.0: 持久化 key_env_var 和 preset_id
            if p.key_env_var:
                pd["key_env_var"] = p.key_env_var
            if p.preset_id:
                pd["preset_id"] = p.preset_id
            if p.anthropic_base:
                pd["anthropic_base"] = p.anthropic_base
            if p.timeout and p.timeout != 60:
                pd["timeout"] = p.timeout
            if not p.enabled:
                pd["enabled"] = False
            if not p.discovery_enabled:
                pd["discovery_enabled"] = False
            if p.discovery_endpoint and p.discovery_endpoint != "/models":
                pd["discovery_endpoint"] = p.discovery_endpoint
            if p.discovery_interval and p.discovery_interval != 3600:
                pd["discovery_interval"] = p.discovery_interval
            if p.include_pattern:
                pd["include_pattern"] = p.include_pattern
            # v6.1 Phase 1: 白名单（勾选的发现候选；非空才写）
            if p.enabled_models:
                pd["enabled_models"] = list(p.enabled_models)
            if p.routing_default:
                pd["routing_default"] = p.routing_default
            # 序列化 models
            if p.model_specs:
                models = {}
                for mid, spec in p.model_specs.items():
                    md = {}
                    if mid:
                        md["model_id"] = mid
                    if spec.get("price_input"):
                        md["price_input"] = spec["price_input"]
                    if spec.get("price_output"):
                        md["price_output"] = spec["price_output"]
                    # L2b: 能力标签显式落盘 —— 此前在 known 排除集里被静默
                    # 丢弃（手写 YAML 标签一经策展操作 save_config 即蒸发）。
                    # 显式 False 也写：关闭标签靠持久化 false，不靠删键。
                    if spec.get("supports_vision") is not None:
                        md["supports_vision"] = bool(spec["supports_vision"])
                    if spec.get("supports_tools") is not None:
                        md["supports_tools"] = bool(spec["supports_tools"])
                    # Include other spec fields beyond the known capability keys
                    known = {"name", "contextWindow", "maxTokens", "input", "reasoning",
                             "context_window", "max_output_tokens", "supports_vision", "supports_tools",
                             "price_input", "price_output"}
                    for k, v in spec.items():
                        if k not in known and k not in md:
                            md[k] = v
                    if md:
                        # key 用 short name
                        key = mid.split("/")[-1] if "/" in mid else mid
                        models[key] = md
                if models:
                    pd["models"] = models
            result["providers"][name] = pd
        return result

    @staticmethod
    def _merge_model_spec(model: CloudModel, cfg: ProviderConfig,
                          spec_key: str | None = None) -> CloudModel:
        """将 ProviderConfig.model_specs 中的手动属性合并到 CloudModel。

        手动配置优先：如果 model_specs 中有该模型，覆盖/补充其能力属性。

        spec_key: 显式指定 model_specs 的查找键。spec 别名场景下注册表 key 是
                  短名（如 "k3"）而 model.model_id 是上游全名（如 "kimi/k3"），
                  二者不一致，必须显式传入，否则 spec 属性会被漏合并。
        """
        spec = cfg.model_specs.get(spec_key or model.model_id)
        if not spec:
            return model
        # 新字段
        if spec.get("name"):
            model.name = spec["name"]
        if spec.get("contextWindow") is not None:
            model.contextWindow = spec["contextWindow"]
        if spec.get("maxTokens") is not None:
            model.maxTokens = spec["maxTokens"]
        if spec.get("input") is not None:
            model.input = list(spec["input"])
        if spec.get("reasoning") is not None:
            model.reasoning = bool(spec["reasoning"])
        # 原有字段
        if spec.get("context_window") is not None:
            model.context_window = spec["context_window"]
        if spec.get("max_output_tokens") is not None:
            model.max_output_tokens = spec["max_output_tokens"]
        if spec.get("supports_vision") is not None:
            model.supports_vision = bool(spec["supports_vision"])
        if spec.get("supports_tools") is not None:
            model.supports_tools = bool(spec["supports_tools"])
        # G-2: 价格字段
        if spec.get("price_input") is not None:
            model.price_input = float(spec["price_input"])
        if spec.get("price_output") is not None:
            model.price_output = float(spec["price_output"])
        # 收集非标准字段到 extra
        known = {"name", "contextWindow", "maxTokens", "input", "reasoning",
                 "context_window", "max_output_tokens", "supports_vision", "supports_tools",
                 "price_input", "price_output"}
        for k, v in spec.items():
            if k not in known:
                model.extra[k] = v
        return model

    def _register_spec_only_models(self, merged: dict[str, CloudModel]):
        """注册仅有 model_specs 但未被 /models 发现的模型。

        场景：模型暂时下线但配置已知，仍需在注册表中保留。
        """
        for name, cfg in self._providers.items():
            if not cfg.enabled:
                continue
            for mid, spec in cfg.model_specs.items():
                if mid in merged:
                    continue  # 已发现，跳过
                # v6.6：spec 的 key 是注册表短名（routing 按 split("/")[-1] 查表），
                # spec["model_id"] 才是上游真实模型 id —— _serialize_providers 保存时
                # 正是 key=短名 / model_id=全名。二者不同时：注册表 key 用短名，
                # CloudModel.model_id 用全名，转发时由 forward_to_cloud 覆盖 data["model"]。
                upstream_id = (spec or {}).get("model_id") or mid
                # 仅 spec 注册，协议信息从 provider 配置推断
                m = CloudModel(
                    model_id=upstream_id,
                    provider=name,
                    openai_available=bool(cfg.openai_base),
                    anthropic_available=bool(cfg.anthropic_base),
                    discovered_at=0.0,  # 0 表示未实际发现，仅配置
                )
                m = self._merge_model_spec(m, cfg, spec_key=mid)
                merged[mid] = m
                merged[f"{name}/{mid}"] = m
                log.debug("Registered spec-only model: %s/%s (upstream %s)",
                          name, mid, upstream_id)

    def _load_config(self, config_path: Path):
        # v4.7.0: Inject secrets.env before expanding ${VAR}
        self._inject_secrets_env()

        if not config_path.exists():
            log.info("cloud_provider.yaml not found — cloud discovery disabled")
            return
        try:
            with open(config_path) as f:
                raw = f.read()
            # Expand environment variables (${VAR} syntax)
            import re as _re
            def _env_replace(m):
                var = m.group(1)
                val = os.environ.get(var)
                if not val:
                    log.warning("cloud_provider.yaml: env var $%s not set", var)
                return val or ""
            expanded = _re.sub(r"\$\{(\w+)\}", _env_replace, raw)
            cfg = yaml.safe_load(expanded)
        except Exception as e:
            log.error("Failed to load cloud_provider.yaml: %s", e)
            self._config_corrupt = True
            return
        if not cfg:
            return

        for name, pcfg in (cfg.get("providers") or {}).items():
            if not isinstance(pcfg, dict):
                continue
            discovery = pcfg.get("discovery") or {}
            routing = pcfg.get("routing") or {}
            # v4.6.0: Parse model specs
            model_specs = {}
            for mid, mspec in (pcfg.get("models") or {}).items():
                if isinstance(mspec, dict):
                    model_specs[mid] = mspec
            # v4.7.0: Derive key_env_var from raw api_key if it's a ${VAR} reference
            raw_api_key = pcfg.get("api_key") or ""
            key_env_var = pcfg.get("key_env_var", "")
            if not key_env_var and raw_api_key.startswith("${") and raw_api_key.endswith("}"):
                key_env_var = raw_api_key[2:-1]
            if not key_env_var:
                key_env_var = self._env_key_for_provider(name)

            provider = ProviderConfig(
                name=name,
                api_key=raw_api_key,
                openai_base=pcfg.get("openai_base", ""),
                anthropic_base=pcfg.get("anthropic_base", ""),
                timeout=pcfg.get("timeout", 600),
                enabled=pcfg.get("enabled", True),
                discovery_enabled=discovery.get("enabled", True),
                discovery_endpoint=discovery.get("endpoint", "/models"),
                discovery_interval=discovery.get("interval", 3600),
                include_pattern=discovery.get("filter", {}).get("include_pattern", ""),
                routing_default=routing.get("default", "cloud_only"),
                # v6.1 Phase 1: 白名单
                enabled_models=list(pcfg.get("enabled_models") or []),
                model_specs=model_specs,
                key_env_var=key_env_var,
                preset_id=pcfg.get("preset_id", ""),
            )
            self._providers[name] = provider

        log.info("Cloud config loaded: %d providers, %d model specs",
                 len(self._providers),
                 sum(len(p.model_specs) for p in self._providers.values()))
        self._config_corrupt = False  # 成功加载后重置 corrupt 标志

    def _discover_provider(self, cfg: ProviderConfig) -> list[CloudModel]:
        """对单个 provider 执行 GET /models。"""
        if not cfg.openai_base:
            return []

        url = f"{cfg.openai_base.rstrip('/')}{cfg.discovery_endpoint}"
        log.debug("Discovering models from %s: %s", cfg.name, url)

        req = urllib.request.Request(url)
        req.add_header("User-Agent", _CLOUD_UA)
        if cfg.api_key:
            req.add_header("Authorization", f"Bearer {cfg.api_key}")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                body = _json.loads(resp.read().decode('utf-8')) or {}
        except urllib.error.HTTPError as e:
            log.warning("Provider %s returned HTTP %d", cfg.name, e.code)
            return []
        except Exception as e:
            log.warning("Provider %s request failed: %s", cfg.name, e)
            return []

        raw_models = body.get("data") or []
        pattern = re.compile(cfg.include_pattern) if cfg.include_pattern else None
        has_anthropic = bool(cfg.anthropic_base)

        results = []
        for m in raw_models:
            mid = m.get("id", "")
            if not mid:
                continue
            if pattern and not pattern.match(mid):
                continue
            results.append(CloudModel(
                model_id=mid,
                provider=cfg.name,
                openai_available=True,
                anthropic_available=has_anthropic,
                discovered_at=time.time(),
            ))

        return results

    def _poll_loop(self, interval: int):
        """后台轮询循环。"""
        while not self._stop_event.wait(timeout=interval):
            try:
                self.discover_all()
            except Exception as e:
                log.warning("Polling discovery error: %s", e)
