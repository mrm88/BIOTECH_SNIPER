# IWM importer test fixtures

Synthetic iShares IWM holdings CSV bytes used by
`tests/universe/test_iwm_importer.py` and
`tests/universe/test_iwm_importer_schema.py`.

These fixtures act as VCR cassettes for the iShares
`www.ishares.com` CSV endpoint:

* `iwm_happy.csv` — 9-row preamble + canonical header + 5 equity
  rows + 2 non-equity (Cash) rows. Used by the happy-path test.
* `iwm_with_bom.csv` — same shape as `iwm_happy.csv` but prefixed
  with the UTF-8 BOM (`\ufeff`).
* `iwm_missing_asset_class.csv` — header row removes the
  ``Asset Class`` column. Used to verify the importer fails-loud
  with `IWMSchemaError`.
* `iwm_renamed_ticker.csv` — header row renames ``Ticker`` to
  ``Symbol``. Same fail-loud assertion target.
* `iwm_2k_rows.csv` — 1990 synthetic equity rows used by the
  ≥1900-rows verification check.

The fixtures are committed in plain UTF-8; no real iShares
copyright content appears in them. The format mirrors the
public-facing iShares CSV layout exactly enough for the parser
to exercise its preamble-skipping and column-validation logic.
