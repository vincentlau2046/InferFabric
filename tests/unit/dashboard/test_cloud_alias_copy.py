"""unit/dashboard/test_cloud_alias_copy.py — #73：别名复制按钮剪贴板契约。

钉住 cloud.js 复制调用名三契约（源码断言风格，同 test_cloud_alias_attrs.py）：
  1. writeText 以 .then( 消费结果 —— 成功/失败 toast 由 promise 结果驱动
  2. navigator.clipboard 不可用（局域网 http 非安全上下文）时走
     document.execCommand('copy') 回退
  3. 不允许「先无条件成功 toast、不等剪贴板结果」的旧形态；回退/失败
     均有失败提示路径
"""

import re
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "inferfabric" / "dashboard"


def _js():
    return (DASHBOARD_DIR / "js" / "cloud.js").read_text(encoding="utf-8")


class TestAliasCopyClipboard:
    def test_write_text_result_is_consumed(self):
        js = _js()
        # 事件分支消费 helper 的 promise（成功/失败 toast 由结果驱动）
        assert "copyTextToClipboard(copyAlias).then(" in js
        # clipboard API 的 promise 本体同样带结果消费（成功/拒绝双分支）
        assert "writeText(text).then(" in js

    def test_execcommand_fallback_exists(self):
        js = _js()
        # 优先路径仍在
        assert "navigator.clipboard && navigator.clipboard.writeText" in js
        # 回退路径存在（非安全上下文 http 下 clipboard API 缺失）
        assert "execCommand('copy')" in js

    def test_failure_toast_path_exists(self):
        js = _js()
        assert "复制失败" in js

    def test_no_fire_and_forget_success_toast(self):
        js = _js()
        # 旧形态：writeText(...) 后紧随无条件成功 toast（不等结果）
        assert not re.search(r"writeText\([^)]*\);\s*\n\s*UI\.toast\('已复制", js), \
            "不得先 toast 成功再丢弃 promise"
