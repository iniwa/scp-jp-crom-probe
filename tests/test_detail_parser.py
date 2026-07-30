from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "scp_jp_crom_detail_probe.py"
SPEC = importlib.util.spec_from_file_location("detail_probe", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DetailParserTests(unittest.TestCase):
    def test_credit_title(self) -> None:
        source = """[[include :scp-jp:credit:start]]
**タイトル:** SCP-4733-JP - 吝嗇の飲食
**著者:** [[*user wavekey]]
**作成年:** 2026
[[include :scp-jp:credit:end]]"""
        fields = MODULE.extract_credit_fields(source)
        self.assertEqual(fields["title"], "SCP-4733-JP - 吝嗇の飲食")
        self.assertEqual(fields["author"], "wavekey")
        self.assertEqual(fields["year"], "2026")

    def test_scp_subtitle_from_credit(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/scp-4733-jp",
            "title": "SCP-4733-JP",
            "tags": ["jp", "scp"],
            "alternateTitles": [],
        }
        title, subtitle = MODULE.derive_titles(
            page, {"title": "SCP-4733-JP - 吝嗇の飲食"}
        )
        self.assertEqual(title, "SCP-4733-JP")
        self.assertEqual(subtitle, "吝嗇の飲食")

    def test_scp_subtitle_from_alternate(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/scp-3137-jp",
            "title": "SCP-3137-JP",
            "tags": ["jp", "scp"],
            "alternateTitles": [
                {"title": "上野ペンギン関係マップ", "source": "index"}
            ],
        }
        _, subtitle = MODULE.derive_titles(page, {})
        self.assertEqual(subtitle, "上野ペンギン関係マップ")

    def test_article_filter(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/example",
            "tags": ["jp", "tale"],
            "isHidden": False,
            "category": "_default",
        }
        self.assertTrue(MODULE.is_monitor_article(page))
        page["category"] = "fragment"
        self.assertFalse(MODULE.is_monitor_article(page))


if __name__ == "__main__":
    unittest.main()
