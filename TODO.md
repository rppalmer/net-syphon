# Net-Syphon — Implementation plan

Ordered work list. Item IDs are stable, because [ARCHITECTURE.md](ARCHITECTURE.md)
and commit messages refer to them by number.

Approved direction (2026-09-06): finish Net-Syphon, then integrate ORIS. No
Tavily addition, no fallback, no local browser, no deferred roadmap features.

---

## Phase 1 — Search and retrieval ✅ done

- [x] Strict search inputs: general/news, domain restrictions, relative time, and
      paired ISO dates (start inclusive, end exclusive). Ordinary requests go to
      SearXNG, filtered and news requests go straight to Firecrawl. Never retry,
      never fall back.
- [x] Direct HTTPS Firecrawl requests with a fixed API origin, a protected key,
      audit-before-egress, a 25-second deadline and a 2 MiB streamed response cap.
- [x] Bounded page retrieval: public HTTP(S) ports 80 and 443 only, a DNS
      preflight that rejects any non-global answer, and no local page connections.
      Hosted redirects stay the provider's responsibility, and a reported final
      URL is re-validated without claiming to have observed its network path.
- [x] Return plain text, title, requested and reported final URL, retrieval time,
      media type, truncation flag and a SHA-256 of the returned text. Disable the
      provider cache, skipped TLS checks, enhanced proxy fallback, actions and
      PDF parsing.
- [x] Batch at most five pages sequentially with ordered independent outcomes,
      20,000 characters per page and 40,000 per batch, and a 180-second batch
      deadline. A single page gets 40 seconds including its URL checks.
- [x] Allowlisted search-failure diagnostics with no raw provider logging.
- [x] Fixture tests for malformed input, routing, safe payloads, URL policy,
      response limits, partial batches, cancellation and audit failure.

---

## Phase 2 — ORIS integration ✅ done

- [x] Use the official MCP adapter and runtime tool discovery, following
      Net-Razor's connection pattern. No custom transport, no mirrored schemas.
- [x] Replace direct Tavily web research with Net-Syphon search and bounded page
      retrieval. Synthesis stays tool-free. ORIS owns its per-run retrieval budget.
- [x] Exercise the normal specialist integration boundary and update ORIS's
      README, plan and dated implementation history.

---

## Phase 3 — Consistency review ✅ done (2026-09-07)

Findings from reviewing this project against Net-Razor and ORIS.

### T1 · Errors must survive the adapter — ✅ **done**

**What.** Net-Syphon reports a failure as an MCP protocol error, with
`is_error=true` and a structured `code`. The LangChain MCP adapter raises before
it builds the structured artifact, so ORIS received an untyped
`_MCPToolExecutionError` and its own `SearchProviderError` handling never ran.

**Why it mattered.** Net-Razor returns errors as ordinary dictionaries in a
successful result, so ORIS reads them as data. Both servers use the code
`not_configured` and delivered it two different ways.

**Decision.** Net-Syphon is right and Net-Razor is the outlier. A failed call
should look like a failure to any MCP client, not only to ORIS. Fixed in ORIS by
catching `ToolException` and recovering the code from the payload. Net-Razor
should move to the same shape — see the handoff note.

### T2 · Project documents — ✅ **done**

Added `AGENTS.md` and `ARCHITECTURE.md`, and moved this plan to `TODO.md` to
match Net-Razor's layout. The intent-not-providers rule was previously written
down only as half a sentence in the README.

### T3 · Remove `net_syphon_get_page` — ✅ **done**

The singular tool was fully redundant. `net_syphon_get_pages` with one URL
computes the same 20,000-character budget and makes the same provider call. ORIS
already allowlisted only the batch tool.

### T4 · Drop the `rank` field — ✅ **done**

`rank` was the item's position in the upstream payload, including rejected
entries, so a consumer could receive items ranked 2 and 3 with no rank 1. Once
renumbered it would have equalled the list index, which ORIS's simplicity gate
says not to duplicate. Removed. List order is the only ordering the contract states.

### T5 · Finish the clock seam — ✅ **done**

`datetime.now` now appears only in `clock.py`, matching Net-Razor's third
invariant. `SearchService` takes a `Clock` and passes it to the audit writer and
to retrieval, so response timestamps are pinnable in tests. A guard test fails if
another module reads the system clock.

### T6 · Latest MCP SDK — ✅ **done here**, blocked in ORIS

Net-Syphon is on `mcp` 2.2.0, the latest release. ORIS cannot follow yet:
`langchain-mcp-adapters` 0.3.2 is the newest release and requires `mcp<2.0.0`.

This is not currently a problem. Verified on 2026-09-07: ORIS's `mcp` 1.29 client
and this server negotiate protocol `2025-11-25` over the initialize handshake,
and all tools and errors come through correctly. Re-check when
`langchain-mcp-adapters` ships MCP 2.x support. See T8.

---

## Open work

### T7 · Live Firecrawl verification — **open**

Run live search and retrieval with an operator-configured key. Do not copy a key
from ORIS. Confirm media-type metadata, date coverage and real retrieval
behavior. Do not loosen any restriction if the live contract differs from the
fixtures — report the difference instead.

The SearXNG live contract passed on 2026-09-06 after operator maintenance. See
[docs/search-failure-followup.md](docs/search-failure-followup.md).

### T8 · Watch the MCP major-version split — **open**

ORIS is pinned to `mcp` 1.x by `langchain-mcp-adapters`. The 2.x SDK already
refuses the `initialize` handshake on its own modern connections, so if a future
release drops handshake-era support, ORIS stops being able to connect.

Check whether `langchain-mcp-adapters` has released MCP 2.x support before
upgrading this server past a major version again.

### T9 · Operator deployment and live acceptance — **open**

Deploy and run ORIS's live acceptance and semantic evaluation. The checkout
integration does not reconfigure the running service.

---

## Not committed work

Ideas kept for later evaluation. None is approved.

- **Operator diagnostics.** A `doctor` command for configuration, permissions,
  audit availability and explicit connectivity checks with actionable errors.
  It would become the second protocol adapter, and the no-logic rule applies to it.
- **Small-model-friendly retrieval.** Bounded text with useful headings, clear
  truncation, and possibly section selection. Summarization stays in ORIS.
- **Language filtering** on search, if real usage needs it.
- **Usage safeguards.** Operator-set request allowances and rate-limit cooldowns,
  without automatic retries or fallback. Daily limits would need to survive a
  restart.
- **Local source ingestion.** Accept PDFs, manuals and saved offline content,
  store them as a source collection, and make them retrievable through MCP.
  Ownership, storage and retrieval design are all undecided, and it needs its own
  review of parser isolation, resource limits, privacy, retention and untrusted
  content. Source storage would be separate from the metadata-only audit log.
- **Prompt-injection screening in ORIS.** See
  [docs/threat-model.md](docs/threat-model.md#future-security-work). Not
  implemented and not a selected dependency.
- **Sentinel in ORIS.** A roadmap idea for audit review. Not implemented, no
  commitment, and it would live in ORIS rather than here.

Page-change monitoring is not planned. Tavily fallback and local browser support
are not planned.
