"""An explicit operator-run contract against the existing SearXNG instance."""

from pathlib import Path

import pytest


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_search_contract():
    from net_syphon.contracts import ErrorResponse
    from net_syphon.service import SearchService

    result = await SearchService(Path.home() / ".net-syphon").call({"query": "Python type hints"})
    if isinstance(result, ErrorResponse):
        pytest.fail(
            f"{result.code.value}: {result.message} call_id={result.call_id}", pytrace=False
        )
    assert result.results, "Expected results for the harmless live contract query"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_retrieval_contract():
    from net_syphon.config import load_settings
    from net_syphon.contracts import ErrorResponse
    from net_syphon.service import SearchService

    root = Path.home() / ".net-syphon"
    if load_settings(root).firecrawl_api_key is None:
        pytest.skip("Retrieval key not configured")
    service = SearchService(root)
    for arguments, name in [
        (
            {"query": "Python documentation", "include_domains": ["python.org"]},
            "net_syphon_search_web",
        ),
        ({"urls": ["https://example.com/"]}, "net_syphon_get_pages"),
    ]:
        result = await service.call(arguments, tool_name=name)
        if isinstance(result, ErrorResponse):
            pytest.fail(
                f"{result.code.value}: {result.message} call_id={result.call_id}", pytrace=False
            )
        if name == "net_syphon_get_pages":
            page = result.results[0].page
            assert page is not None and page.text and page.media_type == "text/html"
        else:
            assert result.results


@pytest.mark.live
@pytest.mark.asyncio
async def test_news_search_carries_usable_dates(capsys):
    """Guards the finding of 2026-09-07: news dates arrive as relative labels.

    Asserts the property a consumer depends on — that a news result reaches it
    with a date it can compare — not the label format, which the provider owns.
    """
    from net_syphon.config import load_settings
    from net_syphon.contracts import ErrorResponse
    from net_syphon.service import SearchService

    root = Path.home() / ".net-syphon"
    if load_settings(root).firecrawl_api_key is None:
        pytest.skip("Retrieval key not configured")

    result = await SearchService(root).call(
        {"query": "artificial intelligence", "search_category": "news", "max_results": 5}
    )
    if isinstance(result, ErrorResponse):
        pytest.fail(f"{result.code.value}: {result.message}", pytrace=False)

    dated = [item for item in result.results if item.published_at]
    with capsys.disabled():
        print(f"\n  news results: {len(result.results)}, with a date: {len(dated)}")
    assert result.results, "news search returned nothing to judge"
    # Zero coverage is the regression this exists to catch.
    assert dated, "no news result carried a usable date; check the provider's date field"
