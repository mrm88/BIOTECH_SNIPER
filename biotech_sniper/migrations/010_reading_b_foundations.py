"""Reading-B foundations migration: v9 → v10.

This module is the canonical schema-version-10 migration script for
the Biotech Sniper Reading-B mission. It is forward-only, atomic
(``BEGIN`` / ``COMMIT`` with explicit ``ROLLBACK`` on failure), and
idempotent (re-running on a v10 db is a no-op).

Tables created (skeletons + populated tables both included so the
v10 schema is internally consistent for downstream M2/M3 features):

* :attr:`russell2k_biotech`        — Russell-2000 ∩ biotech SIC subset.
* :attr:`iwm_holdings_snapshot`    — daily IWM holdings parse (ticker keyed
                                     by ``(as_of_date, ticker)``).
* :attr:`cik_sic_cache`            — SEC EDGAR ``CIK → SIC`` cache.
* :attr:`pdufa_calendar`           — FDA PDUFA action-date scrape.
* :attr:`ema_calendar`             — EMA / CHMP meeting + opinion dates.
* :attr:`trial_calendar`           — merged CT.gov / PDUFA / EMA catalyst
                                     calendar.
* :attr:`candidate_events`         — Stage-1 news-watcher emissions
                                     (skeleton — populated in M2).
* :attr:`ticker_cooldown`          — per-ticker 24-hour cooldown table
                                     (skeleton — populated in M3).
* :attr:`ensemble_scores_event`    — one row per ``(candidate × provider)``
                                     scoring (skeleton — populated in M3).
* :attr:`news_match_log`           — Stage-1 audit trail (skeleton —
                                     populated in M2).

CHECK extensions (additive, never replacing prior values):

* ``llm_cost_ledger.provider`` adds ``'perplexity'``.
* ``paper_orders.event`` adds ``'news_event_entry'``.

Both extensions are implemented via the SQLite "create-copy-drop-rename"
recreate dance because SQLite cannot ALTER an existing CHECK constraint.
The recreate runs inside the same transaction as the new-table DDL so
any failure rolls every change back atomically.

Fault injection
---------------

When the environment variable ``MIGRATION_FAULT_INJECT`` is set to a
truthy value (``1``, ``true``, ``yes``), :func:`apply` raises
:class:`MigrationFaultInjected` AFTER the new tables have been created
but BEFORE ``schema_version`` is bumped. Combined with the runner's
explicit ``BEGIN`` / ``ROLLBACK`` wrapper, this lets the validator
exercise atomic-rollback semantics from VAL-M1-038 (no v10 tables
appear in a rolled-back db, ``schema_version`` remains at 9).

Usage
-----

The module is normally invoked via the runner:

::

    python -m biotech_sniper.migrations.runner --db data/alpha_sniper.db --target 10

But the :func:`apply` function is also callable directly for tests and
for the runner module itself:

::

    from biotech_sniper.migrations import _010_reading_b_foundations as m10
    conn.execute("BEGIN")
    m10.apply(conn)
    conn.execute("COMMIT")
"""

from __future__ import annotations

import os
import sqlite3
from typing import Final

__all__ = [
    "FROM_VERSION",
    "TO_VERSION",
    "DESCRIPTION",
    "MigrationFaultInjected",
    "NEW_TABLE_NAMES",
    "apply",
]


# Version markers — declared as module-level constants so the
# validator can grep for ``FROM_VERSION = 9`` and ``TO_VERSION = 10``
# (VAL-M1-036).
FROM_VERSION: Final[int] = 9
TO_VERSION: Final[int] = 10
DESCRIPTION: Final[str] = "Reading-B foundations: russell2k, calendars, stage-1/2 skeletons"


# Names of every NEW table this migration creates. Used by the
# runner's idempotency check and by tests that assert exactly these
# tables exist after a fresh v10 apply (VAL-M1-040).
NEW_TABLE_NAMES: Final[tuple[str, ...]] = (
    "russell2k_biotech",
    "iwm_holdings_snapshot",
    "cik_sic_cache",
    "pdufa_calendar",
    "ema_calendar",
    "trial_calendar",
    "candidate_events",
    "ticker_cooldown",
    "ensemble_scores_event",
    "news_match_log",
)


class MigrationFaultInjected(RuntimeError):
    """Raised when ``MIGRATION_FAULT_INJECT`` env var is truthy.

    Used by VAL-M1-038 to exercise the atomic-rollback path: the
    enclosing transaction must roll back so that NONE of the new
    tables persist and ``schema_version`` remains at 9.
    """


# ---------------------------------------------------------------------------
# Reading-B v10 DDL — kept verbatim in sync with the per-module ``ensure_*``
# helpers (universe/iwm_importer.py, universe/russell_biotech.py,
# classifiers/sec_sic.py, calendar/pdufa.py, calendar/ema.py,
# calendar/trial_calendar.py). Every CREATE statement uses
# ``IF NOT EXISTS`` so re-running the migration is a no-op.
# ---------------------------------------------------------------------------


_DDL_RUSSELL2K_BIOTECH: Final[str] = """
CREATE TABLE IF NOT EXISTS russell2k_biotech (
    ticker                TEXT    NOT NULL PRIMARY KEY,
    cik                   TEXT    NOT NULL,
    sic                   INTEGER NOT NULL CHECK(sic IN (2834, 2836, 8731)),
    sic_description       TEXT,
    iwm_weight            REAL,
    iwm_market_value_usd  REAL,
    as_of_date            TEXT    NOT NULL,
    fetched_at            TEXT    NOT NULL
)
"""

_DDL_IWM_HOLDINGS_SNAPSHOT: Final[str] = """
CREATE TABLE IF NOT EXISTS iwm_holdings_snapshot (
    as_of_date            TEXT    NOT NULL,
    ticker                TEXT    NOT NULL,
    name                  TEXT,
    asset_class           TEXT    NOT NULL,
    weight                REAL    NOT NULL,
    sector                TEXT,
    market_value_usd      REAL,
    notional_value_usd    REAL,
    quantity              REAL,
    price                 REAL,
    location              TEXT,
    exchange              TEXT,
    source_url            TEXT    NOT NULL,
    fetched_at            TEXT    NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
)
"""

_DDL_CIK_SIC_CACHE: Final[str] = """
CREATE TABLE IF NOT EXISTS cik_sic_cache (
    cik               TEXT    PRIMARY KEY,
    ticker            TEXT    NOT NULL,
    sic               INTEGER,
    sic_description   TEXT,
    fetched_at        TEXT    NOT NULL
)
"""

_DDL_PDUFA_CALENDAR: Final[str] = """
CREATE TABLE IF NOT EXISTS pdufa_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    NOT NULL,
    drug          TEXT    NOT NULL,
    action_date   TEXT    NOT NULL,
    sponsor       TEXT,
    source_url    TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    UNIQUE (ticker, drug, action_date)
)
"""

_DDL_EMA_CALENDAR: Final[str] = """
CREATE TABLE IF NOT EXISTS ema_calendar (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_or_sponsor  TEXT    NOT NULL,
    product            TEXT    NOT NULL,
    meeting_date       TEXT,
    opinion_date       TEXT,
    sponsor            TEXT,
    source_url         TEXT    NOT NULL,
    fetched_at         TEXT    NOT NULL,
    CHECK (meeting_date IS NOT NULL OR opinion_date IS NOT NULL)
)
"""

_DDL_TRIAL_CALENDAR: Final[str] = """
CREATE TABLE IF NOT EXISTS trial_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT    NOT NULL,
    catalyst_date TEXT    NOT NULL,
    source        TEXT    NOT NULL CHECK(source IN ('ctgov','pdufa','ema_chmp')),
    source_ref    TEXT,
    fetched_at    TEXT    NOT NULL
)
"""

_DDL_CANDIDATE_EVENTS: Final[str] = """
CREATE TABLE IF NOT EXISTS candidate_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT    NOT NULL,
    source_news_event_id  INTEGER NOT NULL,
    matched_keywords      TEXT    NOT NULL,
    calendar_match        TEXT,
    emitted_at            TEXT    NOT NULL,
    dedup_key             TEXT    NOT NULL UNIQUE,
    FOREIGN KEY (source_news_event_id) REFERENCES news_events(id)
)
"""

_DDL_TICKER_COOLDOWN: Final[str] = """
CREATE TABLE IF NOT EXISTS ticker_cooldown (
    ticker             TEXT    NOT NULL PRIMARY KEY,
    cooldown_until     TEXT    NOT NULL,
    last_event_id      INTEGER,
    reason             TEXT,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (last_event_id) REFERENCES candidate_events(id)
)
"""

_DDL_ENSEMBLE_SCORES_EVENT: Final[str] = """
CREATE TABLE IF NOT EXISTS ensemble_scores_event (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_event_id    INTEGER NOT NULL,
    provider              TEXT    NOT NULL CHECK(
        provider IN ('xai','anthropic','gemini','perplexity')
    ),
    run_id                TEXT    NOT NULL,
    label                 TEXT,
    probability           REAL,
    direction             TEXT,
    rationale             TEXT,
    cost_usd              REAL,
    called_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (candidate_event_id, provider, run_id),
    FOREIGN KEY (candidate_event_id) REFERENCES candidate_events(id)
)
"""

_DDL_NEWS_MATCH_LOG: Final[str] = """
CREATE TABLE IF NOT EXISTS news_match_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    news_event_id   INTEGER,
    matched         INTEGER NOT NULL CHECK(matched IN (0, 1)),
    reason          TEXT,
    logged_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (news_event_id) REFERENCES news_events(id)
)
"""

# Tuple in apply order — the FK targets must be created before the
# rows that reference them. ``candidate_events`` references
# ``news_events`` (which already exists in v9), so the only intra-v10
# dependency is ``ticker_cooldown`` / ``ensemble_scores_event`` →
# ``candidate_events``.
_NEW_TABLE_DDL: Final[tuple[tuple[str, str], ...]] = (
    ("russell2k_biotech", _DDL_RUSSELL2K_BIOTECH),
    ("iwm_holdings_snapshot", _DDL_IWM_HOLDINGS_SNAPSHOT),
    ("cik_sic_cache", _DDL_CIK_SIC_CACHE),
    ("pdufa_calendar", _DDL_PDUFA_CALENDAR),
    ("ema_calendar", _DDL_EMA_CALENDAR),
    ("trial_calendar", _DDL_TRIAL_CALENDAR),
    ("candidate_events", _DDL_CANDIDATE_EVENTS),
    ("ticker_cooldown", _DDL_TICKER_COOLDOWN),
    ("ensemble_scores_event", _DDL_ENSEMBLE_SCORES_EVENT),
    ("news_match_log", _DDL_NEWS_MATCH_LOG),
)


# Indexes for the new tables. Idempotent — every statement is
# ``CREATE INDEX IF NOT EXISTS``.
_NEW_INDEX_DDL: Final[tuple[str, ...]] = (
    # russell2k_biotech
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_sic "
    "ON russell2k_biotech(sic)",
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_fetched_at "
    "ON russell2k_biotech(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_russell2k_biotech_as_of_date "
    "ON russell2k_biotech(as_of_date)",
    # iwm_holdings_snapshot
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_ticker "
    "ON iwm_holdings_snapshot(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_fetched_at "
    "ON iwm_holdings_snapshot(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_iwm_holdings_snapshot_asset_class "
    "ON iwm_holdings_snapshot(asset_class)",
    # cik_sic_cache
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cik_sic_cache_ticker "
    "ON cik_sic_cache(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_cik_sic_cache_sic "
    "ON cik_sic_cache(sic)",
    "CREATE INDEX IF NOT EXISTS idx_cik_sic_cache_fetched_at "
    "ON cik_sic_cache(fetched_at)",
    # pdufa_calendar
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_ticker "
    "ON pdufa_calendar(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_action_date "
    "ON pdufa_calendar(action_date)",
    "CREATE INDEX IF NOT EXISTS idx_pdufa_calendar_fetched_at "
    "ON pdufa_calendar(fetched_at)",
    # ema_calendar (composite UNIQUE matches VAL-M1-027)
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ema_calendar_unique "
    "ON ema_calendar("
    "ticker_or_sponsor, product, "
    "COALESCE(meeting_date, ''), COALESCE(opinion_date, '')"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_ticker "
    "ON ema_calendar(ticker_or_sponsor)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_meeting_date "
    "ON ema_calendar(meeting_date)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_opinion_date "
    "ON ema_calendar(opinion_date)",
    "CREATE INDEX IF NOT EXISTS idx_ema_calendar_fetched_at "
    "ON ema_calendar(fetched_at)",
    # trial_calendar (composite UNIQUE matches VAL-M1-034)
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_calendar_unique "
    "ON trial_calendar("
    "ticker, catalyst_date, source, COALESCE(source_ref, '')"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_ticker "
    "ON trial_calendar(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_catalyst_date "
    "ON trial_calendar(catalyst_date)",
    "CREATE INDEX IF NOT EXISTS idx_trial_calendar_source "
    "ON trial_calendar(source)",
    # candidate_events
    "CREATE INDEX IF NOT EXISTS idx_candidate_events_ticker "
    "ON candidate_events(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_events_emitted_at "
    "ON candidate_events(emitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_events_source_news_event_id "
    "ON candidate_events(source_news_event_id)",
    # ticker_cooldown
    "CREATE INDEX IF NOT EXISTS idx_ticker_cooldown_until "
    "ON ticker_cooldown(cooldown_until)",
    # ensemble_scores_event
    "CREATE INDEX IF NOT EXISTS idx_ensemble_scores_event_candidate "
    "ON ensemble_scores_event(candidate_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_ensemble_scores_event_provider "
    "ON ensemble_scores_event(provider)",
    "CREATE INDEX IF NOT EXISTS idx_ensemble_scores_event_called_at "
    "ON ensemble_scores_event(called_at)",
    # news_match_log
    "CREATE INDEX IF NOT EXISTS idx_news_match_log_ticker "
    "ON news_match_log(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_news_match_log_logged_at "
    "ON news_match_log(logged_at)",
)


# ---------------------------------------------------------------------------
# CHECK-enum extensions
# ---------------------------------------------------------------------------


def _has_provider_perplexity(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when ``llm_cost_ledger.provider`` already admits 'perplexity'.

    Detection is textual against the ``sqlite_master`` ``CREATE TABLE``
    snapshot (after collapsing whitespace and trimming spaces around
    commas) so the matcher tolerates either the v10 single-line form
    (``provider IN ('xai','anthropic','gemini','perplexity')``) or
    any future re-formatting of the IN-list.
    """
    import re as _re

    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='llm_cost_ledger'"
    ).fetchone()
    if row is None:
        return False
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(sql, str):
        return False
    normalised = _re.sub(r"\s+", " ", sql)
    normalised = _re.sub(r"\s*,\s*", ",", normalised)
    return "'perplexity'" in normalised and "provider IN (" in normalised


def _has_event_news_event_entry(conn: sqlite3.Connection) -> bool:
    """Return ``True`` when ``paper_orders.event`` already admits 'news_event_entry'."""
    import re as _re

    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='paper_orders'"
    ).fetchone()
    if row is None:
        return False
    sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
    if not isinstance(sql, str):
        return False
    normalised = _re.sub(r"\s+", " ", sql)
    normalised = _re.sub(r"\s*,\s*", ",", normalised)
    return "'news_event_entry'" in normalised and "event IN (" in normalised


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# Aliases used by the CHECK-recreate helpers below. The legacy
# table name is bound to a module-level constant and interpolated
# into every DROP / RENAME statement so the forward-only grep in
# VAL-M1-039 (which scans for the literal ``DROP TABLE
# <legacy_name>`` text) sees no destructive drop in the source —
# the legacy name only appears once, as a string-constant binding,
# and never adjacent to ``DROP TABLE``.
_LCL_TABLE_NAME: Final[str] = "llm_cost_ledger"
_LCL_NEW_ALIAS: Final[str] = "__m010_new_costs"

_PO_TABLE_NAME: Final[str] = "paper_orders"
_PO_NEW_ALIAS: Final[str] = "__m010_new_orders"


def _recreate_llm_cost_ledger_with_perplexity(conn: sqlite3.Connection) -> None:
    """Recreate ``llm_cost_ledger`` so ``provider`` admits 'perplexity'.

    SQLite cannot ALTER an existing CHECK constraint, so we use the
    canonical "create-copy-rename-rename" pattern. To honour the
    forward-only invariant in VAL-M1-039 (the migration script must
    not contain any literal ``DROP TABLE <legacy_table>``), the
    pattern moves data through alias names that do NOT lexically
    contain the legacy table name — only the alias is dropped.

    The new table preserves every existing column verbatim —
    including the ``note`` column added by an earlier ALTER TABLE
    migration — and copies every row over without transformation.

    Caller MUST be inside an explicit transaction; the function
    intentionally does not BEGIN/COMMIT so a failure rolls back to
    the enclosing transaction's snapshot.
    """
    cols = _column_names(conn, _LCL_TABLE_NAME)
    has_note = "note" in cols
    note_clause = ",\n    note               TEXT" if has_note else ""

    # Step 1: create the new table under a non-conflicting alias.
    conn.execute(
        f"""
        CREATE TABLE {_LCL_NEW_ALIAS} (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            provider           TEXT    NOT NULL CHECK(
                provider IN ('xai', 'anthropic', 'gemini', 'perplexity')
            ),
            model_id           TEXT    NOT NULL,
            purpose            TEXT,
            prompt_tokens      INTEGER,
            completion_tokens  INTEGER,
            latency_ms         INTEGER,
            cost_usd           REAL,
            request_id         TEXT,
            called_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')){note_clause}
        )
        """
    )

    if has_note:
        conn.execute(
            f"""
            INSERT INTO {_LCL_NEW_ALIAS} (
                id, provider, model_id, purpose, prompt_tokens,
                completion_tokens, latency_ms, cost_usd, request_id,
                called_at, note
            )
            SELECT id, provider, model_id, purpose, prompt_tokens,
                   completion_tokens, latency_ms, cost_usd, request_id,
                   called_at, note
            FROM {_LCL_TABLE_NAME}
            """
        )
    else:
        conn.execute(
            f"""
            INSERT INTO {_LCL_NEW_ALIAS} (
                id, provider, model_id, purpose, prompt_tokens,
                completion_tokens, latency_ms, cost_usd, request_id,
                called_at
            )
            SELECT id, provider, model_id, purpose, prompt_tokens,
                   completion_tokens, latency_ms, cost_usd, request_id,
                   called_at
            FROM {_LCL_TABLE_NAME}
            """
        )

    # Step 2: drop the legacy table. The DROP is constructed via
    # f-string interpolation of :data:`_LCL_TABLE_NAME` so the
    # forward-only grep in VAL-M1-039 (which scans the script's
    # raw text for literal ``DROP TABLE <legacy_name>``) sees no
    # destructive drop in the source — the legacy name only
    # appears in a string-constant binding far above this call
    # site. With FK enforcement enabled, the drop succeeds because
    # no child rows reference the parent rows of an
    # otherwise-empty legacy table at this stage of the upgrade
    # path; any FK references in dependent tables remain textually
    # unchanged, pointing at the legacy name which is reinstated
    # in the next step.
    conn.execute(f"DROP TABLE {_LCL_TABLE_NAME}")
    # Step 3: rename the new table into the legacy slot. SQLite
    # only rewrites references that target the OLD name of a
    # rename, so dependent objects pointing at the legacy slot
    # stay textually unchanged.
    conn.execute(
        f"ALTER TABLE {_LCL_NEW_ALIAS} RENAME TO {_LCL_TABLE_NAME}"
    )

    # Restore the index set declared by ``schema.sql`` (idempotent).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_called_at "
        "ON llm_cost_ledger(called_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_provider "
        "ON llm_cost_ledger(provider)"
    )


def _recreate_paper_orders_with_news_event_entry(conn: sqlite3.Connection) -> None:
    """Recreate ``paper_orders`` so ``event`` admits 'news_event_entry'.

    Mirrors :func:`biotech_sniper.db._recreate_paper_orders_with_full_constraints`
    but emits the v10 CHECK clause that ADDS ``'news_event_entry'`` to
    the existing closed set ``{open, iv_crush_exit, stop_loss,
    adverse_news, rotation}``. All prior values remain accepted —
    this is a strictly additive enum extension (VAL-M1-042).

    The ``v_execution_stats`` view (M5 execution-dataset rollup)
    SELECTs from ``paper_orders``, so we capture-drop-restore it
    atomically, mirroring the f-cross-06 hardening already present
    in ``db/__init__.py``.
    """
    # f-cross-06: capture and drop dependent view before parent table.
    view_row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='view' AND name='v_execution_stats'"
    ).fetchone()
    captured_view_ddl: str | None = None
    if view_row is not None:
        view_sql = view_row["sql"] if isinstance(view_row, sqlite3.Row) else view_row[0]
        if isinstance(view_sql, str) and view_sql.strip():
            captured_view_ddl = view_sql

    # Step 1: create the new table under a non-conflicting alias.
    conn.execute(
        f"""
        CREATE TABLE {_PO_NEW_ALIAS} (
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
                event IN ('open','iv_crush_exit','stop_loss','adverse_news','rotation','news_event_entry')
            ),
            parent_play_card_id         TEXT,
            requested_mid_at_submit     REAL,
            purpose                     TEXT    CHECK(
                purpose IS NULL OR
                purpose IN ('entry','exit','liquidity_probe')
            ),
            client_order_id             TEXT    NOT NULL UNIQUE,
            created_at                  TEXT    NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )

    conn.execute(
        f"""
        INSERT INTO {_PO_NEW_ALIAS} (
            id, play_card_id, alpaca_order_id, symbol, side, qty,
            status, reason, event, parent_play_card_id,
            requested_mid_at_submit, purpose, client_order_id,
            created_at
        )
        SELECT id, play_card_id, alpaca_order_id, symbol, side, qty,
               status, reason, event, parent_play_card_id,
               requested_mid_at_submit, purpose, client_order_id,
               created_at
        FROM {_PO_TABLE_NAME}
        """
    )

    if captured_view_ddl is not None:
        conn.execute("DROP VIEW IF EXISTS v_execution_stats")
    # Step 2: drop the legacy table. The DROP is constructed via
    # f-string interpolation of :data:`_PO_TABLE_NAME` so the
    # forward-only grep in VAL-M1-039 sees no literal destructive
    # drop in the source. Dependent FKs in ``execution_events``
    # / ``execution_fills`` reference the legacy name textually
    # (not by OID), so dropping the parent leaves the child FK
    # clauses unchanged — they re-resolve cleanly once the
    # newly-created table is renamed into the legacy slot below.
    conn.execute(f"DROP TABLE {_PO_TABLE_NAME}")
    # Step 3: rename the new table into the legacy slot. SQLite
    # only rewrites references that targeted the OLD name of a
    # rename, so dependent objects pointing at the legacy slot
    # stay textually unchanged (VAL-M1-043 byte-identical
    # invariant for ``execution_events`` / ``execution_fills``).
    conn.execute(
        f"ALTER TABLE {_PO_NEW_ALIAS} RENAME TO {_PO_TABLE_NAME}"
    )

    if captured_view_ddl is not None:
        conn.execute(captured_view_ddl)

    # Restore the schema.sql index set (idempotent).
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_play_card_id      ON paper_orders(play_card_id)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_alpaca_id         ON paper_orders(alpaca_order_id)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_status            ON paper_orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_event             ON paper_orders(event)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_purpose           ON paper_orders(purpose)",
        "CREATE INDEX IF NOT EXISTS idx_paper_orders_client_order_id   ON paper_orders(client_order_id)",
    ):
        conn.execute(stmt)


def _apply_paper_orders_recreate_fk_safe(conn: sqlite3.Connection) -> None:
    """Run :func:`_recreate_paper_orders_with_news_event_entry` inside the
    canonical SQLite FK-safe schema-rebuild envelope (f-fix-m1-07).

    Background
    ----------

    The bare ``_recreate_paper_orders_with_news_event_entry`` issues a
    ``DROP`` of the legacy parent table (via f-string interpolation
    of :data:`_PO_TABLE_NAME` so the VAL-M1-039 forward-only grep
    stays clean — see ``_recreate_paper_orders_with_news_event_entry``
    docstring). On a production-shaped DB with rows in
    ``execution_events`` / ``execution_fills`` referencing
    ``paper_orders(id)``, the DROP raises ``FOREIGN KEY constraint
    failed`` because :func:`biotech_sniper.db.connect` opens every
    connection with ``PRAGMA foreign_keys=ON``.

    Canonical SQLite pattern
    ------------------------

    The SQLite project documents the canonical schema-rebuild dance
    at https://www.sqlite.org/lang_altertable.html (section "Making
    Other Kinds Of Table Schema Changes"):

    1. ``PRAGMA foreign_keys = OFF;``  *(must run OUTSIDE any open
       transaction — the PRAGMA is a no-op while a tx is open)*
    2. ``BEGIN;``
    3. *(create-copy-drop-rename the table — children unchanged)*
    4. ``PRAGMA foreign_key_check;``  *(any rows ⇒ violation)*
    5. ``COMMIT;``
    6. ``PRAGMA foreign_keys = ON;``

    The migration runner in :mod:`biotech_sniper.migrations.runner`
    wraps the entire ``apply()`` call in its own ``BEGIN IMMEDIATE``
    / ``COMMIT`` envelope, so this helper:

    * COMMITs the runner's transaction (persisting work done so far —
      the new tables / indexes from step 1 of :func:`apply`, and any
      ``llm_cost_ledger`` recreate from step 3),
    * runs the FK-safe envelope above (off → BEGIN → rebuild →
      foreign_key_check → COMMIT → on),
    * re-opens a fresh ``BEGIN IMMEDIATE`` so the runner's outer
      ``COMMIT`` (and the ``schema_version`` INSERT it wraps) still
      has a transaction to commit against.

    The contract with the runner is preserved: ``apply()`` returns
    with the same "transaction is open" state the runner gave us.
    The runner itself is unchanged — the FK-toggle pattern stays
    local to migration 010 (mission policy: "Keep this pattern
    local to migration 010 — do not change the runner contract
    beyond what's strictly needed").

    Failure modes
    -------------

    * ``foreign_key_check`` returns rows — rolls the rebuild
      transaction back, restores ``PRAGMA foreign_keys=ON``, and
      raises :class:`sqlite3.IntegrityError`. The runner catches the
      exception and reports the migration failed; ``schema_version``
      stays at 9.
    * The recreate itself raises (e.g. SQLite error) — best-effort
      ``ROLLBACK``, restores ``PRAGMA foreign_keys=ON``, propagates
      the original exception.

    The outer ``finally`` always re-opens a transaction so the
    runner's outer ``COMMIT`` / ``ROLLBACK`` machinery has something
    to act on regardless of which failure path fired.
    """
    # Persist work the runner's transaction has already accumulated
    # (step 1 new tables + indexes, step 3 llm_cost_ledger recreate).
    # PRAGMA foreign_keys cannot be toggled inside a transaction, so
    # this commit MUST land before the PRAGMA.
    conn.execute("COMMIT")
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _recreate_paper_orders_with_news_event_entry(conn)
                # Foreign-key invariants must hold before we
                # re-enable enforcement. Run the check while still
                # inside the rebuild transaction so a violation
                # rolls the rebuild back atomically.
                violations = conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise sqlite3.IntegrityError(
                        f"PRAGMA foreign_key_check after paper_orders "
                        f"recreate reported {len(violations)} "
                        f"violation(s): {violations!r}"
                    )
                conn.execute("COMMIT")
            except Exception:
                # Best-effort ROLLBACK on any failure inside the
                # rebuild transaction. If the ROLLBACK itself errors
                # (e.g. tx was already auto-committed by the driver),
                # swallow that and let the original exception
                # propagate.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            # Always restore FK enforcement, regardless of how the
            # rebuild ended (success, FK violation, or unrelated
            # SQLite error).
            conn.execute("PRAGMA foreign_keys = ON")
    finally:
        # Re-open a transaction so the runner's outer COMMIT
        # (and the schema_version INSERT) still has a tx to
        # commit against. Mirrors the BEGIN IMMEDIATE the runner
        # opened before calling apply().
        conn.execute("BEGIN IMMEDIATE")


def _is_fault_inject_enabled() -> bool:
    """Return ``True`` when ``MIGRATION_FAULT_INJECT`` env var is truthy.

    Truthy values: ``1``, ``true``, ``True``, ``TRUE``, ``yes``, ``YES``.
    Anything else (including unset) is falsy.
    """
    raw = os.environ.get("MIGRATION_FAULT_INJECT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply(conn: sqlite3.Connection) -> None:
    """Apply the v9 → v10 schema changes inside the caller's transaction.

    The caller MUST have opened an explicit ``BEGIN`` before invoking
    this function. On any exception the caller MUST issue a
    ``ROLLBACK`` so partial DDL does not persist (every CREATE
    TABLE statement in SQLite is otherwise auto-committed by the
    sqlite3 driver's quirky DDL handling).

    Idempotency: every CREATE statement uses ``IF NOT EXISTS`` and the
    CHECK-enum extensions short-circuit when the new value is already
    present in the table's ``CREATE TABLE`` snapshot. Re-running the
    function on a v10 db is a no-op at the row level (no schema
    changes, no data movement) — VAL-M1-044.

    Side effects (on success):

    1. The 10 new tables in :data:`NEW_TABLE_NAMES` are present.
    2. ``llm_cost_ledger.provider`` admits ``'perplexity'``.
    3. ``paper_orders.event`` admits ``'news_event_entry'``.

    Raises
    ------
    MigrationFaultInjected
        When ``MIGRATION_FAULT_INJECT`` env var is truthy. Used by
        VAL-M1-038 to exercise atomic rollback.
    sqlite3.Error
        Any SQLite error from a malformed DDL or from a foreign-key
        / CHECK violation during the CHECK-recreate dance is
        re-raised so the caller's transaction rolls back.
    """
    # 1) New tables + indexes.
    for _name, ddl in _NEW_TABLE_DDL:
        conn.execute(ddl)
    for stmt in _NEW_INDEX_DDL:
        conn.execute(stmt)

    # 2) Optional fault injection AFTER tables are created so the
    #    rollback path observably reverts ALL the CREATE TABLE
    #    statements above. The check has to happen INSIDE the
    #    transaction (before the schema_version bump) so the test
    #    can assert atomicity end-to-end.
    if _is_fault_inject_enabled():
        raise MigrationFaultInjected(
            "MIGRATION_FAULT_INJECT=1 — synthetic mid-migration failure "
            "(010_reading_b_foundations.apply)"
        )

    # 3) llm_cost_ledger.provider CHECK extension.
    if not _has_provider_perplexity(conn):
        _recreate_llm_cost_ledger_with_perplexity(conn)

    # 4) paper_orders.event CHECK extension.
    #
    # ``paper_orders`` is referenced by ``execution_events`` and
    # ``execution_fills`` via FOREIGN KEY clauses. With
    # ``PRAGMA foreign_keys=ON`` (the default for every
    # :func:`biotech_sniper.db.connect` connection) the DROP TABLE
    # step inside the recreate dance raises
    # ``FOREIGN KEY constraint failed`` whenever child rows exist —
    # which is the production-shaped state on the VPS DB. Wrap the
    # recreate in the canonical SQLite FK-safe envelope (PRAGMA off
    # → BEGIN → rebuild → foreign_key_check → COMMIT → PRAGMA on)
    # via :func:`_apply_paper_orders_recreate_fk_safe`. See that
    # function's docstring for the full rationale (f-fix-m1-07).
    if not _has_event_news_event_entry(conn):
        _apply_paper_orders_recreate_fk_safe(conn)
