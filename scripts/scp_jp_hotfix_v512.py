#!/usr/bin/env python3
"""Runtime hotfix layer for SCP-JP Daily Monitor v5.1.2.

The v5.1.1 monitor remains the source of truth for feed serialization and state
layout.  This module patches the expensive and error-prone parts at runtime:

* reuse unchanged article snapshots from ``monitor-state``;
* respect ``Retry-After`` and accept complete GraphQL data returned with HTTP 429;
* avoid leaking response bodies into public diagnostics;
* classify branch-tagged translations as confirmed;
* prevent JP artwork licence metadata from being mistaken for page translation
  metadata.

Keeping this logic isolated makes the emergency fix reviewable while preserving
v5.1.1's public JSON schema.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOTFIX_VERSION = "5.1.2"
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_DEFAULT_BACKOFF_SECONDS = (2.0, 8.0, 20.0)


class CromRequestError(RuntimeError):
    """Sanitized Crom request failure with structured retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        rate_limit_remaining: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_remaining = rate_limit_remaining


def _header_map(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except Exception:
        return {}


def _int_header(headers: dict[str, str], name: str) -> int | None:
    raw = headers.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _retry_after_seconds(headers: dict[str, str], *, now: datetime | None = None) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - current).total_seconds())


def _decode_json(raw: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _complete_data(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload or payload.get("errors"):
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def graphql_request(
    endpoint: str,
    query: str,
    variables: dict[str, Any],
    *,
    timeout: float,
    attempts: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Perform a Crom GraphQL request with bounded, header-aware retries.

    Crom has occasionally returned a complete ``data`` object with HTTP 429.
    Such a response is safe to consume because JSON decoding completed and no
    GraphQL errors were reported.  Other failures are reduced to structured,
    non-sensitive metadata instead of embedding the response body in health.json.
    """

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
                "User-Agent": "iniwa-scp-jp-monitor/1.0 (GitHub Actions; v5.1.2)",
            },
        )
        retry_after: float | None = None
        retryable = False
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                headers = _header_map(response.headers)
                status = int(response.status)
            payload = _decode_json(raw)
            data = _complete_data(payload)
            if not 200 <= status < 300:
                if status == 429 and data is not None:
                    return data, headers
                raise CromRequestError(
                    f"Crom request failed with HTTP {status}",
                    http_status=status,
                    retry_after_seconds=_retry_after_seconds(headers),
                    rate_limit_remaining=_int_header(headers, "x-ratelimit-remaining"),
                )
            if payload is None:
                raise CromRequestError("Crom returned a non-JSON response")
            if payload.get("errors"):
                rendered = json.dumps(payload["errors"], ensure_ascii=False)
                raise CromRequestError(f"Crom GraphQL error: {rendered[:800]}")
            if data is None:
                raise CromRequestError("Crom response did not contain a data object")
            return data, headers
        except HTTPError as exc:
            headers = _header_map(exc.headers)
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            data = _complete_data(_decode_json(raw))
            if exc.code == 429 and data is not None:
                return data, headers
            retry_after = _retry_after_seconds(headers)
            last_error = CromRequestError(
                f"Crom request failed with HTTP {exc.code}",
                http_status=exc.code,
                retry_after_seconds=retry_after,
                rate_limit_remaining=_int_header(headers, "x-ratelimit-remaining"),
            )
            retryable = exc.code in RETRYABLE_HTTP_STATUS
        except CromRequestError as exc:
            last_error = exc
            retry_after = exc.retry_after_seconds
            retryable = exc.http_status in RETRYABLE_HTTP_STATUS
        except (URLError, TimeoutError, OSError) as exc:
            last_error = CromRequestError(f"Could not contact Crom: {exc}")
            retryable = True

        if not retryable or attempt >= attempts:
            break
        fallback = _DEFAULT_BACKOFF_SECONDS[min(attempt - 1, len(_DEFAULT_BACKOFF_SECONDS) - 1)]
        wait_seconds = retry_after if retry_after is not None else fallback
        time.sleep(min(max(wait_seconds, 0.0), 60.0))

    raise last_error or CromRequestError("Crom request failed for an unknown reason")


_CREDIT_BLOCK_PATTERNS = (
    re.compile(
        r"\[\[include\s+:scp-jp:credit:start[^\]]*\]\](.*?)"
        r"\[\[include\s+:scp-jp:credit:end[^\]]*\]\]",
        flags=re.I | re.S,
    ),
    re.compile(
        r"\[\[include\s+:scp-jp:info:start[^\]]*\]\](.*?)"
        r"\[\[include\s+:scp-jp:info:end[^\]]*\]\]",
        flags=re.I | re.S,
    ),
)


def primary_credit_source(source: str | None) -> str:
    """Return the first formal page-level credit block when one exists."""

    if not source:
        return ""
    for pattern in _CREDIT_BLOCK_PATTERNS:
        match = pattern.search(source)
        if match:
            return match.group(1)
    return source


def make_credit_pairs(core: Any, original_credit_pairs: Callable[[str | None], Iterable[tuple[str, str, str]]]):
    def credit_pairs(source: str | None) -> Iterable[tuple[str, str, str]]:
        return original_credit_pairs(primary_credit_source(source))

    return credit_pairs


def make_classify_edition(core: Any):
    def classify_edition(
        page: dict[str, Any], credit: dict[str, str], original_url: str | None
    ) -> dict[str, Any]:
        tags = {str(tag) for tag in page.get("tags") or []}
        attribution_types = {
            str(item.get("type") or "").upper()
            for item in page.get("attributions") or []
            if isinstance(item, dict)
        }
        source_branch, branch_basis = core.infer_source_branch(original_url, tags)

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
        if has_jp_tag:
            # JP artwork often contains translated source credits in its licence
            # section.  Only page-level translator attribution or the explicit
            # "翻訳責任者" field is strong enough to contradict the JP tag.
            conflict_reasons: list[str] = []
            if "TRANSLATOR" in attribution_types:
                conflict_reasons.append("crom_translator_attribution")
            if credit.get("translation_responsible"):
                conflict_reasons.append("credit_translation_responsible")
            if conflict_reasons:
                return {
                    "edition": "conflict",
                    "confidence": "conflict",
                    "reasons": ["jp_tag", *conflict_reasons],
                    "source_branch": source_branch,
                    "source_branch_basis": branch_basis,
                }
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
        if source_branch and branch_basis == "tag":
            return {
                "edition": "translation",
                "confidence": "confirmed",
                "reasons": ["source_branch_tag"],
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

    return classify_edition


def _normalized_tags(value: Any) -> tuple[str, ...]:
    return tuple(sorted(str(item) for item in (value or [])))


def refresh_cached_article(core: Any, article: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    refreshed = dict(article)
    raw_url = str(metadata.get("url") or refreshed.get("url") or "")
    created_by = metadata.get("createdBy") if isinstance(metadata.get("createdBy"), dict) else {}
    refreshed.update(
        {
            "wikidot_id": str(metadata.get("wikidotId") or refreshed.get("wikidot_id") or ""),
            "page_name": core.page_name(raw_url) if raw_url else refreshed.get("page_name"),
            "url": core.public_page_url(raw_url) if raw_url else refreshed.get("url"),
            "created_at_utc": metadata.get("createdAt") or refreshed.get("created_at_utc"),
            "created_at_jst": core.to_jst_string(metadata.get("createdAt"))
            or refreshed.get("created_at_jst"),
            "rating": metadata.get("rating"),
            "vote_count": metadata.get("voteCount"),
            "revision_count": metadata.get("revisionCount"),
            "comment_count": metadata.get("commentCount"),
            "tags": list(metadata.get("tags") or []),
            "created_by": core.clean_credit_value(str(created_by.get("displayName") or ""))
            or refreshed.get("created_by"),
        }
    )
    return refreshed


def reusable_cached_article(core: Any, state: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    article_id = str(metadata.get("wikidotId") or "")
    if not article_id:
        return None
    seen = state.get("seen")
    entry = seen.get(article_id) if isinstance(seen, dict) else None
    cached = entry.get("article") if isinstance(entry, dict) else None
    if not isinstance(cached, dict):
        return None
    if cached.get("edition") not in {"jp_original", "translation"}:
        return None
    if cached.get("classification_confidence") != "confirmed":
        return None
    if not cached.get("has_text_content") or not cached.get("summary_basis"):
        return None
    current_revision = metadata.get("revisionCount")
    if current_revision is None or cached.get("revision_count") != current_revision:
        return None
    if _normalized_tags(cached.get("tags")) != _normalized_tags(metadata.get("tags")):
        return None
    return refresh_cached_article(core, cached, metadata)


def pending_error(core: Any, url: str, exc: Exception) -> dict[str, Any]:
    item: dict[str, Any] = {
        "page_name": core.page_name(url),
        "url": core.public_page_url(url),
        "reason": "detail_fetch_failed",
        "error_type": type(exc).__name__,
        "message": str(exc)[:500],
    }
    for attr, key in (
        ("http_status", "http_status"),
        ("retry_after_seconds", "retry_after_seconds"),
        ("rate_limit_remaining", "rate_limit_remaining"),
    ):
        value = getattr(exc, attr, None)
        if value is not None:
            item[key] = value
    return item


def make_execute(core: Any):
    def execute(args: Any, paths: Any) -> tuple[dict[str, Any], int]:
        now_utc = (
            core.parse_aware_datetime(args.now).astimezone(timezone.utc)
            if args.now
            else datetime.now(timezone.utc)
        )
        health: dict[str, Any] = {
            "schema_version": 1,
            "status": "starting",
            "hotfix_version": HOTFIX_VERSION,
            "generated_at_utc": core.utc_iso(now_utc),
            "generated_at_jst": core.jst_iso(now_utc),
            "generated_date_jst": now_utc.astimezone(core.JST).date().isoformat(),
            "run_id": core.os.getenv("GITHUB_RUN_ID"),
            "repository": core.os.getenv("GITHUB_REPOSITORY"),
        }
        try:
            core.validate_args(args)
            baseline = core.load_baseline(Path(args.baseline_file))
            previous_path = Path(args.previous_state_file) if args.previous_state_file else None
            state, state_source = core.load_state(previous_path, baseline)
            mode = "incremental" if state_source == "previous_state" else "bootstrap"
            bootstrap_since = core.parse_aware_datetime(str(baseline["bootstrap_since_jst"]))
            since_utc = core.query_since_utc(
                mode=mode,
                now_utc=now_utc,
                window_days=args.window_days,
                bootstrap_since=bootstrap_since,
            )

            raw_pages, truncated, quota_samples = core.collect_recent_pages(args, since_utc)
            if truncated:
                raise core.MonitorError(
                    f"raw page count reached --max-pages {args.max_pages}; result is incomplete"
                )

            rejection_counts: dict[str, int] = {}
            candidates: list[dict[str, Any]] = []
            for page in raw_pages:
                reason = core.candidate_rejection_reason(page)
                if reason is None:
                    candidates.append(page)
                else:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

            confirmed: list[dict[str, Any]] = []
            pending: list[dict[str, Any]] = []
            classification_issues: list[dict[str, Any]] = []
            details_fetched = 0
            details_reused = 0
            detail_failures = 0
            classification_pending = 0
            text_pending = 0

            for metadata in candidates:
                url = metadata.get("url")
                if not isinstance(url, str) or not url:
                    pending.append({"page_name": None, "url": None, "reason": "missing_url"})
                    continue

                cached = reusable_cached_article(core, state, metadata)
                if cached is not None:
                    confirmed.append(cached)
                    details_reused += 1
                    continue

                if details_fetched >= args.max_detail_pages:
                    raise core.MonitorError(
                        f"detail fetch count reached --max-detail-pages {args.max_detail_pages}; result is incomplete"
                    )
                details_fetched += 1
                try:
                    detail, remaining = core.fetch_detail(args, url)
                    quota_samples.append(remaining)
                    article = core.serialize_article(detail)
                except Exception as exc:
                    detail_failures += 1
                    pending.append(pending_error(core, url, exc))
                    continue

                if (
                    article["edition"] in {"unknown", "conflict"}
                    or article["classification_confidence"] != "confirmed"
                ):
                    classification_pending += 1
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
                    text_pending += 1
                    pending.append(
                        {
                            "page_name": article["page_name"],
                            "url": article["url"],
                            "reason": "text_content_not_ready",
                        }
                    )
                    continue
                confirmed.append(article)

            effective_snapshot_days = max(args.snapshot_days, args.window_days + 7)
            state, new_ids = core.update_state(
                state,
                confirmed,
                now_utc=now_utc,
                snapshot_days=effective_snapshot_days,
            )
            state_seen = state.get("seen") or {}
            latest_articles = [
                core.decorate_article(article, state_seen[str(article["wikidot_id"])], new_ids)
                for article in confirmed
                if str(article.get("wikidot_id") or "") in state_seen
            ]
            latest_articles.sort(
                key=lambda item: str(item.get("created_at_utc") or ""), reverse=True
            )
            delta_articles = core.notification_articles(
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
                        "since_utc": core.utc_iso(since_utc),
                        "since_jst": core.jst_iso(since_utc),
                        "window_days": args.window_days,
                        "notification_retention_hours": args.notification_hours,
                        "snapshot_retention_days": effective_snapshot_days,
                        "truncated": False,
                    },
                    "counts": {
                        "raw_recent_pages": len(raw_pages),
                        "content_candidates": len(candidates),
                        "details_confirmed": len(confirmed),
                        "details_fetched": details_fetched,
                        "details_reused": details_reused,
                        "detail_fetch_failures": detail_failures,
                        "classification_pending": classification_pending,
                        "text_pending": text_pending,
                        "jp_originals": jp_count,
                        "translations": translation_count,
                        "new_this_run": len(new_ids),
                        "new_jp_originals": new_jp,
                        "new_translations": new_translation,
                        "notification_candidates": len(delta_articles),
                        "pending": len(pending),
                        "rejected": rejection_counts,
                    },
                    "rate_limit": core.rate_limit_summary(quota_samples),
                    "pending_pages": pending,
                    "warnings": warnings,
                }
            )
            latest = {
                "schema_version": 1,
                "generated_at_utc": core.utc_iso(now_utc),
                "generated_at_jst": core.jst_iso(now_utc),
                "window_days": args.window_days,
                "articles": latest_articles,
            }
            delta = {
                "schema_version": 1,
                "generated_at_utc": core.utc_iso(now_utc),
                "generated_at_jst": core.jst_iso(now_utc),
                "retention_hours": args.notification_hours,
                "new_this_run_ids": sorted(new_ids),
                "articles": delta_articles,
            }
            debug = {
                "schema_version": 1,
                "hotfix_version": HOTFIX_VERSION,
                "health": health,
                "classification_issues": classification_issues,
                "latest_article_ids": [item["wikidot_id"] for item in latest_articles],
                "delta_article_ids": [item["wikidot_id"] for item in delta_articles],
            }
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.public.mkdir(parents=True, exist_ok=True)
            core.write_json(paths.health, health)
            core.write_json(paths.latest, latest)
            core.write_json(paths.delta, delta)
            core.write_json(paths.state, state)
            core.write_json(paths.debug, debug)
            paths.summary.write_text(core.build_summary(health, delta), encoding="utf-8")
            paths.index.write_text(core.build_index(health, delta), encoding="utf-8")
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
            core.write_json(paths.debug, {"schema_version": 1, "health": health})
            paths.summary.write_text(
                core.build_summary(health, {"articles": []}), encoding="utf-8"
            )
            return health, 1

    return execute


def install(core: Any) -> Any:
    """Install all v5.1.2 patches into the imported v5.1.1 module."""

    original_credit_pairs = core.credit_pairs
    core.graphql_request = graphql_request
    core.credit_pairs = make_credit_pairs(core, original_credit_pairs)
    core.classify_edition = make_classify_edition(core)
    core.execute = make_execute(core)
    return core
