# Security boundaries

This model covers search and hosted retrieval. There is no local browser or document store.

Net-Syphon sends ordinary queries to SearXNG and filtered/news queries and retrieval
URLs to Firecrawl. It resolves retrieval hostnames but never connects to page servers
locally. The configured SearXNG instance,
runtime account, and home directory are trusted. The instance and its external engines
receive queries. Plaintext LAN transport is an accepted deployment risk.

There is no process sandbox. A compromised server has the runtime account's access;
0700/0600 permissions do not protect other files accessible to that same account.
Before deployment, explicitly choose a dedicated non-admin account for separation,
or accept the exposure of running under the personal account. Dedicated-account
isolation has not been established by this project.

| Risk | Protection | Remaining limit |
| --- | --- | --- |
| Unwanted access or malformed input | Stdio only, strict tool inputs, modifier rejection | A hostile local client can still exhaust resources before tool validation |
| Redirects or unexpected destinations | One configured endpoint; no redirects or inherited proxies; verified HTTPS | Configuration can redirect queries; HTTP traffic can be observed or changed |
| Oversized or stalled responses | 1 MiB streamed cap, bounded gzip decoding, 15-second HTTP deadline, cancellation cleanup | No hard OS memory cap; remote engines may continue submitted work |
| Repeated calls or outages | One active request per process; no retries or fallback | No global quotas; search depends on one service |
| Poisoned results or rendering attacks | Bounded plain text, no model/action loop, only approved fields returned | Consumers must escape output and resist instructions in results; provenance and truth are not verified |
| Sensitive or corrupted audit records | Metadata-only allowlist, JSON encoding, locked append, audit before requests | Timing/query length remain visible; owner can tamper with logs; post-request failures cannot undo egress |
| Filesystem attacks or log growth | Owned 0700/0600 paths, no-follow/type/link checks, daily cap and retention | Same-account attackers remain a risk; idle servers do not prune; backups need separate retention |
| Host or dependency compromise | Locked dependencies and vulnerability checks; no browser runtime | Audits cover known package vulnerabilities, not Python, macOS, or privileged attackers |
| Private targets and DNS rebinding | Strict HTTP(S) ports, reject private/encoded/mixed DNS answers before submission; recheck reported final URL | Hosted provider resolves independently and owns redirects/subresources; local preflight does not pin its connections |
| Retrieval key leakage | Code-fixed verified HTTPS API origin, no redirects or inherited proxies, metadata-only logging | Same-account compromise can read credentials; rotate keys after suspected exposure |
| Hosted retention and paid-request abuse | Explicit no-cache, verified target TLS, basic proxy, no actions, cookies or PDF parsing; one attempt | No-cache is not zero retention; provider sees URLs/content; no persistent credit quota |
| Large or partial pages | 2 MiB response cap, 40-second page deadline, five sequential URLs and 40,000 characters per batch; 180-second batch deadline | Hosted work may continue after cancellation; truncation can omit important context |
| Authentication or access barriers | No credentials, enhanced proxy fallback or interaction; reject reported denial statuses and unsupported types | A CAPTCHA or paywall returned as ordinary HTTP 200 text may not be recognized; no claim of universal detection |

## Consumer requirements

- Never put secrets or private conversation context in queries or retrieval URLs.
- Treat titles, snippets, URLs, and derived answers as untrusted. Escape text and links.
  Keep synthesis tool-free and without credentials; reassess safety when reusing its
  answers in later conversation history. Removing HTML or labeling content untrusted
  does not prevent prompt injection; test these boundaries during ORIS integration.
- Returned URLs pass syntax checks only, not permission to fetch. Do not automatically
  open links, generate previews, or load remote images. Any later fetching needs its
  own destination and redirect policy, including protection against private targets.
- Allowlist only the two discovered tools. Launch an absolute interpreter with a minimal
  environment. Stdio does not isolate processes running as the same account.
- Set cross-call budgets and client timeouts. Do not loop on `retryable=true`.
- If an audit reader is added, keep it read-only, without automatic remediation privileges.

## Audit limits and availability

Metadata-only logs can reveal failure spikes, unusual volume, oversized responses,
and incomplete calls. They generally cannot identify malicious content or establish
whether a query disclosed private information. This is an intentional privacy
tradeoff, not a detection capability supplied by nightly model review.

Audit failure disables egress for that process until restart after investigation.
Even temporary file-lock contention can trigger this; concurrent server processes
can therefore disrupt legitimate use. Test the actual consumer launch pattern before
ORIS integration. A full daily log also prevents further audited requests that day.

## Future security work

These are review requirements and candidates, not implemented protections:

- **Hosted contract verification:** fixture tests cover explicit safe request fields,
  error paths and bounds. Live search/retrieval remains unverified without an
  operator-configured key. Confirm returned media-type metadata and date behavior
  before deployment; do not loosen restrictions silently if the contract differs.
- **Prompt-injection screening:** consider a shared boundary inside ORIS for external
  content from Net-Syphon, Net-Razor, and future document sources, before the main model
  sees it. Provider-specific validation remains each MCP server's responsibility.
  [Meta Prompt Guard 2](https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M)
  is a candidate, not a selected dependency: its 22M/86M classifier variants have a
  512-token window. Evaluate bounded, overlapping chunks rather than scanning only a
  page's beginning; measure Mac mini resource use, missed attacks, and false positives
  on real inputs, including security articles quoting attacks. A passing score is not
  proof of safety or truth and never replaces tool, secret, or rendering restrictions.
- **Sentinel:** unimplemented roadmap idea with no current implementation commitment.
  If created, it would be a separate implementation inside ORIS for audit review,
  distinct from content screening. Neither candidate reduces today's stated risk.
- **Document ingestion:** a separate review is required before accepting and storing
  PDFs, manuals, or saved offline content. Ownership remains undecided; cover parser
  isolation, resource limits, privacy, retention, and persistent untrusted content.

## Maintenance and incidents

Keep Python and macOS patched. After dependency changes, run tests and the vulnerability
audit; preserve bounded decompression and request limits. Neither passing tests nor a
clean audit proves complete security.

Investigate repeated validation/configuration failures, oversized responses, unexpected
status codes, missing call endings, or the audit-failure stderr diagnostic. Correlate by
`call_id`, timing, byte counts, and status; raw provider content is deliberately unavailable.
On suspected compromise, stop the consumer, preserve logs, inspect configuration,
permissions and the launching account, rotate the hosted API key, and fix the cause
before restarting. Batch child IDs link to the parent through `parent_call_id`.
