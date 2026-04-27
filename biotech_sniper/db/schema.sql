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
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT    NOT NULL,
    source            TEXT    NOT NULL,
    published_at      TEXT,
    title             TEXT    NOT NULL,
    url               TEXT,
    ingested_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    raw_payload       TEXT,
    -- f-m3-09: optional LLM-enrichment tag attached to the headline
    -- (e.g. ``'negative_material'`` for an adverse-news exit hook
    -- target). NULL for un-enriched rows.
    enrichment_label  TEXT
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

-- ---------------------------------------------------------------------------
-- ``paper_orders`` — persisted Alpaca paper-trading order lifecycle.
--
-- Originally named ``orders`` (f-m3-03..f-m3-09), renamed to
-- ``paper_orders`` in f-m3-11 to align with the M3 validation
-- contract VAL-M3-061+ which makes the broker target explicit
-- (paper sandbox) and to free the unqualified ``orders`` namespace
-- for any future shared/internal order ledger. The
-- :func:`run_migrations` helper performs an idempotent
-- ``ALTER TABLE orders RENAME TO paper_orders`` for legacy
-- databases; new dbs land directly on the ``paper_orders`` name.
--
-- One row per order intent. Successful submissions write a row with
-- ``status`` reflecting the broker's last-known state (typically
-- ``'submitted'`` or ``'accepted'`` immediately after the call, then
-- transitioned to ``'filled'`` by a downstream poll loop). Rejections
-- write a row with ``status='rejected'`` and ``reason`` set to the
-- broker error message so the failure reason is queryable from
-- SQLite without consulting the log file.
--
-- Idempotency: ``id`` is an internally-generated UUID4 (one per
-- :func:`PaperExecutor.execute` invocation). ``alpaca_order_id`` is
-- the broker-assigned id (NULL on rejection paths where the broker
-- never returned an id). ``play_card_id`` links back to the play
-- card that triggered the entry. ``parent_play_card_id`` is set on
-- exit orders (e.g. iv_crush_exit) to point at the parent entry's
-- ``play_card_id``.
--
-- f-m3-11 augmentation
-- ~~~~~~~~~~~~~~~~~~~~
-- * ``requested_mid_at_submit REAL`` — snapshot of the option mid
--   ((bid+ask)/2) at the moment the row is written. Used by
--   :mod:`biotech_sniper.execution_fills` to compute side-aware
--   slippage when a fill arrives.
-- * ``purpose TEXT CHECK(...)`` — disambiguates entry vs exit vs
--   liquidity-probe orders so downstream consumers can filter
--   probe traffic out of real-entry analytics. Allowed values:
--   ``'entry'``, ``'exit'``, ``'liquidity_probe'``.
-- * ``client_order_id TEXT NOT NULL UNIQUE`` — deterministic id
--   stamped by the executor BEFORE submission. Enables the
--   write-then-submit invariant (VAL-M3-069): a row exists locally
--   before any Alpaca call, and a same-day re-submission of the
--   same logical exit short-circuits on the UNIQUE conflict instead
--   of double-submitting to the broker.
--
-- f-m3-09 ``event`` CHECK constraint
-- ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
-- The ``event`` enum is intentionally open to NULL so legacy rows
-- written before the f-m3-09 migration remain valid. The wire
-- value ``'iv_crush_exit'`` is preserved (rather than ``'iv_crush'``
-- from the original spec) because f-m3-05 already shipped that
-- exact string per VAL-M3-028 evidence.
CREATE TABLE IF NOT EXISTS paper_orders (
    id                          TEXT    NOT NULL PRIMARY KEY,
    play_card_id                TEXT,
    alpaca_order_id             TEXT,
    symbol                      TEXT,
    side                        TEXT,
    qty                         INTEGER,
    status                      TEXT    NOT NULL,
    reason                      TEXT,
    event                       TEXT    CHECK(
        event IS NULL OR
        event IN ('open','iv_crush_exit','stop_loss','adverse_news','rotation')
    ),
    parent_play_card_id         TEXT,
    requested_mid_at_submit     REAL,
    purpose                     TEXT    CHECK(
        purpose IS NULL OR
        purpose IN ('entry','exit','liquidity_probe')
    ),
    client_order_id             TEXT    NOT NULL UNIQUE,
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_play_card_id      ON paper_orders(play_card_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_alpaca_id         ON paper_orders(alpaca_order_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_status            ON paper_orders(status);
CREATE INDEX IF NOT EXISTS idx_paper_orders_event             ON paper_orders(event);
CREATE INDEX IF NOT EXISTS idx_paper_orders_purpose           ON paper_orders(purpose);
CREATE INDEX IF NOT EXISTS idx_paper_orders_client_order_id   ON paper_orders(client_order_id);

-- ---------------------------------------------------------------------------
-- ``execution_events`` — per-order lifecycle events (f-m3-11).
--
-- One row per Alpaca order-state change, written by
-- :mod:`biotech_sniper.execution_subscriber` as it observes the
-- broker (poll or stream). The ``event_type`` enum mirrors the
-- closed set of broker states the executor cares about; anything
-- outside the enum is rejected by the CHECK constraint at insert
-- time. ``raw_payload`` captures the broker JSON (best-effort) for
-- post-hoc forensics — the DB stays self-contained for replay.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_order_id    TEXT    NOT NULL,
    event_type        TEXT    NOT NULL CHECK(
        event_type IN ('submitted','accepted','partial_fill',
                       'filled','canceled','expired','rejected')
    ),
    event_at          TEXT    NOT NULL,
    raw_payload       TEXT,
    FOREIGN KEY (paper_order_id) REFERENCES paper_orders(id)
);

CREATE INDEX IF NOT EXISTS idx_execution_events_paper_order_id
    ON execution_events(paper_order_id);
CREATE INDEX IF NOT EXISTS idx_execution_events_event_at
    ON execution_events(event_at);
CREATE INDEX IF NOT EXISTS idx_execution_events_event_type
    ON execution_events(event_type);

-- ---------------------------------------------------------------------------
-- ``execution_fills`` — per-fill records with side-aware slippage
-- (f-m3-11).
--
-- One row per fill (partial OR full). ``slippage_bps`` is computed
-- with the mid snapshot stored on the parent ``paper_orders`` row at
-- submit time:
--
--     slippage_bps = ((filled_price - requested_mid_at_submit) /
--                     requested_mid_at_submit) * 10000
--                 * (+1 if side='buy' else -1)
--
-- The side-orientation flips sign so a ``buy`` filled ABOVE mid is
-- reported as positive bps (worse-than-mid slippage), and a
-- ``sell`` filled BELOW mid is also positive bps. Validators
-- (VAL-M3-063) recompute this and assert equality within 1e-6.
-- ``time_to_fill_ms`` is measured from the parent
-- ``paper_orders.created_at`` to the fill timestamp. ``partial_qty_remaining``
-- is the open contract count after this fill (0 once fully filled).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution_fills (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_order_id              TEXT    NOT NULL,
    filled_at                   TEXT    NOT NULL,
    filled_price                REAL    NOT NULL,
    filled_qty                  INTEGER NOT NULL,
    requested_mid_at_submit     REAL    NOT NULL,
    slippage_bps                REAL    NOT NULL,
    slippage_usd                REAL    NOT NULL,
    time_to_fill_ms             INTEGER NOT NULL,
    partial_qty_remaining       INTEGER NOT NULL,
    FOREIGN KEY (paper_order_id) REFERENCES paper_orders(id)
);

CREATE INDEX IF NOT EXISTS idx_execution_fills_paper_order_id
    ON execution_fills(paper_order_id);
CREATE INDEX IF NOT EXISTS idx_execution_fills_filled_at
    ON execution_fills(filled_at);
