"""Tests for ``python -m biotech_sniper.cli.force_scan`` (f-live-02).

The force_scan CLI is the operator-triggered Stage-2 fan-out for
ad-hoc verification. It supports two mutually-exclusive modes:

* ``--dry-run`` — runs the cheap-first chain with mock providers /
  ``persist=False`` so ZERO rows hit ``llm_cost_ledger`` AND ZERO
  rows hit ``ensemble_scores_event``. ``.armed`` is NOT consulted.
* ``--live`` — requires ``.armed`` (non-zero size) BEFORE invoking
  any LLM client. If absent, exits non-zero with a stderr message
  AND persists ``news_match_log`` rows (``reason='armed_file_missing'``,
  ``gate_outcome='rejected'``) per in-scope candidate AND writes
  ZERO ``llm_cost_ledger`` rows.

Scopes:

* ``pdufa-soon`` (default) — top-N tickers with PDUFA in next 7d
  AND a ``candidate_event`` in last 24h.
* ``all`` — every emitted candidate this poll cycle.
* ``tickers`` — explicit comma list intersected with universe
  (``russell2k_biotech ∩ universe.tier IN (watch, tradeable)``).

Pinned by the f-live-02 verification step:
``.venv/bin/pytest -q tests/test_cli_force_scan.py (RED then GREEN)``.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from biotech_sniper import db as project_db
from biotech_sniper.migrations.runner import run as run_migrations_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alpha.db"
    conn = project_db.connect(db_path)
    try:
        project_db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(
        db_path,
        target_version=project_db.CURRENT_VERSION,
        take_backup_first=False,
    )
    return db_path


def _seed_news_event(
    db_path: Path,
    *,
    ticker: str,
    title: str = "headline",
    url: str = "https://example.com/x",
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO news_events ("
            "ticker, source, title, url, published_at, ingested_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                "rss",
                title,
                url,
                "2026-04-30T12:00:00Z",
                "2026-04-30T12:00:01Z",
            ),
        )
        nid = int(
            conn.execute("SELECT MAX(id) FROM news_events").fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return nid


def _seed_candidate(
    db_path: Path,
    *,
    ticker: str,
    matched_keywords: str = "pdufa,approval",
    dedup_seed: str = "fs-001",
) -> int:
    nid = _seed_news_event(
        db_path,
        ticker=ticker,
        url=f"https://example.com/{ticker.lower()}-{dedup_seed}",
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO candidate_events ("
            "ticker, source_news_event_id, matched_keywords, "
            "emitted_at, dedup_key) VALUES (?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (
                ticker,
                nid,
                matched_keywords,
                f"dedup-{ticker.lower()}-{dedup_seed}",
            ),
        )
        cid = int(
            conn.execute(
                "SELECT MAX(id) FROM candidate_events"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def _seed_pdufa(
    db_path: Path,
    *,
    ticker: str,
    drug: str = "drugX",
    days_offset: int = 3,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pdufa_calendar ("
            "ticker, drug, action_date, sponsor, source_url, fetched_at"
            ") VALUES (?, ?, DATE('now','+' || ? || ' days'), ?, ?, ?)",
            (
                ticker,
                drug,
                int(days_offset),
                "TestSponsor",
                "https://www.fda.gov/test",
                "2026-04-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_universe_row(db_path: Path, *, ticker: str, tier: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO universe (ticker, tier) VALUES (?, ?)",
            (ticker, tier),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_russell_row(
    db_path: Path,
    *,
    ticker: str,
    cik: str = "0000000001",
    sic: int = 2834,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO russell2k_biotech ("
            "ticker, cik, sic, sic_description, as_of_date, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticker,
                cik,
                int(sic),
                "PHARMACEUTICAL PREPARATIONS",
                "2026-04-30",
                "2026-04-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_armed_file(tmp_path: Path) -> Path:
    armed = tmp_path / ".armed"
    armed.write_text("ok")
    return armed


def _provider_callable(
    label: str = "material",
    direction: str = "bullish",
    probability: float = 0.92,
):
    def _call(_candidate, *, name: str = "stub") -> dict[str, Any]:
        return {
            "label": label,
            "probability": probability,
            "direction": direction,
            "rationale": f"{name} stub",
            "citations": [],
            "latency_ms": 5,
            "cost_usd": 0.001,
        }

    return _call


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    yield _build_db(tmp_path)


@pytest.fixture
def armed_path(tmp_path: Path) -> Path:
    return _make_armed_file(tmp_path)


@pytest.fixture
def all_providers() -> dict[str, Any]:
    from biotech_sniper.llm.ensemble import ALL_PROVIDERS

    return {name: _provider_callable() for name in ALL_PROVIDERS}


def _count(
    db_path: Path,
    table: str,
    where: str = "1=1",
    params: tuple = (),
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", params
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — dry_run_writes_zero_ledger_rows
# ---------------------------------------------------------------------------


def test_dry_run_writes_zero_ledger_rows(
    db_path: Path,
    armed_path: Path,
    all_providers,
    capsys,
):
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("DRY1", "DRY2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"dr-{n}")

    rc = main(
        argv=[
            "--dry-run",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    assert _count(db_path, "llm_cost_ledger") == 0


# ---------------------------------------------------------------------------
# 2 — dry_run_writes_zero_ensemble_rows
# ---------------------------------------------------------------------------


def test_dry_run_writes_zero_ensemble_rows(
    db_path: Path,
    armed_path: Path,
    all_providers,
):
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("DRYE1", "DRYE2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"dre-{n}")

    rc = main(
        argv=[
            "--dry-run",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    assert _count(db_path, "ensemble_scores_event") == 0


# ---------------------------------------------------------------------------
# 3 — dry_run_outputs_simulated_report
# ---------------------------------------------------------------------------


def test_dry_run_outputs_simulated_report(
    db_path: Path,
    armed_path: Path,
    all_providers,
    capsys,
):
    from biotech_sniper.cli.force_scan import main

    _seed_pdufa(db_path, ticker="SIMA", days_offset=2)
    _seed_candidate(db_path, ticker="SIMA", dedup_seed="sim-1")
    _seed_pdufa(db_path, ticker="SIMB", days_offset=3)
    _seed_candidate(db_path, ticker="SIMB", dedup_seed="sim-2")

    rc = main(
        argv=[
            "--dry-run",
            "--scope=pdufa-soon",
            "--n=5",
            "--json",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["mode"] == "dry-run"
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 2
    for entry in candidates:
        assert "ticker" in entry
        assert "mean_probability" in entry
        assert "label" in entry
        assert "gate_failed_reason" in entry
        assert "would_submit" in entry
        assert isinstance(entry["would_submit"], bool)


# ---------------------------------------------------------------------------
# 4 — live_without_armed_exits_nonzero
# ---------------------------------------------------------------------------


def test_live_without_armed_exits_nonzero(
    db_path: Path,
    tmp_path: Path,
    all_providers,
    capsys,
):
    from biotech_sniper.cli.force_scan import main

    _seed_pdufa(db_path, ticker="LNA1", days_offset=2)
    _seed_candidate(db_path, ticker="LNA1", dedup_seed="lna-1")

    missing = tmp_path / "no_such" / ".armed"
    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=missing,
    )
    assert rc != 0
    captured = capsys.readouterr()
    assert "armed file missing" in captured.err


# ---------------------------------------------------------------------------
# 5 — live_without_armed_writes_zero_cost_rows
# ---------------------------------------------------------------------------


def test_live_without_armed_writes_zero_cost_rows(
    db_path: Path,
    tmp_path: Path,
    all_providers,
):
    from biotech_sniper.cli.force_scan import main

    _seed_pdufa(db_path, ticker="LNZ1", days_offset=2)
    _seed_candidate(db_path, ticker="LNZ1", dedup_seed="lnz-1")

    missing = tmp_path / "absent" / ".armed"
    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=missing,
    )
    assert rc != 0
    assert _count(db_path, "llm_cost_ledger") == 0
    assert _count(db_path, "ensemble_scores_event") == 0


# ---------------------------------------------------------------------------
# 6 — live_without_armed_writes_armed_missing_news_match_log
# ---------------------------------------------------------------------------


def test_live_without_armed_writes_armed_missing_news_match_log(
    db_path: Path,
    tmp_path: Path,
    all_providers,
):
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("LMA1", "LMA2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"lma-{n}")

    missing = tmp_path / "absent" / ".armed"
    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=missing,
    )
    assert rc != 0
    rejected = _count(
        db_path,
        "news_match_log",
        "reason = ? AND gate_outcome = ?",
        ("armed_file_missing", "rejected"),
    )
    assert rejected >= 2


# ---------------------------------------------------------------------------
# 7 — live_with_armed_runs_chain
# ---------------------------------------------------------------------------


def test_live_with_armed_runs_chain(
    db_path: Path,
    armed_path: Path,
    all_providers,
):
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("LWA1", "LWA2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"lwa-{n}")

    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    assert _count(db_path, "ensemble_scores_event") == 2 * 4


# ---------------------------------------------------------------------------
# 8 — scope_tickers_intersects_universe
# ---------------------------------------------------------------------------


def test_scope_tickers_intersects_universe(
    db_path: Path,
    armed_path: Path,
    all_providers,
):
    """Tickers given via --tickers must be intersected with the
    russell2k_biotech ∩ universe.tier IN (watch,tradeable) set."""
    from biotech_sniper.cli.force_scan import main

    for t in ("INSCOPE", "OUTUNI", "OUTRUS"):
        _seed_candidate(db_path, ticker=t, dedup_seed=f"sti-{t}")

    _seed_universe_row(db_path, ticker="INSCOPE", tier="watch")
    _seed_russell_row(db_path, ticker="INSCOPE")

    _seed_russell_row(db_path, ticker="OUTUNI")

    _seed_universe_row(db_path, ticker="OUTRUS", tier="watch")

    rc = main(
        argv=[
            "--dry-run",
            "--tickers=INSCOPE,OUTUNI,OUTRUS",
            f"--db={db_path}",
            "--json",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    import json as _json

    assert rc == 0
    out = _json.loads(sys.stdout.getvalue() if isinstance(sys.stdout, io.StringIO) else "{}")
    # capsys not used here — instead read from the dispatch result by
    # re-running with a captured stream. We simplify by re-invoking
    # via the helper API to extract the structured result.
    from biotech_sniper.cli.force_scan import (
        resolve_force_scan_candidates,
    )

    cands = resolve_force_scan_candidates(
        db_path=db_path,
        scope="tickers",
        tickers=["INSCOPE", "OUTUNI", "OUTRUS"],
        n=10,
    )
    tickers = sorted(c["ticker"] for c in cands)
    assert tickers == ["INSCOPE"]


# ---------------------------------------------------------------------------
# 9 — scope_pdufa_soon_default
# ---------------------------------------------------------------------------


def test_scope_pdufa_soon_default(
    db_path: Path,
    armed_path: Path,
    all_providers,
    capsys,
):
    """When --scope is omitted, default is pdufa-soon (top-N=5)."""
    from biotech_sniper.cli.force_scan import (
        DEFAULT_SCOPE,
        DEFAULT_N,
        build_parser,
    )

    parser = build_parser()
    args = parser.parse_args(["--dry-run", f"--db={db_path}"])
    assert args.scope == DEFAULT_SCOPE == "pdufa-soon"
    assert args.n == DEFAULT_N == 5


# ---------------------------------------------------------------------------
# 10 — daily_cap_fires_mid_loop
# ---------------------------------------------------------------------------


def test_daily_cap_fires_mid_loop(
    db_path: Path,
    armed_path: Path,
    all_providers,
    monkeypatch,
    capsys,
):
    """When the daily cap fires mid-loop, force_scan must:

    * exit 0 (NOT non-zero — the cap is a soft stop)
    * emit ``daily LLM cap exceeded; remaining candidates skipped``
      to stderr
    * include a cap-fired marker in the output payload
    """
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("CAPA", "CAPB", "CAPC"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"cap-{n}")

    monkeypatch.setenv("LLM_STAGE2_DAILY_USD_CAP", "0.0001")

    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            "--json",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "daily LLM cap exceeded" in captured.err
    payload = json.loads(captured.out)
    assert payload.get("cap_fired") is True


# ---------------------------------------------------------------------------
# 11 — summary_output_text_and_json
# ---------------------------------------------------------------------------


def test_summary_output_text_and_json(
    db_path: Path,
    armed_path: Path,
    all_providers,
    capsys,
):
    from biotech_sniper.cli.force_scan import main

    _seed_pdufa(db_path, ticker="SUMA", days_offset=2)
    _seed_candidate(db_path, ticker="SUMA", dedup_seed="sum-a")
    _seed_pdufa(db_path, ticker="SUMB", days_offset=3)
    _seed_candidate(db_path, ticker="SUMB", dedup_seed="sum-b")

    # JSON output
    rc = main(
        argv=[
            "--dry-run",
            "--scope=pdufa-soon",
            "--n=5",
            "--json",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    summary = payload["summary"]
    for key in (
        "in_scope",
        "processed",
        "passed_gate",
        "would_submit",
        "total_cost_usd_today",
    ):
        assert key in summary
    assert summary["in_scope"] == 2
    assert summary["processed"] == 2

    # Text output (no --json)
    rc = main(
        argv=[
            "--dry-run",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc == 0
    captured = capsys.readouterr()
    text_out = captured.out
    assert "in_scope" in text_out
    assert "processed" in text_out
    assert "passed_gate" in text_out


# ---------------------------------------------------------------------------
# 12 — argparse: exactly one of --dry-run / --live required
# ---------------------------------------------------------------------------


def test_requires_exactly_one_mode(
    db_path: Path,
    armed_path: Path,
    all_providers,
    capsys,
):
    from biotech_sniper.cli.force_scan import main

    # Neither flag.
    rc = main(
        argv=[f"--db={db_path}"],
        provider_overrides=all_providers,
        armed_path=armed_path,
    )
    assert rc != 0


# ---------------------------------------------------------------------------
# 13 — live_with_empty_armed_file_exits_nonzero (f-fix-live-02)
# ---------------------------------------------------------------------------


def test_live_with_empty_armed_file_exits_nonzero(
    db_path: Path,
    tmp_path: Path,
    all_providers,
    capsys,
):
    """A zero-byte ``.armed`` file MUST exit non-zero with a stderr
    message indicating the file is empty / requires non-zero size.

    Pins VAL-LIVE-003 against the f-fix-live-02 contract: a stray
    ``touch /root/alpha_sniper/.armed`` on the production VPS would
    arm Stage-2 under the prior existence-only check; the f-fix-live-02
    pre-check refuses the arming.
    """
    from biotech_sniper.cli.force_scan import main

    _seed_pdufa(db_path, ticker="EMP1", days_offset=2)
    _seed_candidate(db_path, ticker="EMP1", dedup_seed="emp-1")

    empty = tmp_path / ".armed"
    empty.write_bytes(b"")
    assert empty.exists() and empty.stat().st_size == 0

    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=empty,
    )
    assert rc != 0
    captured = capsys.readouterr()
    err_lower = captured.err.lower()
    assert "empty" in err_lower or "non-zero size" in err_lower


# ---------------------------------------------------------------------------
# 14 — live_with_empty_armed_file_writes_zero_cost_rows (f-fix-live-02)
# ---------------------------------------------------------------------------


def test_live_with_empty_armed_file_writes_zero_cost_rows(
    db_path: Path,
    tmp_path: Path,
    all_providers,
):
    """An empty ``.armed`` file MUST short-circuit BEFORE any LLM
    spend: zero ``llm_cost_ledger`` rows AND zero
    ``ensemble_scores_event`` rows AND ≥1 ``news_match_log`` row
    with the canonical rejected reason.
    """
    from biotech_sniper.cli.force_scan import main

    for n, t in enumerate(("EMPZ1", "EMPZ2"), start=1):
        _seed_pdufa(db_path, ticker=t, days_offset=n)
        _seed_candidate(db_path, ticker=t, dedup_seed=f"empz-{n}")

    empty = tmp_path / ".armed"
    empty.write_bytes(b"")

    rc = main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
        armed_path=empty,
    )
    assert rc != 0
    assert _count(db_path, "llm_cost_ledger") == 0
    assert _count(db_path, "ensemble_scores_event") == 0
    rejected = _count(
        db_path,
        "news_match_log",
        "reason = ? AND gate_outcome = ?",
        ("armed_file_missing", "rejected"),
    )
    assert rejected >= 1


# ---------------------------------------------------------------------------
# 15 — live_with_canonical_default_empty_armed_prints_empty_stderr
# (f-fix-live-06)
# ---------------------------------------------------------------------------


def test_live_with_canonical_default_empty_armed_prints_empty_stderr(
    db_path: Path,
    tmp_path: Path,
    all_providers,
    monkeypatch,
    capsys,
):
    """When ``armed_path`` kwarg is omitted (the typical operator
    invocation from the VPS) and the canonical
    :data:`biotech_sniper.paths.READING_B_ARMED_FILE` is a zero-byte
    file, the CLI MUST print ``ARMED_EMPTY_STDERR`` (containing the
    ``NON-ZERO SIZE`` signal) — NOT ``ARMED_MISSING_STDERR``.

    Pins f-fix-live-06: the empty-file detection in ``_run_live`` must
    resolve the effective path the SAME way :func:`armed_gate` does
    (falling back to ``READING_B_ARMED_FILE`` when the kwarg is None)
    so the canonical-default case is not silently misclassified as
    missing.
    """
    from biotech_sniper.cli import force_scan as force_scan_module
    from biotech_sniper import paths as paths_module

    _seed_pdufa(db_path, ticker="CANE", days_offset=2)
    _seed_candidate(db_path, ticker="CANE", dedup_seed="cane-1")

    canonical = tmp_path / ".armed"
    canonical.write_bytes(b"")
    assert canonical.exists() and canonical.stat().st_size == 0

    monkeypatch.setattr(
        paths_module, "READING_B_ARMED_FILE", canonical, raising=True
    )

    rc = force_scan_module.main(
        argv=[
            "--live",
            "--scope=pdufa-soon",
            "--n=5",
            f"--db={db_path}",
        ],
        provider_overrides=all_providers,
    )

    assert rc != 0
    captured = capsys.readouterr()
    assert "NON-ZERO SIZE" in captured.err
    assert "armed file missing:" not in captured.err
