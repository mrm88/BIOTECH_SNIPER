"""Calendar scrapers for the Reading-B trial-catalyst pipeline.

This package owns the calendar producers feeding the M1 / M2
``trial_calendar`` merge:

* :mod:`biotech_sniper.calendar.pdufa` — FDA PDUFA dates from
  BiopharmCatalyst (HTML) with seed-data fallback (f-m1-04).
* :mod:`biotech_sniper.calendar.ema` — EMA / CHMP meeting + opinion
  dates (f-m1-05).
* :mod:`biotech_sniper.calendar.trial_calendar` — merge layer that
  unions CT.gov + PDUFA + EMA into a single per-ticker catalyst
  lookup (f-m1-06).

Each producer is invokable as a documented ``python -m`` CLI and
writes into its own table with composite UNIQUE keys for natural
idempotency. Last-good fallback semantics are uniform across all
three: a 4xx/5xx/timeout from the upstream source results in a
non-zero exit and a verbatim-preserved prior snapshot.
"""

from __future__ import annotations
