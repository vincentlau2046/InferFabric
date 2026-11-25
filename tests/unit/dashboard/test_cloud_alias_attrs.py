"""
unit/dashboard/test_cloud_alias_attrs.py — PR2 Task 4：模型别名入口 UI 契约

钉住 cloud.js 别名功能五项契约（源码断言风格，同 test_cloud_edit_attrs.py；
dashboard 无 Node 工具链，行为验证靠 Task 5 双端验收走查）：
  1. 可路由模型行（候选勾选/已声明）有「别名」入口（data-action="cpm-alias"）
  2. 提交走 POST /admin/cloud/provider-alias 且带 expected_config_fingerprint（CAS）
  3. 空输入提交 = 移除（alias:"" 语义接线）
  4. 409 config_conflict / alias_conflict / model_not_routable 展示后端 detail
  5. 已设别名的行显示别名徽标，复制调用片段用别名（cpm-alias-copy）
"""

from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "inferfabric" / "dashboard"


def _js():
    return (DASHBOARD_DIR / "js" / "cloud.js").read_text(encoding="utf-8")


def _panel_js():
    """renderModelPanel 函数体（别名入口只在模型策展面板行内断言）。"""
    js = _js()
    start = js.index("function renderModelPanel(")
    end = js.index("\n  function ", start + 10)
    return js[start:end]


class TestAliasEntry:
    def test_routable_rows_have_alias_action(self):
        panel = _panel_js()
        # 候选行（可路由才给）与已声明行都调用别名入口模板（helper 复用）
        assert 'data-action="cpm-alias"' in panel
        assert "? aliasEntry(mid)" in panel      # 候选行：可路由才给
        assert "aliasEntry(s.id)" in panel       # 已声明行：恒可路由
        assert "别名" in panel

    def test_alias_badge_for_set_rows(self):
        panel = _panel_js()
        # 已设别名的行渲染徽标（数据来自 GET providers 的 aliases）
        assert "aliases" in panel
        assert "cpm-alias-badge" in panel

    def test_copy_snippet_uses_alias(self):
        js = _js()
        assert 'data-action="cpm-alias-copy"' in js
        # 复制走剪贴板，内容为别名（客户端 model 字段值）
        assert "clipboard.writeText" in js


class TestAliasSubmit:
    def test_targets_provider_alias_with_cas(self):
        js = _js()
        assert "/admin/cloud/provider-alias" in js
        assert "expected_config_fingerprint" in js
        assert "model_id" in js

    def test_empty_input_means_remove(self):
        js = _js()
        # 空输入提交为移除：alias 以空串提交（后端 alias:"" = 移除语义）
        assert 'alias: aliasVal' in js or ('alias:' in js and "trim()" in js)

    def test_error_feedback_paths(self):
        js = _js()
        # 409：提示刷新重试；冲突/校验拒绝：展示后端 detail
        assert "config_conflict" in js
        assert "alias_conflict" in js
        assert "model_not_routable" in js
