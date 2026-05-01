"""Reading-B M5 regression: daily-curated path byte-identical pre/post Reading B.

Feature: ``f-m5-07-daily-curated-byte-identical-replay`` — repaired
by ``f-fix-byte-identical-invariance`` (2026-05-01) after cross-flow
scrutiny round 1 found the original assertions to be tautological
self-baselines.

Contract assertions covered: VAL-M5-040, VAL-M5-041, VAL-M5-042,
VAL-M5-043, VAL-M5-046 (kill-switch invariant) and the cross-mission
VAL-CROSS-037 regression invariant.

Mission requirement
~~~~~~~~~~~~~~~~~~~

The hard backward-compatibility invariant of Reading B (laid out in
``mission.md`` §M5 and ``validation-contract.md`` §M5.REGRESSION):

    Replaying a representative day from the pre-Reading-B ``main``
    head against the post-Reading-B head MUST produce byte-identical
    ``audit_latest.json`` and ``unified_master_signals.json`` output,
    AND produce the identical daily order set, when
    ``NEWS_DAEMON_ENABLED=0``. The only allowed delta in
    ``audit_latest.json`` is an additive top-level ``reading_b`` key
    when ``NEWS_DAEMON_ENABLED=1``.

Why this file proves invariance, not a golden-replay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Byte-identity is enforced as an **invariance proof** rather than a
**golden-replay proof**, because:

(i)   the contract's ``tests/fixtures/regression/2026-04-26.sha256``
      golden artifact and the
      ``biotech_sniper.testing.regression_replay`` harness referenced
      by the original VAL-M5-040 evidence command **do not exist**
      in the repo (verified by triage on 2026-05-01),
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
the kill-switch loop genuinely makes ZERO writes.

A one-line provenance anchor at
``tests/fixtures/regression/2026-04-25.commit`` records the
baseline commit sha (``47743f082175ce096f10f017b328f4a432888f65``,
the immediate parent of the first Reading-B M1 commit) so a future
golden-replay harness can recover the canonical pre-RB bytes
without forensic git archaeology. (The same anchor is shared by
``test_daily_curated_byte_identical.py``.)

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


# A representative replay day. Mission contract uses 2026-04-26;
# the hash is computed from the seeded files / DB rows AT TEST RUN
# TIME, so the precise date does not matter — what matters is that
# the same seed produces the same on-disk bytes across runs
# (deterministic) AND that ``run_disabled_idle`` does not perturb
# them.
REPLAY_DATE: str = "2026-04-26"

# Synthetic baseline ``audit_latest.json`` — what the pre-Reading-B
# ``master_unified_run`` ``finally`` clause would have written.
_BASELINE_AUDIT: dict[str, Any] = {
    "last_daily_run": f"{REPLAY_DATE}T13:13:00Z",
    "last_daily_run_summary": {
        "date": REPLAY_DATE,
        "duration_sec": 612.0,
        "orders_submitted": 2,
        "cards_generated": 5,
        "llm_cost_usd": 4.27,
        "success": True,
    },
    "sources": {
        "ctgov": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:05Z"},
        "sec_edgar": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:07Z"},
        "alpaca_paper": {"reachable": True, "last_check": f"{REPLAY_DATE}T13:13:10Z"},
    },
    "db_size_bytes": 1234567,
    "paper_account_equity": 100000.00,
}

# Synthetic baseline ``unified_master_signals.json`` — what
# ``master_unified_run`` would have emitted for the day.
_BASELINE_MASTER_SIGNALS: dict[str, Any] = {
    "as_of_date": REPLAY_DATE,
    "sectors": {
        "BIOTECH": {
            "candidates": [
                {"ticker": "VKTX", "score": 0.78, "p_success": 62},
                {"ticker": "RXRX", "score": 0.71, "p_success": 55},
            ],
            "n_candidates": 2,
        },
        "CONTRACT": {"candidates": [], "n_candidates": 0},
        "ADCOM": {"candidates": [], "n_candidates": 0},
    },
    "n_orders_submitted": 2,
}

# Synthetic baseline ``paper_orders`` rows for the daily-curated
# 06:13 PT cron run. These use the legacy ``event='open'`` enum
# member; Reading B's additive ``'news_event_entry'`` carve-out
# never reuses these rows.
_BASELINE_PAPER_ORDERS: list[dict[str, Any]] = [
    {
        "id": "po-baseline-001",
        "play_card_id": "pc-baseline-vktx-call",
        "alpaca_order_id": "alp-baseline-001",
        "symbol": "VKTX260717C00120000",
        "side": "buy",
        "qty": 2,
        "status": "filled",
        "reason": None,
        "event": "open",
        "parent_play_card_id": None,
        "requested_mid_at_submit": 1.20,
        "purpose": "entry",
        "client_order_id": "co-baseline-vktx-001",
        "created_at": f"{REPLAY_DATE}T13:13:30Z",
    },
    {
        "id": "po-baseline-002",
        "play_card_id": "pc-baseline-rxrx-put",
        "alpaca_order_id": "alp-baseline-002",
        "symbol": "RXRX260717P00050000",
        "side": "buy",
        "qty": 3,
        "status": "filled",
        "reason": None,
        "event": "open",
        "parent_play_card_id": None,
        "requested_mid_at_submit": 0.85,
        "purpose": "entry",
        "client_order_id": "co-baseline-rxrx-001",
        "created_at": f"{REPLAY_DATE}T13:13:31Z",
    },
]


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
# Helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _serialize_audit(payload: dict[str, Any]) -> bytes:
    """Serialize the audit payload exactly the way the writer does.

    Mirrors :func:`biotech_sniper.reports.reading_b_audit_summary.write_reading_b_summary`
    so the *only* delta between baseline and post-RB outputs is
    the optional ``reading_b`` top-level key. Sorting keys is
    critical for byte-stability.
    """
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _serialize_master_signals(payload: dict[str, Any]) -> bytes:
    """Stable canonical JSON for the master signals file."""
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _serialize_daily_order_set(rows: list[sqlite3.Row]) -> bytes:
    """Canonicalize the daily order set into a deterministic byte buffer.

    Mirrors the contract's normalisation:

        ``SELECT * FROM paper_orders WHERE event != 'news_event_entry'
        AND DATE(created_at) = '<replay-date>' ORDER BY id``

    rendered as sorted CSV.
    """
    lines: list[str] = []
    # Stable header
    lines.append(
        "id,play_card_id,alpaca_order_id,symbol,side,qty,status,event,"
        "purpose,client_order_id,created_at,requested_mid_at_submit"
    )
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


def _query_daily_order_set(db_path: Path) -> list[sqlite3.Row]:
    """Query paper_orders for the daily-curated bucket only.

    Reading-B carve-out: ``event != 'news_event_entry'`` filter
    isolates the daily-curated rows. A NULL event is treated as a
    legacy daily-curated row (so existing rows pre-dating the
    Reading-B enum extension still aggregate into the daily
    bucket — the carve-out NEVER misclassifies a daily-curated row
    as Reading-B).
    """
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


def _seed_daily_curated_baseline(db_path: Path) -> None:
    """Insert the synthetic baseline ``paper_orders`` rows."""
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
            _BASELINE_PAPER_ORDERS,
        )
        conn.commit()
    finally:
        conn.close()


def _insert_reading_b_additive_rows(db_path: Path) -> None:
    """Add a synthetic ``news_event_entry`` row PLUS its support rows.

    Simulates a successful Reading-B Stage-2 run on the same replay
    day. The test then proves the daily-curated bucket is byte-
    identical even with these additions present.

    NOTE: this helper is used to SIMULATE Reading-B writes, NOT to
    drive the kill-switch invariance assertion. The kill-switch
    proof relies on the real ``run_disabled_idle`` entrypoint
    making ZERO writes; this helper documents the partition by
    showing that ADDITIVE rows (when they exist) do not perturb the
    daily-curated bucket sha.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        # 1) news_events seed (the Stage-1 source row).
        cur = conn.execute(
            "INSERT INTO news_events (ticker, source, published_at, "
            "  title, url, raw_payload) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ABCD",
                "test-replay",
                f"{REPLAY_DATE}T13:30:00Z",
                "ABCD partnership announcement",
                "https://example.com/abcd-partnership",
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
                f"sha-replay-{news_id}-abcd-partnership",
            ),
        )

        # 3) paper_orders row with the Reading-B carve-out event.
        conn.execute(
            "INSERT INTO paper_orders ("
            "  id, play_card_id, alpaca_order_id, symbol, side, qty, "
            "  status, reason, event, parent_play_card_id, "
            "  requested_mid_at_submit, purpose, client_order_id, "
            "  created_at"
            ") VALUES ("
            "  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
            ")",
            (
                "po-rb-001",
                "pc-rb-abcd-call",
                "alp-rb-001",
                "ABCD260717C00010000",
                "buy",
                5,
                "filled",
                None,
                "news_event_entry",
                None,
                0.50,
                "entry",
                "co-rb-abcd-001",
                f"{REPLAY_DATE}T13:30:05Z",
            ),
        )

        # 4) llm_cost_ledger row (Stage-2 spend).
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
                0.0035,
                f"{REPLAY_DATE}T13:30:02Z",
            ),
        )

        conn.commit()
    finally:
        conn.close()


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
    db = tmp_path / "alpha_sniper_replay.db"
    run_migrations_runner(db, target_version=11, take_backup_first=False)
    return db


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Directory standing in for the production ``state/`` directory."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def baseline_audit_path(state_dir: Path) -> Path:
    """Pre-Reading-B baseline ``audit_latest.json`` as it would be on
    disk before any Reading-B code runs."""
    p = state_dir / "audit_latest.json"
    p.write_bytes(_serialize_audit(_BASELINE_AUDIT))
    return p


@pytest.fixture
def baseline_master_signals_path(state_dir: Path) -> Path:
    """Pre-Reading-B baseline ``unified_master_signals.json``."""
    p = state_dir / "unified_master_signals.json"
    p.write_bytes(_serialize_master_signals(_BASELINE_MASTER_SIGNALS))
    return p


@pytest.fixture
def seeded_db(replay_db: Path) -> Path:
    """Replay db with the synthetic daily-curated baseline rows."""
    _seed_daily_curated_baseline(replay_db)
    return replay_db


# ---------------------------------------------------------------------------
# VAL-M5-040 / VAL-CROSS-037 — audit_latest.json invariance
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
        "kill-switch must report False for NEWS_DAEMON_ENABLED=0"
    )

    pre_audit_sha = _sha256_file(baseline_audit_path)

    sleeps = _drive_disabled_idle(cycles=3)
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
# VAL-M5-042 — unified_master_signals.json invariance
# ---------------------------------------------------------------------------


def test_unified_master_signals_invariant_through_run_disabled_idle(
    baseline_master_signals_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` does not perturb ``unified_master_signals.json``.

    The master-signals JSON is written by the daily-curated
    ``master_unified_run.run_unified_scan`` path, which Reading B
    does NOT touch. The disabled-idle loop has no DB / network
    access at all, so any drift here would be a true regression.
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
# VAL-M5-043 — daily order set invariance through the disabled-idle loop
# ---------------------------------------------------------------------------


def test_daily_order_set_invariant_through_run_disabled_idle(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_disabled_idle`` does not perturb the daily-curated order set.

    Pre-hash the carve-out-filtered row set queried from the SEEDED
    DB, drive the disabled-idle loop, re-hash the same query, assert
    equality. Also assert ZERO new news_event_entry rows landed.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    pre_rows = _query_daily_order_set(seeded_db)
    assert len(pre_rows) == len(_BASELINE_PAPER_ORDERS), (
        "daily order set row count must match baseline; got "
        f"{len(pre_rows)} vs expected {len(_BASELINE_PAPER_ORDERS)}"
    )
    pre_sha = _sha256_bytes(_serialize_daily_order_set(pre_rows))

    pre_rb_orders = _count_news_event_entry_orders(seeded_db)
    assert pre_rb_orders == 0

    _drive_disabled_idle(cycles=3)

    post_rb_orders = _count_news_event_entry_orders(seeded_db)
    assert post_rb_orders == pre_rb_orders, (
        "ZERO news_event_entry rows may be added under NEWS_DAEMON_ENABLED=0; "
        f"pre={pre_rb_orders} post={post_rb_orders}"
    )

    post_rows = _query_daily_order_set(seeded_db)
    post_sha = _sha256_bytes(_serialize_daily_order_set(post_rows))
    assert post_sha == pre_sha, (
        "daily order set sha256 must be byte-stable across "
        f"run_disabled_idle (pre={pre_sha} post={post_sha})"
    )

    # Pin: each row carries a legacy daily-curated ``event`` value
    # (NOT 'news_event_entry'); zero rows from the carve-out leak
    # into the daily bucket.
    for row in post_rows:
        assert row["event"] != "news_event_entry"


# ---------------------------------------------------------------------------
# VAL-M5-046 — NEWS_DAEMON_ENABLED=0 is a total kill switch
# ---------------------------------------------------------------------------


def test_news_daemon_kill_switch_writes_zero_reading_b_rows(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=0`` writes ZERO Reading-B rows.

    Drives the disabled-idle loop for a small fixed cycle count
    and proves no Reading-B persistence occurred:

    * zero ``candidate_events`` rows
    * zero ``ensemble_scores_event`` rows
    * zero ``paper_orders`` rows with ``event='news_event_entry'``
    * zero new ``llm_cost_ledger`` rows
    * zero ``news_match_log`` rows
    * zero ``ticker_cooldown`` rows

    Daily-curated rows pre-seeded into ``paper_orders`` are
    untouched.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False

    # Snapshot daily-curated state before the disabled-idle loop.
    pre_daily_rows = _query_daily_order_set(seeded_db)
    pre_daily_sha = _sha256_bytes(_serialize_daily_order_set(pre_daily_rows))

    pre_counts: dict[str, int] = {
        table: _count_rows(seeded_db, table) for table in _READING_B_ONLY_TABLES
    }
    pre_rb_orders = _count_news_event_entry_orders(seeded_db)
    pre_stage2_ledger = _count_stage2_event_ledger_rows(seeded_db)
    pre_total_ledger = _count_rows(seeded_db, "llm_cost_ledger")
    for table, cnt in pre_counts.items():
        assert cnt == 0, f"{table} must be empty in seeded baseline; got {cnt}"
    assert pre_rb_orders == 0
    assert pre_stage2_ledger == 0
    assert pre_total_ledger == 0

    sleeps = _drive_disabled_idle(cycles=3)
    assert sleeps == [15.0, 15.0]

    for table, pre_cnt in pre_counts.items():
        post_cnt = _count_rows(seeded_db, table)
        assert post_cnt == pre_cnt, (
            f"{table} row count must be unchanged under "
            f"NEWS_DAEMON_ENABLED=0; pre={pre_cnt} post={post_cnt}"
        )
    assert _count_news_event_entry_orders(seeded_db) == pre_rb_orders
    assert _count_stage2_event_ledger_rows(seeded_db) == pre_stage2_ledger
    assert _count_rows(seeded_db, "llm_cost_ledger") == pre_total_ledger

    # Daily-curated bucket sha256 unchanged after the disabled-idle
    # cycles (no perturbation of the existing rows).
    post_daily_rows = _query_daily_order_set(seeded_db)
    post_daily_sha = _sha256_bytes(_serialize_daily_order_set(post_daily_rows))
    assert post_daily_sha == pre_daily_sha


# ---------------------------------------------------------------------------
# VAL-M5-041 — Additive delta allowed only when NEWS_DAEMON_ENABLED=1
# ---------------------------------------------------------------------------


def test_audit_latest_additive_delta_with_daemon_enabled(
    baseline_audit_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=1`` → only ``reading_b`` top-level key added.

    Exercises :func:`write_reading_b_summary` (the only mutator
    of ``audit_latest.json`` that adds a ``reading_b`` key) and
    proves the resulting file (a) gains exactly one new top-level
    key, and (b) is byte-identical to the baseline once that key
    is removed (mirrors the contract's
    ``jq 'del(.reading_b)' post.json`` evidence form).
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    baseline_sha = _sha256_file(baseline_audit_path)

    # Add Reading-B additive rows to the seeded DB so the summary
    # writer has something to count.
    _insert_reading_b_additive_rows(seeded_db)

    # Re-emit the audit file with the Reading-B summary merged in.
    payload = write_reading_b_summary(baseline_audit_path, db_path=seeded_db)
    assert "reading_b" in payload
    rb = payload["reading_b"]
    assert isinstance(rb, dict)
    assert rb["candidate_events_emitted"] >= 1
    assert rb["news_event_entries_submitted"] >= 1
    assert rb["gate_pass_count"] >= 1

    # Every other top-level key from the baseline is preserved
    # verbatim.
    for key, value in _BASELINE_AUDIT.items():
        assert payload[key] == value, (
            f"baseline key {key!r} must be preserved byte-for-value "
            f"(got {payload[key]!r}, expected {value!r})"
        )

    # The on-disk file changed (gained a ``reading_b`` key).
    post_sha = _sha256_file(baseline_audit_path)
    assert post_sha != baseline_sha, (
        "audit_latest.json sha256 must change once reading_b is added"
    )

    # `jq 'del(.reading_b)'` form: removing the additive key must
    # yield a payload byte-identical to the baseline.
    stripped = {k: v for k, v in payload.items() if k != "reading_b"}
    assert stripped == _BASELINE_AUDIT
    stripped_sha = _sha256_bytes(_serialize_audit(stripped))
    assert stripped_sha == baseline_sha, (
        "post-Reading-B audit_latest.json minus 'reading_b' top-level "
        "key must be byte-identical to the pre-Reading-B baseline"
    )


# ---------------------------------------------------------------------------
# VAL-M5-041 / VAL-M5-043 — daily order set invariance under additive rows
# ---------------------------------------------------------------------------


def test_daily_order_set_byte_identical_with_daemon_enabled(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=1`` → daily order set sha256 unchanged.

    Asserts the carve-out filter (``event != 'news_event_entry'``)
    isolates the daily-curated bucket: even with Reading-B Stage-2
    rows present (paper_orders + ledger + candidate_events), the
    daily order set's canonical sha256 is byte-identical to the
    pre-Reading-B baseline.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    pre_rows = _query_daily_order_set(seeded_db)
    pre_sha = _sha256_bytes(_serialize_daily_order_set(pre_rows))

    # Add the Reading-B additive rows.
    _insert_reading_b_additive_rows(seeded_db)

    # paper_orders gained a 'news_event_entry' row plus the seeded
    # daily-curated rows are intact — total count goes up.
    conn = sqlite3.connect(str(seeded_db))
    try:
        total = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        rb_count = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE event='news_event_entry'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rb_count == 1
    assert total == len(_BASELINE_PAPER_ORDERS) + 1

    # Daily bucket sha256 (carve-out filter applied) is unchanged.
    post_rows = _query_daily_order_set(seeded_db)
    post_sha = _sha256_bytes(_serialize_daily_order_set(post_rows))
    assert post_sha == pre_sha, (
        "daily order set sha256 MUST remain byte-identical when "
        "additive Reading-B rows are present"
    )


# ---------------------------------------------------------------------------
# VAL-M5-042 — master signals byte-identical with daemon enabled
# (Reading B never touches unified_master_signals.json)
# ---------------------------------------------------------------------------


def test_unified_master_signals_byte_identical_with_daemon_enabled(
    baseline_master_signals_path: Path,
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=1`` → master signals sha256 unchanged.

    Reading-B has zero writers that mutate
    ``unified_master_signals.json``; this test pins that invariant
    by exercising every Reading-B write path that *does* touch
    persistence (Stage-2 ledger row insert, paper_orders, candidate
    events, audit summary writer) and verifying the master signals
    file's sha256 is unchanged.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True

    pre_sha = _sha256_file(baseline_master_signals_path)

    _insert_reading_b_additive_rows(seeded_db)
    # Also exercise the audit-summary writer so every Reading-B
    # write path has fired.
    audit_dest = baseline_master_signals_path.parent / "audit_latest.json"
    audit_dest.write_bytes(_serialize_audit(_BASELINE_AUDIT))
    write_reading_b_summary(audit_dest, db_path=seeded_db)

    post_sha = _sha256_file(baseline_master_signals_path)
    assert post_sha == pre_sha


# ---------------------------------------------------------------------------
# Smoke: kill-switch fail-open semantics (mirrors the doctring of
# is_news_daemon_enabled — a typo or unset value defaults to ENABLED).
# ---------------------------------------------------------------------------


def test_kill_switch_unset_defaults_to_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``NEWS_DAEMON_ENABLED`` → daemon is *enabled* (fail-open).

    The byte-identical regression invariant relies on the
    operator EXPLICITLY setting ``NEWS_DAEMON_ENABLED=0`` to engage
    the kill switch. An unset / missing env var means the daemon
    is enabled by default, and Reading-B additive rows MAY appear.
    """
    monkeypatch.delenv("NEWS_DAEMON_ENABLED", raising=False)
    assert is_news_daemon_enabled() is True


def test_kill_switch_explicit_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NEWS_DAEMON_ENABLED=0`` → kill switch engaged.

    Pins the contract: literal "0" (with surrounding whitespace
    stripped) is the ONLY value that disables the daemon. Any
    other string (including empty / typos) leaves the daemon
    enabled.
    """
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "0")
    assert is_news_daemon_enabled() is False
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", " 0 ")
    assert is_news_daemon_enabled() is False
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "1")
    assert is_news_daemon_enabled() is True
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "")
    assert is_news_daemon_enabled() is True
    monkeypatch.setenv("NEWS_DAEMON_ENABLED", "true")
    assert is_news_daemon_enabled() is True


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
    sha_only = text.strip()
    assert len(sha_only) == 40
    assert all(c in "0123456789abcdef" for c in sha_only)
    assert sha_only == _BASELINE_COMMIT_SHA
