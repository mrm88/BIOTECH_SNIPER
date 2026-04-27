"""Database migrations for the biotech_sniper package.

Each migration lives in its own module under this package. The first
migration (``migrate_json_to_sqlite``) backfills the legacy JSON state
files (``state/active_plays.json``, ``state/resolved_plays.json``,
``state/performance_ledger.json``, ``state/discovery_state.json``,
``state/scoring_cache.json``) into the SQLite schema declared in
``biotech_sniper/db/schema.sql``.

Migrations are intended to be idempotent and safe to re-run.
"""
