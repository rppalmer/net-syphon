"""Calls audit before egress and release their resources after every outcome."""

import asyncio
import json

import httpx2 as httpx
import pytest


def events(root):
    return [
        json.loads(line)
        for path in (root / "logs").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


@pytest.mark.asyncio
async def test_missing_config_returns_safe_error_without_http(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    result = await SearchService(tmp_path / "private").call({"query": "python"})
    assert result.code == "not_configured" and not result.retryable
    assert [e["event"] for e in events(tmp_path / "private")] == ["call_start", "call_end"]


@pytest.mark.asyncio
async def test_validation_errors_and_unknown_tools_never_reach_http(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    requests = []
    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(requests.append))
    for arguments, name in [
        ({"query": "!!secret"}, "net_syphon_search_web"),
        ({"query": "python", "mode": "browser"}, "net_syphon_search_web"),
        ({"query": "python"}, "unknown"),
    ]:
        result = await service.call(arguments, tool_name=name)
        assert result.code == "invalid_input" and not result.retryable
    assert not requests
    assert "secret" not in json.dumps(events(tmp_path / "private"))


@pytest.mark.asyncio
async def test_success_has_call_id_timestamp_and_private_complete_audit(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    root = tmp_path / "private"

    def handler(request):
        assert [e["event"] for e in events(root)] == ["call_start", "outbound_start"]
        return httpx.Response(
            200, json={"results": [{"title": "Example", "url": "https://a.test/"}]}
        )

    result = await SearchService(root, transport=httpx.MockTransport(handler)).call(
        {"query": "secret"}
    )
    assert result.call_id.version == 4 and result.retrieved_at.utcoffset() is not None
    assert result.results[0].url == "https://a.test/"
    records = events(root)
    assert [e["event"] for e in records] == [
        "call_start",
        "outbound_start",
        "outbound_end",
        "normalized",
        "call_end",
    ]
    assert all(e["call_id"] == str(result.call_id) for e in records)
    assert "secret" not in json.dumps(records) and "a.test" not in json.dumps(records)


@pytest.mark.parametrize("failure_event", ["call_start", "outbound_start"])
@pytest.mark.asyncio
async def test_failed_pre_egress_audit_prevents_all_requests(
    tmp_path, monkeypatch, failure_event, capsys
):
    from net_syphon.audit import AuditError
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    requests = []
    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(requests.append))
    original_emit = service.audit.emit

    def emit(event, *args, **kwargs):
        if event == failure_event:
            raise AuditError
        original_emit(event, *args, **kwargs)

    monkeypatch.setattr(service.audit, "emit", emit)
    for _ in range(2):
        assert (await service.call({"query": "secret"})).code == "internal_error"
    assert requests == []
    captured = capsys.readouterr()
    assert captured.out == "" and "secret" not in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.asyncio
async def test_completion_audit_failure_returns_error_and_disables_further_egress(
    tmp_path, monkeypatch
):
    from net_syphon.audit import AuditError
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(handler))
    original_emit = service.audit.emit

    def emit(event, *args, **kwargs):
        if event == "call_end":
            raise AuditError
        original_emit(event, *args, **kwargs)

    monkeypatch.setattr(service.audit, "emit", emit)
    assert (await service.call({"query": "test"})).code == "internal_error"
    assert (await service.call({"query": "test"})).code == "internal_error"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_failure", [False, True])
async def test_busy_cancellation_and_next_call_release_lock_and_client(
    tmp_path, monkeypatch, audit_failure
):
    from net_syphon.audit import AuditError
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    started = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = WaitingStream()
    responses = [
        httpx.Response(200, headers={"content-type": "application/json"}, stream=stream),
        httpx.Response(200, json={"results": []}),
    ]
    service = SearchService(
        tmp_path / "private", transport=httpx.MockTransport(lambda r: responses.pop(0))
    )
    task = asyncio.create_task(service.call({"query": "python"}))
    await asyncio.wait_for(started.wait(), 2)
    assert (await service.call({"query": "python"})).code == "busy"
    if audit_failure:
        original_emit = service.audit.emit

        def emit(event, *args, **kwargs):
            if event == "outbound_end":
                raise AuditError
            original_emit(event, *args, **kwargs)

        monkeypatch.setattr(service.audit, "emit", emit)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    followup = await service.call({"query": "python"})
    if audit_failure:
        assert followup.code == "internal_error"
        assert len(responses) == 1
    else:
        assert followup.results == []
        assert any(e["event"] == "cancelled" for e in events(tmp_path / "private"))


@pytest.mark.asyncio
async def test_unexpected_exception_cannot_leak_input(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")

    def handler(request):
        raise RuntimeError("searxng private credential and query")

    service = SearchService(tmp_path / "private", transport=httpx.MockTransport(handler))
    result = await service.call({"query": "secret"})
    assert result.code == "internal_error"
    assert "private" not in result.message and "searxng" not in result.message
    assert "credential" not in json.dumps(events(tmp_path / "private"))


@pytest.mark.asyncio
async def test_search_timestamp_comes_from_the_injected_clock(tmp_path, monkeypatch):
    """One injected clock reaches the response, so a timestamp is reproducible in a test."""
    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://search.invalid")
    moment = datetime(2026, 9, 7, 12, 30, tzinfo=UTC)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"results": [{"title": "Example", "url": "https://a.test/"}]}
        )
    )
    service = SearchService(tmp_path / "private", transport=transport, clock=FixedClock(moment))
    result = await service.call({"query": "python"})
    assert result.retrieved_at == moment


@pytest.mark.asyncio
async def test_page_timestamp_comes_from_the_injected_clock(tmp_path, monkeypatch):
    """Retrieval reads the same injected clock rather than calling the system one itself."""
    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock
    from net_syphon.service import SearchService

    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "test-key")
    moment = datetime(2026, 9, 7, 12, 30, tzinfo=UTC)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "html": "<p>Body</p>",
                    "metadata": {"statusCode": 200, "contentType": "text/html"},
                },
            },
        )
    )
    service = SearchService(tmp_path / "private", transport=transport, clock=FixedClock(moment))
    result = await service.call({"urls": ["https://8.8.8.8/"]}, tool_name="net_syphon_get_pages")
    assert result.results[0].page.retrieved_at == moment


def test_only_the_clock_module_reads_the_system_clock():
    """The seam rots silently unless something fails when a new module reads the wall clock."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src/net_syphon"
    offenders = [
        path.name
        for path in package.glob("*.py")
        if path.name != "clock.py" and "datetime.now" in path.read_text()
    ]
    assert offenders == []
