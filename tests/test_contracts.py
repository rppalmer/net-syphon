"""Input and presentation boundaries must not forward executable query syntax or markup."""

import pytest
from pydantic import ValidationError

from net_syphon.contracts import DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS


def test_default_request_trims_query_and_limits_results():
    from net_syphon.contracts import SearchRequest

    request = SearchRequest.model_validate({"query": "  python typing  "})
    assert request.query == "python typing"
    assert request.max_results == DEFAULT_SEARCH_RESULTS


def test_accepts_the_whole_documented_result_range():
    from net_syphon.contracts import SearchRequest

    for count in (1, DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS):
        request = SearchRequest.model_validate({"query": "python", "max_results": count})
        assert request.max_results == count


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": 123},
        {"query": "hello\nworld"},
        {"query": "hello\x1bworld"},
        {"query": "hello\u202eworld"},
        {"query": "hello\ud800"},
        {"query": "!google python"},
        {"query": "python !images"},
        {"query": "!!python"},
        {"query": "python !!"},
        {"query": "python :fr"},
        {"query": "python <3"},
        {"query": "python <850"},
        {"query": "python <٣"},
        {"query": "python", "engine": "google"},
        *[
            {"query": "python", "max_results": n}
            for n in (0, MAX_SEARCH_RESULTS + 1, True, 2.5, "3")
        ],
    ],
)
def test_rejects_invalid_or_provider_shaped_input(arguments):
    from net_syphon.contracts import SearchRequest

    with pytest.raises(ValidationError):
        SearchRequest.model_validate(arguments)


@pytest.mark.parametrize("query", ["C++", "site:python.org typing", "Hello!", "why != works"])
def test_keeps_ordinary_search_text(query):
    from net_syphon.contracts import SearchRequest

    assert SearchRequest(query=query).query == query


def test_plain_text_discards_active_markup_and_bounds_output():
    from net_syphon.contracts import plain_text

    assert plain_text("<b>Hello</b> &amp; <i>world</i>\x1b\u202e", 100) == "Hello & world"
    assert plain_text("a<script>secret()</script><style>body{}</style>b", 100) == "a b"
    assert plain_text("x" * 301, 300) == "x" * 300


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.org/",
        "https://example.org/\n",
        "https://example.org:0/",
        "https://example.org:bad/",
        "https://example.org/" + "a" * 2048,
        "https://example.org\\@evil.test/",
        "https://",
        "https://exa mple.org/",
        "https://example.org/%zz",
    ],
)
def test_result_url_rejects_ambiguous_or_non_web_values(url):
    from net_syphon.contracts import valid_web_url

    assert not valid_web_url(url)


def test_result_url_validation_does_not_require_dns_or_public_address():
    from net_syphon.contracts import valid_web_url

    assert valid_web_url("https://does-not-resolve.invalid/?q=hello%20world")
    assert valid_web_url("http://192.168.1.2:8080/path")


def test_search_items_carry_no_rank_field():
    """List order is the only ordering the contract states, so a rank cannot disagree with it."""
    from net_syphon.contracts import SearchItem

    assert "rank" not in SearchItem.model_fields


@pytest.mark.parametrize(
    "label,expected",
    [
        ("59 minutes ago", "2026-09-07T11:01:00+00:00"),
        ("2 hours ago", "2026-09-07T10:00:00+00:00"),
        ("1 day ago", "2026-09-06"),
        ("3 days ago", "2026-09-04"),
        ("yesterday", "2026-09-06"),
        ("1 week ago", "2026-08-31"),
        # Calendar months vary, so a month is approximated and documented as such.
        ("2 months ago", "2026-07-09"),
        ("1 year ago", "2025-09-07"),
    ],
)
def test_relative_publication_labels_resolve_against_the_clock(label, expected):
    """News results carry labels like '1 day ago'. Dropping them loses real recency."""
    from datetime import UTC, datetime

    from net_syphon.contracts import publication_date

    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    assert publication_date(label, now=now) == expected


@pytest.mark.parametrize(
    "label", ["", "ages ago", "soon", "1 fortnight ago", "9999 days ago", "tomorrow", "in 2 days"]
)
def test_unparseable_publication_labels_stay_unknown(label):
    """A label we cannot resolve is reported as unknown rather than guessed at."""
    from datetime import UTC, datetime

    from net_syphon.contracts import publication_date

    assert publication_date(label, now=datetime(2026, 9, 7, 12, 0, tzinfo=UTC)) is None
