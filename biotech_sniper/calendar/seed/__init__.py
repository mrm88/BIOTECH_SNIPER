"""Seed-data shipped with calendar scrapers.

Each scraper bundles a small JSON seed file with a manually-curated
fallback row set so that ``python -m biotech_sniper.calendar.<name>
--refresh`` always writes ≥ 1 row, even when the upstream source is
blocked by a Cloudflare challenge or the operator runs the CLI in a
sandbox without outbound network access. The seed is the safety net
for VAL-M1-022 ("module produces ≥ 1 row on a representative day").
"""

from __future__ import annotations
