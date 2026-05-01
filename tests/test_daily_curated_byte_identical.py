"""Reading-B cross-flow regression: daily-curated path + ``performance_ledger`` byte-identical.

Feature: ``f-cross-10-daily-curated-byte-identical`` — repaired by
``f-fix-byte-identical-invariance`` (2026-05-01) after cross-flow
scrutiny round 1 found the original assertions to be tautological
self-baselines.

Contract assertions covered:

* **VAL-CROSS-037** — Daily-curated 06:13 PT cron output is byte-stable
  pre/post Reading-B for a representative replay day. SHA256 of
  ``audit_latest.json``, ``unified_master_signals.json``, and the
  day's ``paper_orders`` row set (canonicalized SQL, with the
  ``event != 'news_event_entry'`` carve-out filter) is unchanged
  before and after the Reading-B disabled-idle loop runs.
* **VAL-CROSS-038** — ``performance_ledger`` rows for the replay day
  (filtered to the daily-curated event paths, i.e. ``event !=
  'news_event_entry'``) are byte-stable through the same loop.

Why this file proves invariance, not a golden-replay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Byte-identity is enforced as an **invariance proof** rather than a
**golden-replay proof**, because:

(i)   the contract's ``tests/fixtures/regression/2026-04-25.sha256``
      artifact and the ``biotech_sniper.testing.regression_replay``
      harness referenced by the original VAL-CROSS-037 evidence
      command **do not exist** in the repo (verified by triage on
      2026-05-01),
(ii)  the contract's intent — "Reading B does not perturb the daily-
      curated path" — is fully captured by an invariance proof:
      seed real bytes, run the real ``run_disabled_idle`` entrypoint
      for several cycles, re-hash the SAME on-disk bytes / DB rows,
      assert equality, AND assert ZERO new rows appear in any
      Reading-B-only table,
(iii) capturing a true pre-Reading-B golden requires a cassette set
      (CT.gov + SEC EDGAR + every RSS feed + Alpaca paper) and a
      ``regression_replay`` harness that is itself a multi-feature
      scope (orchestrator-tracked as a future follow-up).

The previous round wrote pre-bytes from a Python literal, hashed
them, wrote the bytes to disk, hashed the disk file, and asserted
the two hashes equal. That round-trip is tautological — equality
is guaranteed by construction and proves nothing about Reading-B's
runtime behavior. The new shape below explicitly drives the real
``run_disabled_idle(poll_seconds=15, max_cycles=3, ...)`` between
the pre-hash and the post-hash, so the assertion can only pass if
the kill-switch loop genuinely makes ZERO writes to the seeded
state.

A one-line provenance anchor at
``tests/fixtures/regression/2026-04-25.commit`` records the
baseline commit sha (``47743f082175ce096f10f017b328f4a432888f65``,
the immediate parent of the first Reading-B M1 commit) so a future
golden-replay harness can recover the canonical pre-RB bytes
without forensic git archaeology.

See ``AGENTS.md`` § "Baseline-Artifact Workflow (byte-identical
replay)" for the canonical taxonomy of golden / invariance /
tautological approaches.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import performance_ledger as performance_ledger_module
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon.poll_loop import (
    is_news_daemon_enabled,
    run_disabled_idle,
)
from biotech_sniper.reports.reading_b_audit_summary import (
    write_reading_b_summary,
)


# ---------------------------------------------------------------------------
# Constants — synthetic seed for the representative replay day.
#
# These constants do NOT participate in the byte-identity assertion: they
# only define what the *seeded* daily-curated baseline looks like on disk.
# The pre/post sha256 comparisons read from disk / DB on BOTH sides of the
# `run_disabled_idle` invocation, so the equality is a real invariance
# proof regardless of what these literals contain.
# ---------------------------------------------------------------------------


# The contract uses 2026-04-25 / 2026-04-26 as the representative
# replay date. The exact date is irrelevant to the invariance
# assertion (every sha256 here is computed from the seeded files
# / DB rows AT TEST RUN TIME), but we pin a literal so the
# assertions read clearly.
REPLAY_DATE: str = "2026-04-25"


# Synthetic ``audit_latest.json`` seed — the shape ``master_unified_run``'s
# ``finally`` block writes after a successful daily cron. The exact
# contents are irrelevant to the invariance: we hash the file AFTER
# it has been written to disk by the seed helper, on BOTH sides of
# ``run_disabled_idle``, and assert equality.
_BASELINE_AUDIT: dict[str, Any] = {
    "last_daily_run": f"{REPLAY_DATE}T13:13:00Z",
    "last_daily_run_summary": {
        "date": REPLAY_DATE,
        "duration_sec": 599.0,
        "orders_submitted": 2,
        "cards_generated": 4,
        "llm_cost_usd": 3.92,
        "success": True,
    },
    "sources": {
        "ctgov": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:05Z"},
        "sec_edgar": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:07Z"},
        "alpaca_paper": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:10Z"},
    },
    "db_size_bytes": 987654,
    "paper_account_equity": 100000.00,
}


# Synthetic ``unified_master_signals.json`` seed — what the daily-
# curated unified scan would have emitted for the day.
_BASELINE_MASTER_SIGNALS: dict[str, Any] = {
    "as_of_date": REPLAY_DATE,
    "sectors": {
        "BIOTECH": {
            "candidates": [
                {"ticker": "VKTX", "score": 0.81, "p_success": 65},
                {"ticker": "RXRX", "score": 0.69, "p_success": 52},
            ],
            "n_candidates": 2,
        },
        "CONTRACT": {"candidates": [], "n_candidates": 0},
        "ADCOM": {"candidates": [], "n_candidates": 0},
    },
    "n_orders_submitted": 2,
}


# Synthetic ``paper_orders`` rows for the daily-curated cron run.
# Each row carries ``event='open'`` — the legacy daily-curated entry
# enum value. None of these rows are mutated or read by Reading-B.
_BASELINE_DAILY_ORDERS: list[dict[str, Any]] = [
    {
        "id": "po-daily-vktx",
        "play_card_id": "PC-DAILY-VKTX",
        "alpaca_order_id": "alp-daily-vktx",
        "symbol": "VKTX260619C00120000",
        "side": "buy",
        "qty": 2,
        "status": "filled",
        "reason": None,
        "event": "open",
        "parent_play_card_id": None,
        "requested_mid_at_submit": 1.20,
        "purpose": "entry",
        "client_order_id": "co-daily-vktx",
        "created_at": f"{REPLAY_DATE}T13:13:30Z",
    },
    {
        "id": "po-daily-rxrx",
        "play_card_id": "PC-DAILY-RXRX",
        "alpaca_order_id": "alp-daily-rxrx",
        "symbol": "RXRX260619P00050000",
        "side": "buy",
        "qty": 3,
        "status": "filled",
        "reason": None,
        "event": "open",
        "parent_play_card_id": None,
        "requested_mid_at_submit": 0.85,
        "purpose": "entry",
        "client_order_id": "co-daily-rxrx",
        "created_at": f"{REPLAY_DATE}T13:13:31Z",
    },
]


# Synthetic ``execution_fills`` rows matching the entry orders above.
# These power the ``performance_ledger.roll_up_day`` rollup (which
# walks ``execution_fills`` joined to ``paper_orders``).
_BASELINE_DAILY_FILLS: list[dict[str, Any]] = [
    {
        "paper_order_id": "po-daily-vktx",
        "filled_at": f"{REPLAY_DATE}T13:14:00.000Z",
        "filled_price": 1.20,
        "filled_qty": 2,
        "requested_mid_at_submit": 1.20,
    },
    {
        "paper_order_id": "po-daily-rxrx",
        "filled_at": f"{REPLAY_DATE}T13:14:01.000Z",
        "filled_price": 0.85,
        "filled_qty": 3,
        "requested_mid_at_submit": 0.85,
    },
]


# Synthetic ``performance_ledger`` row for the replay day. The
# pre-Reading-B daily cron writes one row per ``as_of_date``; this
# is the byte-stable artifact VAL-CROSS-038 pins.
_BASELINE_PERF_LEDGER_ROW: dict[str, Any] = {
    "as_of_date": REPLAY_DATE,
    "realized_pnl_usd": 0.00,
    "unrealized_pnl_usd": 124.50,
    "play_count": 2,
    "notes": "daily-curated baseline: 2 active plays VKTX/RXRX",
}


# Reading-B-only tables. Under ``NEWS_DAEMON_ENABLED=0`` the
# ``run_disabled_idle`` loop MUST add ZERO rows to any of these.
_READING_B_ONLY_TABLES: tuple[str, ...] = (
    "candidate_events",
    "news_match_log",
    "ensemble_scores_event",
    "ticker_cooldown",
)


# Provenance anchor: the immediate parent of the first Reading-B
# M1 commit. Captured under
# ``tests/fixtures/regression/2026-04-25.commit`` (single line + LF)
# so future workers can recover the canonical pre-RB bytes without
# forensic git archaeology if/when a real golden-replay harness is
# implemented.
_BASELINE_COMMIT_SHA: str = "47743f082175ce096f10f017b328f4a432888f65"


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash the on-disk bytes of ``path``.

    This is the project's canonical way of computing pre/post-shas
    for the byte-identical regression invariant. The bytes that go
    into the hash are exactly the bytes the operator-facing watchdog
    / cassette harness would read.
    """
    return _sha256_bytes(path.read_bytes())


def _serialize_json(payload: dict[str, Any]) -> bytes:
    """Stable JSON serialisation matching ``write_reading_b_summary``'s.

    ``sort_keys=True`` and ``indent=2`` are critical for byte-stability
    across runs and across platform locales.
    """
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _serialize_perf_ledger_row(row: tuple[Any, ...] | sqlite3.Row | None) -> bytes:
    """Canonicalise a single ``performance_ledger`` row to deterministic bytes.

    Mirrors the contract's evidence form:

        ``SELECT as_of_date, realized_pnl_usd, unrealized_pnl_usd,
                play_count, notes
         FROM performance_ledger WHERE as_of_date='<replay-date>'``

    rendered as a single CSV-style line so ``sha256sum`` is stable.
    """
    if row is None:
        return b"\n"
    if isinstance(row, sqlite3.Row):
        as_of_date = row["as_of_date"]
        realized = row["realized_pnl_usd"]
        unrealized = row["unrealized_pnl_usd"]
        play_count = row["play_count"]
        notes = row["notes"]
    else:
        as_of_date, realized, unrealized, play_count, notes = row
    cells = [
        str(as_of_date),
        f"{float(realized or 0.0):.4f}",
        f"{float(unrealized or 0.0):.4f}",
        str(int(play_count or 0)),
        str(notes if notes is not None else ""),
    ]
    return (",".join(cells) + "\n").encode("utf-8")


def _serialize_paper_orders_rowset(rows: list[sqlite3.Row]) -> bytes:
    """Canonicalise the daily-curated ``paper_orders`` row set.

    Matches the contract evidence form: ``SELECT * FROM paper_orders
    WHERE event != 'news_event_entry' AND DATE(created_at) =
    '<replay-date>' ORDER BY id``, rendered as deterministic CSV
    with a fixed column order (so the sha256 is stable across SQLite
    minor versions).
    """
    lines: list[str] = [
        "id,play_card_id,alpaca_order_id,symbol,side,qty,status,event,"
        "purpose,client_order_id,created_at,requested_mid_at_submit",
    ]
    for row in rows:
        cells = [
            str(row["id"]),
            str(row["play_card_id"] or ""),
            str(row["alpaca_order_id"] or ""),
            str(row["symbol"] or ""),
            str(row["side"] or ""),
            str(row["qty"] or 0),
            str(row["status"] or ""),
            str(row["event"] or ""),
            str(row["purpose"] or ""),
            str(row["client_order_id"] or ""),
            str(row["created_at"] or ""),
            f"{float(row['requested_mid_at_submit'] or 0.0):.4f}",
        ]
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _serialize_rollup(result: performance_ledger_module.RollUpResult) -> bytes:
    """Canonicalise a ``RollUpResult`` to deterministic CSV bytes.

    Sorts plays by ``play_id`` so the output is order-stable across
    runs even if SQLite's ORDER BY plan changes between minor
    versions. Realized P&L is formatted with 4 decimals (cent
    precision is the project's option-pricing standard).
    """
    lines: list[str] = [
        f"as_of_date={result.as_of_date}",
        f"event_filter={result.event_filter or 'NONE'}",
        f"play_count={result.play_count}",
        f"realized_pnl={result.realized_pnl:.4f}",
    ]
    for play in sorted(result.plays, key=lambda p: p.play_id):
        lines.append(
            f"{play.play_id},{play.event_path},{play.realized_pnl:.4f},{play.fill_count}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------


def _seed_baseline_paper_orders(db_path: Path) -> None:
    """Insert the synthetic daily-curated ``paper_orders`` rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO paper_orders ("
            "  id, play_card_id, alpaca_order_id, symbol, side, qty, "
            "  status, reason, event, parent_play_card_id, "
            "  requested_mid_at_submit, purpose, client_order_id, "
            "  created_at"
            ") VALUES ("
            "  :id, :play_card_id, :alpaca_order_id, :symbol, :side, :qty, "
            "  :status, :reason, :event, :parent_play_card_id, "
            "  :requested_mid_at_submit, :purpose, :client_order_id, "
            "  :created_at"
            ")",
            _BASELINE_DAILY_ORDERS,
        )
        conn.executemany(
            "INSERT INTO execution_fills ("
            "  paper_order_id, filled_at, filled_price, filled_qty, "
            "  requested_mid_at_submit, slippage_bps, slippage_usd, "
            "  time_to_fill_ms, partial_qty_remaining"
            ") VALUES ("
            "  :paper_order_id, :filled_at, :filled_price, :filled_qty, "
            "  :requested_mid_at_submit, 0, 0, 0, 0"
            ")",
            _BASELINE_DAILY_FILLS,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_baseline_performance_ledger(db_path: Path) -> None:
    """Insert the synthetic ``performance_ledger`` row for the replay day."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO performance_ledger ("
            "  as_of_date, realized_pnl_usd, unrealized_pnl_usd, "
            "  play_count, notes"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                _BASELINE_PERF_LEDGER_ROW["as_of_date"],
                _BASELINE_PERF_LEDGER_ROW["realized_pnl_usd"],
                _BASELINE_PERF_LEDGER_ROW["unrealized_pnl_usd"],
                _BASELINE_PERF_LEDGER_ROW["play_count"],
                _BASELINE_PERF_LEDGER_ROW["notes"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_reading_b_additive_rows(db_path: Path) -> None:
    """Add a synthetic Reading-B Stage-2 acceptance + fills + ledger entry.

    Mirrors what a successful Reading-B run would persist on the
    same replay day. The carve-out filter (``event != 'news_event_entry'``)
    must isolate every byte-identical assertion FROM these rows.

    NOTE: this helper is used to SIMULATE Reading-B writes, NOT to
    drive the kill-switch invariance assertion. The kill-switch
    proof relies on the real ``run_disabled_idle`` entrypoint
    making ZERO writes; this helper documents the partition by
    showing that ADDITIVE rows (when they exist) do not perturb the
    daily-curated bucket sha.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        # 1) Stage-1 news_events seed.
        cur = conn.execute(
            "INSERT INTO news_events (ticker, source, published_at, "
            "  title, url, raw_payload) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ABCD",
                "test-replay-cross10",
                f"{REPLAY_DATE}T13:30:00Z",
                "ABCD partnership announcement",
                "https://example.com/abcd-partnership-cross10",
                "ABCD partnership announcement",
            ),
        )
        news_id = int(cur.lastrowid or 0)

        # 2) candidate_events row.
        conn.execute(
            "INSERT INTO candidate_events ("
            "  ticker, source_news_event_id, matched_keywords, "
            "  calendar_match, emitted_at, dedup_key"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ABCD",
                news_id,
                "partnership",
                None,
                f"{REPLAY_DATE}T13:30:01Z",
                f"sha-cross10-{news_id}-abcd-partnership",
            ),
        )

        # 3) paper_orders entry with the Reading-B carve-out event.
        conn.execute(
            "INSERT INTO paper_orders ("
            "  id, play_card_id, alpaca_order_id, symbol, side, qty, "
            "  status, reason, event, parent_play_card_id, "
            "  requested_mid_at_submit, purpose, client_order_id, "
            "  created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "po-rb-abcd",
                "PC-RB-ABCD",
                "alp-rb-abcd",
                "ABCD260717C00010000",
                "buy",
                5,
                "filled",
                None,
                "news_event_entry",
                None,
                0.50,
                "entry",
                "co-rb-abcd",
                f"{REPLAY_DATE}T13:30:05Z",
            ),
        )

        # 4) execution_fills row for the news_event_entry order.
        conn.execute(
            "INSERT INTO execution_fills ("
            "  paper_order_id, filled_at, filled_price, filled_qty, "
            "  requested_mid_at_submit, slippage_bps, slippage_usd, "
            "  time_to_fill_ms, partial_qty_remaining"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "po-rb-abcd",
                f"{REPLAY_DATE}T13:30:06.000Z",
                0.50,
                5,
                0.50,
                0.0,
                0.0,
                0,
                0,
            ),
        )

        # 5) llm_cost_ledger row (Stage-2 spend).
        conn.execute(
            "INSERT INTO llm_cost_ledger ("
            "  provider, model_id, purpose, prompt_tokens, "
            "  completion_tokens, latency_ms, cost_usd, called_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "perplexity",
                "sonar",
                "stage2_event",
                500,
                250,
                1200,
                0.0042,
                f"{REPLAY_DATE}T13:30:02Z",
            ),
        )

        conn.commit()
    finally:
        conn.close()


def _query_daily_paper_orders(db_path: Path) -> list[sqlite3.Row]:
    """Query the daily-curated bucket — the carve-out filter applied."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, play_card_id, alpaca_order_id, symbol, side, qty, "
            "       status, event, purpose, client_order_id, "
            "       created_at, requested_mid_at_submit "
            "FROM paper_orders "
            "WHERE (event IS NULL OR event != 'news_event_entry') "
            "  AND DATE(created_at) = ? "
            "ORDER BY id",
            (REPLAY_DATE,),
        ).fetchall()
    finally:
        conn.close()
    return list(rows)


def _query_perf_ledger_row(db_path: Path) -> sqlite3.Row | None:
    """Query the ``performance_ledger`` row for the replay day."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT as_of_date, realized_pnl_usd, unrealized_pnl_usd, "
            "       play_count, notes "
            "FROM performance_ledger WHERE as_of_date = ?",
            (REPLAY_DATE,),
        ).fetchone()
    finally:
        conn.close()
    return row


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _count_news_event_entry_orders(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE event='news_event_entry'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _count_stage2_event_ledger_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM llm_cost_ledger WHERE purpose='stage2_event'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _drive_disabled_idle(cycles: int = 3) -> list[float]:
    """Drive ``run_disabled_idle`` for ``cycles`` iterations with stub sleep.

    Returns the list of sleep deltas observed by the stub so callers
    can pin the cadence contract.
    """
    sleeps_called: list[float] = []
    rc = run_disabled_idle(
        poll_seconds=15,
        max_cycles=cycles,
        sleep_func=lambda s: sleeps_called.append(float(s)),
    )
    assert rc == 0
    return sleeps_called


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def replay_db(tmp_path: Path) -> Path:
    """A fresh sqlite db migrated to v10 (Reading-B foundations)."""
    db = tmp_path / "alpha_sniper_cross10.db"
    run_migrations_runner(db, target_version=10, take_backup_first=False)
    return db


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def baseline_audit_path(state_dir: Path) -> Path:
    p = state_dir / "audit_latest.json"
    p.write_bytes(_serialize_json(_BASELINE_AUDIT))
    return p


@pytest.fixture
def baseline_master_signals_path(state_dir: Path) -> Path:
    p = state_dir / "unified_master_signals.json"
    p.write_bytes(_serialize_json(_BASELINE_MASTER_SIGNALS))
    return p


@pytest.fixture
def seeded_db(replay_db: Path) -> Path:
    """Replay db pre-seeded with daily-curated baseline rows."""
    _seed_baseline_paper_orders(replay_db)
    _seed_baseline_performance_ledger(replay_db)
    return replay_db


# ---------------------------------------------------------------------------
# VAL-CROSS-037 — audit_latest.json invariance under disabled-idle loop
# ---------------------------------------------------------------------------


def test_audit_latest_invariant_through_run_disabled_idle(
    baseline_audit_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` does not perturb ``audit_latest.json``.

    Invariance proof (NOT a tautological self-baseline):

    1. The ``baseline_audit_path`` fixture writes the seeded
       JSON to disk; we hash THE FILE (not the source dict) to
       capture ``pre_audit_sha``.
    2. We drive the real ``run_disabled_idle(poll_seconds=15,
       max_cycles=3, ...)`` entrypoint — the only kill-switch
       loop in the daemon — for several cycles.
    3. We re-hash THE FILE on disk to capture
       ``post_audit_sha`` and assert byte-equality.
    4. We assert the file has no ``reading_b`` top-level key
       (Reading-B has no writer that touches this file under
       the disabled-idle loop).

    Because the pre/post hashes both come from the same on-disk
    bytes — observed BEFORE and AFTER an actual loop run — the
    equality cannot be true by construction; it can only be true
    if the loop genuinely makes zero writes.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False, (
        "kill-switch must report False under NEWS_DAEMON_ENABLED=0"
    )

    pre_audit_sha = _sha256_file(baseline_audit_path)

    sleeps = _drive_disabled_idle(cycles=3)
    # max_cycles=3 → 2 sleep calls (the loop returns before the
    # final sleep on the last iteration).
    assert sleeps == [15.0, 15.0]

    post_audit_sha = _sha256_file(baseline_audit_path)
    assert post_audit_sha == pre_audit_sha, (
        "audit_latest.json sha256 must be byte-stable across "
        f"run_disabled_idle (pre={pre_audit_sha} post={post_audit_sha})"
    )

    payload = json.loads(baseline_audit_path.read_text(encoding="utf-8"))
    assert "reading_b" not in payload, (
        "audit_latest.json MUST NOT acquire a 'reading_b' top-level "
        "key under NEWS_DAEMON_ENABLED=0"
    )


# ---------------------------------------------------------------------------
# VAL-CROSS-037 — unified_master_signals.json invariance
# ---------------------------------------------------------------------------


def test_unified_master_signals_invariant_through_run_disabled_idle(
    baseline_master_signals_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` does not perturb ``unified_master_signals.json``.

    Reading-B has zero writers that touch this file. The disabled-
    idle loop has no DB / network access at all, so any drift here
    would be a true regression.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    pre_sha = _sha256_file(baseline_master_signals_path)

    _drive_disabled_idle(cycles=3)

    post_sha = _sha256_file(baseline_master_signals_path)
    assert post_sha == pre_sha, (
        "unified_master_signals.json sha256 must be byte-stable across "
        f"run_disabled_idle (pre={pre_sha} post={post_sha})"
    )


# ---------------------------------------------------------------------------
# VAL-CROSS-037 — daily-curated ``paper_orders`` row set invariance
# ---------------------------------------------------------------------------


def test_daily_paper_orders_rowset_invariant_through_run_disabled_idle(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daily-curated ``paper_orders`` row sha256 stable through the kill-switch loop.

    Pre-hash the carve-out-filtered row set queried from the SEEDED
    DB, drive the disabled-idle loop, re-hash the same query, assert
    equality. Also assert ZERO new news_event_entry rows landed.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    pre_rows = _query_daily_paper_orders(seeded_db)
    pre_sha = _sha256_bytes(_serialize_paper_orders_rowset(pre_rows))
    assert len(pre_rows) == len(_BASELINE_DAILY_ORDERS)

    pre_rb_orders = _count_news_event_entry_orders(seeded_db)
    assert pre_rb_orders == 0

    _drive_disabled_idle(cycles=3)

    post_rb_orders = _count_news_event_entry_orders(seeded_db)
    assert post_rb_orders == pre_rb_orders, (
        "ZERO news_event_entry rows may be added under NEWS_DAEMON_ENABLED=0; "
        f"pre={pre_rb_orders} post={post_rb_orders}"
    )

    post_rows = _query_daily_paper_orders(seeded_db)
    post_sha = _sha256_bytes(_serialize_paper_orders_rowset(post_rows))
    assert post_sha == pre_sha, (
        "daily-curated paper_orders row sha256 must be byte-stable "
        f"across run_disabled_idle (pre={pre_sha} post={post_sha})"
    )


def test_daily_paper_orders_rowset_invariant_with_carveout_under_additive_rows(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carve-out filter holds even when Reading-B additive rows exist.

    The kill-switch invariance is the primary guarantee, but the
    contract also requires the carve-out filter (``event !=
    'news_event_entry'``) to isolate the daily bucket from
    Reading-B writes. Simulate a successful Stage-2 run by
    inserting additive rows directly and confirm the daily bucket
    sha is unchanged.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    pre_rows = _query_daily_paper_orders(seeded_db)
    pre_sha = _sha256_bytes(_serialize_paper_orders_rowset(pre_rows))

    _insert_reading_b_additive_rows(seeded_db)

    rb_count = _count_news_event_entry_orders(seeded_db)
    assert rb_count == 1

    post_rows = _query_daily_paper_orders(seeded_db)
    assert len(post_rows) == len(pre_rows), (
        "daily-curated bucket row count must be unchanged "
        f"(pre={len(pre_rows)} post={len(post_rows)})"
    )
    post_sha = _sha256_bytes(_serialize_paper_orders_rowset(post_rows))
    assert post_sha == pre_sha, (
        "daily-curated paper_orders row set sha256 must be "
        f"byte-stable (pre={pre_sha} post={post_sha})"
    )

    for row in post_rows:
        assert row["event"] != "news_event_entry"


# ---------------------------------------------------------------------------
# VAL-CROSS-038 — ``performance_ledger`` invariance under disabled-idle loop
# ---------------------------------------------------------------------------


def test_performance_ledger_row_invariant_through_run_disabled_idle(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` writes ZERO ``performance_ledger`` rows.

    Pre-hash the seeded ``performance_ledger`` row queried from the
    DB, drive the disabled-idle loop, re-hash the same row, assert
    equality. Also assert the total row count is unchanged.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    pre_row = _query_perf_ledger_row(seeded_db)
    assert pre_row is not None
    pre_sha = _sha256_bytes(_serialize_perf_ledger_row(pre_row))

    pre_total_count = _count_rows(seeded_db, "performance_ledger")

    sleeps = _drive_disabled_idle(cycles=3)
    assert sleeps == [15.0, 15.0]

    post_row = _query_perf_ledger_row(seeded_db)
    assert post_row is not None
    post_sha = _sha256_bytes(_serialize_perf_ledger_row(post_row))
    assert post_sha == pre_sha, (
        "performance_ledger row sha256 must be byte-stable across "
        f"run_disabled_idle (pre={pre_sha} post={post_sha})"
    )

    post_total_count = _count_rows(seeded_db, "performance_ledger")
    assert post_total_count == pre_total_count, (
        "Reading-B kill-switch must add ZERO performance_ledger rows; "
        f"pre={pre_total_count} post={post_total_count}"
    )


def test_performance_ledger_row_invariant_under_additive_rows(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``performance_ledger`` row unchanged even with Reading-B additive rows.

    Reading-B has NO writer that mutates the ``performance_ledger``
    table. Even when Stage-2 successfully accepts a paper order
    (which touches ``paper_orders``, ``execution_fills``,
    ``llm_cost_ledger``, ``candidate_events``), the
    ``performance_ledger`` row for the replay date is byte-stable.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    pre_row = _query_perf_ledger_row(seeded_db)
    pre_sha = _sha256_bytes(_serialize_perf_ledger_row(pre_row))

    _insert_reading_b_additive_rows(seeded_db)

    post_row = _query_perf_ledger_row(seeded_db)
    post_sha = _sha256_bytes(_serialize_perf_ledger_row(post_row))
    assert post_sha == pre_sha, (
        "performance_ledger row sha256 must be byte-stable even "
        "with Reading-B additive rows present "
        f"(pre={pre_sha} post={post_sha})"
    )


# ---------------------------------------------------------------------------
# VAL-CROSS-038 — ``performance_ledger.roll_up_day`` carve-out invariance
# ---------------------------------------------------------------------------


def test_roll_up_day_open_filter_byte_identical_with_reading_b_additive_rows(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``roll_up_day(date, event_filter='open')`` sha256 unchanged.

    The carve-out is provable at the rollup level too: even when
    a ``news_event_entry`` paper_order + fill + ledger row are
    present on the same replay day, the daily-curated rollup
    (``event_filter='open'``) returns the SAME plays with the
    SAME realized P&L.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    pre = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter="open", db_path=seeded_db
    )
    pre_sha = _sha256_bytes(_serialize_rollup(pre))
    # Sanity: the daily-curated bucket has both seeded plays.
    assert pre.play_count == len(_BASELINE_DAILY_ORDERS)

    _insert_reading_b_additive_rows(seeded_db)

    post = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter="open", db_path=seeded_db
    )
    post_sha = _sha256_bytes(_serialize_rollup(post))
    assert post_sha == pre_sha, (
        "roll_up_day(event_filter='open') sha256 must be "
        "byte-identical with Reading-B additive rows present "
        f"(pre={pre_sha} post={post_sha})"
    )

    # Reading-B carve-out IS visible under its own filter — confirms
    # the additive rows actually landed.
    rb = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter="news_event_entry", db_path=seeded_db
    )
    assert rb.play_count == 1
    assert {p.event_path for p in rb.plays} == {"news_event_entry"}


def test_roll_up_day_partition_holds_daily_plus_news_equals_union(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sum-of-partitions == union sum (no double-counting, no missing fills).

    Pins VAL-M5-033's partition invariant for the replay day:
    ``open + news_event_entry == None`` within $0.01.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    _insert_reading_b_additive_rows(seeded_db)

    daily = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter="open", db_path=seeded_db
    )
    news = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter="news_event_entry", db_path=seeded_db
    )
    union = performance_ledger_module.roll_up_day(
        REPLAY_DATE, event_filter=None, db_path=seeded_db
    )

    assert union.play_count == daily.play_count + news.play_count
    assert union.realized_pnl == pytest.approx(
        daily.realized_pnl + news.realized_pnl, abs=0.01
    )


# ---------------------------------------------------------------------------
# Cross-flow invariance: ZERO new rows in Reading-B-only tables
# ---------------------------------------------------------------------------


def test_run_disabled_idle_writes_zero_rows_to_reading_b_only_tables(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` adds ZERO rows to Reading-B-only tables.

    Pins the contract's "no Reading-B persistence under kill-switch"
    invariant against the real disabled-idle entrypoint:

    * zero ``candidate_events`` rows
    * zero ``news_match_log`` rows
    * zero ``ensemble_scores_event`` rows
    * zero ``ticker_cooldown`` rows
    * zero ``paper_orders WHERE event='news_event_entry'`` rows
    * zero ``llm_cost_ledger WHERE purpose='stage2_event'`` rows
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    pre_counts: dict[str, int] = {
        table: _count_rows(seeded_db, table) for table in _READING_B_ONLY_TABLES
    }
    pre_rb_orders = _count_news_event_entry_orders(seeded_db)
    pre_stage2_ledger = _count_stage2_event_ledger_rows(seeded_db)

    # Sanity: a freshly-migrated v10 DB starts empty for these tables.
    for table, cnt in pre_counts.items():
        assert cnt == 0, f"{table} must be empty in seeded baseline; got {cnt}"
    assert pre_rb_orders == 0
    assert pre_stage2_ledger == 0

    _drive_disabled_idle(cycles=3)

    for table, pre_cnt in pre_counts.items():
        post_cnt = _count_rows(seeded_db, table)
        assert post_cnt == pre_cnt, (
            f"{table} row count must be unchanged under "
            f"NEWS_DAEMON_ENABLED=0; pre={pre_cnt} post={post_cnt}"
        )
    assert _count_news_event_entry_orders(seeded_db) == pre_rb_orders
    assert _count_stage2_event_ledger_rows(seeded_db) == pre_stage2_ledger


# ---------------------------------------------------------------------------
# Cross-artifact: write_reading_b_summary additive only adds reading_b
# (so audit_latest.json minus that key is byte-identical to the baseline).
# ---------------------------------------------------------------------------


def test_audit_latest_post_reading_b_minus_reading_b_key_matches_baseline(
    baseline_audit_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``audit_latest.json`` post-Reading-B minus ``reading_b`` = baseline.

    Mirrors the contract's ``jq 'del(.reading_b)' post.json`` form:
    after invoking the Reading-B summary writer, every other
    top-level key / value is preserved byte-for-value.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    baseline_sha = _sha256_file(baseline_audit_path)

    _insert_reading_b_additive_rows(seeded_db)
    payload = write_reading_b_summary(baseline_audit_path, db_path=seeded_db)
    assert "reading_b" in payload

    # Stripping reading_b yields the baseline payload byte-for-byte.
    stripped = {k: v for k, v in payload.items() if k != "reading_b"}
    assert stripped == _BASELINE_AUDIT
    stripped_sha = _sha256_bytes(_serialize_json(stripped))
    assert stripped_sha == baseline_sha


# ---------------------------------------------------------------------------
# Provenance anchor — pre-Reading-B baseline commit
# ---------------------------------------------------------------------------


def test_baseline_commit_anchor_exists() -> None:
    """``tests/fixtures/regression/2026-04-25.commit`` records the baseline sha.

    The anchor file records the immediate parent of the first
    Reading-B M1 commit (``47743f082175ce096f10f017b328f4a432888f65``).
    Future workers implementing a true golden-replay harness can
    use this sha to recover the canonical pre-RB bytes from a
    detached worktree without forensic git archaeology.

    The file MUST contain the 40-character sha followed by a single
    trailing newline (41 bytes total). Any drift here breaks the
    forensic recovery path.
    """
    anchor = (
        Path(__file__).parent / "fixtures" / "regression" / "2026-04-25.commit"
    )
    assert anchor.is_file(), f"baseline-commit anchor file missing: {anchor}"

    raw = anchor.read_bytes()
    assert raw == (_BASELINE_COMMIT_SHA + "\n").encode("ascii"), (
        "baseline-commit anchor file must contain exactly the canonical "
        "baseline sha followed by a single trailing newline; got "
        f"{raw!r}"
    )

    text = anchor.read_text(encoding="ascii")
    assert text == f"{_BASELINE_COMMIT_SHA}\n"
    # Defensive: the sha is well-formed and matches the literal in
    # AGENTS.md § "Baseline-Artifact Workflow".
    sha_only = text.strip()
    assert len(sha_only) == 40
    assert all(c in "0123456789abcdef" for c in sha_only)
    assert sha_only == _BASELINE_COMMIT_SHA
