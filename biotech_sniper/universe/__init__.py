"""Reading-B universe construction modules.

This sub-package owns the M1 universe-foundation pipeline that
materialises ``russell2k_biotech``: an IWM-derived equity universe
filtered to the biotech SIC subset.

Pipeline overview
-----------------

1. :mod:`biotech_sniper.universe.iwm_importer` — download the
   iShares Russell 2000 ETF (IWM) holdings CSV from
   ``www.ishares.com``, parse it, and persist one
   ``iwm_holdings_snapshot`` row per ``(as_of_date, ticker)``.

2. :mod:`biotech_sniper.universe.russell_biotech` (separate
   feature) — intersect the latest ``iwm_holdings_snapshot`` with
   the SEC EDGAR SIC classifier output and write the resulting
   biotech-only set to the ``russell2k_biotech`` table.

Both modules expose a ``python -m`` CLI entrypoint suitable for
cron consumption. Refer to ``library/architecture.md`` for the
end-to-end data flow and the M1 validation contract.
"""

from __future__ import annotations

__all__: list[str] = []
