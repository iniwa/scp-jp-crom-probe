#!/usr/bin/env python3
"""Build the daily SCP-JP monitor feed from Crom.

The monitor discovers recently created SCP-JP pages, keeps only the agreed
work/hub/essay/news categories, classifies JP originals and translations, and
writes a small static feed for a ChatGPT monitoring task.

State is external to the public site. The workflow loads ``state.json`` from a
separate Git branch, passes it to this program, deploys the public feed, and only
then persists the candidate state. This ordering prefers a possible duplicate
notification over silently losing an article.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://apiv2.crom.avn.sh/graphql"
SITE_PREFIX = "http://scp-jp.wikidot.com"
JST = timezone(timedelta(hours=9))

CONTENT_TAGS = frozenset(
    {
        "scp",
        "tale",
        "goi-format",
        "アートワーク",
        "サイト",
        "合作",
        "設定集",
        "ハブ",
        "エッセイ",
        "ニュース",
        "news",
        "外部ウィキアーカイブ",
    }
)
EXCLUDED_TAGS = frozenset(
    {
        "著者ページ",
        "作者ページ",
        "訳者ページ",
        "コンポーネント",
        "テーマ",
    }
)
EXCLUDED_CATEGORIES = frozenset({"fragment", "deleted"})
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

BRANCH_BY_HOST = {
    "scp-wiki.wikidot.com": "EN",
    "scp-cn.wikidot.com": "CN",
    "scp-wiki-cn.wikidot.com": "CN",
    "scpko.wikidot.com": "KO",
    "fondationscp.wikidot.com": "FR",
    "scp-wiki-de.wikidot.com": "DE",
    "scp-ru.wikidot.com": "RU",
    "scp-pl.wikidot.com": "PL",
    "scp-es.wikidot.com": "ES",
    "scp-pt-br.wikidot.com": "PT-BR",
    "scp-cs.wikidot.com": "CS",
    "scp-th.wikidot.com": "TH",
    "scp-vn.wikidot.com": "VN",
    "scp-int.wikidot.com": "INT",
    "scp-zh-tr.wikidot.com": "ZH-TR",
    "scp-idn.wikidot.com": "ID",
    "scp-el.wikidot.com": "EL",
    "scp-tr.wikidot.com": "TR",
    "scp-ukrainian.wikidot.com": "UA",
}
BRANCH_BY_TAG = {
    "en": "EN",
    "cn": "CN",
    "zh": "CN",
    "ko": "KO",
    "fr": "FR",
    "de": "DE",
    "ru": "RU",
    "pl": "PL",
    "es": "ES",
    "pt-br": "PT-BR",
    "pt": "PT-BR",
    "cs": "CS",
    "th": "TH",
    "vn": "VN",
    "int": "INT",
    "zh-tr": "ZH-TR",
    "id": "ID",
    "el": "EL",
    "tr": "TR",
    "ua": "UA",
    "uk": "UA",
}

LIST_QUERY = r"""
query RecentPages(
  $filter: PageQueryFilter
  $sort: PagesSort
  $first: Int
  $after: ID
) {
  pages(filter: $filter, sort: $sort, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        __typename
        ... on WikidotPage {
          url
          wikidotId
          title
          rating
          voteCount
          category
          tags
          createdAt
          revisionCount
          commentCount
          isHidden
          isUserPage
          createdBy { displayName unixName wikidotId }
          alternateTitles { title source }
        }
      }
    }
  }
}
"""

DETAIL_QUERY = r"""
query ArticleDetail($url: URL!) {
  wikidotPage(url: $url) {
    url
    wikidotId
    title
    rating
    voteCount
    category
    tags
    createdAt
    revisionCount
    commentCount
    isHidden
    isUserPage
    thumbnailUrl
    createdBy { displayName unixName wikidotId }
    parent { url }
    source
    textContent
    summary
    attributions {
      type
      date
      order
      user { __typename displayName }
    }
    alternateTitles { title source }
  }
}
"""


@dataclass(frozen=True)
class OutputPaths:
    root: Path
    public: Path
    health: Path
    latest: Path
    delta: Path
    state: Path
    summary: Path
    debug: Path
    index: Path


class MonitorError(RuntimeError):
    """Raised for a global failure that must not publish a new feed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="monitor-output")
    parser.add_argument("--baseline-file", default="config/baseline.json")
    parser.add_argument("--previous-state-file", default="")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--notification-hours", type=int, default=168)
    parser.add_argument("--snapshot-days", type=int, default=14)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-detail-pages", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--now",
        default="",
        help="Optional aware ISO-8601 timestamp for deterministic tests.",
    )
    return parser.parse_args()


def parse_aware_datetime(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an explicit UTC offset")
    return parsed


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def jst_iso(value: datetime) -> str:
    return value.astimezone(JST).isoformat()


def page_name(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def public_page_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "scp-jp.wikidot.com":
        return urlunparse(("https", parsed.netloc, parsed.path, "", "", ""))
    return url


def graphql_request(
    endpoint: str,
    query: str,
    variables: dict[str, Any],
    *,
    timeout: float,
    attempts: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    body = json.dumps(
        {"query": query, "variables": variables}, ensure_ascii=False
    ).encode("utf-8")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "iniwa-scp-jp-monitor/1.0 (GitHub Actions)",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                status = response.status
            if not 200 <= status < 300:
                raise MonitorError(f"Crom returned HTTP {status}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MonitorError("Crom returned a non-JSON response") from exc
            if not isinstance(payload, dict):
                raise MonitorError("Crom returned a non-object JSON response")
            if payload.get("errors"):
                rendered = json.dumps(payload["errors"], ensure_ascii=False)
                raise MonitorError(f"Crom GraphQL error: {rendered[:4000]}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise MonitorError("Crom response did not contain a data object")
            return data, headers
        except HTTPError as exc:
            try:
                response_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                response_body = ""
            last_error = MonitorError(
                f"Crom returned HTTP {exc.code}: {response_body[:1000]}"
            )
            retryable = exc.code in RETRYABLE_HTTP_STATUS
        except (URLError, TimeoutError, OSError) as exc:
            last_error = MonitorError(f"Could not contact Crom: {exc}")
            retryable = True
        except MonitorError as exc:
            last_error = exc
            retryable = False

        if not retryable or attempt >= attempts:
            break
        time.sleep((1, 4, 10)[min(attempt - 1, 2)])

    raise last_error or MonitorError("Crom request failed for an unknown reason")


def recent_filter(since_utc: datetime) -> dict[str, Any]:
    return {
        "_and": [
            {"url": {"startsWith": SITE_PREFIX}},
            {"onWikidotPage": {"createdAt": {"gte": since_utc.isoformat()}}},
            {"onWikidotPage": {"isHidden": {"eq": False}}},
            {"onWikidotPage": {"category": {"neq": "fragment"}}},
            {"onWikidotPage": {"category": {"neq": "deleted"}}},
        ]
    }


def quota_value(headers: dict[str, str]) -> int | None:
    raw = headers.get("x-ratelimit-remaining")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def collect_recent_pages(
    args: argparse.Namespace, since_utc: datetime
) -> tuple[list[dict[str, Any]], bool, list[int | None]]:
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    after: str | None = None
    quota: list[int | None] = []

    while True:
        data, headers = graphql_request(
            args.endpoint,
            LIST_QUERY,
            {
                "filter": recent_filter(since_utc),
                "sort": {"key": "WIKIDOT_CREATED_AT", "order": "DESC"},
                "first": args.page_size,
                "after": after,
            },
            timeout=args.timeout,
            attempts=args.attempts,
        )
        quota.append(quota_value(headers))
        connection = data.get("pages")
        if not isinstance(connection, dict):
            raise MonitorError("data.pages is missing or invalid")
        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise MonitorError("data.pages.edges is missing or invalid")
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict) or node.get("__typename") != "WikidotPage":
                continue
            url = node.get("url")
            if not isinstance(url, str) or url in seen:
                continue
            seen.add(url)
            pages.append(node)
            if len(pages) >= args.max_pages:
                return pages, True, quota
        info = connection.get("pageInfo")
        if not isinstance(info, dict):
            raise MonitorError("data.pages.pageInfo is missing or invalid")
        if not info.get("hasNextPage"):
            return pages, False, quota
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise MonitorError("Crom reported another page without endCursor")
        after = cursor


def fetch_detail(
    args: argparse.Namespace, url: str
) -> tuple[dict[str, Any], int | None]:
    data, headers = graphql_request(
        args.endpoint,
        DETAIL_QUERY,
        {"url": url},
        timeout=args.timeout,
        attempts=args.attempts,
    )
    page = data.get("wikidotPage")
    if not isinstance(page, dict):
        raise MonitorError(f"Crom did not return detail for {url}")
    return page, quota_value(headers)


def candidate_rejection_reason(page: dict[str, Any]) -> str | None:
    tags = {str(tag) for tag in page.get("tags") or []}
    if page.get("isHidden") is not False:
        return "hidden"
    if page.get("category") in EXCLUDED_CATEGORIES:
        return "excluded_category"
    if tags.intersection(EXCLUDED_TAGS):
        return "excluded_tag"
    if not tags.intersection(CONTENT_TAGS):
        return "not_monitored_content"
    return None


def is_content_candidate(page: dict[str, Any]) -> bool:
    return candidate_rejection_reason(page) is None


def classify_genre(page: dict[str, Any]) -> str:
    tags = {str(tag) for tag in page.get("tags") or []}
    name = page_name(str(page.get("url") or "")).lower()
    if "scp" in tags and name.startswith("scp-"):
        return "SCP報告書"
    if "tale" in tags:
        return "Tale"
    if "goi-format" in tags:
        return "GoIフォーマット"
    for tag, label in (
        ("アートワーク", "アートワーク"),
        ("ハブ", "ハブ"),
        ("エッセイ", "エッセイ"),
        ("ニュース", "ニュース"),
        ("news", "ニュース"),
        ("設定集", "設定集"),
        ("合作", "合作"),
        ("外部ウィキアーカイブ", "外部ウィキアーカイブ"),
        ("サイト", "サイト記事"),
    ):
        if tag in tags:
            return label
    return "その他作品"


def strip_wikidot_markup(value: str) -> str:
    cleaned = value.replace("\u00a0", " ")
    cleaned = re.sub(r"\[\[\*?user\s+([^\]]+)\]\]", r"\1", cleaned, flags=re.I)
    cleaned = re.sub(r"\[\*?(https?://[^\s\]]+)\s+([^\]]+)\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"(?:\s*@@\s*)+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def clean_credit_value(value: str) -> str:
    cleaned = strip_wikidot_markup(value)
    cleaned = cleaned.rstrip("; ")
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def normalize_credit_line(raw_line: str) -> str:
    line = raw_line.strip()
    line = re.sub(r"^(?:[>\-*+]\s*)+", "", line)
    return line.replace("**", "").replace("__", "").strip()


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", "", label).casefold()


CREDIT_ALIASES = {
    "タイトル": "title",
    "title": "title",
    "著者": "author",
    "author": "author",
    "作成年": "year",
    "公開年": "year",
    "year": "year",
    "翻訳責任者": "translation_responsible",
    "翻訳者": "translator",
    "訳者": "translator",
    "翻訳年": "translation_year",
    "原題": "original_title",
    "原著者": "copyright_holder",
    "著作権者": "copyright_holder",
    "元記事リンク": "original_url",
    "原記事リンク": "original_url",
    "原文リンク": "original_url",
    "ソース": "source_url",
    "source": "source_url",
    "初訳時参照リビジョン": "reference_revision",
    "初訳時参考リビジョン": "reference_revision",
    "初訳参照版": "reference_revision",
}


def credit_pairs(source: str | None) -> Iterable[tuple[str, str, str]]:
    if not source:
        return []
    pairs: list[tuple[str, str, str]] = []
    for raw_line in source.splitlines():
        line = normalize_credit_line(raw_line)
        match = re.match(r"^([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
        if not match:
            continue
        label = normalize_label(match.group(1))
        value = match.group(2).strip()
        key = CREDIT_ALIASES.get(label)
        if key:
            pairs.append((key, value, raw_line))
    return pairs


def extract_credit_fields(source: str | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value, _raw in credit_pairs(source):
        cleaned = clean_credit_value(value)
        if not cleaned:
            continue
        if key not in fields:
            fields[key] = cleaned
            continue
        # Some highly styled pages contain JavaScript/CSS metadata before the
        # actual credit block. Replace a suspicious first title with the later
        # human-readable credit title instead of leaking code into the feed.
        if key == "title" and suspicious_title(fields[key]) and not suspicious_title(cleaned):
            fields[key] = cleaned
    return fields


def extract_first_url(value: str) -> str | None:
    match = re.search(r"https?://[^\s\]\[<>'\"]+", value)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)）】」』")


def extract_credit_urls(source: str | None) -> dict[str, str]:
    urls: dict[str, str] = {}
    for key, value, raw in credit_pairs(source):
        if key in urls:
            continue
        url = extract_first_url(value) or extract_first_url(raw)
        if url:
            urls[key] = url
    return urls


def first_alternate_title(page: dict[str, Any]) -> str | None:
    base = str(page.get("title") or "").strip().casefold()
    for item in page.get("alternateTitles") or []:
        if not isinstance(item, dict):
            continue
        value = clean_credit_value(str(item.get("title") or ""))
        if value and value.casefold() != base:
            return value
    return None


def suspicious_title(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.casefold()
    return (
        len(value) > 300
        or any(token in value for token in ("{", "}", ";"))
        or lowered.startswith(("var ", "const ", "let ", "function "))
        or "document." in lowered
    )


def split_numbered_title(base_title: str, candidate: str | None) -> str | None:
    if not candidate or suspicious_title(candidate):
        return None
    candidate = candidate.strip()
    if not candidate or candidate.casefold() == base_title.strip().casefold():
        return None
    prefix_pattern = re.compile(
        rf"^\s*{re.escape(base_title.strip())}\s*(?:[-‐‑‒–—―:：|｜]+)\s*(.+?)\s*$",
        flags=re.I,
    )
    match = prefix_pattern.match(candidate)
    if match:
        return match.group(1).strip() or None
    generic = re.match(
        r"^\s*SCP-[A-Z]?\d+(?:-[A-Z0-9]+)*-?JP?\s*(?:[-‐‑‒–—―:：|｜]+)\s*(.+?)\s*$",
        candidate,
        flags=re.I,
    )
    if generic:
        return generic.group(1).strip() or None
    return candidate


def derive_titles(page: dict[str, Any], credit: dict[str, str]) -> tuple[str, str | None]:
    base = clean_credit_value(
        str(page.get("title") or page_name(str(page.get("url") or "")))
    )
    if classify_genre(page) != "SCP報告書":
        # Non-numbered SCP-JP works generally use the complete page title. Treating
        # arbitrary credit strings as subtitles caused code fragments and duplicate
        # hub titles to leak into notifications.
        return base, None
    subtitle = split_numbered_title(base, first_alternate_title(page))
    if subtitle is None:
        subtitle = split_numbered_title(base, credit.get("title"))
    return base, subtitle


def infer_source_branch(
    original_url: str | None, tags: Iterable[str]
) -> tuple[str | None, str | None]:
    if original_url:
        host = (urlparse(original_url).hostname or "").casefold()
        if host in BRANCH_BY_HOST:
            return BRANCH_BY_HOST[host], "original_url"
        for known_host, branch in BRANCH_BY_HOST.items():
            if host.endswith("." + known_host):
                return branch, "original_url"
    lowered = {str(tag).casefold() for tag in tags}
    for tag, branch in BRANCH_BY_TAG.items():
        if tag in lowered:
            return branch, "tag"
    return None, None


def split_people(value: str | None) -> list[str]:
    if not value:
        return []
    cleaned = clean_credit_value(value)
    parts = re.split(r"\s*(?:,|，|、|;|；|\s+/\s+|\s*および\s*|\s+and\s+)\s*", cleaned, flags=re.I)
    return dedupe_strings(part for part in parts if part.strip())


def dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = clean_credit_value(str(value))
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def effective_original_url(
    credit: dict[str, str], credit_urls: dict[str, str]
) -> str | None:
    direct = credit_urls.get("original_url")
    if direct:
        return direct
    has_translation_marker = any(
        credit.get(key)
        for key in (
            "translation_responsible",
            "translator",
            "translation_year",
            "original_title",
        )
    )
    candidate = credit_urls.get("source_url") if has_translation_marker else None
    if not candidate:
        return None
    host = (urlparse(candidate).hostname or "").casefold()
    if host in BRANCH_BY_HOST or any(host.endswith("." + known) for known in BRANCH_BY_HOST):
        return candidate
    return None


def classify_edition(
    page: dict[str, Any], credit: dict[str, str], original_url: str | None
) -> dict[str, Any]:
    tags = {str(tag) for tag in page.get("tags") or []}
    attribution_types = {
        str(item.get("type") or "").upper()
        for item in page.get("attributions") or []
        if isinstance(item, dict)
    }
    source_branch, branch_basis = infer_source_branch(original_url, tags)

    strong_reasons: list[str] = []
    if "TRANSLATOR" in attribution_types:
        strong_reasons.append("crom_translator_attribution")
    if credit.get("translation_responsible"):
        strong_reasons.append("credit_translation_responsible")
    if credit.get("translator"):
        strong_reasons.append("credit_translator")
    if credit.get("translation_year"):
        strong_reasons.append("credit_translation_year")
    if credit.get("original_title"):
        strong_reasons.append("credit_original_title")
    if original_url:
        strong_reasons.append("credit_original_url")

    has_jp_tag = "jp" in tags
    weak_reason = "source_branch_tag" if source_branch and branch_basis == "tag" else None

    if has_jp_tag and strong_reasons:
        return {
            "edition": "conflict",
            "confidence": "conflict",
            "reasons": ["jp_tag", *strong_reasons],
            "source_branch": source_branch,
            "source_branch_basis": branch_basis,
        }
    if has_jp_tag:
        return {
            "edition": "jp_original",
            "confidence": "confirmed",
            "reasons": ["jp_tag"],
            "source_branch": "JP",
            "source_branch_basis": "jp_tag",
        }
    if strong_reasons:
        return {
            "edition": "translation",
            "confidence": "confirmed",
            "reasons": strong_reasons,
            "source_branch": source_branch,
            "source_branch_basis": branch_basis,
        }
    if weak_reason:
        return {
            "edition": "translation",
            "confidence": "probable",
            "reasons": [weak_reason],
            "source_branch": source_branch,
            "source_branch_basis": branch_basis,
        }
    return {
        "edition": "unknown",
        "confidence": "unknown",
        "reasons": ["no_jp_or_translation_evidence"],
        "source_branch": None,
        "source_branch_basis": None,
    }


def normalize_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        lines.append(line)
    return lines


_METADATA_LABELS = frozenset(
    {
        *CREDIT_ALIASES.keys(),
        "ライセンス",
        "画像出展",
        "画像出典",
        "文字数",
        "最新参照版",
        "重訳元題",
        "重訳元翻訳者",
        "重訳元翻訳年",
        "重訳元参照リビジョン",
        "重訳元リンク",
    }
)


def is_metadata_or_ui_line(line: str) -> bool:
    if not line:
        return False
    stripped = line.strip()
    lowered = stripped.casefold()
    if stripped in {"Info", "クレジット", "詳細情報"}:
        return True
    if re.match(r"^[+\-]\s*(?:コンポーネント|詳細情報|開く|閉じる|コード)", stripped):
        return True
    if lowered.startswith(
        (
            "this bit down here controls",
            "//<![cdata[",
            "ozone.dom.",
            "var ",
            "const ",
            "let ",
        )
    ):
        return True
    if stripped.startswith("本記事は、CC BY-SA") or stripped.startswith("「コンテンツ」とは"):
        return True
    match = re.match(r"^([^:：]{1,40})\s*[:：]", stripped)
    if match and normalize_label(match.group(1)) in {
        normalize_label(label) for label in _METADATA_LABELS
    }:
        return True
    return False


def compact_text(lines: Iterable[str]) -> str:
    output: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if output and not previous_blank:
                output.append("")
            previous_blank = True
            continue
        previous_blank = False
        output.append(line)
    while output and not output[-1]:
        output.pop()
    return "\n".join(output).strip()


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def extract_content_warnings(text: str, source: str) -> list[str]:
    warnings: list[str] = []
    for line in [*normalize_lines(text), *normalize_lines(source)]:
        if re.search(r"コンテンツ\s*警告|content\s*warning", line, flags=re.I):
            cleaned = clean_credit_value(re.sub(r"^[⚠️!！\s]+", "", line))
            if cleaned:
                warnings.append(cleaned)
    return dedupe_strings(warnings)


def strip_leading_metadata(lines: list[str]) -> list[str]:
    result: list[str] = []
    skipping = True
    for line in lines:
        if skipping:
            if not line or is_metadata_or_ui_line(line):
                continue
            if re.match(r"^[<>]$|^\d+$", line):
                continue
            skipping = False
        if not is_metadata_or_ui_line(line):
            result.append(line)
    return result


def stop_at_heading(lines: list[str], patterns: tuple[str, ...], minimum_chars: int) -> list[str]:
    output: list[str] = []
    length = 0
    for line in lines:
        if length >= minimum_chars and any(re.match(pattern, line, flags=re.I) for pattern in patterns):
            break
        output.append(line)
        length += len(line) + 1
    return output


def summary_basis(text: str, genre: str) -> tuple[str, str]:
    lines = normalize_lines(text)
    if genre == "SCP報告書":
        start = next(
            (index for index, line in enumerate(lines) if re.match(r"^説明\s*[:：]", line)),
            None,
        )
        if start is not None:
            selected = lines[start:]
            selected = stop_at_heading(
                selected,
                (
                    r"^(?:補遺|追記|付記|実験記録|インタビュー|Footnotes|脚注)(?:\s|[:：0-9])",
                    r"^[A-Za-z0-9_.-]+\.pdf$",
                ),
                0,
            )
            return trim_text(compact_text(selected), 1800), "description"
        selected = strip_leading_metadata(lines)
        return trim_text(compact_text(selected), 2200), "opening"

    selected = strip_leading_metadata(lines)
    if genre in {"ハブ", "設定集", "合作", "サイト記事", "外部ウィキアーカイブ"}:
        overview = next(
            (
                index
                for index, line in enumerate(selected)
                if re.match(r"^(?:概要|紹介)\s*$", line)
            ),
            None,
        )
        if overview is not None:
            selected = selected[overview + 1 :]
        selected = stop_at_heading(selected, (r"^関連", r"^エントリー", r"^目次"), 500)
        return trim_text(compact_text(selected), 2800), "overview"
    if genre == "Tale":
        return trim_text(compact_text(selected), 1800), "opening"
    if genre == "アートワーク":
        filtered = [line for line in selected if not re.match(r"^[<>]$|^\d+$", line)]
        return trim_text(compact_text(filtered), 1600), "description"
    if genre in {"エッセイ", "ニュース"}:
        return trim_text(compact_text(selected), 2400), "opening"
    return trim_text(compact_text(selected), 2200), "opening"


def to_jst_string(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return jst_iso(parse_aware_datetime(raw))
    except ValueError:
        return None


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def serialize_article(page: dict[str, Any]) -> dict[str, Any]:
    source = page.get("source") if isinstance(page.get("source"), str) else ""
    text_content = page.get("textContent") if isinstance(page.get("textContent"), str) else ""
    credit = extract_credit_fields(source)
    credit_urls = extract_credit_urls(source)
    original_url = effective_original_url(credit, credit_urls)
    edition = classify_edition(page, credit, original_url)
    article_title, subtitle = derive_titles(page, credit)
    genre = classify_genre(page)
    basis, basis_type = summary_basis(text_content, genre)

    created_by = page.get("createdBy") if isinstance(page.get("createdBy"), dict) else {}
    attributions = [
        {
            "type": item.get("type"),
            "date": item.get("date"),
            "order": item.get("order"),
            "display_name": (
                item.get("user", {}).get("displayName")
                if isinstance(item.get("user"), dict)
                else None
            ),
        }
        for item in page.get("attributions") or []
        if isinstance(item, dict)
    ]
    attribution_translators = [
        str(item.get("display_name") or "")
        for item in attributions
        if str(item.get("type") or "").upper() == "TRANSLATOR"
    ]
    translation_responsible = split_people(credit.get("translation_responsible"))
    translators = dedupe_strings(
        [
            *translation_responsible,
            *split_people(credit.get("translator")),
            *attribution_translators,
        ]
    )
    jp_authors = split_people(credit.get("author"))
    if not jp_authors and created_by:
        jp_authors = dedupe_strings([str(created_by.get("displayName") or "")])
    original_authors = split_people(credit.get("author") or credit.get("copyright_holder"))

    crom_url = str(page.get("url") or "")
    article = {
        "wikidot_id": str(page.get("wikidotId") or ""),
        "page_name": page_name(crom_url),
        "url": public_page_url(crom_url),
        "edition": edition["edition"],
        "classification_confidence": edition["confidence"],
        "classification_reasons": edition["reasons"],
        "source_branch": edition["source_branch"],
        "source_branch_basis": edition["source_branch_basis"],
        "genre": genre,
        "article_title": article_title,
        "subtitle": subtitle,
        "authors": jp_authors if edition["edition"] == "jp_original" else [],
        "original_title": credit.get("original_title"),
        "original_url": original_url,
        "original_authors": original_authors if edition["edition"] == "translation" else [],
        "original_year": credit.get("year") if edition["edition"] == "translation" else None,
        "translation_year": credit.get("translation_year"),
        "translation_responsible": translation_responsible,
        "translators": translators,
        "created_at_utc": page.get("createdAt"),
        "created_at_jst": to_jst_string(page.get("createdAt")),
        "created_by": clean_credit_value(str(created_by.get("displayName") or "")) or None,
        "content_warnings": extract_content_warnings(text_content, source),
        "summary_basis": basis,
        "summary_basis_type": basis_type,
        "tags": list(page.get("tags") or []),
        "rating": page.get("rating"),
        "vote_count": page.get("voteCount"),
        "revision_count": page.get("revisionCount"),
        "comment_count": page.get("commentCount"),
        "content_hash": text_hash(text_content),
        "has_text_content": bool(text_content),
        "text_content_length": len(text_content),
    }
    return article


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MonitorError(f"JSON root must be an object: {path}")
    return data


def load_baseline(path: Path) -> dict[str, Any]:
    baseline = read_json(path)
    if baseline.get("schema_version") != 1:
        raise MonitorError("Unsupported baseline schema_version")
    bootstrap_raw = baseline.get("bootstrap_since_jst")
    if not isinstance(bootstrap_raw, str):
        raise MonitorError("baseline.bootstrap_since_jst is required")
    parse_aware_datetime(bootstrap_raw)
    reported = baseline.get("reported_articles")
    if not isinstance(reported, list):
        raise MonitorError("baseline.reported_articles must be a list")
    for item in reported:
        if not isinstance(item, dict) or not str(item.get("wikidot_id") or ""):
            raise MonitorError("Each baseline article needs wikidot_id")
    return baseline


def baseline_seen(baseline: dict[str, Any]) -> dict[str, dict[str, Any]]:
    recorded = str(baseline.get("recorded_at_utc") or "2026-07-30T00:00:00+00:00")
    result: dict[str, dict[str, Any]] = {}
    for item in baseline.get("reported_articles") or []:
        article_id = str(item["wikidot_id"])
        result[article_id] = {
            "wikidot_id": article_id,
            "page_name": item.get("page_name"),
            "first_seen_at_utc": recorded,
            "last_seen_at_utc": recorded,
            "baseline": True,
            "article": None,
        }
    return result


def load_state(path: Path | None, baseline: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if path is None or not path.exists():
        return {
            "schema_version": 1,
            "generated_at_utc": None,
            "seen": baseline_seen(baseline),
        }, "baseline"
    state = read_json(path)
    if state.get("schema_version") != 1 or not isinstance(state.get("seen"), dict):
        raise MonitorError("Unsupported or invalid previous state")
    seen = state["seen"]
    for article_id, item in baseline_seen(baseline).items():
        seen.setdefault(article_id, item)
        if isinstance(seen.get(article_id), dict):
            seen[article_id]["baseline"] = True
    return state, "previous_state"


def parse_state_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return parse_aware_datetime(raw).astimezone(timezone.utc)
    except ValueError:
        return None


def update_state(
    state: dict[str, Any],
    articles: list[dict[str, Any]],
    *,
    now_utc: datetime,
    snapshot_days: int,
) -> tuple[dict[str, Any], set[str]]:
    seen = state.setdefault("seen", {})
    if not isinstance(seen, dict):
        raise MonitorError("state.seen must be an object")
    new_ids: set[str] = set()
    now_raw = utc_iso(now_utc)
    snapshot_cutoff = now_utc - timedelta(days=snapshot_days)

    for article in articles:
        article_id = str(article.get("wikidot_id") or "")
        if not article_id:
            continue
        existing = seen.get(article_id)
        if not isinstance(existing, dict):
            existing = None
        baseline = bool(existing and existing.get("baseline"))
        first_seen = (
            str(existing.get("first_seen_at_utc"))
            if existing and existing.get("first_seen_at_utc")
            else now_raw
        )
        if existing is None and not baseline:
            new_ids.add(article_id)
        snapshot = dict(article)
        snapshot.pop("is_new_this_run", None)
        snapshot.pop("first_seen_at_jst", None)
        snapshot.pop("baseline", None)
        seen[article_id] = {
            "wikidot_id": article_id,
            "page_name": article.get("page_name"),
            "first_seen_at_utc": first_seen,
            "last_seen_at_utc": now_raw,
            "baseline": baseline,
            "article": snapshot,
        }

    for item in seen.values():
        if not isinstance(item, dict) or item.get("baseline"):
            continue
        first_seen = parse_state_time(item.get("first_seen_at_utc"))
        if first_seen and first_seen < snapshot_cutoff:
            item["article"] = None

    state["schema_version"] = 1
    state["generated_at_utc"] = now_raw
    return state, new_ids


def decorate_article(
    article: dict[str, Any], state_entry: dict[str, Any], new_ids: set[str]
) -> dict[str, Any]:
    result = dict(article)
    article_id = str(article.get("wikidot_id") or "")
    first_seen = parse_state_time(state_entry.get("first_seen_at_utc"))
    result["first_seen_at_jst"] = jst_iso(first_seen) if first_seen else None
    result["baseline"] = bool(state_entry.get("baseline"))
    result["is_new_this_run"] = article_id in new_ids
    result["notification_id"] = article_id
    return result


def notification_articles(
    state: dict[str, Any], *, now_utc: datetime, retention_hours: int, new_ids: set[str]
) -> list[dict[str, Any]]:
    cutoff = now_utc - timedelta(hours=retention_hours)
    candidates: list[dict[str, Any]] = []
    seen = state.get("seen") or {}
    for article_id, entry in seen.items():
        if not isinstance(entry, dict) or entry.get("baseline"):
            continue
        first_seen = parse_state_time(entry.get("first_seen_at_utc"))
        article = entry.get("article")
        if first_seen is None or first_seen < cutoff or not isinstance(article, dict):
            continue
        candidates.append(decorate_article(article, entry, new_ids))
    candidates.sort(key=lambda item: str(item.get("created_at_utc") or ""), reverse=True)
    return candidates


def rate_limit_summary(samples: list[int | None]) -> dict[str, Any]:
    values = [value for value in samples if isinstance(value, int)]
    return {
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
        "minimum": min(values) if values else None,
        "request_count": len(samples),
    }


def build_index(health: dict[str, Any], delta: dict[str, Any]) -> str:
    status = html.escape(str(health.get("status") or "unknown"))
    generated = html.escape(str(health.get("generated_at_jst") or "unknown"))
    retention_hours = html.escape(str(delta.get("retention_hours") or "unknown"))
    count = len(delta.get("articles") or [])
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SCP-JP Daily Monitor</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 3rem auto; padding: 0 1rem; line-height: 1.65; }}
    code {{ background: #f3f3f3; padding: .15rem .35rem; border-radius: .25rem; }}
  </style>
</head>
<body>
  <h1>SCP-JP Daily Monitor</h1>
  <p>Status: <strong>{status}</strong></p>
  <p>Generated: <code>{generated}</code></p>
  <p>Notification candidates retained for {retention_hours} hours: <strong>{count}</strong></p>
  <ul>
    <li><a href="health.json">health.json</a></li>
    <li><a href="delta.json">delta.json</a></li>
    <li><a href="latest.json">latest.json</a></li>
  </ul>
</body>
</html>
"""


def markdown_table_row(values: Iterable[Any]) -> str:
    escaped = [str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ") for value in values]
    return "| " + " | ".join(escaped) + " |"


def build_summary(health: dict[str, Any], delta: dict[str, Any]) -> str:
    counts = health.get("counts") or {}
    lines = [
        "# SCP-JP daily monitor",
        "",
        f"- Status: **{health.get('status', 'unknown')}**",
        f"- Generated (JST): `{health.get('generated_at_jst', 'unknown')}`",
        f"- Mode: `{health.get('mode', 'unknown')}`",
        f"- Query cutoff: `{health.get('query', {}).get('since_jst', 'unknown')}`",
        f"- JP originals in window: **{counts.get('jp_originals', 0)}**",
        f"- Translations in window: **{counts.get('translations', 0)}**",
        f"- New this run: **{counts.get('new_this_run', 0)}**",
        f"- Notification candidates: **{counts.get('notification_candidates', 0)}**",
        f"- Pending: **{counts.get('pending', 0)}**",
        "",
        "## Notification candidates",
        "",
        "| Created at (JST) | Edition | Genre | Title | Subtitle | ID |",
        "|---|---|---|---|---|---|",
    ]
    for item in delta.get("articles") or []:
        lines.append(
            markdown_table_row(
                (
                    item.get("created_at_jst"),
                    item.get("edition"),
                    item.get("genre"),
                    item.get("article_title"),
                    item.get("subtitle"),
                    item.get("wikidot_id"),
                )
            )
        )
    pending = health.get("pending_pages") or []
    if pending:
        lines.extend(["", "## Pending pages", ""])
        for item in pending:
            lines.append(f"- `{item.get('page_name') or item.get('url')}`: {item.get('reason')}")
    warnings = health.get("warnings") or []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.page_size <= 100:
        raise ValueError("--page-size must be between 1 and 100")
    for name in ("window_days", "notification_hours", "snapshot_days", "max_pages", "max_detail_pages", "attempts"):
        if int(getattr(args, name)) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")


def query_since_utc(
    *,
    mode: str,
    now_utc: datetime,
    window_days: int,
    bootstrap_since: datetime,
) -> datetime:
    """Return the oldest creation time that this run may query.

    Incremental runs use an overlapping lookback window so delayed Crom indexing
    can still be recovered. The window must never extend earlier than the
    monitor's bootstrap boundary, otherwise pre-monitor history that is absent
    from state is incorrectly marked as newly discovered.
    """
    bootstrap_utc = bootstrap_since.astimezone(timezone.utc)
    if mode == "bootstrap":
        return bootstrap_utc
    if mode != "incremental":
        raise ValueError(f"unsupported monitor mode: {mode}")
    return max(now_utc - timedelta(days=window_days), bootstrap_utc)


def execute(args: argparse.Namespace, paths: OutputPaths) -> tuple[dict[str, Any], int]:
    now_utc = (
        parse_aware_datetime(args.now).astimezone(timezone.utc)
        if args.now
        else datetime.now(timezone.utc)
    )
    health: dict[str, Any] = {
        "schema_version": 1,
        "status": "starting",
        "generated_at_utc": utc_iso(now_utc),
        "generated_at_jst": jst_iso(now_utc),
        "generated_date_jst": now_utc.astimezone(JST).date().isoformat(),
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "repository": os.getenv("GITHUB_REPOSITORY"),
    }
    try:
        validate_args(args)
        baseline = load_baseline(Path(args.baseline_file))
        previous_path = Path(args.previous_state_file) if args.previous_state_file else None
        state, state_source = load_state(previous_path, baseline)
        mode = "incremental" if state_source == "previous_state" else "bootstrap"
        bootstrap_since = parse_aware_datetime(str(baseline["bootstrap_since_jst"]))
        since_utc = query_since_utc(
            mode=mode,
            now_utc=now_utc,
            window_days=args.window_days,
            bootstrap_since=bootstrap_since,
        )

        raw_pages, truncated, quota_samples = collect_recent_pages(args, since_utc)
        if truncated:
            raise MonitorError(
                f"raw page count reached --max-pages {args.max_pages}; result is incomplete"
            )

        rejection_counts: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        for page in raw_pages:
            reason = candidate_rejection_reason(page)
            if reason is None:
                candidates.append(page)
            else:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        if len(candidates) > args.max_detail_pages:
            raise MonitorError(
                f"candidate count {len(candidates)} exceeds --max-detail-pages {args.max_detail_pages}"
            )

        confirmed: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        classification_issues: list[dict[str, Any]] = []
        for metadata in candidates:
            url = metadata.get("url")
            if not isinstance(url, str) or not url:
                pending.append({"page_name": None, "url": None, "reason": "missing_url"})
                continue
            try:
                detail, remaining = fetch_detail(args, url)
                quota_samples.append(remaining)
                article = serialize_article(detail)
            except Exception as exc:
                pending.append(
                    {
                        "page_name": page_name(url),
                        "url": public_page_url(url),
                        "reason": f"detail_fetch_failed: {type(exc).__name__}: {exc}",
                    }
                )
                continue
            if article["edition"] in {"unknown", "conflict"} or article["classification_confidence"] != "confirmed":
                classification_issues.append(article)
                pending.append(
                    {
                        "page_name": article["page_name"],
                        "url": article["url"],
                        "reason": "classification_not_confirmed",
                        "classification": article["edition"],
                        "confidence": article["classification_confidence"],
                    }
                )
                continue
            if not article["has_text_content"] or not article["summary_basis"]:
                pending.append(
                    {
                        "page_name": article["page_name"],
                        "url": article["url"],
                        "reason": "text_content_not_ready",
                    }
                )
                continue
            confirmed.append(article)

        state, new_ids = update_state(
            state,
            confirmed,
            now_utc=now_utc,
            snapshot_days=args.snapshot_days,
        )
        state_seen = state.get("seen") or {}
        latest_articles = [
            decorate_article(article, state_seen[str(article["wikidot_id"])], new_ids)
            for article in confirmed
            if str(article.get("wikidot_id") or "") in state_seen
        ]
        latest_articles.sort(key=lambda item: str(item.get("created_at_utc") or ""), reverse=True)
        delta_articles = notification_articles(
            state,
            now_utc=now_utc,
            retention_hours=args.notification_hours,
            new_ids=new_ids,
        )

        status = "degraded" if pending else "ok"
        warnings: list[str] = []
        if pending:
            warnings.append(
                f"{len(pending)} page(s) are pending and were not marked as seen; they will be retried."
            )
        jp_count = sum(item["edition"] == "jp_original" for item in latest_articles)
        translation_count = sum(item["edition"] == "translation" for item in latest_articles)
        new_jp = sum(
            item["edition"] == "jp_original" and item["is_new_this_run"]
            for item in latest_articles
        )
        new_translation = sum(
            item["edition"] == "translation" and item["is_new_this_run"]
            for item in latest_articles
        )

        health.update(
            {
                "status": status,
                "mode": mode,
                "state_source": state_source,
                "query": {
                    "endpoint": args.endpoint,
                    "since_utc": utc_iso(since_utc),
                    "since_jst": jst_iso(since_utc),
                    "window_days": args.window_days,
                    "notification_retention_hours": args.notification_hours,
                    "truncated": False,
                },
                "counts": {
                    "raw_recent_pages": len(raw_pages),
                    "content_candidates": len(candidates),
                    "details_confirmed": len(confirmed),
                    "jp_originals": jp_count,
                    "translations": translation_count,
                    "new_this_run": len(new_ids),
                    "new_jp_originals": new_jp,
                    "new_translations": new_translation,
                    "notification_candidates": len(delta_articles),
                    "pending": len(pending),
                    "rejected": rejection_counts,
                },
                "rate_limit": rate_limit_summary(quota_samples),
                "pending_pages": pending,
                "warnings": warnings,
            }
        )
        latest = {
            "schema_version": 1,
            "generated_at_utc": utc_iso(now_utc),
            "generated_at_jst": jst_iso(now_utc),
            "window_days": args.window_days,
            "articles": latest_articles,
        }
        delta = {
            "schema_version": 1,
            "generated_at_utc": utc_iso(now_utc),
            "generated_at_jst": jst_iso(now_utc),
            "retention_hours": args.notification_hours,
            "new_this_run_ids": sorted(new_ids),
            "articles": delta_articles,
        }
        debug = {
            "schema_version": 1,
            "health": health,
            "classification_issues": classification_issues,
            "latest_article_ids": [item["wikidot_id"] for item in latest_articles],
            "delta_article_ids": [item["wikidot_id"] for item in delta_articles],
        }
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.public.mkdir(parents=True, exist_ok=True)
        write_json(paths.health, health)
        write_json(paths.latest, latest)
        write_json(paths.delta, delta)
        write_json(paths.state, state)
        write_json(paths.debug, debug)
        paths.summary.write_text(build_summary(health, delta), encoding="utf-8")
        paths.index.write_text(build_index(health, delta), encoding="utf-8")
        return health, 0
    except Exception as exc:
        health.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        paths.root.mkdir(parents=True, exist_ok=True)
        write_json(paths.debug, {"schema_version": 1, "health": health})
        paths.summary.write_text(build_summary(health, {"articles": []}), encoding="utf-8")
        return health, 1


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.output_dir)
    public = root / "public"
    paths = OutputPaths(
        root=root,
        public=public,
        health=public / "health.json",
        latest=public / "latest.json",
        delta=public / "delta.json",
        state=root / "state.json",
        summary=root / "monitor-summary.md",
        debug=root / "monitor-debug.json",
        index=public / "index.html",
    )
    health, code = execute(args, paths)
    print(
        json.dumps(
            {
                "status": health.get("status"),
                "generated_at_jst": health.get("generated_at_jst"),
                "counts": health.get("counts", {}),
                "output_dir": str(root),
            },
            ensure_ascii=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
