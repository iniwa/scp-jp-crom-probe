from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "scp_jp_monitor.py"
SPEC = importlib.util.spec_from_file_location("scp_jp_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MonitorTests(unittest.TestCase):
    def test_excluded_page_types(self) -> None:
        base = {
            "url": "http://scp-jp.wikidot.com/example",
            "isHidden": False,
            "category": "_default",
        }
        for tag in ("著者ページ", "コンポーネント", "テーマ"):
            self.assertFalse(MODULE.is_content_candidate({**base, "tags": ["tale", tag]}))

    def test_translation_source_fallback(self) -> None:
        source = """
        タイトル: フォーマットスクリュー ハブ
        著者: Henzoid, Tstaffor, TopDownUnder
        原題: Format Screw Hub
        翻訳者: oplax-counterpoint
        翻訳年: 2026
        ソース: https://scp-wiki.wikidot.com/format-screw-hub
        """
        credit = MODULE.extract_credit_fields(source)
        urls = MODULE.extract_credit_urls(source)
        self.assertEqual(
            MODULE.effective_original_url(credit, urls),
            "https://scp-wiki.wikidot.com/format-screw-hub",
        )

    def test_wikidot_artifacts_are_removed_from_people(self) -> None:
        self.assertEqual(MODULE.split_people("[[*user radian462]] @@ @@"), ["radian462"])
        self.assertEqual(MODULE.clean_credit_value('"Example";'), "Example")

    def test_non_scp_does_not_invent_subtitle(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/market-garden-art",
            "title": "硝煙の彩: 我らこそ戦争 魔法少女たちの1944年 共同芸術設定集",
            "tags": ["cn", "アートワーク"],
            "alternateTitles": [],
        }
        title, subtitle = MODULE.derive_titles(page, {"title": '"UNMA 公文書館";'})
        self.assertIn("硝煙の彩", title)
        self.assertIsNone(subtitle)

    def test_numbered_scp_subtitle(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/scp-567-ko",
            "title": "SCP-567-KO",
            "tags": ["ko", "scp"],
            "alternateTitles": [],
        }
        title, subtitle = MODULE.derive_titles(
            page, {"title": "SCP-567-KO - アリス殺し"}
        )
        self.assertEqual(title, "SCP-567-KO")
        self.assertEqual(subtitle, "アリス殺し")

    def test_original_author_prefers_author_field(self) -> None:
        page = {
            "url": "http://scp-jp.wikidot.com/format-screw-hub",
            "wikidotId": "1",
            "title": "フォーマットスクリュー ハブ",
            "tags": ["en", "ハブ"],
            "createdAt": "2026-07-29T11:40:19.000Z",
            "isHidden": False,
            "category": "_default",
            "source": """
            著者: Henzoid, Tstaffor, TopDownUnder
            原題: Format Screw Hub
            翻訳者: oplax-counterpoint
            翻訳年: 2026
            ソース: https://scp-wiki.wikidot.com/format-screw-hub
            著作権者: unrelated-image-credit
            """,
            "textContent": "紹介\nフォーマットを崩す記事を集めたハブです。",
            "attributions": [],
            "alternateTitles": [],
            "createdBy": {"displayName": "oplax-counterpoint"},
        }
        article = MODULE.serialize_article(page)
        self.assertEqual(
            article["original_authors"], ["Henzoid", "Tstaffor", "TopDownUnder"]
        )
        self.assertEqual(
            article["original_url"],
            "https://scp-wiki.wikidot.com/format-screw-hub",
        )

    def test_content_warning_extraction(self) -> None:
        warnings = MODULE.extract_content_warnings(
            "⚠️ コンテンツ警告: 本作品には死の描写が含まれます。",
            "",
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("コンテンツ警告", warnings[0])

    def test_scp_summary_starts_from_description_and_stops_before_addendum(self) -> None:
        text = """
        クレジット
        タイトル: SCP-0000-JP - テスト
        特別収容プロトコル: 長い収容文です。
        説明: これは異常な試験物品です。
        追加の説明です。
        補遺1: ここから先は重大な展開です。
        """
        basis, basis_type = MODULE.summary_basis(text, "SCP報告書")
        self.assertEqual(basis_type, "description")
        self.assertTrue(basis.startswith("説明:"))
        self.assertNotIn("重大な展開", basis)
        self.assertNotIn("クレジット", basis)

    def test_tale_summary_strips_credit_block(self) -> None:
        text = """
        Info
        翻訳責任者: T
        翻訳年: 2026
        原題: Example
        元記事リンク: https://example.com

        夜の川辺で、研究員はひとり立っていた。
        風が静かに吹いた。
        """
        basis, basis_type = MODULE.summary_basis(text, "Tale")
        self.assertEqual(basis_type, "opening")
        self.assertTrue(basis.startswith("夜の川辺"))
        self.assertNotIn("翻訳責任者", basis)

    def test_public_wikidot_url_uses_https(self) -> None:
        self.assertEqual(
            MODULE.public_page_url("http://scp-jp.wikidot.com/scp-1234-jp"),
            "https://scp-jp.wikidot.com/scp-1234-jp",
        )

    def test_state_baseline_and_new_article(self) -> None:
        baseline = {
            "reported_articles": [
                {"wikidot_id": "100", "page_name": "old", "article_title": "Old"}
            ],
            "recorded_at_utc": "2026-07-30T00:00:00+00:00",
        }
        state = {"schema_version": 1, "seen": MODULE.baseline_seen(baseline)}
        now = datetime(2026, 7, 30, 3, 20, tzinfo=timezone.utc)
        article = {
            "wikidot_id": "200",
            "page_name": "new",
            "created_at_utc": "2026-07-30T02:00:00+00:00",
            "edition": "translation",
            "article_title": "New",
        }
        state, new_ids = MODULE.update_state(
            state, [article], now_utc=now, snapshot_days=14
        )
        self.assertEqual(new_ids, {"200"})
        self.assertTrue(state["seen"]["100"]["baseline"])
        candidates = MODULE.notification_articles(
            state, now_utc=now, retention_hours=72, new_ids=new_ids
        )
        self.assertEqual([item["wikidot_id"] for item in candidates], ["200"])

    def test_rerun_preserves_first_seen_and_is_not_new(self) -> None:
        now = datetime(2026, 7, 30, 3, 20, tzinfo=timezone.utc)
        article = {
            "wikidot_id": "200",
            "page_name": "new",
            "created_at_utc": "2026-07-30T02:00:00+00:00",
            "edition": "translation",
            "article_title": "New",
        }
        state = {"schema_version": 1, "seen": {}}
        state, first_ids = MODULE.update_state(
            state, [article], now_utc=now, snapshot_days=14
        )
        first_seen = state["seen"]["200"]["first_seen_at_utc"]
        state, second_ids = MODULE.update_state(
            state, [article], now_utc=now + timedelta(hours=1), snapshot_days=14
        )
        self.assertEqual(first_ids, {"200"})
        self.assertEqual(second_ids, set())
        self.assertEqual(state["seen"]["200"]["first_seen_at_utc"], first_seen)

    def test_notification_retention_expires(self) -> None:
        now = datetime(2026, 7, 30, 3, 20, tzinfo=timezone.utc)
        state = {
            "schema_version": 1,
            "seen": {
                "200": {
                    "wikidot_id": "200",
                    "page_name": "new",
                    "first_seen_at_utc": MODULE.utc_iso(now - timedelta(hours=73)),
                    "last_seen_at_utc": MODULE.utc_iso(now),
                    "baseline": False,
                    "article": {
                        "wikidot_id": "200",
                        "page_name": "new",
                        "created_at_utc": MODULE.utc_iso(now),
                    },
                }
            },
        }
        self.assertEqual(
            MODULE.notification_articles(
                state, now_utc=now, retention_hours=72, new_ids=set()
            ),
            [],
        )


    def test_execute_bootstrap_and_rerun_keep_notification_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            baseline_path = root / "baseline.json"
            baseline_path.write_text(
                """{
                  \"schema_version\": 1,
                  \"bootstrap_since_jst\": \"2026-07-26T00:00:00+09:00\",
                  \"recorded_at_utc\": \"2026-07-30T00:00:00+00:00\",
                  \"reported_articles\": [
                    {\"wikidot_id\": \"100\", \"page_name\": \"scp-100-jp\"}
                  ]
                }""",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                output_dir=str(root / "out1"),
                baseline_file=str(baseline_path),
                previous_state_file="",
                endpoint="https://example.invalid/graphql",
                window_days=30,
                notification_hours=72,
                snapshot_days=14,
                page_size=100,
                max_pages=1000,
                max_detail_pages=200,
                timeout=30.0,
                attempts=3,
                now="2026-07-30T03:20:00+00:00",
            )
            metadata = [
                {
                    "__typename": "WikidotPage",
                    "url": "http://scp-jp.wikidot.com/scp-100-jp",
                    "wikidotId": "100",
                    "title": "SCP-100-JP",
                    "tags": ["jp", "scp"],
                    "createdAt": "2026-07-29T01:00:00+00:00",
                    "isHidden": False,
                    "category": "_default",
                },
                {
                    "__typename": "WikidotPage",
                    "url": "http://scp-jp.wikidot.com/scp-200",
                    "wikidotId": "200",
                    "title": "SCP-200",
                    "tags": ["en", "scp"],
                    "createdAt": "2026-07-29T02:00:00+00:00",
                    "isHidden": False,
                    "category": "_default",
                },
            ]
            details = {
                "http://scp-jp.wikidot.com/scp-100-jp": {
                    **metadata[0],
                    "source": "タイトル: SCP-100-JP - 既報\n著者: A",
                    "textContent": "説明: 既報のJP記事です。",
                    "createdBy": {"displayName": "A"},
                    "attributions": [],
                    "alternateTitles": [],
                },
                "http://scp-jp.wikidot.com/scp-200": {
                    **metadata[1],
                    "source": (
                        "タイトル: SCP-200 - 翻訳記事\n"
                        "翻訳責任者: T\n翻訳年: 2026\n"
                        "原題: SCP-200 - Original\n"
                        "著作権者: O\n"
                        "元記事リンク: https://scp-wiki.wikidot.com/scp-200"
                    ),
                    "textContent": "説明: 新しい翻訳記事です。",
                    "createdBy": {"displayName": "T"},
                    "attributions": [],
                    "alternateTitles": [],
                },
            }

            def output_paths(directory: pathlib.Path):
                public = directory / "public"
                return MODULE.OutputPaths(
                    root=directory,
                    public=public,
                    health=public / "health.json",
                    latest=public / "latest.json",
                    delta=public / "delta.json",
                    state=directory / "state.json",
                    summary=directory / "monitor-summary.md",
                    debug=directory / "monitor-debug.json",
                    index=public / "index.html",
                )

            with patch.object(MODULE, "collect_recent_pages", return_value=(metadata, False, [300000])), patch.object(
                MODULE,
                "fetch_detail",
                side_effect=lambda _args, url: (details[url], 299990),
            ):
                health, code = MODULE.execute(args, output_paths(root / "out1"))
            self.assertEqual(code, 0)
            self.assertEqual(health["counts"]["new_this_run"], 1)
            first_delta = MODULE.read_json(root / "out1" / "public" / "delta.json")
            self.assertEqual([item["wikidot_id"] for item in first_delta["articles"]], ["200"])

            args.previous_state_file = str(root / "out1" / "state.json")
            args.output_dir = str(root / "out2")
            args.now = "2026-07-30T04:20:00+00:00"
            with patch.object(MODULE, "collect_recent_pages", return_value=(metadata, False, [299980])), patch.object(
                MODULE,
                "fetch_detail",
                side_effect=lambda _args, url: (details[url], 299970),
            ):
                health2, code2 = MODULE.execute(args, output_paths(root / "out2"))
            self.assertEqual(code2, 0)
            self.assertEqual(health2["counts"]["new_this_run"], 0)
            second_delta = MODULE.read_json(root / "out2" / "public" / "delta.json")
            self.assertEqual([item["wikidot_id"] for item in second_delta["articles"]], ["200"])


if __name__ == "__main__":
    unittest.main()
