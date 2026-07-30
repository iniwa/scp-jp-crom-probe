from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "scp_jp_crom_unified_probe.py"
SPEC = importlib.util.spec_from_file_location("unified_probe", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class UnifiedParserTests(unittest.TestCase):
    def test_translation_credit_fields_and_url(self) -> None:
        source = """
        **タイトル:** SCP-5001 - 神聖にして侵すべからず
        **翻訳責任者:** [[*user ai0240]]
        **翻訳年:** 2024
        **原題:** Sacrosanct
        **著作権者:** [[*user Yossipossi]]
        **作成年:** 2020
        **元記事リンク:** [https://scp-wiki.wikidot.com/scp-5001 ソース]
        """
        fields = MODULE.extract_credit_fields(source)
        urls = MODULE.extract_credit_urls(source)
        self.assertEqual(fields["translation_responsible"], "ai0240")
        self.assertEqual(fields["original_title"], "Sacrosanct")
        self.assertEqual(fields["copyright_holder"], "Yossipossi")
        self.assertEqual(urls["original_url"], "https://scp-wiki.wikidot.com/scp-5001")

    def test_blockquote_credit_format(self) -> None:
        source = """
        > タイトル: 無間地獄
        > 著者: lanlanmag
        > 原題: 무간지옥
        > 翻訳責任者: Amplifier
        > 元記事リンク: https://scpko.wikidot.com/scp-444-ko
        """
        fields = MODULE.extract_credit_fields(source)
        self.assertEqual(fields["title"], "無間地獄")
        self.assertEqual(fields["translation_responsible"], "Amplifier")
        self.assertEqual(fields["original_title"], "무간지옥")

    def test_jp_original_classification(self) -> None:
        page = {"tags": ["jp", "scp"], "attributions": []}
        classified = MODULE.classify_edition(page, {}, {})
        self.assertEqual(classified["edition"], "jp_original")
        self.assertEqual(classified["confidence"], "confirmed")

    def test_translation_from_crom_attribution(self) -> None:
        page = {
            "tags": ["en", "scp"],
            "attributions": [{"type": "TRANSLATOR", "user": {"displayName": "T"}}],
        }
        classified = MODULE.classify_edition(page, {}, {})
        self.assertEqual(classified["edition"], "translation")
        self.assertEqual(classified["confidence"], "confirmed")

    def test_translation_from_credit(self) -> None:
        page = {"tags": ["scp"], "attributions": []}
        credit = {"translation_responsible": "T", "original_title": "Original"}
        classified = MODULE.classify_edition(page, credit, {})
        self.assertEqual(classified["edition"], "translation")
        self.assertEqual(classified["confidence"], "confirmed")

    def test_translation_from_branch_tag_is_probable(self) -> None:
        page = {"tags": ["fr", "tale"], "attributions": []}
        classified = MODULE.classify_edition(page, {}, {})
        self.assertEqual(classified["edition"], "translation")
        self.assertEqual(classified["confidence"], "probable")
        self.assertEqual(classified["source_branch"], "FR")

    def test_jp_translation_conflict(self) -> None:
        page = {
            "tags": ["jp", "scp"],
            "attributions": [{"type": "TRANSLATOR", "user": {"displayName": "T"}}],
        }
        classified = MODULE.classify_edition(page, {}, {})
        self.assertEqual(classified["edition"], "conflict")

    def test_unknown_without_evidence(self) -> None:
        page = {"tags": ["scp"], "attributions": []}
        classified = MODULE.classify_edition(page, {}, {})
        self.assertEqual(classified["edition"], "unknown")

    def test_excluded_author_component_theme_pages(self) -> None:
        base = {
            "url": "http://scp-jp.wikidot.com/example",
            "isHidden": False,
            "category": "_default",
        }
        for tag in ("著者ページ", "コンポーネント", "テーマ"):
            page = {**base, "tags": ["tale", tag]}
            self.assertFalse(MODULE.is_content_candidate(page), tag)

    def test_external_wiki_archive_is_included(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/archive-example",
            "tags": ["外部ウィキアーカイブ"],
            "isHidden": False,
            "category": "_default",
        }
        self.assertTrue(MODULE.is_content_candidate(page))
        self.assertEqual(MODULE.classify_genre(page), "外部ウィキアーカイブ")

    def test_source_branch_from_url(self) -> None:
        branch, basis = MODULE.infer_source_branch(
            "https://scpko.wikidot.com/scp-444-ko", ["scp"]
        )
        self.assertEqual((branch, basis), ("KO", "original_url"))

    def test_translated_scp_subtitle(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/scp-5001",
            "title": "SCP-5001",
            "tags": ["en", "scp"],
            "alternateTitles": [],
        }
        title, subtitle = MODULE.derive_titles(
            page, {"title": "SCP-5001 - 神聖にして侵すべからず"}
        )
        self.assertEqual(title, "SCP-5001")
        self.assertEqual(subtitle, "神聖にして侵すべからず")

    def test_non_numbered_foreign_scp_subtitle(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/scp-444-ko",
            "title": "SCP-444-KO",
            "tags": ["ko", "scp"],
            "alternateTitles": [],
        }
        _, subtitle = MODULE.derive_titles(page, {"title": "無間地獄"})
        self.assertEqual(subtitle, "無間地獄")


if __name__ == "__main__":
    unittest.main()
