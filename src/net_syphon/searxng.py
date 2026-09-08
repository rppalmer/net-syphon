"""One bounded SearXNG request; no redirects, retries, result fetching, or raw storage."""

import asyncio
import json
import time
from dataclasses import dataclass
from uuid import UUID

import httpx2 as httpx

from net_syphon.audit import AuditWriter
from net_syphon.contracts import (
    ErrorCode,
    SearchError,
    SearchItem,
    SearchRequest,
    plain_text,
    publication_date,
    valid_web_url,
)

REQUEST_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass
class SearchBatch:
    results: list[SearchItem]
    partial: bool


def _normalize(payload: object, limit: int, audit: AuditWriter, call_id: UUID) -> SearchBatch:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
    failures = payload.get("unresponsive_engines", [])
    if not isinstance(failures, list):
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
    results: list[SearchItem] = []
    rejected = 0
    failure_counts = {
        "upstream_captcha_count": 0,
        "upstream_rate_limited_count": 0,
        "upstream_access_denied_count": 0,
        "upstream_timeout_count": 0,
    }
    for failure in failures:
        if not isinstance(failure, list) or len(failure) < 2 or not isinstance(failure[1], str):
            continue
        description = failure[1].lower()
        for phrase, field in (
            ("captcha", "upstream_captcha_count"),
            ("too many requests", "upstream_rate_limited_count"),
            ("access denied", "upstream_access_denied_count"),
            ("timeout", "upstream_timeout_count"),
        ):
            if phrase in description:
                failure_counts[field] += 1
                break
    for item in payload["results"]:
        if not isinstance(item, dict) or not valid_web_url(item.get("url")):
            rejected += 1
            continue
        title = plain_text(item["title"], 300) if isinstance(item.get("title"), str) else ""
        if not title:
            rejected += 1
            continue
        # Scanning does not stop at the limit. The audit is the only diagnostic
        # channel this server has, so it counts every rejection in the payload.
        # Only what the caller could still have received decides `partial`.
        if len(results) >= limit:
            continue
        content = item.get("content")
        snippet = plain_text(content, 1000) if isinstance(content, str) else None
        results.append(
            SearchItem(
                title=title,
                url=item["url"],
                snippet=snippet or None,
                published_at=publication_date(item.get("publishedDate")),
            )
        )
    audit.emit(
        "normalized",
        str(call_id),
        result_count=len(results),
        rejected_count=rejected,
        upstream_failure_count=len(failures),
        **failure_counts,
    )
    if not results and (rejected or failures):
        raise SearchError(
            ErrorCode.ENGINES_UNAVAILABLE if failures else ErrorCode.UPSTREAM_UNAVAILABLE
        )
    return SearchBatch(results, partial=bool(failures) or (bool(rejected) and len(results) < limit))


async def search(
    endpoint: str,
    request: SearchRequest,
    audit: AuditWriter,
    call_id: UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SearchBatch:
    """Own the HTTP lifecycle, including audit-before-egress and a wall-clock deadline."""
    started = time.monotonic()
    byte_count = 0
    http_status: int | None = None
    status = "error"
    code: str | None = None
    reason: str | None = None
    # This write must succeed before creating any connection.
    audit.emit("outbound_start", str(call_id))
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT),
            httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
                verify=True,
                transport=transport,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip, identity"},
            ) as client,
            client.stream(
                "POST",
                endpoint,
                data={
                    "q": request.query,
                    "format": "json",
                    "categories": "general",
                    "pageno": "1",
                },
            ) as response,
        ):
            http_status = response.status_code
            if http_status == 429:
                raise SearchError(ErrorCode.RATE_LIMITED)
            if http_status in {401, 403}:
                raise SearchError(ErrorCode.ACCESS_DENIED)
            if http_status != 200:
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
                "application/json"
            ):
                reason = "content_type"
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            if response.headers.get("content-encoding", "identity").strip().lower() not in {
                "identity",
                "gzip",
            }:
                reason = "encoding"
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            body = bytearray()
            # The locked HTTP client decodes gzip incrementally in bounded allocations.
            async for chunk in response.aiter_bytes(chunk_size=16_384):
                byte_count += len(chunk)
                if byte_count > MAX_RESPONSE_BYTES or (
                    response.num_bytes_downloaded > MAX_RESPONSE_BYTES
                ):
                    raise SearchError(ErrorCode.TOO_LARGE)
                body.extend(chunk)
            try:
                payload = json.loads(body)
            except (ValueError, RecursionError):
                reason = "invalid_json"
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE) from None
            status = "success"
    except (TimeoutError, httpx.TimeoutException):
        code = ErrorCode.TIMEOUT.value
        raise SearchError(ErrorCode.TIMEOUT) from None
    except httpx.HTTPError:
        code = ErrorCode.UPSTREAM_UNAVAILABLE.value
        reason = "network"
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE) from None
    except SearchError as exc:
        code = exc.code.value
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        audit.emit(
            "outbound_end",
            str(call_id),
            status=status,
            code=code,
            reason=reason,
            http_status=http_status,
            response_bytes=byte_count,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return _normalize(payload, request.max_results, audit, call_id)
