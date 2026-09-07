"""Exercise the adapter through a real HTTP client with an in-memory upstream."""

import asyncio
import gzip
import json
from urllib.parse import parse_qs
from uuid import uuid4

import httpx2 as httpx
import pytest


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, delay: float = 0):
        self.body = body
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), 512):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.body[offset : offset + 512]

    async def aclose(self):
        self.closed = True


def upstream(payload, *, status=200, headers=None, delay=0):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    stream = ByteStream(body, delay=delay)
    response = httpx.Response(
        status, headers={"content-type": "application/json", **(headers or {})}, stream=stream
    )
    return response, stream


async def fetch(tmp_path, handler, arguments=None):
    from net_syphon.audit import AuditWriter
    from net_syphon.contracts import SearchRequest
    from net_syphon.searxng import search

    return await search(
        "https://search.example.org/search",
        SearchRequest.model_validate(arguments or {"query": "python"}),
        AuditWriter(tmp_path / "private"),
        uuid4(),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_exact_request_and_normalized_partial_results(tmp_path):
    requests = []
    response, stream = upstream(
        {
            "results": [
                {
                    "title": "<b>First</b>",
                    "url": "https://one.example/",
                    "content": "<i>Preview</i>",
                    "engine": "secret_backend",
                    "publishedDate": "2026-08-15T12:00:00Z",
                },
                {"title": "Unsafe", "url": "file:///etc/passwd"},
                {"title": "Third", "url": "https://three.example/", "content": None},
            ],
            "unresponsive_engines": [["secret_backend", "failure with secret"]],
            "answers": ["Never expose this"],
        }
    )

    def handler(request):
        requests.append(request)
        return response

    batch = await fetch(tmp_path, handler, {"query": "test & python", "max_results": 2})
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://search.example.org/search"
    assert parse_qs(request.content.decode()) == {
        "q": ["test & python"],
        "format": ["json"],
        "categories": ["general"],
        "pageno": ["1"],
    }
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept-encoding"] == "gzip, identity"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert [item.url for item in batch.results] == [
        "https://one.example/",
        "https://three.example/",
    ]
    assert batch.results[0].title == "First"
    assert batch.results[0].snippet == "Preview"
    assert batch.results[0].published_at == "2026-08-15T12:00:00+00:00"
    assert batch.results[1].snippet is None
    assert batch.partial and stream.closed
    records = [
        json.loads(line)
        for path in (tmp_path / "private/logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [r["event"] for r in records] == ["outbound_start", "outbound_end", "normalized"]
    assert "secret_backend" not in str(records) and "test & python" not in str(records)


@pytest.mark.asyncio
async def test_empty_results_are_success_not_provider_failure(tmp_path):
    response, _ = upstream({"results": []})
    batch = await fetch(tmp_path, lambda request: response)
    assert batch.results == [] and not batch.partial


@pytest.mark.asyncio
async def test_bounds_fields_and_result_count(tmp_path):
    response, _ = upstream(
        {
            "results": [
                {
                    "title": "x" * 301,
                    "url": f"https://example.org/{i}",
                    "content": "y" * 1001,
                    "publishedDate": "not a date",
                }
                for i in range(15)
            ]
        }
    )
    batch = await fetch(tmp_path, lambda request: response)
    assert len(batch.results) == 5
    assert all(
        len(item.title) == 300 and len(item.snippet) == 1000 and item.published_at is None
        for item in batch.results
    )
    assert not batch.partial


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "upstream_unavailable"),
        (401, "access_denied"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (503, "upstream_unavailable"),
        (204, "upstream_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_status_errors_never_redirect_or_retry(tmp_path, status, code):
    from net_syphon.contracts import SearchError

    requests = []
    response, stream = upstream({}, status=status, headers={"location": "http://127.0.0.1/secret"})

    def handler(request):
        requests.append(request)
        return response

    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, handler)
    assert caught.value.code == code and len(requests) == 1
    assert stream.closed


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [],
        {},
        {"results": None},
        {"results": [{"url": "https://example.org"}]},
    ],
)
@pytest.mark.asyncio
async def test_rejects_unusable_payloads(tmp_path, payload):
    from net_syphon.contracts import SearchError

    response, _ = upstream(payload)
    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, lambda request: response)
    assert caught.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_engine_failure_is_distinct_and_audits_only_categories(tmp_path):
    from net_syphon.contracts import SearchError

    response, _ = upstream(
        {
            "results": [],
            "unresponsive_engines": [
                ["private-engine", "CAPTCHA"],
                ["other", "Too many requests"],
            ],
        }
    )
    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, lambda request: response)
    assert caught.value.code == "engines_unavailable"
    records = [
        json.loads(line)
        for path in (tmp_path / "private/logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert records[-1]["upstream_captcha_count"] == 1
    assert records[-1]["upstream_rate_limited_count"] == 1
    assert "private-engine" not in json.dumps(records)


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.asyncio
async def test_streaming_size_limit_including_decompression_bombs(tmp_path, compressed):
    from net_syphon.contracts import SearchError

    body = b" " * (1024 * 1024 + 1)
    response, stream = upstream(
        gzip.compress(body) if compressed else body,
        headers={"content-encoding": "gzip"} if compressed else {},
    )
    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, lambda request: response)
    assert caught.value.code == "too_large" and stream.closed


@pytest.mark.asyncio
async def test_valid_compression_and_bad_encoding(tmp_path):
    from net_syphon.contracts import SearchError

    response, _ = upstream(gzip.compress(b'{"results":[]}'), headers={"content-encoding": "gzip"})
    assert (await fetch(tmp_path, lambda request: response)).results == []
    response, _ = upstream(b"garbage", headers={"content-encoding": "br"})
    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, lambda request: response)
    assert caught.value.code == "upstream_unavailable"


@pytest.mark.asyncio
async def test_total_deadline_closes_slow_stream(tmp_path, monkeypatch):
    from net_syphon import searxng
    from net_syphon.contracts import SearchError

    monkeypatch.setattr(searxng, "REQUEST_TIMEOUT", 0.02)
    response, stream = upstream(b" " * 2048, delay=0.01)
    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, lambda request: response)
    assert caught.value.code == "timeout" and stream.closed


@pytest.mark.asyncio
async def test_network_failure_is_safe_and_single_attempt(tmp_path):
    from net_syphon.contracts import SearchError

    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("secret query and endpoint")

    with pytest.raises(SearchError) as caught:
        await fetch(tmp_path, handler)
    assert caught.value.code == "upstream_unavailable" and len(requests) == 1
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_junk_beyond_the_requested_limit_is_not_reported_as_partial(tmp_path):
    """`partial` means the caller lost something, not that the payload had junk in it.

    SearXNG returns far more results than were asked for. Malformed entries past
    the limit were never going to be returned, so they cost the caller nothing.
    """
    response, _ = upstream(
        {
            "results": [{"title": f"Good {i}", "url": f"https://ok{i}.example/"} for i in range(5)]
            + [
                {"title": "Junk", "url": "javascript:alert(1)"},
                {"title": "", "url": "https://notitle.example/"},
            ],
            "unresponsive_engines": [],
        }
    )
    batch = await fetch(tmp_path, lambda request: response, {"query": "x", "max_results": 5})

    assert len(batch.results) == 5
    assert batch.partial is False


@pytest.mark.asyncio
async def test_a_genuine_shortfall_is_still_reported_as_partial(tmp_path):
    """Rejections that leave the caller short of what it asked for still count."""
    response, _ = upstream(
        {
            "results": [
                {"title": "Good", "url": "https://ok.example/"},
                {"title": "Junk", "url": "javascript:alert(1)"},
            ],
            "unresponsive_engines": [],
        }
    )
    batch = await fetch(tmp_path, lambda request: response, {"query": "x", "max_results": 5})

    assert len(batch.results) == 1
    assert batch.partial is True
