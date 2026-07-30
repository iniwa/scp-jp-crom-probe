#!/usr/bin/env python3
"""Probe recent SCP-JP originals and translations through Crom GraphQL.

The probe deliberately separates discovery from classification:

1. Fetch every visible SCP-JP page created after the cutoff.
2. Keep only work/hub/essay/news candidates and reject author pages,
   components, themes, fragments, and deleted pages.
3. Fetch costly detail fields only for those candidates.
4. Classify each page as a JP original or translation using tags,
   Crom attributions, and credit-block evidence.

Unknown or contradictory classification is never reported as "no new article".
The workflow writes diagnostics and exits non-zero with status ``degraded``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://apiv2.crom.avn.sh/graphql"
SITE_PREFIX = "http://scp-jp.wikidot.com"
JST = timezone(timedelta(hours=9))

DEFAULT_TARGETS = (
    "SCP-4037-JP",
    "SCP-4543-JP",
    "SCP-4733-JP",
    "SCP-4119-JP",
)

# "作品・ハブ・エッセイ・ニュース". These tags cover the work types used by
# SCP-JP's original and translated new-page listings.
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

# Explicitly excluded by the agreed monitoring policy.
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
    pageInfo {
      hasNextPage
      endCursor
    }
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
          createdBy {
            displayName
            unixName
            wikidotId
          }
          alternateTitles {
            title
            source
          }
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
    createdBy {
      displayName
      unixName
      wikidotId
    }
    parent {
      url
    }
    source
    textContent
    summary
    attributions {
      type
      date
      order
      user {
        __typename
        displayName
      }
    }
    alternateTitles {
      title
      source
    }
  }
}
"""


@dataclass(frozen=True)
class OutputPaths:
    root: Path
    json: Path
    markdown: Path
    articles: Path


class ProbeError(RuntimeError):
    """Raised for network, HTTP, GraphQL, or response-shape failures."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since-jst",
        default="2026-07-26T00:00:00+09:00",
        help="Inclusive ISO-8601 cutoff with an explicit UTC offset.",
    )
    parser.add_argument("--output-dir", default="unified-output")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-detail-pages", type=int, default=150)
    parser.add_argument("--excerpt-chars", type=int, default=2400)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="Expected JP-original page names used as regression checks.",
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


def page_name(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


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
                "User-Agent": "iniwa-scp-jp-unified-probe/0.4 (GitHub Actions)",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                status = response.status
            if not 200 <= status < 300:
                raise ProbeError(f"Crom returned HTTP {status}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProbeError("Crom returned a non-JSON response") from exc
            if not isinstance(payload, dict):
                raise ProbeError("Crom returned a non-object JSON response")
            if payload.get("errors"):
                rendered = json.dumps(payload["errors"], ensure_ascii=False)
                raise ProbeError(f"Crom GraphQL error: {rendered[:4000]}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ProbeError("Crom response did not contain a data object")
            return data, headers
        except HTTPError as exc:
            try:
                response_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                response_body = ""
            last_error = ProbeError(
                f"Crom returned HTTP {exc.code}: {response_body[:1000]}"
            )
            retryable = exc.code in RETRYABLE_HTTP_STATUS
        except (URLError, TimeoutError, OSError) as exc:
            last_error = ProbeError(f"Could not contact Crom: {exc}")
            retryable = True
        except ProbeError as exc:
            last_error = exc
            retryable = False

        if not retryable or attempt >= attempts:
            break
        time.sleep((1, 4, 10)[min(attempt - 1, 2)])

    raise last_error or ProbeError("Crom request failed for an unknown reason")


def recent_filter(since_utc: datetime) -> dict[str, Any]:
    # Do not filter by jp tag here: translations live on SCP-JP but generally lack it.
    return {
        "_and": [
            {"url": {"startsWith": SITE_PREFIX}},
            {"onWikidotPage": {"createdAt": {"gte": since_utc.isoformat()}}},
            {"onWikidotPage": {"isHidden": {"eq": False}}},
            {"onWikidotPage": {"category": {"neq": "fragment"}}},
            {"onWikidotPage": {"category": {"neq": "deleted"}}},
        ]
    }


def candidate_rejection_reason(page: dict[str, Any]) -> str | None:
    tags = set(page.get("tags") or [])
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
    tags = set(page.get("tags") or [])
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
        quota.append(_quota_value(headers))
        connection = data.get("pages")
        if not isinstance(connection, dict):
            raise ProbeError("data.pages is missing or invalid")
        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise ProbeError("data.pages.edges is missing or invalid")
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
            raise ProbeError("data.pages.pageInfo is missing or invalid")
        if not info.get("hasNextPage"):
            return pages, False, quota
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ProbeError("Crom reported another page without endCursor")
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
        raise ProbeError(f"Crom did not return detail for {url}")
    return page, _quota_value(headers)


def _quota_value(headers: dict[str, str]) -> int | None:
    raw = headers.get("x-ratelimit-remaining")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def strip_wikidot_markup(value: str) -> str:
    value = re.sub(r"\[\[\*?user\s+([^\]]+)\]\]", r"\1", value, flags=re.I)
    value = re.sub(r"\[\*?(https?://[^\s\]]+)\s+([^\]]+)\]", r"\2", value)
    value = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = value.replace("**", "").replace("__", "")
    return value.strip()


def _normalize_credit_line(raw_line: str) -> str:
    line = raw_line.strip()
    line = re.sub(r"^(?:[>\-*+]\s*)+", "", line)
    return line.replace("**", "").replace("__", "").strip()


def _normalize_label(label: str) -> str:
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
    "初訳時参照リビジョン": "reference_revision",
    "初訳時参考リビジョン": "reference_revision",
}


def _credit_pairs(source: str | None) -> Iterable[tuple[str, str, str]]:
    if not source:
        return []
    pairs: list[tuple[str, str, str]] = []
    for raw_line in source.splitlines():
        line = _normalize_credit_line(raw_line)
        match = re.match(r"^([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
        if not match:
            continue
        label = _normalize_label(match.group(1))
        value = match.group(2).strip()
        key = CREDIT_ALIASES.get(label)
        if key:
            pairs.append((key, value, raw_line))
    return pairs


def extract_credit_fields(source: str | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value, _raw in _credit_pairs(source):
        if key not in fields:
            fields[key] = strip_wikidot_markup(value)
    return fields


def extract_first_url(value: str) -> str | None:
    match = re.search(r"https?://[^\s\]\[<>'\"]+", value)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)）】」』")


def extract_credit_urls(source: str | None) -> dict[str, str]:
    urls: dict[str, str] = {}
    for key, value, raw in _credit_pairs(source):
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
        value = str(item.get("title") or "").strip()
        if value and value.casefold() != base:
            return value
    return None


def split_numbered_title(base_title: str, candidate: str | None) -> str | None:
    if not candidate:
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
    base = str(page.get("title") or page_name(str(page.get("url") or ""))).strip()
    genre = classify_genre(page)
    credit_title = credit.get("title")
    alternate = first_alternate_title(page)
    if genre == "SCP報告書":
        subtitle = split_numbered_title(base, credit_title)
        if subtitle is None:
            subtitle = split_numbered_title(base, alternate)
        return base, subtitle
    subtitle = None
    if credit_title and credit_title.casefold() != base.casefold():
        subtitle = credit_title
    elif alternate and alternate.casefold() != base.casefold():
        subtitle = alternate
    return base, subtitle


def infer_source_branch(original_url: str | None, tags: Iterable[str]) -> tuple[str | None, str | None]:
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
    cleaned = strip_wikidot_markup(value)
    parts = re.split(r"\s*(?:,|，|、|;|；|\s+/\s+)\s*", cleaned)
    return dedupe_strings(part for part in parts if part.strip())


def dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def classify_edition(
    page: dict[str, Any], credit: dict[str, str], credit_urls: dict[str, str]
) -> dict[str, Any]:
    tags = {str(tag) for tag in page.get("tags") or []}
    attribution_types = {
        str(item.get("type") or "").upper()
        for item in page.get("attributions") or []
        if isinstance(item, dict)
    }
    original_url = credit_urls.get("original_url")
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


def normalize_excerpt(text: str | None, limit: int) -> str:
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip() + "…"


def _to_jst(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(JST).isoformat()
    except ValueError:
        return None


def serialize_detail(page: dict[str, Any], excerpt_chars: int) -> dict[str, Any]:
    source = page.get("source") if isinstance(page.get("source"), str) else ""
    text_content = (
        page.get("textContent") if isinstance(page.get("textContent"), str) else ""
    )
    credit = extract_credit_fields(source)
    credit_urls = extract_credit_urls(source)
    article_title, subtitle = derive_titles(page, credit)
    edition = classify_edition(page, credit, credit_urls)
    created_by = page.get("createdBy")
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
    original_authors = split_people(
        credit.get("copyright_holder") or credit.get("author")
    )
    jp_authors = split_people(credit.get("author"))
    if not jp_authors and isinstance(created_by, dict):
        jp_authors = dedupe_strings([str(created_by.get("displayName") or "")])

    return {
        "page_name": page_name(str(page.get("url") or "")),
        "url": page.get("url"),
        "wikidot_id": page.get("wikidotId"),
        "edition": edition["edition"],
        "classification_confidence": edition["confidence"],
        "classification_reasons": edition["reasons"],
        "source_branch": edition["source_branch"],
        "source_branch_basis": edition["source_branch_basis"],
        "genre": classify_genre(page),
        "page_title": page.get("title") or "",
        "article_title": article_title,
        "subtitle": subtitle,
        "credit": credit,
        "credit_urls": credit_urls,
        "authors": jp_authors if edition["edition"] == "jp_original" else [],
        "translation_responsible": translation_responsible,
        "translators": translators,
        "original_title": credit.get("original_title"),
        "original_url": credit_urls.get("original_url"),
        "original_authors": original_authors,
        "original_year": credit.get("year") if edition["edition"] == "translation" else None,
        "translation_year": credit.get("translation_year"),
        "reference_revision": credit.get("reference_revision"),
        "alternate_titles": [
            {"title": item.get("title") or "", "source": item.get("source")}
            for item in page.get("alternateTitles") or []
            if isinstance(item, dict)
        ],
        "created_at": page.get("createdAt"),
        "created_at_jst": _to_jst(page.get("createdAt")),
        "created_by": (
            {
                "display_name": created_by.get("displayName"),
                "unix_name": created_by.get("unixName"),
                "wikidot_id": created_by.get("wikidotId"),
            }
            if isinstance(created_by, dict)
            else None
        ),
        "attributions": attributions,
        "category": page.get("category"),
        "tags": list(page.get("tags") or []),
        "rating": page.get("rating"),
        "vote_count": page.get("voteCount"),
        "revision_count": page.get("revisionCount"),
        "comment_count": page.get("commentCount"),
        "summary_from_crom": page.get("summary"),
        "summary_basis": normalize_excerpt(text_content, excerpt_chars),
        "has_source": bool(source),
        "has_text_content": bool(text_content),
        "source_length": len(source),
        "text_content_length": len(text_content),
        "_source": source,
        "_text_content": text_content,
    }


def public_detail(detail: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    page = str(detail["page_name"])
    edition = str(detail.get("edition") or "unknown")
    target_dir = article_dir / edition
    target_dir.mkdir(parents=True, exist_ok=True)
    source_path = target_dir / f"{page}.source.txt"
    text_path = target_dir / f"{page}.text.txt"
    source_path.write_text(str(detail.pop("_source", "")), encoding="utf-8")
    text_path.write_text(str(detail.pop("_text_content", "")), encoding="utf-8")
    detail["source_file"] = f"articles/{edition}/{source_path.name}"
    detail["text_file"] = f"articles/{edition}/{text_path.name}"
    return detail


def expected_status(details: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    originals = {
        str(item.get("page_name") or "").casefold()
        for item in details
        if item.get("edition") == "jp_original"
    }
    rows = []
    for target in targets:
        normalized = target.strip().lower().replace("ｰ", "-").replace("‐", "-")
        rows.append({"target": target.strip().upper(), "present": normalized in originals})
    return rows


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _people_text(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "—"
    return ", ".join(md_escape(item) for item in values)


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# SCP-JP Crom unified probe",
        "",
        f"- Status: **{md_escape(result.get('status', 'unknown'))}**",
        f"- Generated: `{md_escape(result.get('generated_at_utc', 'unknown'))}`",
        f"- Cutoff (input): `{md_escape(result.get('query', {}).get('since_input', 'unknown'))}`",
        f"- Cutoff (UTC): `{md_escape(result.get('query', {}).get('since_utc', 'unknown'))}`",
    ]
    counts = result.get("counts") or {}
    if counts:
        lines.extend(
            [
                f"- Raw SCP-JP pages after cutoff: **{counts.get('raw_recent_pages', 0)}**",
                f"- Content candidates: **{counts.get('content_candidates', 0)}**",
                f"- JP originals: **{counts.get('jp_originals', 0)}**",
                f"- Translations: **{counts.get('translations', 0)}**",
                f"- Unknown/conflicting: **{counts.get('classification_issues', 0)}**",
                f"- Details fetched: **{counts.get('details_fetched', 0)}**",
                f"- Truncated by safety cap: **{result.get('query', {}).get('truncated', False)}**",
            ]
        )

    if result.get("status") == "error":
        lines.extend(
            [
                "",
                "## Error",
                "",
                f"- Type: `{md_escape(result.get('error_type', 'unknown'))}`",
                "",
                "```text",
                str(result.get("error", "unknown error")),
                "```",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "",
            "## Expected JP-original pages",
            "",
            "| Target | Present |",
            "|---|---:|",
        ]
    )
    for item in result.get("targets") or []:
        lines.append(
            f"| {md_escape(item.get('target', '—'))} | {'yes' if item.get('present') else 'no'} |"
        )

    originals = result.get("jp_originals") or []
    lines.extend(
        [
            "",
            "## JP originals",
            "",
            "| Created at (JST) | Genre | Page | Article title | Subtitle | Author(s) | Content |",
            "|---|---|---|---|---|---|---:|",
        ]
    )
    for item in originals:
        lines.append(
            "| {created} | {genre} | {page} | {title} | {subtitle} | {authors} | {content} |".format(
                created=md_escape(item.get("created_at_jst") or "—"),
                genre=md_escape(item.get("genre") or "—"),
                page=md_escape(item.get("page_name") or "—"),
                title=md_escape(item.get("article_title") or "—"),
                subtitle=md_escape(item.get("subtitle") or "—"),
                authors=_people_text(item.get("authors")),
                content="yes" if item.get("has_text_content") else "no",
            )
        )

    translations = result.get("translations") or []
    lines.extend(
        [
            "",
            "## Translations",
            "",
            "| JP created at | Genre | Page | Japanese title | Subtitle | Branch | Original title | Translator(s) | Confidence | Content |",
            "|---|---|---|---|---|---|---|---|---|---:|",
        ]
    )
    for item in translations:
        lines.append(
            "| {created} | {genre} | {page} | {title} | {subtitle} | {branch} | {original} | {translators} | {confidence} | {content} |".format(
                created=md_escape(item.get("created_at_jst") or "—"),
                genre=md_escape(item.get("genre") or "—"),
                page=md_escape(item.get("page_name") or "—"),
                title=md_escape(item.get("article_title") or "—"),
                subtitle=md_escape(item.get("subtitle") or "—"),
                branch=md_escape(item.get("source_branch") or "不明"),
                original=md_escape(item.get("original_title") or "—"),
                translators=_people_text(item.get("translators")),
                confidence=md_escape(item.get("classification_confidence") or "—"),
                content="yes" if item.get("has_text_content") else "no",
            )
        )

    issues = result.get("classification_issues") or []
    if issues:
        lines.extend(
            [
                "",
                "## Classification issues",
                "",
                "| Page | Edition | Confidence | Reasons | Tags |",
                "|---|---|---|---|---|",
            ]
        )
        for item in issues:
            lines.append(
                "| {page} | {edition} | {confidence} | {reasons} | {tags} |".format(
                    page=md_escape(item.get("page_name") or "—"),
                    edition=md_escape(item.get("edition") or "—"),
                    confidence=md_escape(item.get("classification_confidence") or "—"),
                    reasons=md_escape(", ".join(item.get("classification_reasons") or [])),
                    tags=md_escape(", ".join(item.get("tags") or [])),
                )
            )

    lines.extend(["", "## Translation previews", ""])
    for item in translations:
        lines.extend(
            [
                f"### {md_escape(item.get('article_title') or item.get('page_name') or 'Article')}",
                "",
                f"- Page: `{md_escape(item.get('page_name') or '—')}`",
                f"- Japanese subtitle: {md_escape(item.get('subtitle') or '—')}",
                f"- Source branch: {md_escape(item.get('source_branch') or '不明')}",
                f"- Original title: {md_escape(item.get('original_title') or '—')}",
                f"- Original URL: {md_escape(item.get('original_url') or '—')}",
                f"- Translator(s): {_people_text(item.get('translators'))}",
                "",
                "```text",
                str(item.get("summary_basis") or "No text content returned."),
                "```",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def execute(args: argparse.Namespace, paths: OutputPaths) -> tuple[dict[str, Any], int]:
    generated = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "schema_version": 4,
        "status": "starting",
        "generated_at_utc": generated,
        "query": {
            "endpoint": args.endpoint,
            "site_prefix": SITE_PREFIX,
            "since_input": args.since_jst,
            "content_tags": sorted(CONTENT_TAGS),
            "excluded_tags": sorted(EXCLUDED_TAGS),
            "excluded_categories": sorted(EXCLUDED_CATEGORIES),
        },
    }
    try:
        if not 1 <= args.page_size <= 100:
            raise ValueError("--page-size must be between 1 and 100")
        if args.max_pages <= 0 or args.max_detail_pages <= 0:
            raise ValueError("page caps must be positive")
        if args.excerpt_chars <= 0:
            raise ValueError("--excerpt-chars must be positive")
        since_input = parse_aware_datetime(args.since_jst)
        since_utc = since_input.astimezone(timezone.utc)
        result["query"]["since_input"] = since_input.isoformat()
        result["query"]["since_utc"] = since_utc.isoformat()

        raw, truncated, quota = collect_recent_pages(args, since_utc)
        result["query"]["truncated"] = truncated
        if truncated:
            raise ProbeError(
                f"raw page count reached --max-pages {args.max_pages}; result is incomplete"
            )

        rejection_counts: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        for page in raw:
            reason = candidate_rejection_reason(page)
            if reason is None:
                candidates.append(page)
            else:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

        if len(candidates) > args.max_detail_pages:
            raise ProbeError(
                f"candidate count {len(candidates)} exceeds --max-detail-pages {args.max_detail_pages}"
            )

        paths.articles.mkdir(parents=True, exist_ok=True)
        details: list[dict[str, Any]] = []
        for metadata in candidates:
            url = metadata.get("url")
            if not isinstance(url, str) or not url:
                raise ProbeError("candidate page did not contain a URL")
            page, remaining = fetch_detail(args, url)
            quota.append(remaining)
            details.append(
                public_detail(serialize_detail(page, args.excerpt_chars), paths.articles)
            )

        originals = [item for item in details if item.get("edition") == "jp_original"]
        translations = [item for item in details if item.get("edition") == "translation"]
        issues = [
            item
            for item in details
            if item.get("edition") in {"unknown", "conflict"}
            or item.get("classification_confidence") != "confirmed"
        ]
        targets = expected_status(details, args.targets)
        missing = [row["target"] for row in targets if not row["present"]]

        status = "ok"
        code = 0
        errors: list[str] = []
        if missing:
            status = "degraded"
            code = 1
            errors.append("Expected JP-original pages missing: " + ", ".join(missing))
        if issues:
            status = "degraded"
            code = 1
            errors.append(f"{len(issues)} page(s) have uncertain or conflicting classification")
        if any(not item.get("has_text_content") for item in details):
            status = "degraded"
            code = 1
            errors.append("One or more candidate pages did not return textContent")

        result.update(
            {
                "status": status,
                "counts": {
                    "raw_recent_pages": len(raw),
                    "content_candidates": len(candidates),
                    "details_fetched": len(details),
                    "jp_originals": len(originals),
                    "translations": len(translations),
                    "classification_issues": len(issues),
                    "rejected": rejection_counts,
                },
                "rate_limit_remaining_samples": quota,
                "targets": targets,
                "jp_originals": originals,
                "translations": translations,
                "classification_issues": issues,
            }
        )
        if errors:
            result["warnings"] = errors
            result["error_type"] = "ProbeDegraded"
            result["error"] = "; ".join(errors)
        return result, code
    except Exception as exc:  # Always produce diagnostics.
        result.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return result, 1


def write_outputs(paths: OutputPaths, result: dict[str, Any]) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths.markdown.write_text(render_markdown(result), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.output_dir)
    paths = OutputPaths(
        root=root,
        json=root / "unified-result.json",
        markdown=root / "unified-summary.md",
        articles=root / "articles",
    )
    result, code = execute(args, paths)
    try:
        write_outputs(paths, result)
        print(paths.markdown.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to write outputs: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
