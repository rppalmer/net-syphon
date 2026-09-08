# Net-Syphon

A stdio-only MCP server for web search and anonymous public-page retrieval:

```text
net_syphon_search_web(query, max_results=5, search_category="general", include_domains=[],
                      time_range=None, start_date=None, end_date=None)
net_syphon_get_pages(urls, max_characters=20000)
```

Search returns links in the provider's order, and optional previews, not verified evidence. Retrieval
returns bounded plain text, title, requested and provider-reported final URL, timestamp,
media type, truncation flag, and a hash of the returned text.
Net-Syphon owns validation, request limits, errors, and auditing. Consumers own planning,
cross-call budgets, safe rendering, and synthesis. Ordinary search uses SearXNG;
news or filtered search and retrieval use Firecrawl's direct API. Consumers select
intent, never providers. There is no automatic fallback or local browser.

## Status and roadmap

Search, filtered/news search and retrieval are implemented with fixture-based tests.
The deployed SearXNG live contract passed on 2026-09-06 after operator maintenance;
see the [diagnostic history](docs/search-failure-followup.md). Firecrawl live verification
requires an operator-configured key and remains outstanding. The ORIS checkout now
uses Net-Syphon for search plus bounded page retrieval; deployment and live acceptance
remain separate. See the [current plan](TODO.md).

Tavily fallback and local browser support are not planned. Prefer simple, explicit
behavior; add complexity only when actual use demonstrates a need.

### Future considerations—not committed work

Net-Syphon's scope is broad web search and scraping.
Net-Razor handles targeted information collection, including its existing RSS/Atom
support. OpenAlex is a
possible Net-Razor addition, not a Net-Syphon proposal. Page-change monitoring is
not planned here.

Keep these ideas for later evaluation; none is approved for implementation:

- **Operator diagnostics:** a `doctor` command for configuration, permissions,
  audit availability, and explicit connectivity checks with actionable errors.
- **Small-model-friendly retrieval:** bounded text, useful headings, clear
  truncation, and possibly section selection. Summarization stays in ORIS.
- **Additional search controls:** language filtering if real usage needs it.
- **Usage safeguards:** optional operator-set request allowances and rate-limit
  cooldowns, without automatic retries or fallback. Daily limits would need to
  survive server restarts.
- **Local source ingestion:** accept PDFs, manuals, and saved offline content,
  store them as a source collection, and make that information retrievable by a
  local AI through MCP. This could be a Net-Syphon module or a separate tool;
  ownership, storage, and retrieval design remain undecided. It would require a
  separate review of document parsing, resource limits, privacy, retention, and
  untrusted content. Source storage would be separate from metadata-only audit logs.
- **Prompt-injection screening in ORIS:** evaluate a small dedicated classifier,
  such as Meta Prompt Guard 2, before external content reaches the main model.
  This is not implemented or a selected dependency; see the
  [security considerations](docs/threat-model.md#future-security-work).
- **Sentinel in ORIS:** a roadmap idea for audit review, not implemented and with
  no current implementation commitment. If built, it would be a separate
  implementation inside ORIS, not a Net-Syphon component or current safeguard.

## Setup

Use a current Python 3.12 patch release, uv, and a SearXNG instance with JSON output enabled.
The filesystem protections target macOS/POSIX.

```sh
uv sync --locked
```

Configure your MCP consumer with an absolute interpreter path and your instance's
**base URL**, not its `/search` endpoint:

```json
{
  "command": "/absolute/path/to/net-syphon/.venv/bin/python",
  "args": ["-m", "net_syphon"],
  "env": {"NET_SYPHON_SEARXNG_URL": "http://192.0.2.10:8080"}
}
```

Replace both example paths/addresses. Alternatively, store the setting in
`~/.net-syphon/.env` using [.env.example](.env.example). Environment values take precedence.
The directory must be owned by the runtime user with mode `0700`; the file must be
owned, regular, single-link, and mode `0600`. Symlinks and repository-local dotenv files
are not supported.

Set `NET_SYPHON_FIRECRAWL_API_KEY` privately in the same protected dotenv file or
launch environment for retrieval and filtered/news search. Missing configuration
disables only the affected capability. No key is needed for ordinary SearXNG search.
Use a client timeout of at least 210 seconds to accommodate a bounded batch.
The `net-syphon` console command starts stdio transport. Its one subcommand,
`net-syphon doctor`, checks configuration, permissions and audit health and exits
non-zero when something needs fixing. It creates and repairs nothing. Add
`--connect` to make one real request per configured capability, which spends one
hosted retrieval request. Connectivity is skipped when the audit is unwritable,
because egress is disabled in that state.

## Operation

- Queries allow 1–500 characters; result limits are integers from 1–10. Provider-control
  modifiers are rejected. Discover the tool's schema for the complete contract.
- Search accepts up to ten domain restrictions, a relative day/week/month/year, or
  paired ISO dates (start inclusive, end exclusive). Relative and absolute periods
  cannot be combined. Dates constrain the hosted search; they are not verified
  publication dates. `published_at` uses the provider's own date where there is one.
  News recency arrives as a relative label and is resolved against the retrieval
  time, so it is accurate to the unit stated rather than exact. It is null when the
  provider supplies nothing, which is common: ordinary search carries a date on
  roughly a fifth of results, and filtered non-news search carries none at all.
- Retrieval supports public HTTP port 80 and HTTPS port 443, HTML and plain text only.
  It preflights DNS but does not connect to page servers locally. Hosted redirects
  remain the provider's responsibility. A final URL is provider-reported, or null.
- Every page in a batch gets the same allowance, whatever the batch size. It defaults
  to 20,000 characters and `max_characters` moves it between 1,000 and 50,000. Raise it
  to read one page deeply; lower it to survey several cheaply. A longer page comes back
  with `truncated` set. Batches accept 1–5 URLs sequentially and preserve per-URL
  success and errors. Deadlines:
  SearXNG 15 seconds; hosted request 25; individual retrieval 40; batch 180.
  Already completed pages survive a batch deadline. Response caps are 1 MiB for
  SearXNG and 2 MiB for hosted requests, including decompressed data.
- Empty results are valid. `partial=true` means the response is short of what you asked
  for: entries were rejected and could not be replaced, or the search service reported
  engine failures. Malformed entries beyond your requested limit cost you nothing and do
  not set it. On a retrieval batch it means at least one page failed.
  Errors contain `call_id`, `code`, `message`, and `retryable`; retries are never automatic.
- `not_configured`: check configuration and permissions. `access_denied`: check instance
  access and JSON support. `busy`: another tool call is active in this process.
  `engines_unavailable`: zero usable results with reported engine failures; audit
  counters distinguish CAPTCHA, rate-limit, denial and timeout categories.
- Private audit logs are at `~/.net-syphon/logs/`. They record IDs, timings, status and
  counts—not queries, URLs, or content. Retention is 30 UTC dates, pruned during use,
  with a 10 MiB daily cap.
- Audit failure disables further requests in that process. Investigate the stderr
  diagnostic, preserve logs, fix the cause, then restart. A full daily file normally
  requires waiting for the next UTC day. Retention deletion is permanent without backups.

Read the [security guidance](docs/threat-model.md) before connecting a consumer.
Contributors should start with [AGENTS.md](AGENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

## Development

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pip-audit
```

Default tests use local fixtures, including temporary loopback HTTP/TLS servers.
To intentionally query the configured service: `uv run --locked pytest --live -m live`.
