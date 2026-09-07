"""Bounded hosted search and page retrieval; no SDK, retries, or local page fetching."""

import asyncio
import hashlib
import json
import time
import unicodedata
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx2 as httpx

from net_syphon.audit import AuditWriter
from net_syphon.clock import Clock
from net_syphon.contracts import (
    ErrorCode,
    PageResponse,
    SearchError,
    SearchItem,
    SearchRequest,
    plain_text,
    publication_date,
    valid_web_url,
)
from net_syphon.policy import authorize_url
from net_syphon.searxng import SearchBatch

API_ORIGIN = "https://api.firecrawl.dev"
REQUEST_TIMEOUT = 25.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


async def _post(
    path: str,
    payload: dict,
    key: str,
    audit: AuditWriter,
    call_id: UUID,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[dict, bool]:
    started = time.monotonic()
    byte_count = 0
    http_status = None
    status = "error"
    audit.emit("outbound_start", str(call_id))
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT),
            httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                verify=True,
                trust_env=False,
                follow_redirects=False,
                transport=transport,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, identity",
                },
            ) as client,
            client.stream("POST", API_ORIGIN + path, json=payload) as response,
        ):
            http_status = response.status_code
            if http_status in {401, 403}:
                raise SearchError(ErrorCode.ACCESS_DENIED)
            if http_status in {402, 429}:
                raise SearchError(ErrorCode.RATE_LIMITED)
            if http_status in {408, 504}:
                raise SearchError(ErrorCode.TIMEOUT)
            if http_status != 200:
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            if response.headers.get("content-encoding", "identity").strip().lower() not in {
                "identity",
                "gzip",
            }:
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                byte_count += len(chunk)
                if (
                    byte_count > MAX_RESPONSE_BYTES
                    or response.num_bytes_downloaded > MAX_RESPONSE_BYTES
                ):
                    raise SearchError(ErrorCode.TOO_LARGE)
                body.extend(chunk)
            try:
                decoded = json.loads(body)
            except (ValueError, RecursionError):
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE) from None
            if (
                not isinstance(decoded, dict)
                or decoded.get("success") is not True
                or not isinstance(decoded.get("data"), dict)
            ):
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            status = "success"
            return decoded["data"], bool(decoded.get("warning"))
    except (TimeoutError, httpx.TimeoutException):
        raise SearchError(ErrorCode.TIMEOUT) from None
    except httpx.HTTPError:
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE) from None
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        audit.emit(
            "outbound_end",
            str(call_id),
            status=status,
            http_status=http_status,
            response_bytes=byte_count,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


async def search(
    key: str,
    request: SearchRequest,
    audit: AuditWriter,
    call_id: UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SearchBatch:
    """Send filter intent once; search dates are provider constraints, not verified facts."""
    source = "news" if request.search_category == "news" else "web"
    payload = {
        "query": request.query,
        "limit": request.max_results,
        "sources": [source],
        "timeout": 20000,
    }
    if request.include_domains:
        payload["includeDomains"] = request.include_domains
    if request.time_range:
        unit = {"day": "d", "week": "w", "month": "m", "year": "y"}[request.time_range]
        payload["tbs"] = f"qdr:{unit}"
    if request.start_date:
        end = request.end_date - timedelta(days=1)
        payload["tbs"] = f"cdr:1,cd_min:{request.start_date:%m/%d/%Y},cd_max:{end:%m/%d/%Y}"
    data, warned = await _post("/v2/search", payload, key, audit, call_id, transport)
    raw_results = data.get(source)
    if not isinstance(raw_results, list):
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
    results = []
    rejected = 0
    for item in raw_results:
        if not isinstance(item, dict) or not valid_web_url(item.get("url")):
            rejected += 1
            continue
        host = urlsplit(item["url"]).hostname.lower().rstrip(".")
        if request.include_domains and not any(
            host == domain or host.endswith("." + domain) for domain in request.include_domains
        ):
            rejected += 1
            continue
        title = plain_text(item.get("title", ""), 300) if isinstance(item.get("title"), str) else ""
        if not title:
            rejected += 1
            continue
        snippet = item.get("snippet" if source == "news" else "description")
        if len(results) < request.max_results:
            results.append(
                SearchItem(
                    title=title,
                    url=item["url"],
                    snippet=plain_text(snippet, 1000) or None if isinstance(snippet, str) else None,
                    published_at=publication_date(item.get("date")),
                )
            )
    audit.emit("normalized", str(call_id), result_count=len(results), rejected_count=rejected)
    if not results and (rejected or warned):
        raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
    return SearchBatch(results, partial=bool(rejected or warned))


async def get_page(
    key: str,
    url: str,
    audit: AuditWriter,
    call_id: UUID,
    clock: Clock,
    *,
    max_characters: int = 20000,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PageResponse:
    """Submit an anonymous public URL with explicit safe overrides and return only text."""
    async with asyncio.timeout(40):
        authorized_url = await authorize_url(url, audit, call_id)
        if urlsplit(authorized_url).path.lower().endswith((".pdf", ".zip", ".doc", ".docx")):
            raise SearchError(ErrorCode.UNSUPPORTED_CONTENT)
        data, warned = await _post(
            "/v2/scrape",
            {
                "url": authorized_url,
                "formats": ["html"],
                "onlyMainContent": True,
                "skipTlsVerification": False,
                "maxAge": 0,
                "storeInCache": False,
                "proxy": "basic",
                "parsers": [],
                "actions": [],
                "headers": {},
                "timeout": 20000,
                "waitFor": 0,
            },
            key,
            audit,
            call_id,
            transport,
        )
        if warned or data.get("warning"):
            raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
        status = metadata.get("statusCode")
        if type(status) is not int:
            raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
        if status in {401, 403, 407, 451}:
            raise SearchError(ErrorCode.ACCESS_DENIED)
        if status == 429:
            raise SearchError(ErrorCode.RATE_LIMITED)
        if not 200 <= status < 300 or metadata.get("error"):
            raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
        media_type = metadata.get("contentType", "")
        media_type = (
            media_type.split(";", 1)[0].strip().lower() if isinstance(media_type, str) else ""
        )
        if media_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
            raise SearchError(ErrorCode.UNSUPPORTED_CONTENT)
        final_url = metadata.get("url")
        if final_url is not None:
            if not isinstance(final_url, str):
                raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE)
            final_url = await authorize_url(final_url, audit, call_id)
        content = data.get("html")
        if not isinstance(content, str):
            raise SearchError(ErrorCode.NO_CONTENT)
        if media_type == "text/plain":
            text = " ".join(
                "".join(
                    char
                    for char in content
                    if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
                ).split()
            )
        else:
            # One character past the cap is all it takes to detect truncation below.
            text = plain_text(content, max_characters + 1)
        if not text:
            raise SearchError(ErrorCode.NO_CONTENT)
        truncated = len(text) > max_characters
        text = text[:max_characters]
        title = metadata.get("title")
        return PageResponse(
            call_id=call_id,
            requested_url=url,
            final_url=final_url,
            title=plain_text(title, 300) if isinstance(title, str) else "",
            text=text,
            media_type=media_type,
            retrieved_at=clock.now(),
            truncated=truncated,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
