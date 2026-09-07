# Follow-up: live search failures

## Evidence (2026-09-05)

- User's browser returned results for `Python type hints`, credited to Brave.
- Live test failures `496f8304-b06f-468e-835d-fb2ff778f8a1` and
  `480fd015-30b3-4736-9098-a6ce6e0efef2` received HTTP 200, zero results,
  zero rejected entries, and four reported engine failures.
- An audited diagnostic using the current API request returned 20 Brave results;
  Net-Syphon accepted five with `partial=true`. Other diagnostic requests failed.
- SearXNG's existing `/stats/errors` reported:
  - Brave: `SearxEngineTooManyRequestsException`.
  - DuckDuckGo and Startpage: `SearxEngineCaptchaException`.
  - Qwant: `SearxEngineAccessDeniedException`.

These are failures reported by SearXNG's engine integrations, not proof that the
engines detected Net-Syphon. The recorded statistics are historical, not correlated
to a particular Net-Syphon call. Browser success and API failure remain incompletely
explained. A language comparison also changed request timing, so it did not establish
a language-related cause. Do not infer that the browser used cached results merely
from the screenshot's `cached` links.

## Resolution and remaining limits (2026-09-07)

The live SearXNG contract passed on 2026-09-06 following operator maintenance.
Net-Syphon now distinguishes zero results with engine failures as
`engines_unavailable`, records allowlisted CAPTCHA/rate/denial/timeout counters,
and distinguishes transport, JSON, content-type and encoding failures in audit
metadata. Live-test failures now show a concise code, message and call ID.

This verifies a successful deployed request, not long-term engine reliability or
the exact cause of earlier browser/API differences. No retry, fallback, impersonation
or CAPTCHA bypass was added. If failures recur:

1. Explain the request path: consumer → Net-Syphon → SearXNG → external engines.
   Investigate why browser/API outcomes differ without assuming their effective
   preferences or engine availability were identical. Inspect existing statistics
   first; avoid repeated searches that may worsen rate limiting.
2. Inspect the new audit categories, without enabling raw response logging.
3. Address SearXNG engine availability separately with operator approval. Do not add
   retries, fallback providers, browser impersonation, or CAPTCHA bypass as a workaround.
