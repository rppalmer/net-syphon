# Net-Syphon — Architecture

How the pieces fit together and which rules must hold. For setup, configuration
and the tool surface, see [README.md](README.md). For security boundaries, see
[docs/threat-model.md](docs/threat-model.md). For planned work, see [TODO.md](TODO.md).

This document describes the code **as it is today**. Where the code breaks its own
rules, that is marked as a deviation and points at the fix.

---

## Shape

One local Python process. No ports, no web server, no browser. Two front doors:

```
   MCP host (stdio)              Terminal
          │                          │
          ▼                          ▼
     server.py                    cli.py            protocol adapters —
     2 tool definitions           doctor            no logic lives here
          │                          │
          │                          ▼
          │                   diagnostics.py
          ▼
    service.py · SearchService     composition root
          │                        + the audit boundary
     ┌────┴──────┬─────────┬──────────┐
     ▼           ▼         ▼          ▼
  policy.py   searxng.py  firecrawl.py  config.py
                  │           │
                  └─────┬─────┘
                        ▼
                    audit.py ──────► JSON lines (~/.net-syphon/logs/)
                        │
                        ▼
                    clock.py
```

The two adapters are not symmetric, and deliberately so. MCP carries the whole
tool surface, because an agent is the intended caller. The CLI carries only
`doctor`, because diagnosing a server that will not start is the one thing you
cannot do through that server. Neither adapter contains fetch, parse, or audit
logic.

`create_server()` builds the whole object graph. Every tool body is a single
delegation into `SearchService.call()`.

## Providers

Each provider owns one upstream. HTTP transport is injectable, so tests run
against an in-memory transport instead of the network.

| Intent | Provider | Module | Auth |
| --- | --- | --- | --- |
| Ordinary search | SearXNG (self-hosted) | `searxng.py` | none |
| News or filtered search | Firecrawl `/v2/search` | `firecrawl.py` | API key |
| Page retrieval | Firecrawl `/v2/scrape` | `firecrawl.py` | API key |

The consumer never sees this table. It states what it wants and the server picks.

## The call path

1. `server.py` receives a tool call and passes the raw arguments straight through.
2. `SearchService.call()` mints a `call_id` and writes `call_start` to the audit log.
3. The arguments are validated against the tool's Pydantic contract. A failure
   is recorded and becomes `invalid_input`. Nothing else happens.
4. If another call is already running, this one returns `busy`.
5. Configuration is loaded fresh. A missing setting becomes `not_configured`, and
   only the affected capability is disabled.
6. The request is routed. `SearchRequest.requires_filtered_search` decides between
   SearXNG and Firecrawl. Retrieval always goes to Firecrawl.
7. For retrieval, `policy.py` authorizes each URL before it is submitted, and
   again when the provider reports a different final URL.
8. The provider module writes `outbound_start`, makes exactly one request, and
   writes `outbound_end` whatever happens.
9. `call_end` is written with the outcome. The response goes back to the consumer.

## The rules

These are the invariants the design depends on. Breaking one is a bug.

**1 · Tools expose intent, never providers.** A consumer asks for evidence about
a query, or for the text of some pages. It does not choose a mechanism, and it
must not be able to. The words "SearXNG" and "Firecrawl" never appear in a tool
name, a tool description, an argument, or a response field. A test asserts this
against the real advertised schema.

Reporting which mechanism served a request would be acceptable, the way Net-Razor
reports `transcript_backend`. Accepting a mechanism as an argument is not. If a
capability is missing, report the gap so this server can be changed. Do not push
the decision up to the consumer.

This is the rule most likely to be broken by someone being helpful. It is also
the reason this project exists as a separate server rather than as three
adapters inside ORIS.

**2 · Everything is audited at one boundary, and the audit is written first.**
`SearchService.call()` is the only way a tool body runs. Every outbound request
is preceded by a durable `outbound_start` write, flushed with `fsync`, before any
connection is opened. If that write fails, no request is made.

An audit failure poisons the writer permanently and disables egress for the whole
process. This is deliberate. An unaudited request is worse than a failed one.
Recovery means investigating the stderr diagnostic and restarting.

**3 · Wall-clock time enters at one module.** `clock.py` holds the only call to
`datetime.now`. `SearchService` takes a `Clock` and hands it to the audit writer
and to retrieval. Tests pin it with `FixedClock`. A test walks the package and
fails if any other module reads the system clock, because a seam like this rots
quietly otherwise.

**4 · Metadata only. Content never reaches the audit log.** This is the deliberate
opposite of Net-Razor's rule 4, which stores the complete upstream payload.

Net-Razor collects from a fixed set of known sources, and its stored payloads pay
for themselves: a long transcript is fetched once and paged from storage. Net-Syphon
takes an arbitrary query and returns an arbitrary page. Storing that would mean
keeping a copy of whatever the operator searched for and whatever a stranger's
page said. The audit records identifiers, timings, statuses and counts, and an
allowlist rejects any field outside that set.

The cost is real and is stated in the threat model. The logs cannot tell you what
a call actually returned, so they cannot be used to investigate poisoned content.

**5 · One active call per process.** A second concurrent call returns `busy`
rather than queueing. There is one operator, one consumer, and one API allowance.
Serializing makes the audit log a straight line and makes spend predictable.

Consequence worth knowing: a consumer cannot fan out across this server the way
ORIS fans out across Net-Razor. Batch retrieval exists for exactly that reason.

**6 · No retries, no fallback, no local fetching.** One attempt per call. A
failure is classified and returned with a `retryable` flag, and the consumer
decides. There is no Tavily fallback and no local browser.

Net-Syphon never opens a connection to a page server. It resolves a hostname to
check that it is public, then hands the URL to the hosted provider. The DNS
preflight is a check, not a pin: the provider resolves independently and owns
its own redirects.

**7 · The protocol adapters carry no logic.** `server.py` defines tool schemas
and delegates. `cli.py` parses arguments and formats output. Neither validates,
routes, classifies, or audits.

**8 · Doctor reports; it never repairs.** `diagnostics.py` creates no file and
no directory, and reads no audit event contents. A missing home directory or log
is a healthy not-yet-used state, not a fault. Its connectivity checks go through
the audit boundary like any other request, so they refuse to run when the audit
is unwritable. A test asserts that running the checks against an absent directory
leaves it absent.

## Audit log

JSON lines, one file per UTC date, at `~/.net-syphon/logs/YYYY-MM-DD.jsonl`.

Every write takes an exclusive lock, appends one record, and calls `fsync`. Files
are opened relative to a directory descriptor with `O_NOFOLLOW`, and ownership,
mode, type and link count are checked on every open. A record over 2 KiB is
rejected. A day's file is capped at 10 MiB, and reaching that cap stops audited
requests for the rest of the day.

Retention is 30 UTC dates and runs at most once per day, during use. An idle
server prunes nothing.

Batch retrieval gives each page its own `call_id` and links it to the batch
through `parent_call_id`.

`partial` and the audit's `rejected_count` answer different questions and must not
be collapsed into one count. `partial` is for the caller: did this response lose
something you asked for. `rejected_count` is for the operator: how much junk is
upstream producing. A payload can be full of malformed entries past the requested
limit, costing the caller nothing while saying a lot about the service. So result
normalization scans the whole payload for the audit, and only rejections that left
the caller short set `partial`.

## Adding a provider

Dispatch is a plain `if` in `service.py`, and there is no provider protocol. That
is deliberate. There are two upstreams and no third planned, so a registry would
be structure without a demonstrated requirement.

If a third provider ever arrives, it goes in its own module with an injectable
transport, and it obeys rules 1, 2 and 6. Do not add an abstraction layer before
there is something to abstract.

## Testing model

No test touches the network, and this is structural rather than conventional:

- Provider modules take an injectable `httpx` transport. `SearchService` passes
  it straight through.
- Time is pinned with `FixedClock`. The audit writer runs against real files on
  `tmp_path`, because its whole job is filesystem behavior.
- `test_protocol.py` drives the real server over real stdio and validates
  responses against the advertised output schema. That is what proves the wire
  contract, not the Python helpers.
- Live tests need `--live` and the `live` marker. They are skipped by default.

Assert the property, not a literal that happens to hold today. A search result
list once pinned the ranks `[1, 3]`, which quietly documented a bug: the numbers
were upstream positions and skipped rejected entries. The field is gone now.

## Configuration

Two settings, both optional, both read from `~/.net-syphon/.env` or the process
environment. The environment wins.

| Setting | Enables |
| --- | --- |
| `NET_SYPHON_SEARXNG_URL` | ordinary search |
| `NET_SYPHON_FIRECRAWL_API_KEY` | news search, filtered search, retrieval |

The directory must be owned by the runtime user with mode `0700`. The file must
be owned, regular, single-link, and mode `0600`. Symlinks are refused. A
repository-local `.env` is never read, because the checkout is under version
control and an MCP host chooses the working directory.

Operational numbers are constants, not settings. Deadlines, response caps and
character limits live in code. Nobody tunes a request deadline on a personal
tool, and a wrong default deserves a commit.

## Deliberate non-goals

- **No fallback between providers.** If the configured search service is down,
  the call fails and says so. Silent substitution hides an outage and changes
  what the consumer is reading without telling it.
- **No local browser.** The August 2026 experiment is in `experiments/` and did
  not establish isolation. Reviving it needs a new security design, not a flag.
- **No request cache.** Every call goes upstream.
- **No summarization, ranking or scoring.** Net-Syphon returns text in the
  provider's order. Synthesis belongs to the consuming agent.
- **No content in the audit log.** See rule 4.
- **No multi-user support.** One person, one machine.
