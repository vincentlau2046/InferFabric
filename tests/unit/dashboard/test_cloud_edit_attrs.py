"""
unit/dashboard/test_cloud_edit_attrs.py — PR1 Task 5：Provider 编辑入口 UI 契约

钉住 cloud.js 的编辑模态四项契约（源码断言风格，同 test_cloud_panel_css.py；
dashboard 无 Node 工具链，行为验证靠 Task 6 双端验收走查）：
  1. Provider 行有「编辑」入口（data-action="provider-edit"）
  2. 提交走 POST /admin/cloud/provider-update 且带配置指纹 CAS
  3. key_source == external 时 Key 输入禁用并标注外部托管；留空不改 placeholder
  4. 409 冲突与探针失败（probe_failed / destination_key_unconfirmed）有反馈路径
"""

from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "inferfabric" / "dashboard"


def _js():
    return (DASHBOARD_DIR / "js" / "cloud.js").read_text(encoding="utf-8")


class TestProviderEditEntry:
    def test_row_edit_action_present(self):
        assert 'data-action="provider-edit"' in _js()

    def test_submit_targets_provider_update(self):
        js = _js()
        assert "/admin/cloud/provider-update" in js
        # CAS：提交必须携带编辑时读到的配置指纹
        assert "expected_config_fingerprint" in js
        assert "config_fingerprint" in js

    def test_external_key_input_disabled_and_labeled(self):
        js = _js()
        # 外部托管判定（双引号/单引号都接受）
        assert ("key_source === 'external'" in js) or ('key_source === "external"' in js)
        # Key 输入禁用 + 外部托管标注 + 留空不改语义
        assert "外部托管" in js
        assert "留空不改" in js

    def test_conflict_and_probe_feedback(self):
        js = _js()
        # 409：提示刷新重试
        assert "config_conflict" in js
        # 探针门禁失败：显示后端 detail
        assert "probe_failed" in js
        assert "destination_key_unconfirmed" in js

    def test_row_shows_display_name(self):
        # 显示名随行可见（显示名不是路由名，仅展示）— 断言限定在 providerRow
        # 函数体内（文件其余处 display_name 属预设卡片，不算数）
        js = _js()
        start = js.index("function providerRow")
        end = js.index("function ", start + 10)
        assert "display_name" in js[start:end]
