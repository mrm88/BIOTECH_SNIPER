# yfinance Fixtures

Hermetic fixtures for ingestion tests that exercise the
[`yfinance`](https://pypi.org/project/yfinance/) Python client.

The callers in this project that hit yfinance are:

* `biotech_sniper/calibration_utils.py`
* `biotech_sniper/performance_tracker.py`
* `biotech_sniper/intraday_scanner.py`
* `biotech_sniper/new_opportunity_sniper.py`
* `biotech_sniper/build_spreadsheet.py`
* `biotech_sniper/biotech_sniper_agent.py`
* `biotech_sniper/iv_crush_exit_rules.py`
* `biotech_sniper/intelligence/master_discovery.py`
* `biotech_sniper/intelligence/company_pipeline_manager.py`

> **NOTE — M3 deprecation:** the M3 milestone replaces yfinance options
> chains with the Alpaca options API in `pull_options.py`. These
> fixtures still exist (a) so M2 ingestion tests have something to
> replay against, and (b) so the migration in M3 has a regression
> baseline. See VAL-M3-010 / VAL-M3-011 for the deprecation contract.

## Files

| File | Purpose |
|------|---------|
| `options_chain_TESTX.json` | Snapshot of `yfinance.Ticker("TESTX").option_chain("2026-05-15")` serialized to JSON (calls + puts as records). Used to test downstream consumers of options-chain data. |
| `quote_history_TESTX.json` | Snapshot of `yfinance.Ticker("TESTX").history(period="5d")` serialized to JSON (OHLCV records). Used by the calibration utilities and the intraday scanner. |

## Recording

```python
import yfinance as yf, json
t = yf.Ticker("AAPL")
chain = t.option_chain("2026-05-15")
out = {
    "symbol": "AAPL",
    "expiry": "2026-05-15",
    "calls": chain.calls.to_dict(orient="records"),
    "puts": chain.puts.to_dict(orient="records"),
}
json.dump(out, open("tests/fixtures/yfinance/options_chain_TESTX.json", "w"), default=str, indent=2)
```

The fixtures are renamed to `TESTX` so they can never be confused with
live ticker data when grepping the repo. yfinance does not require
authentication; no secret scrubbing is needed.
