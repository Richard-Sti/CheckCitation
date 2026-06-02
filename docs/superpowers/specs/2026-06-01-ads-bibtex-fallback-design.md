# ADS BibTeX Unauthenticated Fallback — Design Spec

**Date:** 2026-06-01
**Status:** Approved

## Problem

When the ADS API token is rate-limited, `ads_export_bibtex` raises `AdsRateLimitError`.
Currently the check phase marks entries `ADS_UNVERIFIED_RATE_LIMITED` and the replace phase
falls back to a manual-paste prompt. Both degraded paths could be avoided if BibTeX can be
fetched without the API token.

## Goal

Automatically try unauthenticated URL candidates whenever `ads_export_bibtex` is rate-limited,
before falling back to the existing degraded behaviour. No new CLI flags, no new status codes,
no change to day-to-day behaviour when the API is not rate-limited.

## New Function

```
scrape_ads_bibtex_fallback(bibcode: str, timeout: float) -> str | None
```

Tries the following URL candidates in order, returning the first that yields a
parseable single-entry BibTeX string:

1. `GET https://api.adsabs.harvard.edu/v1/export/bibtex/{bibcode}` — same endpoint as the
   authenticated call, no `Authorization` header. Response is JSON `{"export": "..."}`.
   Anonymous requests have a separate rate limit; when the API key quota is exhausted this
   often still succeeds.

2. `GET https://ui.adsabs.harvard.edu/abs/{bibcode}/export/bibtex` — ADS UI export path.
   Returns plain-text BibTeX (not JSON) when the bibcode exists.

For each candidate:
- Fetch with `urllib.request.urlopen` (no retries; a single attempt per candidate).
- Parse the response body (JSON for candidate 1, raw text for candidate 2).
- Verify the extracted string parses as exactly one BibTeX entry via `parse_bibtex_text`.
- On success return the BibTeX string.
- On any exception (`urllib.error.URLError`, `urllib.error.HTTPError`, `json.JSONDecodeError`,
  `ValueError`, `TimeoutError`) silently move to the next candidate.

If both candidates fail, return `None`.

## Integration Points

### Check phase — `verify_ads_bibtex`

Current flow:
```
ads_export_bibtex(bibcode, token, timeout)
  → AdsRateLimitError  →  propagates up → rate_limited_result
```

New flow:
```
ads_export_bibtex(bibcode, token, timeout)
  → AdsRateLimitError
    → scrape_ads_bibtex_fallback(bibcode, timeout)
      → success: continue with scraped BibTeX (normal comparison → OK / ADS_BIBTEX_MISMATCH)
      → None:    re-raise AdsRateLimitError → existing rate_limited_result path
```

The catch is inside `verify_ads_bibtex`, which already has the bibcode and timeout in scope.

### Replace phase — `replace_outdated_entries`

Current flow (entries without pre-fetched `ads_bibtex`):
```
ads_export_bibtex(bibcode, token, timeout)
  → AdsRateLimitError  →  manual-paste prompt
```

New flow:
```
ads_export_bibtex(bibcode, token, timeout)
  → AdsRateLimitError
    → scrape_ads_bibtex_fallback(bibcode, timeout)
      → success: use scraped BibTeX, proceed with normal replacement prompt
      → None:    manual-paste prompt (existing behaviour)
```

## Caching

Successful fallback results are stored in the existing `"bibtex"` cache namespace under the
same bibcode key used by `ads_export_bibtex`. A subsequent run will hit the cache and never
call either the authenticated API or the fallback — the source of the cached value is
invisible to callers.

The fallback function does **not** check the cache itself; callers (`verify_ads_bibtex`,
`replace_outdated_entries`) already call `ads_export_bibtex` first which checks the cache,
so by the time `scrape_ads_bibtex_fallback` is invoked the cache is already known to be cold.
On success, `scrape_ads_bibtex_fallback` stores the result in the cache directly via
`ADS_CACHE.set("bibtex", bibcode, bibtex_string)` before returning.

## Error Handling

| Scenario | Behaviour |
|---|---|
| Both candidates fail | Return `None`; caller uses existing degraded path |
| Candidate 1 HTTP error (non-429) | Silently move to candidate 2 |
| Candidate 2 HTTP error | Silently return `None` |
| Result does not parse as one BibTeX entry | Treat as failure; move to next candidate |
| Unexpected exception | Catch broadly; log; move to next candidate |

No retries within `scrape_ads_bibtex_fallback`. If a candidate fails once it is skipped.

## Non-Goals

- No new CLI flags.
- No new status codes.
- No change to the check or replace flow when the API is not rate-limited.
- No scraping of the React `exportcitation` HTML page.
- No parallelism within the fallback (single-threaded, sequential candidates).
