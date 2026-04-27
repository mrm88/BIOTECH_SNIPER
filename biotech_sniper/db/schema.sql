-- Biotech Sniper — SQLite schema (M2 initial revision).
--
-- Conventions:
--   * Foreign keys are enforced (`PRAGMA foreign_keys=ON;` is set on
--     every connection by ``biotech_sniper.db.connect``).
--   * Journal mode is WAL for concurrent reads across cron units.
--   * Timestamps are stored as ISO-8601 ``TEXT`` (UTC by convention) so
--     they are human readable in ``sqlite3`` shells.
--   * Hot-path indices on ticker, nct_id, status, as_of_date and
--     called_at are declared at the bottom of this file.
--
-- The schema is applied by ``biotech_sniper.db.run_migrations`` which is
-- idempotent: every CREATE statement uses ``IF NOT EXISTS``, and the
-- ``schema_version`` table guards against re-applying a higher migration.

-- ---------------------------------------------------------------------------
-- Migration tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description   TEXT
);

-- ---------------------------------------------------------------------------
-- ``plays`` — unified active + resolved trades.
--
-- Active plays from ``active_plays.json`` (status='active' or 'monitor')
-- and resolved plays from ``resolved_plays.json`` (status='resolved')
-- live in the SAME table, distinguished by the ``status`` column.
--
-- Idempotency is enforced by the ``source_key`` column which encodes the
-- origin of every row (e.g. ``active:IDYA``, ``resolved:RVMD_125C``).
-- The migration uses ``INSERT OR REPLACE`` keyed on ``source_key`` so
-- re-running the migration on the same fixtures never duplicates rows.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plays (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key         TEXT    NOT NULL UNIQUE,
    ticker             TEXT    NOT NULL,
    nct_id             TEXT,
    status             TEXT    NOT NULL CHECK(status IN ('active', 'monitor', 'resolved')),
    direction          TEXT,
    catalyst_type      TEXT,
    catalyst_date      TEXT,
    entry_date         TEXT,
    exit_date          TEXT,
    option_type        TEXT,
    option_strike      REAL,
    option_expiry      TEXT,
    p_success          INTEGER,
    science_grade      TEXT,
    entry_stock        REAL,
    exit_stock         REAL,
    entry_fill         REAL,
    pnl_usd            REAL,
    option_pnl_pct     REAL,
    stock_move_pct     REAL,
    payload            TEXT,   -- full original JSON record for downstream use
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- ``performance_ledger`` — per-day P&L attribution rolled up across all
-- active + resolved plays in scope on a given date.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS performance_ledger (
    as_of_date          TEXT    NOT NULL PRIMARY KEY,
    realized_pnl_usd    REAL    NOT NULL DEFAULT 0,
    unrealized_pnl_usd  REAL    NOT NULL DEFAULT 0,
    play_count          INTEGER NOT NULL DEFAULT 0,
    notes               TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- ``discovery_state`` — deduped NCT-id seen-set + ancillary discovery
-- accumulators (8-K accessions, contract awards, AdComm IDs, RSS URLs).
-- The hot-path identifier is ``nct_id`` (PK) so ingestion modules can
-- ``INSERT OR IGNORE`` cheaply.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS discovery_state (
    nct_id          TEXT    NOT NULL PRIMARY KEY,
    first_seen_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source          TEXT
);

-- ---------------------------------------------------------------------------
-- ``scoring_cache`` — per (ticker, as_of_date) row holding fast-tier and
-- deep-tier scores plus the ensemble and divergence flag.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scoring_cache (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker               TEXT    NOT NULL,
    as_of_date           TEXT    NOT NULL,
    grok_rank            INTEGER,
    grok_score           REAL,
    claude_grade         TEXT,
    claude_probability   REAL,
    gemini_grade         TEXT,
    gemini_probability   REAL,
    science_grade        TEXT,
    ensemble_score       REAL,
    divergence_flag      INTEGER NOT NULL DEFAULT 0,
    payload              TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (ticker, as_of_date)
);

-- ---------------------------------------------------------------------------
-- ``llm_cost_ledger`` — one row per LLM provider call.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llm_cost_ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    provider           TEXT    NOT NULL CHECK(provider IN ('xai', 'anthropic', 'gemini')),
    model_id           TEXT    NOT NULL,
    purpose            TEXT,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    latency_ms         INTEGER,
    cost_usd           REAL,
    request_id         TEXT,
    called_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Hot-path indices.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_plays_ticker          ON plays(ticker);
CREATE INDEX IF NOT EXISTS idx_plays_nct_id          ON plays(nct_id);
CREATE INDEX IF NOT EXISTS idx_plays_status          ON plays(status);
CREATE INDEX IF NOT EXISTS idx_plays_entry_date      ON plays(entry_date);

CREATE INDEX IF NOT EXISTS idx_scoring_cache_ticker      ON scoring_cache(ticker);
CREATE INDEX IF NOT EXISTS idx_scoring_cache_as_of_date  ON scoring_cache(as_of_date);

CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_called_at ON llm_cost_ledger(called_at);
CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_provider  ON llm_cost_ledger(provider);

CREATE INDEX IF NOT EXISTS idx_discovery_state_nct_id    ON discovery_state(nct_id);
