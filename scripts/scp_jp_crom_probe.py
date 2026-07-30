#!/usr/bin/env python3
"""Probe Crom for recently created SCP-JP original articles.

The script performs two checks:
1. It lists JP-tagged, non-hidden SCP-JP pages created after a supplied JST cutoff,
   then applies the same article-tag allow-list used by SCP-JP's new-article feed.
2. It looks up known target SCP numbers individually so indexing gaps can be
   distinguished from date/filter mistakes.

Output is written as machine-readable JSON plus a Markdown summary suitable for
GitHub Actions' step summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from thaumiel import AsyncClient, F, Page, Sort, SortKey, ThaumielError

SITE_PREFIX = "http://scp-jp.wikidot.com"
DEFAULT_TARGETS = (
    "SCP-4037-JP",
    "SCP-4543-JP",
    "SCP-4733-JP",
    "SCP-4119-JP",
)

# Mirrors the tags in SCP-JP's current "new JP articles" feed URL.
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
        "著者ページ",
        "ハブ",
        "エッセイ",
        "コンポーネント",
        "テーマ",
    }
)


@dataclass(frozen=True)
class Paths:
    json_output: Path
    markdown_output: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since-jst",
        default="2026-07-26T00:00:00+09:00",
        help="Inclusive cutoff as an ISO-8601 timestamp with an explicit offset.",
    )
    parser.add_argument(
        "--targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="SCP page names to verify individually.",
    )
    parser.add_argument(
        "--output",
        default="probe-result.json",
        help="JSON output path.",
    )
    parser.add_argument(
        "--summary-output",
        default="probe-summary.md",
        help="Markdown summary output path.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=500,
        help="Safety cap for the date-range query.",
    )
    return parser.parse_args()


def parse_aware_datetime(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--since-jst must include a UTC offset, e.g. +09:00")
    return parsed


def page_name(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def classify_page(page: Page) -> str:
    tags = set(page.tags)
    name = page_name(page.url)
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


def is_monitor_article(page: Page) -> bool:
    return bool(ARTICLE_TAGS.intersection(page.tags))


def serialize_page(page: Page) -> dict[str, Any]:
    alternate_titles = []
    if page.alternate_titles is not None:
        alternate_titles = [
            {"title": item.title, "source": item.source}
            for item in page.alternate_titles
        ]

    return {
        "page_name": page_name(page.url),
        "url": page.url,
        "wikidot_id": page.wikidot_id,
        "title": page.title,
        "genre": classify_page(page),
        "alternate_titles": alternate_titles,
        "created_at": page.created_at.isoformat(),
        "created_by": (
            {
                "display_name": page.created_by.display_name,
                "unix_name": page.created_by.unix_name,
            }
            if page.created_by is not None
            else None
        ),
        "category": page.category,
        "tags": list(page.tags),
        "rating": page.rating,
        "vote_count": page.vote_count,
        "is_hidden": page.is_hidden,
        "is_user_page": page.is_user_page,
    }


async def collect_recent_pages(
    client: AsyncClient, since_utc: datetime, max_pages: int
) -> tuple[list[Page], bool]:
    predicate = (
        F.url.starts_with(SITE_PREFIX)
        & (F.created_at >= since_utc)
        & (F.tag == "jp")
        & (F.is_hidden == False)  # noqa: E712 - overloaded predicate DSL
        & (F.category != "fragment")
        & (F.category != "deleted")
    )

    collected: list[Page] = []
    truncated = False
    async for page in client.pages(
        filter=predicate,
        sort=Sort.by(SortKey.CREATED_AT),
        page_size=100,
        alternate_titles=True,
    ):
        collected.append(page)
        if len(collected) >= max_pages:
            truncated = True
            break
    return collected, truncated


async def collect_target_statuses(
    client: AsyncClient,
    targets: list[str],
    since_utc: datetime,
    monitor_urls: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in targets:
        normalized = target.strip().lower()
        url = f"{SITE_PREFIX}/{normalized}"
        page = await client.page(url, alternate_titles=True)
        if page is None:
            results.append(
                {
                    "target": target.upper(),
                    "url": url,
                    "indexed": False,
                    "in_date_range": None,
                    "eligible_article": None,
                    "present_in_recent_query": False,
                }
            )
            continue

        results.append(
            {
                "target": target.upper(),
                "url": page.url,
                "indexed": True,
                "created_at": page.created_at.isoformat(),
                "in_date_range": page.created_at >= since_utc,
                "eligible_article": is_monitor_article(page),
                "present_in_recent_query": page.url in monitor_urls,
                "page": serialize_page(page),
            }
        )
    return results


def render_markdown(result: dict[str, Any]) -> str:
    query = result.get("query", {})
    lines = [
        "# SCP-JP Crom probe",
        "",
        f"- Status: **{result.get('status', 'error')}**",
        f"- Generated: `{result.get('generated_at_utc', 'unknown')}`",
        f"- Cutoff (input): `{query.get('since_input', query.get('since_input_raw', 'unknown'))}`",
        f"- Cutoff (UTC): `{query.get('since_utc', 'unavailable')}`",
    ]

    if result.get("status") != "ok":
        lines.extend(
            [
                "",
                "## Error",
                "",
                f"- Type: `{result.get('error_type', 'UnknownError')}`",
                "",
                f"```text\n{result.get('error', 'unknown error')}\n```",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            f"- Raw JP pages after cutoff: **{result['counts']['raw_recent_pages']}**",
            f"- Eligible monitor articles: **{result['counts']['monitor_articles']}**",
            f"- Truncated by safety cap: **{result['query']['truncated']}**",
            "",
            "## Target verification",
            "",
            "| Target | Indexed | Created at | In range | Eligible | In result |",
            "|---|---:|---|---:|---:|---:|",
        ]
    )
    for item in result["targets"]:
        lines.append(
            "| {target} | {indexed} | {created} | {in_range} | {eligible} | {present} |".format(
                target=item["target"],
                indexed="yes" if item["indexed"] else "no",
                created=item.get("created_at", "—"),
                in_range=(
                    "yes"
                    if item.get("in_date_range") is True
                    else "no"
                    if item.get("in_date_range") is False
                    else "—"
                ),
                eligible=(
                    "yes"
                    if item.get("eligible_article") is True
                    else "no"
                    if item.get("eligible_article") is False
                    else "—"
                ),
                present="yes" if item["present_in_recent_query"] else "no",
            )
        )

    lines.extend(["", "## Eligible articles", ""])
    if not result["pages"]:
        lines.append("No eligible articles were returned.")
    else:
        lines.extend(
            [
                "| Created at | Genre | Page | Title | Alternate title(s) |",
                "|---|---|---|---|---|",
            ]
        )
        for page in result["pages"]:
            alternate = "; ".join(
                item["title"] for item in page["alternate_titles"]
            ) or "—"
            title = page["title"].replace("|", "\\|")
            alternate = alternate.replace("|", "\\|")
            lines.append(
                f"| {page['created_at']} | {page['genre']} | {page['page_name']} | "
                f"{title} | {alternate} |"
            )
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace, paths: Paths) -> int:
    result: dict[str, Any] = {
        "schema_version": 2,
        "status": "running",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "query": {
            "site_prefix": SITE_PREFIX,
            "since_input_raw": args.since_jst,
            "required_tag": "jp",
            "article_tags": sorted(ARTICLE_TAGS),
            "excluded_categories": ["fragment", "deleted"],
            "max_pages": args.max_pages,
        },
    }

    try:
        if args.max_pages <= 0:
            raise ValueError("--max-pages must be positive")

        since_input = parse_aware_datetime(args.since_jst)
        since_utc = since_input.astimezone(timezone.utc)
        result["query"].update(
            {
                "since_input": since_input.isoformat(),
                "since_utc": since_utc.isoformat(),
            }
        )

        async with AsyncClient(
            user_agent="iniwa-scp-jp-monitor-probe/0.2 (GitHub Actions)"
        ) as client:
            raw_pages, truncated = await collect_recent_pages(
                client, since_utc, args.max_pages
            )
            monitor_pages = [page for page in raw_pages if is_monitor_article(page)]
            monitor_urls = {page.url for page in monitor_pages}
            targets = await collect_target_statuses(
                client, list(args.targets), since_utc, monitor_urls
            )

        result.update(
            {
                "status": "ok",
                "query": {**result["query"], "truncated": truncated},
                "counts": {
                    "raw_recent_pages": len(raw_pages),
                    "monitor_articles": len(monitor_pages),
                },
                "pages": [serialize_page(page) for page in monitor_pages],
                "targets": targets,
            }
        )
        exit_code = 0
    except Exception as exc:  # The probe must serialize unexpected runtime failures.
        result.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        exit_code = 1

    paths.json_output.parent.mkdir(parents=True, exist_ok=True)
    paths.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    paths.json_output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths.markdown_output.write_text(render_markdown(result), encoding="utf-8")
    print(paths.markdown_output.read_text(encoding="utf-8"))
    return exit_code


def main() -> int:
    args = parse_args()
    paths = Paths(Path(args.output), Path(args.summary_output))
    return asyncio.run(run(args, paths))


if __name__ == "__main__":
    raise SystemExit(main())
