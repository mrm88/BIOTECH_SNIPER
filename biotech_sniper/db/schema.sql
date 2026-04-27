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
-- ``universe`` — two-tier biotech ticker universe (M2 universe expansion).
--
-- Rows split between two tiers:
--   * ``tier='watch'``     — broad watch pool (SECTORS seed merged with
--                            auto-discovered NCT-sponsor tickers from the
--                            CT.gov delta-scan). Covers 550+ tickers.
--   * ``tier='tradeable'`` — strict subset of watch with
--                            ``has_options_chain=1``. Only this subset is
--                            eligible for full LLM scoring + paper trades.
--
-- Liquidity filter is intentionally permissive: any chain row from the
-- options-chain probe (seed-backed in M2, Alpaca-backed in M3) is enough
-- to flip the row to ``tier='tradeable'``. Spread / OI / volume filters
-- are explicitly NOT applied here per AGENTS.md "Universe boundaries".
--
-- Idempotency is enforced by the ``ticker`` PRIMARY KEY: re-running
-- ``build_universe()`` upserts each ticker rather than appending.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS universe (
    ticker               TEXT    NOT NULL PRIMARY KEY,
    tier                 TEXT    NOT NULL CHECK(tier IN ('watch', 'tradeable')),
    has_options_chain    BOOLEAN NOT NULL DEFAULT 0,
    last_chain_check_at  TEXT,
    source               TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- ``news_events`` — per-headline persistence (M2 daily news ingest).
--
-- One row per (ticker, source, url, published_at) tuple. Sources are
-- the four watcher modules:
--   * 'universal_news_watcher' (RSS + EDGAR scrape, hourly intraday)
--   * 'intraday_scan_news_rss' (Endpoints/STAT/GNW/PRN intraday)
--   * 'sec_8k_monitor'         (SEC EDGAR 8-K monitor, daily + intraday)
--   * 'ir_events_watcher'      (IR events page + CIK filings, daily)
--
-- Daily cron targets ALL ``universe.tier='watch'`` tickers. If a ticker
-- has zero feed results on a given day, ``audit_latest.json`` records
-- ``news_events_empty: {ticker: reason}`` so the M2-076 assertion can
-- pass via the empty-feed branch.
--
-- De-dup: ``UNIQUE(ticker, source, url, published_at)`` enforced via a
-- unique index using COALESCE so NULL-valued ``url`` / ``published_at``
-- columns still merge instead of stacking duplicates. Re-running the
-- daily cron on the same day inserts zero new rows for already-seen
-- headlines.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS news_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    published_at    TEXT,
    title           TEXT    NOT NULL,
    url             TEXT,
    ingested_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    raw_payload     TEXT
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

CREATE INDEX IF NOT EXISTS idx_universe_tier             ON universe(tier);
CREATE INDEX IF NOT EXISTS idx_universe_has_options      ON universe(has_options_chain);

CREATE INDEX IF NOT EXISTS idx_news_events_ticker        ON news_events(ticker);
CREATE INDEX IF NOT EXISTS idx_news_events_source        ON news_events(source);
CREATE INDEX IF NOT EXISTS idx_news_events_ingested_at   ON news_events(ingested_at);

-- NULL-safe dedup index: SQLite treats two NULLs as distinct in plain
-- UNIQUE constraints, which would let duplicate rows stack when
-- ``published_at`` or ``url`` are missing. Wrapping in COALESCE makes
-- empty / null values collapse to a single sentinel so re-running the
-- daily cron is genuinely idempotent for the (ticker, source, url,
-- published_at) tuple.
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_events_dedup
    ON news_events (
        ticker,
        source,
        COALESCE(url, ''),
        COALESCE(published_at, '')
    );

-- ---------------------------------------------------------------------------
-- ``llm_debate`` — multi-round LLM head-to-head debate transcripts
-- (M2 feature f-m2-12). One row per debate round, linked back to the
-- :data:`scoring_cache` row that fired the debate.
--
-- Triggers (``trigger`` column):
--   * ``'divergence'`` — fired by :mod:`biotech_sniper.llm.llm_debate`
--     when a ``scoring_cache`` row's ``divergence_flag`` is ``1`` for
--     a top-N candidate. Round 1: Claude critiques Gemini's rationale
--     and re-grades. Round 2: Gemini rebuts Claude and re-grades.
--     Round 3: Grok adjudicates and emits a ``final_grade``.
--   * ``'rotation'`` — fired by the M3 rotation engine
--     (:mod:`biotech_sniper.rotation_engine`) before swapping an
--     incumbent active play for a higher-ranked challenger. Same
--     three-round structure (incumbent vs. challenger).
--
-- Hard caps enforced inside :mod:`biotech_sniper.llm.llm_debate`:
--   * ``LLM_DEBATE_MAX_ROUNDS = 3``
--   * ``LLM_DEBATE_DAILY_USD_CAP = 10`` (sum of ``cost_usd`` across
--     today's rows). Once the cap is hit, ``run_debate(...)`` returns
--     the static-ensemble result and writes a
--     ``llm_debate_short_circuit`` key into ``state/audit_latest.json``.
--
-- ``final_grade`` is populated only on the final adjudicating round
-- (Grok); earlier rounds carry a ``NULL`` final_grade so a single
-- ``MAX(final_grade)`` aggregation is safe per debate. Downstream
-- consumers (``play_card_formatter.emit_play_cards``) replace the
-- static ``science_grade`` on the play card with this debate
-- ``final_grade`` whenever it is non-NULL.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llm_debate (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    scoring_cache_id         INTEGER,
    trigger                  TEXT    NOT NULL CHECK(trigger IN ('divergence', 'rotation')),
    round_index              INTEGER NOT NULL,
    model                    TEXT    NOT NULL,
    prompt                   TEXT,
    response                 TEXT,
    latency_ms               INTEGER,
    cost_usd                 REAL,
    final_grade              TEXT,
    transcript_complete_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (scoring_cache_id) REFERENCES scoring_cache(id)
);

CREATE INDEX IF NOT EXISTS idx_llm_debate_scoring_cache_id
    ON llm_debate(scoring_cache_id);
CREATE INDEX IF NOT EXISTS idx_llm_debate_trigger
    ON llm_debate(trigger);
CREATE INDEX IF NOT EXISTS idx_llm_debate_transcript_complete_at
    ON llm_debate(transcript_complete_at);
