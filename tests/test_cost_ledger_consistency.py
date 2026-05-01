"""Cross-flow cost-ledger consistency tests (f-cross-03).

Cross-cutting tests for VAL-CROSS-010, VAL-CROSS-011, VAL-CROSS-012
and VAL-CROSS-042. These verify the project-wide invariants that
tie the LLM cost ledger to the two reporting CLIs:

* VAL-CROSS-010 — every LLM call across both paths writes a row to
  ``llm_cost_ledger`` with the documented non-null fields. This is
  enforced as an audit invariant (every ``*_client.py`` under
  :mod:`biotech_sniper.llm` that hits an external provider must
  contain an ``INSERT INTO llm_cost_ledger`` sibling) plus a
  freshly-inserted multi-provider mix is round-tripped through
  the ledger.
* VAL-CROSS-011 — daily ``SUM(cost_usd)`` per provider matches
  ``reading_b_report --json`` (``stage2_dollars_today`` for
  ``perplexity``) and ``cost_report --since ... --json``
  (per-provider total in the rendered table) within $0.01.
* VAL-CROSS-012 — at least one ``provider='perplexity'`` row is
  representable end-to-end and survives both report renderings.
* VAL-CROSS-042 — legacy ``cost_report`` itemizes ``perplexity``
  as a first-class line item: it appears in the rendered table
  alongside ``xai``/``anthropic``/``gemini`` rows; its
  ``daily_totals`` sum matches the ledger; and the
  :data:`MODEL_PRICING` table contains a ``'perplexity'`` entry so
  ``--check-plausibility`` recognises the provider rather than
  falling through to ``unknown-pricing``.

All tests run against a temp SQLite db (``tmp_path / 'ledger.db'``)
so they exercise the real schema (v10 — Reading-B foundations) end
to end without touching the project database. No live network or
LLM calls are made.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from biotech_sniper import db
from biotech_sniper import db as project_db
from biotech_sniper.llm import cost_report
from biotech_sniper.llm.cost_report import (
    MODEL_PRICING,
    daily_totals,
    format_table,
    main as cost_report_main,
)
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.reports import reading_b_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LLM_PACKAGE = Path(__file__).resolve().parent.parent / "biotech_sniper" / "llm"

#: Per-provider client modules that hit an external network surface
#: and therefore MUST insert into ``llm_cost_ledger``. Kept as the
#: explicit list so a future fifth provider that lands without a
#: ledger sibling is loud at test time.
_PROVIDER_CLIENT_MODULES: tuple[str, ...] = (
    "xai_client.py",
    "claude_client.py",
    "gemini_client.py",
    "perplexity_client.py",
)


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh SQLite db with the v10 (Reading-B) schema applied."""

    db_path = tmp_path / "ledger.db"
    run_migrations_runner(db_path, target_version=project_db.CURRENT_VERSION, take_backup_first=False)
    return db_path


def _insert_rows(db_path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Insert raw rows into ``llm_cost_ledger`` for a given test."""

    conn = db.connect(db_path)
    try:
        with conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO llm_cost_ledger (
                        provider, model_id, purpose,
                        prompt_tokens, completion_tokens,
                        latency_ms, cost_usd, request_id, called_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("provider"),
                        row.get("model_id"),
                        row.get("purpose", "stage2_event_scoring"),
                        row.get("prompt_tokens", 100),
                        row.get("completion_tokens", 50),
                        row.get("latency_ms", 250),
                        float(row.get("cost_usd", 0.0)),
                        row.get("request_id", "req-test"),
                        row.get("called_at", "2026-04-29T10:00:00.000Z"),
                    ),
                )
    finally:
        conn.close()


def _ledger_total_for(
    db_path: Path,
    *,
    provider: str | None = None,
    date_iso: str | None = None,
) -> float:
    """Return ``SUM(cost_usd)`` for the given filter (or whole table)."""

    where: list[str] = []
    params: list[Any] = []
    if provider is not None:
        where.append("provider = ?")
        params.append(provider)
    if date_iso is not None:
        where.append("DATE(called_at) = DATE(?)")
        params.append(date_iso)
    sql = "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_cost_ledger"
    if where:
        sql += " WHERE " + " AND ".join(where)
    conn = db.connect(db_path)
    try:
        result = conn.execute(sql, params).fetchone()
        return float(result[0]) if result else 0.0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-CROSS-010: audit invariant — every LLM client writes the ledger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_filename", _PROVIDER_CLIENT_MODULES)
def test_every_llm_client_module_has_cost_ledger_insert(
    module_filename: str,
) -> None:
    """Audit invariant via grep: every external-network LLM client
    in :mod:`biotech_sniper.llm` MUST have an ``INSERT INTO
    llm_cost_ledger`` sibling.

    This guards against the failure mode where a future provider is
    added but its cost ledger row write is forgotten — which would
    silently break VAL-CROSS-010 on production.
    """

    module_path = _LLM_PACKAGE / module_filename
    assert module_path.exists(), (
        f"expected provider client at {module_path}; the audit "
        f"invariant list in this test file is stale"
    )
    text = module_path.read_text(encoding="utf-8")
    # The literal SQL fragment is canonical across all four current
    # clients — see grep output in f-cross-03 worker notes.
    assert "INSERT INTO llm_cost_ledger" in text, (
        f"{module_filename} hits an external LLM provider but does NOT "
        f"contain an `INSERT INTO llm_cost_ledger` sibling; "
        f"VAL-CROSS-010 audit invariant violated"
    )


def test_each_inserted_row_has_required_non_null_fields(
    tmp_path: Path,
) -> None:
    """The four-provider mix written via the canonical INSERT shape
    yields rows with all VAL-CROSS-010 required fields non-null.
    """

    db_path = _make_db(tmp_path)
    today = "2026-04-29"
    sample_rows = [
        {
            "provider": "xai",
            "model_id": "grok-4-0709",
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "latency_ms": 1500,
            "cost_usd": 0.008,
            "called_at": f"{today}T08:00:00.000Z",
        },
        {
            "provider": "anthropic",
            "model_id": "claude-opus-4-1",
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "latency_ms": 4200,
            "cost_usd": 0.030,
            "called_at": f"{today}T08:00:01.000Z",
        },
        {
            "provider": "gemini",
            "model_id": "gemini-2.5-pro",
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "latency_ms": 1800,
            "cost_usd": 0.00325,
            "called_at": f"{today}T08:00:02.000Z",
        },
        {
            "provider": "perplexity",
            "model_id": "sonar",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "latency_ms": 1200,
            "cost_usd": 0.0065,
            "called_at": f"{today}T08:00:03.000Z",
        },
    ]
    _insert_rows(db_path, sample_rows)

    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT provider, model_id, prompt_tokens, completion_tokens, "
            "latency_ms, cost_usd, called_at FROM llm_cost_ledger "
            "ORDER BY called_at"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 4
    seen_providers = {r["provider"] for r in rows}
    assert seen_providers == {"xai", "anthropic", "gemini", "perplexity"}
    for row in rows:
        assert row["model_id"] not in (None, "")
        assert row["prompt_tokens"] is not None
        assert row["completion_tokens"] is not None
        assert row["latency_ms"] is not None
        assert row["cost_usd"] is not None
        assert row["called_at"] not in (None, "")


# ---------------------------------------------------------------------------
# VAL-CROSS-012: perplexity provider is registered and emittable
# ---------------------------------------------------------------------------


def test_perplexity_provider_check_constraint_admits_value(
    tmp_path: Path,
) -> None:
    """The v10 schema's CHECK enum on ``llm_cost_ledger.provider``
    admits ``'perplexity'`` and rejects an unknown provider.
    """

    db_path = _make_db(tmp_path)
    _insert_rows(
        db_path,
        [
            {
                "provider": "perplexity",
                "model_id": "sonar",
                "cost_usd": 0.0065,
                "called_at": "2026-04-29T10:00:00.000Z",
            }
        ],
    )

    conn = db.connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM llm_cost_ledger WHERE provider='perplexity'"
        ).fetchone()[0]
        assert int(cnt) == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_cost_ledger (provider, model_id, "
                "prompt_tokens, completion_tokens, latency_ms, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("openai", "gpt-4o", 100, 50, 250, 0.001),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VAL-CROSS-011: daily SUM matches reports within $0.01
# ---------------------------------------------------------------------------


_TODAY = "2026-04-29"
_MIXED_DAY_ROWS: tuple[dict[str, Any], ...] = (
    {
        "provider": "xai",
        "model_id": "grok-4-0709",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost_usd": 0.008,
        "called_at": f"{_TODAY}T08:00:00.000Z",
    },
    {
        "provider": "anthropic",
        "model_id": "claude-opus-4-1",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost_usd": 0.030,
        "called_at": f"{_TODAY}T08:00:01.000Z",
    },
    {
        "provider": "gemini",
        "model_id": "gemini-2.5-pro",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cost_usd": 0.00325,
        "called_at": f"{_TODAY}T08:00:02.000Z",
    },
    {
        "provider": "perplexity",
        "model_id": "sonar",
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cost_usd": 0.0065,
        "called_at": f"{_TODAY}T08:00:03.000Z",
    },
    {
        "provider": "perplexity",
        "model_id": "sonar",
        "prompt_tokens": 800,
        "completion_tokens": 400,
        "cost_usd": 0.0062,
        "called_at": f"{_TODAY}T09:00:00.000Z",
    },
)


def test_perplexity_ledger_sum_matches_reading_b_report_stage2_dollars_today(
    tmp_path: Path,
) -> None:
    """``reading_b_report.build_report(...)['stage2_dollars_today']``
    equals the ledger's perplexity-only ``SUM(cost_usd)`` for the
    same day, within $0.01 (VAL-CROSS-011 / VAL-M4-049 evidence).
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    payload = reading_b_report.build_report(
        db_path, _TODAY, top_n=10, cap_usd=20.0
    )

    expected_sum = _ledger_total_for(
        db_path, provider="perplexity", date_iso=_TODAY
    )
    assert expected_sum > 0
    assert abs(payload["stage2_dollars_today"] - expected_sum) <= 0.01


def test_cost_report_daily_totals_match_ledger_sum_per_provider(
    tmp_path: Path,
) -> None:
    """``cost_report.daily_totals(...)`` per-provider sums match the
    raw ledger ``SUM(cost_usd)`` per provider for the day, within
    $0.01 (the same source-of-truth invariant VAL-CROSS-011 wants).
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, _TODAY)
    finally:
        conn.close()

    by_provider: dict[str, float] = {}
    for r in rows:
        if r["day"] != _TODAY:
            continue
        by_provider[str(r["provider"])] = float(r["cost_usd"])

    for provider in ("xai", "anthropic", "gemini", "perplexity"):
        ledger_total = _ledger_total_for(
            db_path, provider=provider, date_iso=_TODAY
        )
        assert provider in by_provider, (
            f"cost_report.daily_totals omitted provider={provider}; "
            f"VAL-CROSS-042 first-class itemization broken"
        )
        assert abs(by_provider[provider] - ledger_total) <= 0.01, (
            f"cost_report.daily_totals provider={provider} "
            f"sum={by_provider[provider]:.6f} drifts from ledger "
            f"sum={ledger_total:.6f}"
        )


def test_cost_report_total_row_matches_full_day_ledger_sum(
    tmp_path: Path,
) -> None:
    """The CLI's TOTAL row sums to the full-day ledger
    ``SUM(cost_usd)`` within $0.01.

    This is the strict-form of VAL-CROSS-011: the report's daily
    aggregate matches the ledger's per-day total, not just per
    provider.
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, _TODAY)
    finally:
        conn.close()

    total = sum(float(r["cost_usd"]) for r in rows if r["day"] == _TODAY)
    expected = _ledger_total_for(db_path, date_iso=_TODAY)
    assert abs(total - expected) <= 0.01


# ---------------------------------------------------------------------------
# VAL-CROSS-042: cost_report itemizes 'perplexity' as first-class
# ---------------------------------------------------------------------------


def test_cost_report_table_has_perplexity_line_item(tmp_path: Path) -> None:
    """The rendered ``format_table`` output contains a per-day row
    whose ``provider`` cell is ``perplexity``, alongside the legacy
    three providers.
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, _TODAY)
    finally:
        conn.close()

    table = format_table(rows)
    assert "perplexity" in table, (
        f"expected 'perplexity' line in cost_report table; got:\n{table}"
    )
    # All four providers must be present so we don't accidentally
    # render a perplexity-only table.
    for provider in ("xai", "anthropic", "gemini", "perplexity"):
        assert provider in table


def test_cost_report_cli_main_emits_perplexity_first_class(
    tmp_path: Path,
) -> None:
    """End-to-end exercise of ``python -m biotech_sniper.llm.cost_report
    --since YYYY-MM-DD --strict --json``:

    * exit code 0 (no null required fields, plausibility off);
    * JSON ``totals`` block contains a per-provider entry for
      ``perplexity`` whose ``cost_usd`` matches the ledger sum
      within $0.01.
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cost_report_main(
            ["--since", _TODAY, "--db", str(db_path), "--json", "--strict"]
        )
    assert rc == 0, buf.getvalue()
    payload = json.loads(buf.getvalue())
    totals = payload["totals"]
    by_provider = {
        (r["day"], r["provider"]): float(r["cost_usd"]) for r in totals
    }
    perplexity_total = by_provider.get((_TODAY, "perplexity"))
    assert perplexity_total is not None, (
        f"cost_report --json totals omitted perplexity; got rows={totals}"
    )
    expected = _ledger_total_for(
        db_path, provider="perplexity", date_iso=_TODAY
    )
    assert abs(perplexity_total - expected) <= 0.01


def test_model_pricing_recognizes_perplexity_provider() -> None:
    """``MODEL_PRICING`` carries a ``'perplexity'`` entry so
    ``--check-plausibility`` recognises the provider as first-class
    (VAL-CROSS-042 — first-class recognition, not just incidental
    grouping by SQL).
    """

    assert "perplexity" in MODEL_PRICING, (
        "cost_report.MODEL_PRICING is missing a 'perplexity' entry; "
        "VAL-CROSS-042 first-class recognition violated"
    )
    table = MODEL_PRICING["perplexity"]
    assert "sonar" in table, (
        "MODEL_PRICING['perplexity'] must include the 'sonar' model "
        "since the perplexity client default is DEFAULT_MODEL='sonar'"
    )
    rates = table["sonar"]
    # sonar $1 / 1M input + $1 / 1M output → $0.001 per 1k tokens.
    assert abs(rates.input_per_1k - 0.001) < 1e-9
    assert abs(rates.output_per_1k - 0.001) < 1e-9


def test_perplexity_pricing_lookup_returns_an_entry() -> None:
    """``cost_report.lookup_pricing('perplexity', 'sonar')`` returns
    a non-None entry — i.e. plausibility skips ``unknown-pricing``
    for valid sonar rows.
    """

    rates = cost_report.lookup_pricing("perplexity", "sonar")
    assert rates is not None
    # Defensive against label drift — only assert input/output rates.
    assert abs(rates.input_per_1k - 0.001) < 1e-9


# ---------------------------------------------------------------------------
# Cross-report cohesion: reading_b_report stage2_dollars matches
# the cost_report perplexity row exactly.
# ---------------------------------------------------------------------------


def test_reading_b_report_and_cost_report_agree_on_perplexity_total(
    tmp_path: Path,
) -> None:
    """``reading_b_report.build_report(...)['stage2_dollars_today']``
    equals the ``cost_report.daily_totals`` perplexity row's
    ``cost_usd`` for the same day, within $0.01.

    This is the cross-CLI cohesion check the feature spec calls out:
    "SUM(cost_usd) WHERE date=today MATCHES reading_b_report
    stage2_dollars_today + cost_report daily total within $0.01".
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    payload = reading_b_report.build_report(
        db_path, _TODAY, top_n=10, cap_usd=20.0
    )
    conn = db.connect(db_path)
    try:
        rows = daily_totals(conn, _TODAY)
    finally:
        conn.close()
    pplx_row = next(
        (
            r
            for r in rows
            if r["day"] == _TODAY and r["provider"] == "perplexity"
        ),
        None,
    )
    assert pplx_row is not None
    assert abs(
        payload["stage2_dollars_today"] - float(pplx_row["cost_usd"])
    ) <= 0.01


def test_cli_table_shows_perplexity_alongside_other_providers(
    tmp_path: Path,
) -> None:
    """End-to-end exercise of the human-readable mode: the CLI's
    table contains a row per ``(_TODAY, perplexity)`` along with the
    other providers, and the TOTAL line matches the day's ledger
    sum within $0.01.
    """

    db_path = _make_db(tmp_path)
    _insert_rows(db_path, _MIXED_DAY_ROWS)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cost_report_main(
            ["--since", _TODAY, "--db", str(db_path), "--strict"]
        )
    assert rc == 0, buf.getvalue()
    text = buf.getvalue()
    assert "perplexity" in text
    # The TOTAL row prints a 4-decimal cost figure; cross-check that
    # the printed value is within $0.01 of the ledger sum.
    expected_total = _ledger_total_for(db_path, date_iso=_TODAY)
    match = re.search(r"TOTAL\s+-\s+\d+\s+\d+\s+\d+\s+([0-9.]+)", text)
    assert match, f"TOTAL line not found in:\n{text}"
    printed_total = float(match.group(1))
    assert abs(printed_total - expected_total) <= 0.01
