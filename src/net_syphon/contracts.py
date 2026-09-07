"""Provider-neutral contracts and conservative input/display normalization."""

import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_CONFIGURED = "not_configured"
    BUSY = "busy"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    TOO_LARGE = "too_large"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INTERNAL_ERROR = "internal_error"
    POLICY_BLOCKED = "policy_blocked"
    UNSUPPORTED_CONTENT = "unsupported_content"
    NO_CONTENT = "no_content"
    ENGINES_UNAVAILABLE = "engines_unavailable"


_ERRORS = {
    ErrorCode.POLICY_BLOCKED: ("The URL is not permitted by the retrieval policy.", False),
    ErrorCode.UNSUPPORTED_CONTENT: ("Only public HTML or plain-text pages are supported.", False),
    ErrorCode.NO_CONTENT: ("The page contained no usable text.", False),
    ErrorCode.ENGINES_UNAVAILABLE: (
        "The search service reported engine failures and no usable results.",
        True,
    ),
    ErrorCode.INVALID_INPUT: ("Invalid arguments. Check the tool input schema.", False),
    ErrorCode.NOT_CONFIGURED: (
        "This capability is not configured. Ask the operator to check settings.",
        False,
    ),
    ErrorCode.BUSY: ("Another request is active in this server process.", True),
    ErrorCode.TIMEOUT: ("The request exceeded its deadline.", True),
    ErrorCode.RATE_LIMITED: ("The service is rate limited or its allowance is exhausted.", True),
    ErrorCode.ACCESS_DENIED: ("Access was denied.", False),
    ErrorCode.TOO_LARGE: ("The response exceeded the allowed size.", False),
    ErrorCode.UPSTREAM_UNAVAILABLE: (
        "The upstream service returned an unusable response or could not be reached.",
        True,
    ),
    ErrorCode.INTERNAL_ERROR: (
        "The request could not complete safely. Ask the operator to check logs.",
        False,
    ),
}


class SearchError(Exception):
    """Carry a fixed classification, never upstream exception text."""

    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(_ERRORS[code][0])


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SearchRequest(Contract):
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Plain text; no control characters, bangs, language or timeout modifiers.",
    )
    max_results: int = Field(default=5, ge=1, le=10)
    search_category: Literal["general", "news"] = "general"
    include_domains: list[str] = Field(default_factory=list, max_length=10)
    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("include_domains")
    @classmethod
    def validate_domains(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > 253 or not re.fullmatch(
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                value,
            ):
                raise ValueError("Domains must be hostnames without paths or operators")
        return [value.lower() for value in values]

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "SearchRequest":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Both dates are required")
        if self.start_date is not None and (
            self.start_date >= self.end_date or self.time_range is not None
        ):
            raise ValueError("Choose either an increasing date range or a relative time range")
        return self

    @property
    def requires_filtered_search(self) -> bool:
        return bool(
            self.search_category == "news"
            or self.include_domains
            or self.time_range
            or self.start_date
        )

    @field_validator("query", mode="before")
    @classmethod
    def validate_query(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
            raise ValueError("Control characters are not allowed")
        value = value.strip()
        if any(
            (word.startswith("!") and word != "!=")
            or word.startswith(":")
            or (word.startswith("<") and word[1:].isdigit())
            for word in value.split()
        ):
            raise ValueError("Search modifiers are not allowed")
        return value


class SearchItem(Contract):
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2048)
    snippet: str | None = Field(default=None, max_length=1000)
    published_at: str | None = Field(
        default=None,
        max_length=40,
        description=(
            "ISO 8601, when the provider supplies a date. Null when it supplies none, "
            "or a relative label such as '2 days ago', which is reported as unknown "
            "rather than guessed at. Never treat null as recent or old."
        ),
    )


class SearchResponse(Contract):
    call_id: UUID
    retrieved_at: datetime
    results: list[SearchItem] = Field(max_length=10)
    partial: bool


class ErrorResponse(Contract):
    call_id: UUID
    code: ErrorCode
    message: str
    retryable: bool


class PagesRequest(Contract):
    urls: list[str] = Field(min_length=1, max_length=5)

    @field_validator("urls")
    @classmethod
    def bounded_urls(cls, values: list[str]) -> list[str]:
        if any(not 1 <= len(value) <= 2048 for value in values):
            raise ValueError("URLs must contain 1 to 2048 characters")
        return values


class PageResponse(Contract):
    call_id: UUID
    requested_url: str = Field(max_length=2048)
    final_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Reported destination, not an independently observed redirect chain.",
    )
    title: str = Field(max_length=300)
    text: str = Field(min_length=1, max_length=20000)
    media_type: Literal["text/html", "text/plain", "application/xhtml+xml"]
    retrieved_at: datetime
    truncated: bool
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PageOutcome(Contract):
    index: int = Field(ge=1, le=5)
    page: PageResponse | None = None
    error: ErrorResponse | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> "PageOutcome":
        if (self.page is None) == (self.error is None):
            raise ValueError("Exactly one page or error is required")
        return self


class PagesResponse(Contract):
    call_id: UUID
    results: list[PageOutcome] = Field(min_length=1, max_length=5)
    partial: bool


def error_response(call_id: UUID, code: ErrorCode) -> ErrorResponse:
    message, retryable = _ERRORS[code]
    return ErrorResponse(call_id=call_id, code=code, message=message, retryable=retryable)


def valid_web_url(value: object) -> bool:
    """Check syntax only. This does not authorize fetching the returned URL."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return False
    if "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        # Percent escapes and delimiter characters have no place in a hostname.
        if any(char in parsed.hostname for char in '%<>"{}|^'):
            return False
        parsed.hostname.encode("idna")
    except (ValueError, UnicodeError):
        return False
    return True


def publication_date(value: object) -> str | None:
    """Normalize an upstream publication label, or report it as unknown.

    Providers disagree about this field and some omit it entirely. Only an ISO
    8601 value is accepted; a relative label such as "2 days ago" is not a date
    and is reported as unknown rather than guessed at.
    """
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.date().isoformat() if len(value) == 10 else parsed.isoformat()


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, limit: int) -> str:
    """Remove markup, collapse whitespace, and bound text; consumers must still escape rendering."""
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    text = " ".join(parser.parts)
    text = "".join(
        char
        for char in text
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(text.split())[:limit]
