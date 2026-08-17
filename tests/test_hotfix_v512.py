from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "scp_jp_hotfix_v512.py"
SPEC = importlib.util.spec_from_file_location("scp_jp_hotfix_v512", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeCore:
    BRANCH_BY_TAG = {"en": "EN", "cn": "CN"}

    @staticmethod
    def infer_source_branch(original_url, tags):
        lowered = {str(tag).casefold() for tag in tags}
        for tag, branch in FakeCore.BRANCH_BY_TAG.items():
            if tag in lowered:
                return branch, "tag"
        return None, None

    @staticmethod
    def page_name(url):
        return url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def public_page_url(url):
        return url.replace("http://scp-jp.wikidot.com", "https://scp-jp.wikidot.com")

    @staticmethod
    def to_jst_string(raw):
        return raw

    @staticmethod
    def clean_credit_value(value):
        return value.strip()


class HotfixTests(unittest.TestCase):
    def test_primary_credit_source_excludes_later_licence_metadata(self):
        source = """
[[include :scp-jp:credit:start]]
著者: JP Author
作成年: 2026
[[include :scp-jp:credit:end]]

+ 画像ライセンス
翻訳者: Someone Else
原題: Referenced Translation
"""
        scoped = MODULE.primary_credit_source(source)
        self.assertIn("著者: JP Author", scoped)
        self.assertNotIn("翻訳者: Someone Else", scoped)

    def test_jp_artwork_with_translator_credit_remains_jp_original(self):
        classify = MODULE.make_classify_edition(FakeCore)
        result = classify(
            {"tags": ["jp", "アートワーク"], "attributions": []},
            {"translator": "Referenced translator", "original_title": "Referenced work"},
            None,
        )
        self.assertEqual(result["edition"], "jp_original")
        self.assertEqual(result["confidence"], "confirmed")

    def test_branch_tag_is_confirmed_translation(self):
        classify = MODULE.make_classify_edition(FakeCore)
        result = classify(
            {"tags": ["en", "tale"], "attributions": []},
            {},
            None,
        )
        self.assertEqual(result["edition"], "translation")
        self.assertEqual(result["confidence"], "confirmed")
        self.assertEqual(result["source_branch"], "EN")

    def test_explicit_translation_responsible_conflicts_with_jp_tag(self):
        classify = MODULE.make_classify_edition(FakeCore)
        result = classify(
            {"tags": ["jp", "scp"], "attributions": []},
            {"translation_responsible": "Translator"},
            None,
        )
        self.assertEqual(result["edition"], "conflict")

    def test_unchanged_confirmed_snapshot_is_reused(self):
        metadata = {
            "wikidotId": "123",
            "url": "http://scp-jp.wikidot.com/example",
            "createdAt": "2026-08-01T00:00:00Z",
            "revisionCount": 4,
            "rating": 10,
            "voteCount": 12,
            "commentCount": 2,
            "tags": ["jp", "scp"],
            "createdBy": {"displayName": "Author"},
        }
        state = {
            "seen": {
                "123": {
                    "article": {
                        "wikidot_id": "123",
                        "edition": "jp_original",
                        "classification_confidence": "confirmed",
                        "has_text_content": True,
                        "summary_basis": "説明: テスト",
                        "revision_count": 4,
                        "tags": ["scp", "jp"],
                        "rating": 1,
                    }
                }
            }
        }
        reused = MODULE.reusable_cached_article(FakeCore, state, metadata)
        self.assertIsNotNone(reused)
        assert reused is not None
        self.assertEqual(reused["rating"], 10)
        self.assertEqual(reused["url"], "https://scp-jp.wikidot.com/example")

    def test_changed_revision_is_not_reused(self):
        metadata = {"wikidotId": "123", "revisionCount": 5, "tags": ["jp", "scp"]}
        state = {
            "seen": {
                "123": {
                    "article": {
                        "edition": "jp_original",
                        "classification_confidence": "confirmed",
                        "has_text_content": True,
                        "summary_basis": "説明",
                        "revision_count": 4,
                        "tags": ["jp", "scp"],
                    }
                }
            }
        }
        self.assertIsNone(MODULE.reusable_cached_article(FakeCore, state, metadata))

    def test_http_429_with_complete_data_is_accepted(self):
        payload = json.dumps({"data": {"wikidotPage": {"wikidotId": "1"}}}).encode()
        error = HTTPError(
            "https://example.invalid/graphql",
            429,
            "Too Many Requests",
            {"Retry-After": "30", "X-RateLimit-Remaining": "10"},
            io.BytesIO(payload),
        )
        with patch.object(MODULE, "urlopen", side_effect=error):
            data, headers = MODULE.graphql_request(
                "https://example.invalid/graphql",
                "query { x }",
                {},
                timeout=1,
                attempts=1,
            )
        self.assertEqual(data["wikidotPage"]["wikidotId"], "1")
        self.assertEqual(headers["retry-after"], "30")

    def test_http_500_with_data_is_still_rejected(self):
        payload = json.dumps({"data": {"wikidotPage": {"wikidotId": "1"}}}).encode()
        error = HTTPError(
            "https://example.invalid/graphql",
            500,
            "Server Error",
            {},
            io.BytesIO(payload),
        )
        with patch.object(MODULE, "urlopen", side_effect=error):
            with self.assertRaises(MODULE.CromRequestError) as raised:
                MODULE.graphql_request(
                    "https://example.invalid/graphql",
                    "query { x }",
                    {},
                    timeout=1,
                    attempts=1,
                )
        self.assertEqual(raised.exception.http_status, 500)

    def test_http_error_message_does_not_include_response_body(self):
        payload = b'{"secret":"do not expose"}'
        error = HTTPError(
            "https://example.invalid/graphql",
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            io.BytesIO(payload),
        )
        with patch.object(MODULE, "urlopen", side_effect=error):
            with self.assertRaises(MODULE.CromRequestError) as raised:
                MODULE.graphql_request(
                    "https://example.invalid/graphql",
                    "query { x }",
                    {},
                    timeout=1,
                    attempts=1,
                )
        self.assertNotIn("do not expose", str(raised.exception))
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.retry_after_seconds, 0.0)

    def test_retry_after_http_date(self):
        now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
        value = MODULE._retry_after_seconds(
            {"retry-after": "Mon, 17 Aug 2026 00:00:30 GMT"}, now=now
        )
        self.assertEqual(value, 30.0)


if __name__ == "__main__":
    unittest.main()
