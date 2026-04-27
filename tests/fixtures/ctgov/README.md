# ClinicalTrials.gov v2 API Fixtures

Hermetic fixtures for ingestion tests that exercise the
`https://clinicaltrials.gov/api/v2/studies` surface.

The callers in this project that hit CT.gov v2 are:

* `biotech_sniper/audit.py` (heartbeat probe — the audit page reads
  the first 5 PHASE3 / ACTIVE_NOT_RECRUITING studies)
* `biotech_sniper/new_opportunity_sniper.py`
* `biotech_sniper/intelligence/bulk_universe_scanner.py`
* `biotech_sniper/intelligence/company_pipeline_manager.py`
* `biotech_sniper/intelligence/master_discovery.py`
* `biotech_sniper/intelligence/amendment_tracker.py`
* `biotech_sniper/intelligence/watchlist_lifecycle.py`
* `biotech_sniper/intelligence/trial_science_reader.py`

Each fixture in this directory is a JSON file that mirrors the public
CT.gov v2 response envelope. They are deliberately tiny — single-study
or short-list payloads — so ingestion tests can replay deterministically
without taking on a hard dependency on `vcrpy` for the simple
`requests.get` callers.

## Files

| File | Purpose |
|------|---------|
| `study_NCT05123456.json` | Single-study GET response: `/api/v2/studies/NCT05123456`. Used to exercise per-NCT enrichment readers (`amendment_tracker`, `trial_science_reader`, `watchlist_lifecycle`). |
| `studies_phase3_recruiting.json` | List response: `/api/v2/studies?filter.advanced=AREA[Phase]PHASE3+AND+AREA[OverallStatus]ACTIVE_NOT_RECRUITING`. Used by the `audit.py` heartbeat and the bulk universe scanner. |

## Recording

If the CT.gov v2 schema changes, refresh the fixtures by:

```
curl -sS 'https://clinicaltrials.gov/api/v2/studies/NCT05123456' \
  > tests/fixtures/ctgov/study_NCT05123456.json
curl -sS 'https://clinicaltrials.gov/api/v2/studies?filter.advanced=AREA%5BPhase%5DPHASE3%20AND%20AREA%5BOverallStatus%5DACTIVE_NOT_RECRUITING&pageSize=5&sort=LastUpdatePostDate' \
  > tests/fixtures/ctgov/studies_phase3_recruiting.json
```

CT.gov is unauthenticated; no secret scrubbing is required.
