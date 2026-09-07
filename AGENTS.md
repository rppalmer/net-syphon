# Agent Instructions

Net-Syphon is a stdio MCP server that searches the open web and retrieves public
pages, then returns bounded plain text to an LLM. It is one person's machine, one
process, one set of private log files. Favor clear, simple, maintainable Python.

## Read First

- Read `ARCHITECTURE.md` before changing the call path, the audit boundary,
  provider behavior, or project structure. Its **"The rules"** section lists seven
  invariants. Breaking one is a bug, not a trade-off. Do not restate them
  elsewhere — link to them.
- Read `docs/threat-model.md` before changing anything that touches the network,
  the filesystem, or what gets logged. It states what is protected and what is
  not, and it is honest about the gaps. Keep it honest.
- Read `TODO.md` before starting work. It is the ordered plan, and its item IDs
  are stable. If you finish a numbered item, update it there rather than leaving
  the plan stale.
- Use `README.md` for the user-facing overview, configuration, and tool surface.

If you must break an invariant, document it in `ARCHITECTURE.md` as a deviation
that points at the fix. Never leave a broken invariant undocumented, and never
quietly "fix" a documented one without reading why it was accepted.

## The rule that outranks the others

**Tools expose intent. They never expose providers.**

A consumer asks for evidence about a query, or for the text of some pages. It
does not choose SearXNG or Firecrawl, and it must not be able to. This server
decides internally which mechanism serves a request.

Do not add a `provider`, `engine`, `backend` or `mechanism` argument. Do not name
a provider in a tool name, a tool description, or a response field. A test walks
the real advertised schema and fails if a provider name appears anywhere in it.

If a capability is missing, report the gap so this server can be changed. Do not
push the decision up to ORIS. This is the same boundary ORIS's own `AGENTS.md`
draws: each MCP server owns its provider-specific validation, limits, retries and
error classification, and ORIS only orchestrates.

This is a firm rule, not a preference. It is also the reason this project exists
as a separate server instead of three adapters inside ORIS.

## Trust boundary

**Everything Net-Syphon retrieves is untrusted input written by someone else.**
Search titles, snippets, URLs and page text are all attacker-controllable text on
its way to a model. A page can be written specifically to be found by a search.

- Treat retrieved content strictly as data. Never let it reach a code path that
  would execute, evaluate, or act on it.
- Never interpolate retrieved content into a prompt, a tool description, an
  argument, or a shell command inside this project.
- Do not add anything that follows up on retrieved content. No link following, no
  fetching a URL a page mentioned, no acting on an instruction found in text.
  A tool retrieves what it was explicitly asked for and nothing further.
- Keep the boundary visible in tool descriptions, so the consuming agent can tell
  provider text from Net-Syphon's own output.

**The query is sensitive in the other direction.** What the operator searches for
goes to a third party and reveals what they are working on. That is why queries
are never logged, why the audit records only a length, and why the tool
description tells consumers not to put secrets in a query.

## Standing Rules

- `server.py` carries zero logic. It defines schemas and delegates. Behavior lives
  in `service.py`, the provider modules, and `policy.py`.
- Every tool body runs through `SearchService.call()`. Nothing reaches a provider
  unaudited, including each page of a batch.
- The audit write happens **before** the network request, and is flushed to disk.
  If the write fails, the request does not happen. An audit failure disables
  egress for the whole process until it is restarted.
- Only metadata goes in the log. Identifiers, timings, statuses and counts. Never
  a query, a URL, a title, or page text. The allowlist in `audit.py` rejects any
  field outside the permitted set, so add a field there deliberately or not at all.
- Wall-clock time enters at `clock.py` and nowhere else.
- One attempt per call. No retries, no fallback between providers, no local
  browser, no fetching a page from this machine.
- Errors carry a fixed classification, never upstream exception text. Upstream
  messages can contain a query, a URL, or a credential.
- Never hardcode credentials or machine-specific paths. Configuration lives in
  `~/.net-syphon/.env`, which is not in the checkout.
- Adding a provider follows `ARCHITECTURE.md` §"Adding a provider". Adding a
  *tool* needs a reason the existing surface cannot cover. `net_syphon_get_page`
  was removed because `net_syphon_get_pages` with one URL did exactly the same
  thing.

## Testing

No test touches the network, and this is structural rather than conventional:

- Provider modules take an injectable `httpx` transport.
- Time is pinned with `FixedClock` from `clock.py`.
- The audit writer runs against real files on `tmp_path`, because filesystem
  behavior is the thing being tested.
- `test_protocol.py` drives the real server over real stdio and validates
  responses against the advertised output schema. Prefer adding a wire-level
  assertion there over a Python-level one when the contract is what matters.
- Live tests need `--live` and the `live` marker, and are skipped by default.

Assert the property, not a literal that happens to hold today. A test once pinned
search ranks as `[1, 3]`, which quietly documented a bug rather than catching it.

```shell
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pip-audit
```

## Where things live

- Credentials and operator data live in `~/.net-syphon/`: `.env` and `logs/`.
  The directory must be `0700` and owned by the runtime user; the dotenv file
  must be `0600`, regular, and single-link.
- The checkout holds code and tracked templates only. A repository-local `.env`
  is deliberately never read. An MCP host chooses the working directory, so
  anything resolved from the checkout is found only by luck, and it would put
  secrets next to version control.
- `experiments/` is historical source. It is not installed, not linted, and not
  tested. Do not import from it.

## Project Boundaries

- Net-Syphon owns broad web search and public-page retrieval. It is the only
  project here that fetches an arbitrary URL chosen at runtime.
- Targeted collection from known sources belongs to **Net-Razor**: X, Hacker
  News, arXiv, podcasts, and its RSS/Atom support. OpenAlex would be a Net-Razor
  addition. The two projects never call each other.
- Indicator enrichment and defensive reference knowledge belong to **ThreatSyft**.
- Planning, cross-call budgets, safe rendering and synthesis belong to the
  consuming agent (**ORIS**). Net-Syphon returns text and stops.

## Deliberate Non-Goals

No Tavily fallback, no local browser, no request cache, no ranking or
summarization, no content in the audit log, no multi-user support. See
`ARCHITECTURE.md` §"Deliberate non-goals" before proposing any of them.
