# SEC EDGAR Fixtures

Hermetic fixtures for ingestion tests that exercise the public SEC
EDGAR endpoints used by this project:

* `https://www.sec.gov/cgi-bin/browse-edgar?...&type=8-K&output=atom`
  — the rolling 8-K filings feed, consumed by
  `biotech_sniper/intelligence/sec_8k_monitor.py` and the
  `audit.py` heartbeat.
* `https://www.sec.gov/files/company_tickers.json` — the canonical
  CIK → (ticker, company name) map, consumed by
  `biotech_sniper/intelligence/company_resolver.py` and the
  `audit.py` heartbeat.

## Files

| File | Purpose |
|------|---------|
| `edgar_8k_atom.xml` | Atom feed of recent 8-K filings (5 entries). Used to verify the 8-K watcher's parser handles ATOM `entry`/`updated`/`title`/`link` correctly. |
| `company_tickers.json` | Trimmed CIK → ticker map (3 sample biotech tickers). Used by company-resolver tests to verify name normalization. |

## Recording

The SEC endpoints require a `User-Agent` header per
[fair-access policy](https://www.sec.gov/os/accessing-edgar-data) but
do not require any secret. To refresh:

```
curl -sS -H 'User-Agent: BiotechSniperTests test@example.com' \
  'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=10&search_text=&output=atom' \
  > tests/fixtures/sec_edgar/edgar_8k_atom.xml

curl -sS -H 'User-Agent: BiotechSniperTests test@example.com' \
  'https://www.sec.gov/files/company_tickers.json' \
  | jq '. as $all | reduce keys[] as $k ({}; if (.|length) < 3 then . + {($k): $all[$k]} else . end)' \
  > tests/fixtures/sec_edgar/company_tickers.json
```

These fixtures contain only public filing metadata; no secret scrubbing
is required.
