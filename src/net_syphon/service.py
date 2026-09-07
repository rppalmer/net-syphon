"""Audited search and retrieval calls, independent of MCP transport details."""

import asyncio
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx2 as httpx
from pydantic import ValidationError

from net_syphon import firecrawl, searxng
from net_syphon.audit import AuditError, AuditWriter
from net_syphon.clock import SYSTEM_CLOCK, Clock
from net_syphon.config import ConfigurationError, load_settings
from net_syphon.contracts import (
    ErrorCode,
    ErrorResponse,
    PageOutcome,
    PagesRequest,
    PagesResponse,
    SearchError,
    SearchRequest,
    SearchResponse,
    error_response,
)

TOOL_NAME = "net_syphon_search_web"
PAGES_TOOL_NAME = "net_syphon_get_pages"
BATCH_TIMEOUT = 180


class SearchService:
    def __init__(
        self,
        root: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.root = root
        self.clock = clock
        self.audit = AuditWriter(root, clock=clock)
        self.transport = transport
        self._lock = asyncio.Lock()
        self._audit_failed = False

    def _disable_egress(self) -> None:
        if not self._audit_failed:
            self._audit_failed = True
            # Never log exceptions or caller-controlled text, even on this emergency path.
            print(
                "Net-Syphon audit unavailable; egress disabled. "
                "Check private logs and permissions.",
                file=sys.stderr,
            )

    async def _get_pages(self, request: PagesRequest, key: str, call_id: UUID) -> PagesResponse:
        """Preserve completed results when a later page exhausts the batch budget."""
        outcomes = []
        character_limit = min(20000, 40000 // len(request.urls))
        deadline = time.monotonic() + BATCH_TIMEOUT
        for index, url in enumerate(request.urls, 1):
            child_id = uuid4()
            self.audit.emit(
                "page_start", str(child_id), parent_call_id=str(call_id), page_index=index
            )
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    page = await firecrawl.get_page(
                        key,
                        url,
                        self.audit,
                        child_id,
                        self.clock,
                        max_characters=character_limit,
                        transport=self.transport,
                    )
                outcome = PageOutcome(index=index, page=page)
            except SearchError as exc:
                outcome = PageOutcome(index=index, error=error_response(child_id, exc.code))
            except TimeoutError:
                outcome = PageOutcome(
                    index=index, error=error_response(child_id, ErrorCode.TIMEOUT)
                )
            except asyncio.CancelledError:
                self.audit.emit(
                    "page_end",
                    str(child_id),
                    parent_call_id=str(call_id),
                    page_index=index,
                    status="cancelled",
                )
                raise
            outcomes.append(outcome)
            self.audit.emit(
                "page_end",
                str(child_id),
                parent_call_id=str(call_id),
                page_index=index,
                status="error" if outcome.error else "success",
                code=outcome.error.code.value if outcome.error else None,
                text_characters=len(outcome.page.text) if outcome.page else 0,
            )
        return PagesResponse(
            call_id=call_id,
            results=outcomes,
            partial=any(item.error is not None for item in outcomes),
        )

    async def call(
        self,
        arguments: object,
        *,
        tool_name: str = TOOL_NAME,
    ) -> SearchResponse | PagesResponse | ErrorResponse:
        """Validate and audit every tool invocation, including rejected and cancelled calls."""
        call_id = uuid4()
        started = time.monotonic()
        if self._audit_failed:
            return error_response(call_id, ErrorCode.INTERNAL_ERROR)
        result: SearchResponse | PagesResponse | ErrorResponse
        try:
            query = arguments.get("query") if isinstance(arguments, dict) else None
            self.audit.emit(
                "call_start",
                str(call_id),
                query_length=len(query) if isinstance(query, str) else None,
            )
            try:
                contract = {
                    TOOL_NAME: SearchRequest,
                    PAGES_TOOL_NAME: PagesRequest,
                }.get(tool_name)
                if contract is None:
                    raise SearchError(ErrorCode.INVALID_INPUT)
                request = contract.model_validate(arguments)
            except (ValidationError, SearchError):
                self.audit.emit("validation_rejected", str(call_id), code="invalid_input")
                raise SearchError(ErrorCode.INVALID_INPUT) from None
            # No await occurs between checking and acquiring this asyncio lock.
            if self._lock.locked():
                raise SearchError(ErrorCode.BUSY)
            async with self._lock:
                try:
                    settings = load_settings(self.root)
                except ConfigurationError:
                    self.audit.emit("configuration_rejected", str(call_id), code="not_configured")
                    raise SearchError(ErrorCode.NOT_CONFIGURED) from None
                if isinstance(request, SearchRequest):
                    if request.requires_filtered_search:
                        if settings.firecrawl_api_key is None:
                            raise SearchError(ErrorCode.NOT_CONFIGURED)
                        batch = await firecrawl.search(
                            settings.firecrawl_api_key.get_secret_value(),
                            request,
                            self.audit,
                            call_id,
                            self.clock,
                            transport=self.transport,
                        )
                    else:
                        if settings.search_endpoint is None:
                            raise SearchError(ErrorCode.NOT_CONFIGURED)
                        batch = await searxng.search(
                            settings.search_endpoint,
                            request,
                            self.audit,
                            call_id,
                            transport=self.transport,
                        )
                    result = SearchResponse(
                        call_id=call_id,
                        retrieved_at=self.clock.now(),
                        results=batch.results,
                        partial=batch.partial,
                    )
                else:
                    if settings.firecrawl_api_key is None:
                        raise SearchError(ErrorCode.NOT_CONFIGURED)
                    key = settings.firecrawl_api_key.get_secret_value()
                    result = await self._get_pages(request, key, call_id)
        except SearchError as exc:
            result = error_response(call_id, exc.code)
        except TimeoutError:
            result = error_response(call_id, ErrorCode.TIMEOUT)
        except AuditError:
            self._disable_egress()
            # A failed audit in the HTTP finally block must not swallow caller cancellation.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            return error_response(call_id, ErrorCode.INTERNAL_ERROR)
        except asyncio.CancelledError:
            try:
                self.audit.emit("cancelled", str(call_id), status="cancelled")
                self.audit.emit(
                    "call_end",
                    str(call_id),
                    status="cancelled",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except AuditError:
                self._disable_egress()
            raise
        except Exception:
            result = error_response(call_id, ErrorCode.INTERNAL_ERROR)
        try:
            failed = isinstance(result, ErrorResponse)
            self.audit.emit(
                "call_end",
                str(call_id),
                status="error"
                if failed
                else "partial"
                if getattr(result, "partial", False)
                else "success",
                code=result.code.value if failed else None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except AuditError:
            self._disable_egress()
            return error_response(call_id, ErrorCode.INTERNAL_ERROR)
        return result
