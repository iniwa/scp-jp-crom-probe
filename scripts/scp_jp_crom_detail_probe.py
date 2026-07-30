#!/usr/bin/env python3
"""Fetch recent SCP-JP articles and their detail fields from Crom GraphQL.

The probe uses only the Python standard library. It first performs a lightweight
recent-page query, applies the same broad article-tag filter used by SCP-JP's
new-JP listing, and then fetches expensive detail fields only for eligible pages.
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
from typing import Any
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
ARTICLE_TAGS = frozenset(
    {
        "scp",
        "tale",
        "goi-format",
        "アートワーク",
        "サイト",
        "合作",
        "設定集",
        "ニュース",
        "news",
        "著者ページ",
        "ハブ",
        "エッセイ",
        "コンポーネント",
        "テーマ",
    }
)
EXCLUDED_CATEGORIES = frozenset({"fragment", "deleted"})
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

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
    parser.add_argument("--output-dir", default="detail-output")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--max-detail-pages", type=int, default=50)
    parser.add_argument("--excerpt-chars", type=int, default=1600)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--targets", nargs="*", default=list(DEFAULT_TARGETS), help="Expected pages."
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
                "User-Agent": "iniwa-scp-jp-monitor-detail-probe/0.3 (GitHub Actions)",
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
        time.sleep(min(2 ** (attempt - 1), 8))

    raise last_error or ProbeError("Crom request failed for an unknown reason")


def recent_filter(since_utc: datetime) -> dict[str, Any]:
    return {
        "_and": [
            {"url": {"startsWith": SITE_PREFIX}},
            {"onWikidotPage": {"createdAt": {"gte": since_utc.isoformat()}}},
            {"onWikidotPage": {"tags": {"eq": "jp"}}},
            {"onWikidotPage": {"isHidden": {"eq": False}}},
            {"onWikidotPage": {"category": {"neq": "fragment"}}},
            {"onWikidotPage": {"category": {"neq": "deleted"}}},
        ]
    }


def is_monitor_article(page: dict[str, Any]) -> bool:
    tags = set(page.get("tags") or [])
    return (
        "jp" in tags
        and bool(tags.intersection(ARTICLE_TAGS))
        and page.get("isHidden") is False
        and page.get("category") not in EXCLUDED_CATEGORIES
    )


def classify_page(page: dict[str, Any]) -> str:
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
        ("設定集", "設定集"),
        ("ニュース", "ニュース"),
        ("news", "ニュース"),
        ("著者ページ", "著者ページ"),
        ("ハブ", "ハブ"),
        ("エッセイ", "エッセイ"),
        ("コンポーネント", "コンポーネント"),
        ("テーマ", "テーマ"),
        ("合作", "合作"),
        ("サイト", "サイト記事"),
    ):
        if tag in tags:
            return label
    return "その他"


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
    value = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    return value.strip()


def extract_credit_fields(source: str | None) -> dict[str, str]:
    if not source:
        return {}
    fields: dict[str, str] = {}
    aliases = {
        "タイトル": "title",
        "title": "title",
        "著者": "author",
        "author": "author",
        "作成年": "year",
        "公開年": "year",
        "year": "year",
    }
    for raw_line in source.splitlines():
        line = raw_line.strip().replace("**", "").replace("__", "")
        match = re.match(r"^([^:：]{1,20})\s*[:：]\s*(.+?)\s*$", line)
        if not match:
            continue
        label = match.group(1).strip().lower()
        key = aliases.get(label)
        if key and key not in fields:
            fields[key] = strip_wikidot_markup(match.group(2))
    return fields


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
        r"^\s*SCP-[A-Z]?\d+(?:-[A-Z0-9]+)*-JP\s*(?:[-‐‑‒–—―:：|｜]+)\s*(.+?)\s*$",
        candidate,
        flags=re.I,
    )
    if generic:
        return generic.group(1).strip() or None
    return candidate


def derive_titles(page: dict[str, Any], credit: dict[str, str]) -> tuple[str, str | None]:
    base = str(page.get("title") or page_name(str(page.get("url") or ""))).strip()
    genre = classify_page(page)
    credit_title = credit.get("title")
    alternate = first_alternate_title(page)
    if genre == "SCP報告書":
        subtitle = split_numbered_title(base, credit_title)
        if subtitle is None:
            subtitle = split_numbered_title(base, alternate)
        return base, subtitle
    # Non-SCP pages generally use the Wikidot title as the article title. Preserve a
    # genuinely different credit title as a subtitle rather than replacing the page title.
    subtitle = None
    if credit_title and credit_title.casefold() != base.casefold():
        subtitle = credit_title
    elif alternate and alternate.casefold() != base.casefold():
        subtitle = alternate
    return base, subtitle


def normalize_excerpt(text: str | None, limit: int) -> str:
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized if len(normalized) <= limit else normalized[:limit].rstrip() + "…"


def serialize_detail(page: dict[str, Any], excerpt_chars: int) -> dict[str, Any]:
    source = page.get("source") if isinstance(page.get("source"), str) else ""
    text_content = (
        page.get("textContent") if isinstance(page.get("textContent"), str) else ""
    )
    credit = extract_credit_fields(source)
    article_title, subtitle = derive_titles(page, credit)
    created_by = page.get("createdBy")
    return {
        "page_name": page_name(str(page.get("url") or "")),
        "url": page.get("url"),
        "wikidot_id": page.get("wikidotId"),
        "genre": classify_page(page),
        "page_title": page.get("title") or "",
        "article_title": article_title,
        "subtitle": subtitle,
        "credit": credit,
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
        "attributions": [
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
        ],
        "category": page.get("category"),
        "tags": list(page.get("tags") or []),
        "rating": page.get("rating"),
        "vote_count": page.get("voteCount"),
        "revision_count": page.get("revisionCount"),
        "comment_count": page.get("commentCount"),
        "summary_from_crom": page.get("summary"),
        "text_excerpt": normalize_excerpt(text_content, excerpt_chars),
        "has_source": bool(source),
        "has_text_content": bool(text_content),
        "source_length": len(source),
        "text_content_length": len(text_content),
        "_source": source,
        "_text_content": text_content,
    }


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


def public_detail(detail: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    page = detail["page_name"]
    source_path = article_dir / f"{page}.source.txt"
    text_path = article_dir / f"{page}.text.txt"
    source_path.write_text(str(detail.pop("_source", "")), encoding="utf-8")
    text_path.write_text(str(detail.pop("_text_content", "")), encoding="utf-8")
    detail["source_file"] = f"articles/{source_path.name}"
    detail["text_file"] = f"articles/{text_path.name}"
    return detail


def expected_status(details: list[dict[str, Any]], targets: list[str]) -> list[dict[str, Any]]:
    names = {str(item.get("page_name") or "").casefold() for item in details}
    rows = []
    for target in targets:
        normalized = target.strip().lower().replace("ｰ", "-").replace("‐", "-")
        rows.append({"target": target.strip().upper(), "present": normalized in names})
    return rows


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# SCP-JP Crom detail probe",
        "",
        f"- Status: **{md_escape(result.get('status', 'unknown'))}**",
        f"- Generated: `{md_escape(result.get('generated_at_utc', 'unknown'))}`",
        f"- Cutoff (input): `{md_escape(result.get('query', {}).get('since_input', 'unknown'))}`",
        f"- Cutoff (UTC): `{md_escape(result.get('query', {}).get('since_utc', 'unknown'))}`",
    ]
    if result.get("status") != "ok":
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

    counts = result.get("counts") or {}
    lines.extend(
        [
            f"- Raw JP pages after cutoff: **{counts.get('raw_recent_pages', 0)}**",
            f"- Eligible articles: **{counts.get('eligible_articles', 0)}**",
            f"- Details fetched: **{counts.get('details_fetched', 0)}**",
            "",
            "## Expected pages",
            "",
            "| Target | Present |",
            "|---|---:|",
        ]
    )
    for item in result.get("targets") or []:
        lines.append(
            f"| {md_escape(item.get('target', '—'))} | {'yes' if item.get('present') else 'no'} |"
        )

    lines.extend(
        [
            "",
            "## Article metadata",
            "",
            "| Created at (JST) | Genre | Page | Article title | Subtitle | Content |",
            "|---|---|---|---|---|---:|",
        ]
    )
    for item in result.get("articles") or []:
        content = "yes" if item.get("has_text_content") else "no"
        lines.append(
            "| {created} | {genre} | {page} | {title} | {subtitle} | {content} |".format(
                created=md_escape(item.get("created_at_jst") or "—"),
                genre=md_escape(item.get("genre") or "—"),
                page=md_escape(item.get("page_name") or "—"),
                title=md_escape(item.get("article_title") or "—"),
                subtitle=md_escape(item.get("subtitle") or "—"),
                content=content,
            )
        )

    lines.extend(["", "## Text previews", ""])
    for item in result.get("articles") or []:
        lines.extend(
            [
                f"### {md_escape(item.get('article_title') or item.get('page_name') or 'Article')}",
                "",
                f"- Page: `{md_escape(item.get('page_name') or '—')}`",
                f"- Subtitle: {md_escape(item.get('subtitle') or '—')}",
                f"- Author: {md_escape((item.get('created_by') or {}).get('display_name') or '—')}",
                "",
                "```text",
                str(item.get("text_excerpt") or "No text content returned."),
                "```",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def execute(args: argparse.Namespace, paths: OutputPaths) -> tuple[dict[str, Any], int]:
    generated = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "schema_version": 3,
        "status": "starting",
        "generated_at_utc": generated,
        "query": {
            "endpoint": args.endpoint,
            "site_prefix": SITE_PREFIX,
            "since_input": args.since_jst,
            "article_tags": sorted(ARTICLE_TAGS),
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
        eligible = [page for page in raw if is_monitor_article(page)]
        if len(eligible) > args.max_detail_pages:
            raise ProbeError(
                f"eligible page count {len(eligible)} exceeds --max-detail-pages {args.max_detail_pages}"
            )

        paths.articles.mkdir(parents=True, exist_ok=True)
        details: list[dict[str, Any]] = []
        for metadata in eligible:
            url = metadata.get("url")
            if not isinstance(url, str) or not url:
                raise ProbeError("eligible page did not contain a URL")
            page, remaining = fetch_detail(args, url)
            quota.append(remaining)
            details.append(public_detail(serialize_detail(page, args.excerpt_chars), paths.articles))

        targets = expected_status(details, args.targets)
        missing = [row["target"] for row in targets if not row["present"]]
        result.update(
            {
                "status": "ok" if not missing else "error",
                "query": {**result["query"], "truncated": truncated},
                "counts": {
                    "raw_recent_pages": len(raw),
                    "eligible_articles": len(eligible),
                    "details_fetched": len(details),
                },
                "rate_limit_remaining_samples": quota,
                "targets": targets,
                "articles": details,
            }
        )
        if missing:
            result["error_type"] = "ExpectedPageMissing"
            result["error"] = "Expected pages missing from result: " + ", ".join(missing)
            return result, 1
        return result, 0
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
        json=root / "detail-result.json",
        markdown=root / "detail-summary.md",
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
