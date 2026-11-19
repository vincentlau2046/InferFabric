"""M2-T4 — cloud 策展属性浮窗：组装与源码契约（计划 2026-11-19 Task 4）。

数据源冻结（本任务不改后端）：
- GET /admin/cloud/providers → model_specs[].{supports_vision, supports_tools,
  context_window, max_output_tokens, price_input, price_output}（handler.py
  已透传六键，None = 未声明/未设置）。
- POST /admin/cloud/provider-models {provider, action:"tag", model, ...changed}
  → update_model_spec 语义：请求字段缺省 = 保留；能力 false = 解除打标
  （键移除回未声明）；数值显式 null/"" = 清除属性；0 是合法数字。

源码契约钉住计划浏览器用例的行为（浏览器验收在沙箱另行执行）：
- 可路由条目有属性入口；打开不改保存不发 POST（无变更短路）；
- 只发变更字段；价格 0 按数字提交、显式清空提交 null；
- 已有显式能力 false 不动即保留（UI 不新增写显式排除的途径）；
- 取消不提交；保存失败显示错误、保留输入可更正，不宣称成功。
"""
from pathlib import Path

from inferfabric import dashboard

JS_DIR = Path(dashboard._DASHBOARD_DIR) / "js"


def _src(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


# ── 组装契约 ─────────────────────────────────────────────────────

def test_cloud_attributes_entry_is_assembled():
    html = dashboard.get_html()
    # 属性入口真实装配（cloud.js 内联进页面，入口 action 随源码可见）
    assert 'data-action="cpm-attrs"' in html


# ── cloud.js 属性浮窗契约 ────────────────────────────────────────

def test_cloud_attrs_entry_on_routable_rows():
    src = _src("cloud.js")
    # 入口出现在：发现候选可路由行、已声明 spec 行、事件分派 case —— 三处以上
    assert src.count("cpm-attrs") >= 3
    assert "case 'cpm-attrs'" in src


def test_cloud_attrs_only_changed_fields_tag_post():
    src = _src("cloud.js")
    # tag 动作 + 只发变更：无变更短路（不发 POST）
    assert "'tag'" in src
    assert "无变更" in src
    # 能力回填以原始声明为准：仅 true 视为已声明；false（显式排除）与
    # null 同渲染为未声明，不动即保留 —— 判定用 `=== true`
    assert "supports_vision === true" in src
    # 数值显式清除发 null（原值存在且输入清空）
    assert ": null" in src or "= null" in src
    # 保存成功回读该 provider
    assert "cloudLoadProviders" in src


def test_cloud_attrs_zero_and_types():
    src = _src("cloud.js")
    # 空串判定用 trim() === ''（区别于数字 0 —— 0 按数字提交，不是未设置）
    assert "trim() === ''" in src
    # token 数为整数、价格为有限数字
    assert "Number.isInteger" in src
    assert "isFinite" in src


def test_cloud_attrs_error_keeps_inputs():
    src = _src("cloud.js")
    # 保存失败：浮窗内联错误显示（保留输入可更正），不宣称保存成功
    assert "cpAttrsErr" in src


def test_cloud_attrs_no_inline_onclick():
    src = _src("cloud.js")
    assert "onclick=" not in src


# ── YAML 落盘回读契约（浏览器验收暴露的序列化缺陷回归钉） ─────────
#
# 属性编辑器管理的六键必须经 save_config → YAML → 重启加载完整回读：
# - context_window / max_output_tokens：此前序列化器在 known 集合里却从不
#   显式写出（known 排除又挡住兜底循环）→ 属性重启静默蒸发；
# - price_input / price_output：此前 truthy 门（`if spec.get(...)`）把
#   合法 0 价当未设置丢弃 —— 0 是合法数字（与前端「0 按数字提交」同语义）。

import yaml as _yaml

from inferfabric.cloud_discovery import CloudDiscovery

_ROUNDTRIP_YAML = """\
providers:
  qa-cloud:
    openai_base: http://127.0.0.1:9
    api_key: ""
    models:
      qa-model:
        model_id: qa-model
        context_window: 8192
        max_output_tokens: 0
        price_input: 0
        price_output: 1.5
        supports_vision: false
        supports_tools: true
"""

_ATTR_KEYS = ("context_window", "max_output_tokens", "price_input",
              "price_output", "supports_vision", "supports_tools")


def test_cloud_attrs_yaml_roundtrip(tmp_path):
    cfg1 = tmp_path / "cloud_provider.yaml"
    cfg1.write_text(_ROUNDTRIP_YAML, encoding="utf-8")
    d1 = CloudDiscovery(cfg1)
    assert "qa-cloud" in d1.providers
    spec1 = d1.providers["qa-cloud"].model_specs["qa-model"]

    serialized = _yaml.safe_dump(d1._serialize_providers(), allow_unicode=True)
    cfg2 = tmp_path / "roundtrip.yaml"
    cfg2.write_text(serialized, encoding="utf-8")
    d2 = CloudDiscovery(cfg2)
    spec2 = d2.providers["qa-cloud"].model_specs["qa-model"]

    for k in _ATTR_KEYS:
        assert spec2.get(k) == spec1.get(k), \
            f"attribute {k!r} lost across save_config round-trip: " \
            f"{spec1.get(k)!r} -> {spec2.get(k)!r}"
