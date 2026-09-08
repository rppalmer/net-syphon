"""Retrieval must not disclose private targets or expand provider capabilities."""

import asyncio
import hashlib
import json

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from net_syphon.contracts import ErrorResponse, SearchRequest
from net_syphon.service import SearchService


async def one_page(service, url):
    """Retrieve a single URL through the batch tool and unwrap its only outcome."""
    result = await service.call({"urls": [url]}, tool_name="net_syphon_get_pages")
    if isinstance(result, ErrorResponse):
        return result
    outcome = result.results[0]
    return outcome.page if outcome.page is not None else outcome.error


def test_filtered_search_contract():
    request = SearchRequest.model_validate(
        {
            "query": "release",
            "search_category": "news",
            "include_domains": ["example.org"],
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
        }
    )
    assert request.start_date.isoformat() == "2026-09-01"
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "x", "start_date": "2026-09-01"})


@pytest.mark.parametrize("items", [[], [{"title": "Result", "url": "https://example.org/"}]])
@pytest.mark.asyncio
async def test_search_warning_is_not_silent_success(tmp_path, monkeypatch, items):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"success": True, "warning": "PRIVATE WARNING", "data": {"web": items}}
        )
    )
    result = await SearchService(tmp_path / "private", transport=transport).call(
        {"query": "x", "time_range": "week"}
    )
    if items:
        assert result.partial is True
    else:
        assert isinstance(result, ErrorResponse)
        assert result.code.value == "upstream_unavailable"
    logs = "".join(p.read_text() for p in (tmp_path / "private/logs").glob("*.jsonl"))
    assert "PRIVATE WARNING" not in logs + result.model_dump_json()


@pytest.mark.asyncio
async def test_scrape_data_warning_is_not_silent_success(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "warning": "PRIVATE WARNING",
                    "html": "Page",
                    "metadata": {"statusCode": 200, "contentType": "text/html"},
                },
            },
        )
    )
    result = await one_page(
        SearchService(tmp_path / "private", transport=transport), "https://8.8.8.8/"
    )
    assert isinstance(result, ErrorResponse)
    assert result.code.value == "upstream_unavailable"
    logs = "".join(p.read_text() for p in (tmp_path / "private/logs").glob("*.jsonl"))
    assert "PRIVATE WARNING" not in logs + result.model_dump_json()


@pytest.mark.asyncio
async def test_private_page_is_blocked_before_http(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")

    def unexpected(request):
        pytest.fail("Private target reached provider")

    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(unexpected))
    result = await one_page(service, "http://127.0.0.1/secret")
    assert isinstance(result, ErrorResponse)
    assert result.code.value == "policy_blocked"


@pytest.mark.asyncio
async def test_page_safe_payload_and_plain_text(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")

    def respond(request):
        assert str(request.url) == "https://api.firecrawl.dev/v2/scrape"
        payload = json.loads(request.content)
        assert payload["skipTlsVerification"] is False
        assert payload["storeInCache"] is False
        assert payload["maxAge"] == 0
        assert payload["proxy"] == "basic"
        assert payload["parsers"] == [] and payload["actions"] == []
        assert payload["formats"] == ["html"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "html": "<h1>Heading</h1><script>bad()</script><p>Body</p>",
                    "metadata": {
                        "title": "Heading",
                        "url": "https://8.8.8.8/",
                        "statusCode": 200,
                        "contentType": "text/html",
                    },
                },
            },
        )

    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(respond))
    result = await one_page(service, "https://8.8.8.8/")
    assert not isinstance(result, ErrorResponse), result
    assert "Body" in result.text and "bad()" not in result.text
    assert result.text_sha256 == hashlib.sha256(result.text.encode()).hexdigest()
    logs = "".join(p.read_text() for p in (tmp_path / "private/logs").glob("*.jsonl"))
    assert "test-key" not in logs and "Heading" not in logs and "8.8.8.8" not in logs


@pytest.mark.asyncio
async def test_batch_keeps_order_and_limits_total_text(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")

    def respond(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "html": "<p>" + "a" * 30000 + "</p>",
                    "metadata": {"statusCode": 200, "contentType": "text/html"},
                },
            },
        )

    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(respond))
    result = await service.call(
        {"urls": ["https://8.8.8.8/", "http://127.0.0.1/", "https://1.1.1.1/"]},
        tool_name="net_syphon_get_pages",
    )
    assert not isinstance(result, ErrorResponse), result
    assert result.partial is True
    assert result.results[1].error.code.value == "policy_blocked"
    assert result.results[0].page.requested_url == "https://8.8.8.8/"
    assert sum(len(item.page.text) for item in result.results if item.page) <= 40000


@pytest.mark.asyncio
async def test_filtered_search_routes_once_and_validates_domains(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://api.firecrawl.dev/v2/search"
        payload = json.loads(request.content)
        assert payload["sources"] == ["news"]
        assert payload["includeDomains"] == ["example.org"]
        assert payload["tbs"] == "cdr:1,cd_min:09/01/2026,cd_max:09/01/2026"
        assert "scrapeOptions" not in payload
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "news": [
                        {"url": "https://notexample.org/a", "title": "Reject"},
                        {
                            "url": "https://news.example.org/a",
                            "title": "Keep",
                            "date": "2 hours ago",
                            "snippet": "Text",
                        },
                    ]
                },
            },
        )

    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock

    result = await SearchService(
        tmp_path / "private",
        transport=httpx.MockTransport(respond),
        clock=FixedClock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC)),
    ).call(
        {
            "query": "news",
            "search_category": "news",
            "include_domains": ["example.org"],
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
        }
    )
    assert result.partial and len(result.results) == 1
    assert result.results[0].title == "Keep"
    # The provider's relative label reaches the caller as a date it can compare.
    assert result.results[0].published_at == "2026-09-07T10:00:00+00:00"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://[::1]/",
        "http://[::ffff:8.8.8.8]/",
        "http://100.64.0.1/",
        "http://224.0.0.1/",
        "http://8.8.8.8:8080/",
        "https://u:p@8.8.8.8/",
        "http://host.local/",
        "http://8.8.8.8./",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_targets_never_reach_provider(tmp_path, monkeypatch, url):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")

    def unexpected(request):
        pytest.fail("Unsafe URL reached HTTP")

    result = await one_page(
        SearchService(tmp_path / "private", transport=httpx.MockTransport(unexpected)), url
    )
    assert result.code.value == "policy_blocked"


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "upstream_unavailable"),
        (401, "access_denied"),
        (402, "rate_limited"),
        (429, "rate_limited"),
        (503, "upstream_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_hosted_errors_never_retry_or_fallback(tmp_path, monkeypatch, status, code):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"location": "http://127.0.0.1/"}, json={"error": "SECRET"}
        )

    result = await SearchService(tmp_path / "private", transport=httpx.MockTransport(respond)).call(
        {"query": "x", "time_range": "week"}
    )
    assert result.code.value == code and len(requests) == 1
    assert "SECRET" not in result.message


@pytest.mark.asyncio
async def test_mixed_dns_is_blocked(tmp_path, monkeypatch):
    import socket
    from uuid import uuid4

    from net_syphon.audit import AuditWriter
    from net_syphon.contracts import SearchError
    from net_syphon.policy import authorize_url

    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in ["8.8.8.8", "127.0.0.1"]
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(SearchError) as exc:
        await authorize_url("https://example.org/", AuditWriter(tmp_path / "private"), uuid4())
    assert exc.value.code.value == "policy_blocked"


@pytest.mark.asyncio
async def test_retrieval_cancellation_closes_response_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    started = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = Waiting()
    service = SearchService(
        tmp_path / "private",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    )
    task = asyncio.create_task(one_page(service, "https://8.8.8.8/"))
    await asyncio.wait_for(started.wait(), 2)
    busy = await one_page(service, "https://8.8.8.8/")
    assert busy.code.value == "busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and not service._lock.locked()


@pytest.mark.asyncio
async def test_batch_deadline_preserves_completed_pages(tmp_path, monkeypatch):
    import net_syphon.service as module

    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(module, "BATCH_TIMEOUT", 0.05, raising=False)
    count = 0

    async def respond(request):
        nonlocal count
        count += 1
        if count > 1:
            await asyncio.sleep(1)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "html": "<p>First</p>",
                    "metadata": {"statusCode": 200, "contentType": "text/html"},
                },
            },
        )

    result = await SearchService(tmp_path / "private", transport=httpx.MockTransport(respond)).call(
        {"urls": ["https://8.8.8.8/1", "https://8.8.8.8/2", "https://8.8.8.8/3"]},
        tool_name="net_syphon_get_pages",
    )
    assert result.results[0].page.text == "First"
    assert [item.error.code.value for item in result.results[1:]] == ["timeout", "timeout"]
    assert count == 2


@pytest.mark.asyncio
async def test_hosted_stream_limit_closes_response(tmp_path, monkeypatch):
    from test_search import upstream

    from net_syphon import firecrawl

    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(firecrawl, "MAX_RESPONSE_BYTES", 100)
    response, stream = upstream({"success": True, "data": {"web": [], "extra": "a" * 200}})
    result = await SearchService(
        tmp_path / "private", transport=httpx.MockTransport(lambda r: response)
    ).call({"query": "x", "time_range": "day"})
    assert result.code.value == "too_large" and stream.closed


@pytest.mark.asyncio
async def test_news_relative_dates_reach_the_consumer_as_absolute_dates(tmp_path, monkeypatch):
    """Live Firecrawl news returns labels like '1 day ago'; a consumer needs a date."""
    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock

    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "news": [
                        {"title": "Fresh", "url": "https://a.test/", "date": "59 minutes ago"},
                        {"title": "Older", "url": "https://b.test/", "date": "3 days ago"},
                        {"title": "Undated", "url": "https://c.test/"},
                    ]
                },
            },
        )
    )
    service = SearchService(
        tmp_path / "private",
        transport=transport,
        clock=FixedClock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC)),
    )

    result = await service.call({"query": "ai", "search_category": "news"})

    assert [item.published_at for item in result.results] == [
        "2026-09-07T11:01:00+00:00",
        "2026-09-04",
        None,
    ]


def news_payload(items):
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": {"news": items}})
    )


@pytest.mark.asyncio
async def test_hosted_search_junk_past_the_limit_is_not_partial(tmp_path, monkeypatch):
    """The hosted path must mean the same thing by `partial` as the ordinary one."""
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = news_payload(
        [{"title": f"Good {i}", "url": f"https://ok{i}.test/"} for i in range(3)]
        + [{"title": "Junk", "url": "javascript:alert(1)"}, {"title": "", "url": "https://b.test/"}]
    )

    result = await SearchService(tmp_path / "private", transport=transport).call(
        {"query": "x", "search_category": "news", "max_results": 3}
    )

    assert len(result.results) == 3
    assert result.partial is False


@pytest.mark.asyncio
async def test_hosted_search_reports_partial_when_it_comes_up_short(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = news_payload(
        [{"title": "Good", "url": "https://ok.test/"}, {"title": "Junk", "url": "not-a-url"}]
    )

    result = await SearchService(tmp_path / "private", transport=transport).call(
        {"query": "x", "search_category": "news", "max_results": 5}
    )

    assert len(result.results) == 1
    assert result.partial is True


@pytest.mark.asyncio
async def test_hosted_search_audit_counts_rejections_past_the_limit(tmp_path, monkeypatch):
    """Upstream health stays visible on this path too, even when nothing was lost."""
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = news_payload(
        [{"title": f"Good {i}", "url": f"https://ok{i}.test/"} for i in range(2)]
        + [{"title": "Junk", "url": "not-a-url"}]
    )

    result = await SearchService(tmp_path / "private", transport=transport).call(
        {"query": "x", "search_category": "news", "max_results": 2}
    )

    normalized = [
        json.loads(line)
        for path in (tmp_path / "private/logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
        if json.loads(line)["event"] == "normalized"
    ]
    assert result.partial is False
    assert normalized[0]["rejected_count"] == 1


@pytest.mark.asyncio
async def test_off_domain_results_count_as_a_loss_when_they_leave_it_short(tmp_path, monkeypatch):
    """A domain restriction that the provider ignores is a real loss, not noise."""
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    transport = news_payload(
        [
            {"title": "Wanted", "url": "https://news.example.org/a"},
            {"title": "Elsewhere", "url": "https://other.test/b"},
        ]
    )

    result = await SearchService(tmp_path / "private", transport=transport).call(
        {"query": "x", "search_category": "news", "include_domains": ["example.org"]}
    )

    assert [item.url for item in result.results] == ["https://news.example.org/a"]
    assert result.partial is True
