"""LLM cost-ledger reporting + plausibility helpers (M2 f-m2-06).

This module turns the ``llm_cost_ledger`` SQLite table — written to
by every successful call inside :mod:`biotech_sniper.llm.xai_client`,
:mod:`biotech_sniper.llm.claude_client` and
:mod:`biotech_sniper.llm.gemini_client` — into a queryable surface
backing three concerns:

1. **Required-field integrity.** Every row must carry non-null
   ``model_id``, ``prompt_tokens``, ``completion_tokens``,
   ``latency_ms``, ``cost_usd`` and ``called_at`` (alias ``ts``).
   :func:`find_rows_with_null_fields` returns the offending rows so
   tests / the daily audit can fail loudly.

2. **Daily per-provider totals.** :func:`daily_totals` groups by
   ``DATE(called_at)`` and ``provider`` and returns a list of dicts
   suitable for tabular printing or downstream JSON. The same SQL
   shape is asserted by ``VAL-M2-051`` in the validation contract.

3. **Cost plausibility.** Each provider's published per-1k token
   pricing is tracked alongside the existing constants in the per-
   client modules. :func:`plausibility_check` recomputes the
   *expected* cost for the recorded ``(prompt_tokens,
   completion_tokens)`` and checks that the recorded ``cost_usd``
   sits within ±``DEFAULT_TOLERANCE`` of it. The default tolerance
   (20 %) absorbs floating-point rounding, occasional cached-prompt
   discounts, and provider experimental-pricing fluctuations without
   being so loose that an order-of-magnitude regression slips
   through.

CLI
---
``python -m biotech_sniper.llm.cost_report --since YYYY-MM-DD``
prints a fixed-width table with columns ``date``, ``provider``,
``calls``, ``prompt_tokens``, ``completion_tokens``, ``cost_usd``,
followed by a ``TOTAL`` row. Optional flags:

* ``--db PATH``         — override the database path.
* ``--check-plausibility`` — also flag any rows whose recorded
  ``cost_usd`` deviates from the published-rate estimate by more
  than the ±20 % tolerance. Exits non-zero when any are found.
* ``--strict``          — exit non-zero when any row has null
  required fields.
* ``--json``            — emit a JSON document instead of a table
  (consumed by the M4 audit pipeline).

The module never writes to the ledger; it only reads.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from biotech_sniper import db
from biotech_sniper.llm.perplexity_client import (
    compute_cost_usd as _perplexity_compute_cost_usd,
)
from biotech_sniper.paths import DATA_DIR

__all__ = [
    "DEFAULT_TOLERANCE",
    "REQUIRED_FIELDS",
    "MODEL_PRICING",
    "PERPLEXITY_SEARCH_CONTEXT_SIZE",
    "ProviderRates",
    "PlausibilityResult",
    "daily_totals",
    "find_rows_with_null_fields",
    "iter_rows_since",
    "lookup_pricing",
    "expected_cost_usd",
    "plausibility_check",
    "find_implausible_rows",
    "format_table",
    "main",
]


#: Search-context-size sent by the Stage-2 Reading-B perplexity client
#: on every call. Pinned to ``"low"`` because the Stage-2 daily $20
#: cap forbids ``medium`` / ``high``. The plausibility recompute for
#: perplexity rows uses this constant so the flat per-call surcharge
#: ($5 / 1k requests at ``low``) is included alongside the per-token
#: rates — the per-1k :data:`MODEL_PRICING` table cannot represent it.
PERPLEXITY_SEARCH_CONTEXT_SIZE: str = "low"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Per-call ledger fields that must be non-null for a row to be
#: considered well-formed. Kept in sync with the validation contract
#: (VAL-M2-052) and the feature description for f-m2-06. ``ts`` is the
#: feature's alias for the schema column ``called_at``.
REQUIRED_FIELDS: tuple[str, ...] = (
    "model_id",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms",
    "cost_usd",
    "called_at",
)

#: Default plausibility tolerance — 20 % around the published list
#: rates, matching the feature description for f-m2-06.
DEFAULT_TOLERANCE: float = 0.20


@dataclass(frozen=True)
class ProviderRates:
    """Published list pricing in USD per 1k tokens."""

    input_per_1k: float
    output_per_1k: float
    label: str = ""


#: Provider + model-prefix → published list pricing (USD per 1k tokens).
#: The ``model_id`` recorded in ``llm_cost_ledger`` is matched by
#: provider first, then by **longest matching prefix** in this dict.
#: Falling back to the provider's bare name (e.g. ``"grok"``) is
#: intentional — it gives a sensible default if the API echoes back a
#: surprise version suffix while keeping the per-version overrides
#: explicit.
#:
#: Numbers are taken from the per-client constants in
#: :mod:`biotech_sniper.llm.xai_client`,
#: :mod:`biotech_sniper.llm.claude_client` and
#: :mod:`biotech_sniper.llm.gemini_client` so we have a single mental
#: model of the source-of-truth.
MODEL_PRICING: dict[str, dict[str, ProviderRates]] = {
    "xai": {
        # $5 / 1M input, $15 / 1M output.
        "grok-4": ProviderRates(0.005, 0.015, label="grok-4"),
        "grok": ProviderRates(0.005, 0.015, label="grok"),
    },
    "anthropic": {
        # Claude Opus 4.x: $15 / 1M input, $75 / 1M output.
        "claude-opus": ProviderRates(0.015, 0.075, label="claude-opus"),
        "claude": ProviderRates(0.015, 0.075, label="claude"),
    },
    "gemini": {
        # Gemini 2.5 Pro: $1.25 / 1M input, $10 / 1M output.
        "gemini-2.5-pro": ProviderRates(0.00125, 0.010, label="gemini-2.5-pro"),
        "gemini": ProviderRates(0.00125, 0.010, label="gemini"),
    },
    # Perplexity sonar list pricing — kept in sync with the per-token
    # constants in :mod:`biotech_sniper.llm.perplexity_client`
    # (``INPUT_USD_PER_TOKEN`` / ``OUTPUT_USD_PER_TOKEN``).
    #
    # NOTE: sonar carries an additional flat per-call surcharge for
    # ``search_context_size='low'`` (``$5 / 1k requests``) which is
    # NOT representable in this per-1k-token table. The Stage-2
    # Reading-B path always sends search-context=low, so a row with
    # tiny token counts can legitimately sit above the per-token-only
    # estimate by up to ``$0.005``. Operators running
    # ``--check-plausibility`` should keep the default 20 % tolerance
    # (or pass ``--tolerance`` higher) — the entry exists so the
    # provider is recognised as first-class rather than falling
    # through to ``unknown-pricing``.
    "perplexity": {
        # sonar — $1 / 1M input, $1 / 1M output.
        "sonar-pro": ProviderRates(0.001, 0.001, label="sonar-pro"),
        "sonar": ProviderRates(0.001, 0.001, label="sonar"),
    },
}


# ---------------------------------------------------------------------------
# Required-field integrity
# ---------------------------------------------------------------------------


_NULL_CHECK_SQL = (
    "SELECT id, provider, model_id, prompt_tokens, completion_tokens, "
    "latency_ms, cost_usd, called_at "
    "FROM llm_cost_ledger "
    "WHERE model_id IS NULL "
    "   OR TRIM(model_id) = '' "
    "   OR prompt_tokens IS NULL "
    "   OR completion_tokens IS NULL "
    "   OR latency_ms IS NULL "
    "   OR cost_usd IS NULL "
    "   OR called_at IS NULL "
    "   OR TRIM(called_at) = '' "
    "ORDER BY id"
)


def find_rows_with_null_fields(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return ledger rows missing any of :data:`REQUIRED_FIELDS`.

    Empty list ⇒ every row satisfies the integrity contract.
    """

    return list(conn.execute(_NULL_CHECK_SQL))


# ---------------------------------------------------------------------------
# Daily totals
# ---------------------------------------------------------------------------


_DAILY_TOTALS_SQL = (
    "SELECT DATE(called_at) AS day, "
    "       provider, "
    "       COUNT(*) AS calls, "
    "       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
    "       COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
    "       COALESCE(SUM(latency_ms), 0) AS latency_ms, "
    "       ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd "
    "FROM llm_cost_ledger "
    "WHERE DATE(called_at) >= ? "
    "GROUP BY day, provider "
    "ORDER BY day DESC, provider"
)


def daily_totals(
    conn: sqlite3.Connection,
    since: str,
) -> list[dict[str, object]]:
    """Return per-day, per-provider sums since ``since`` (inclusive).

    Parameters
    ----------
    conn:
        An open SQLite connection. The caller owns lifecycle.
    since:
        Lower bound for the ``DATE(called_at) >= since`` filter,
        formatted ``YYYY-MM-DD``. Validated by
        :func:`_validate_iso_date` at the CLI layer; the API layer
        accepts any string SQLite would compare against an ISO date.

    Returns
    -------
    A list of dicts (one per ``(day, provider)`` group) with keys
    ``day``, ``provider``, ``calls``, ``prompt_tokens``,
    ``completion_tokens``, ``latency_ms``, ``cost_usd``. Empty list
    when the table is empty or no rows match.
    """

    rows = conn.execute(_DAILY_TOTALS_SQL, (since,)).fetchall()
    return [
        {
            "day": row["day"],
            "provider": row["provider"],
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "latency_ms": int(row["latency_ms"]),
            "cost_usd": float(row["cost_usd"]),
        }
        for row in rows
    ]


def iter_rows_since(
    conn: sqlite3.Connection,
    since: str,
) -> Iterable[sqlite3.Row]:
    """Iterate every ledger row whose ``called_at >= since``.

    Used by :func:`find_implausible_rows` and the M4 audit pipeline.
    """

    return conn.execute(
        "SELECT id, provider, model_id, prompt_tokens, completion_tokens, "
        "       latency_ms, cost_usd, called_at "
        "FROM llm_cost_ledger "
        "WHERE DATE(called_at) >= ? "
        "ORDER BY called_at ASC, id ASC",
        (since,),
    )


# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlausibilityResult:
    """One row's plausibility verdict."""

    row_id: int
    provider: str
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    expected_cost_usd: float
    deviation_pct: float
    ok: bool
    reason: str = ""


def lookup_pricing(provider: str, model_id: str) -> Optional[ProviderRates]:
    """Resolve list pricing for a provider + model.

    Matches by provider name first, then by the **longest** prefix
    inside ``MODEL_PRICING[provider]`` that the ``model_id`` starts
    with. Returns ``None`` when no entry matches (caller decides
    whether that's an error or a "skip plausibility" signal).
    """

    table = MODEL_PRICING.get(provider)
    if not table or not model_id:
        return None

    # Sort prefixes descending by length so e.g. ``grok-4`` wins over
    # the bare ``grok`` fallback.
    for prefix in sorted(table, key=len, reverse=True):
        if model_id.startswith(prefix):
            return table[prefix]
    return None


def expected_cost_usd(
    provider: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Optional[float]:
    """Return the published-rate cost for a single call.

    ``None`` when no pricing table entry is known for the
    ``(provider, model_id)`` pair — callers should skip the
    plausibility check rather than treat that as a failure (a brand-
    new model id is not a regression).
    """

    rates = lookup_pricing(provider, model_id)
    if rates is None:
        return None
    return (
        (prompt_tokens / 1000.0) * rates.input_per_1k
        + (completion_tokens / 1000.0) * rates.output_per_1k
    )


def plausibility_check(
    *,
    provider: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    tolerance: float = DEFAULT_TOLERANCE,
    row_id: int = 0,
) -> PlausibilityResult:
    """Verify the recorded cost matches published rates within tolerance.

    For zero-token rows (``prompt_tokens == 0 and completion_tokens
    == 0``) the only check is ``cost_usd >= 0`` — no rate-based
    expectation can be derived. For unknown ``(provider, model_id)``
    pairs the row is reported as ``ok=True`` with
    ``reason='unknown-pricing'`` so a forward-compatible model
    rollout doesn't blanket-fail today's audit.

    Perplexity-specific path
    ------------------------
    When ``provider == 'perplexity'`` the recompute reuses
    :func:`biotech_sniper.llm.perplexity_client.compute_cost_usd`
    instead of the per-1k :data:`MODEL_PRICING` table. That helper
    adds the flat ``$5 / 1k requests`` surcharge for
    ``search_context_size='low'`` (the Stage-2 Reading-B default,
    pinned in :data:`PERPLEXITY_SEARCH_CONTEXT_SIZE`) on top of the
    per-token rates. Without this branch, low-token perplexity rows
    where the surcharge dominates the per-token cost would trip the
    default 20 % tolerance even when the ledger value is correct
    (feature ``f-misc-11-perplexity-plausibility-surcharge``).
    """

    if provider == "perplexity":
        # Perplexity branch — surcharge-aware recompute. The branch
        # short-circuits the table-driven path entirely so a future
        # ``sonar-medium`` rename (not yet in MODEL_PRICING) still
        # gets a plausibility verdict instead of falling through to
        # ``unknown-pricing``.
        expected = _perplexity_compute_cost_usd(
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            search_context=PERPLEXITY_SEARCH_CONTEXT_SIZE,
        )
    else:
        expected = expected_cost_usd(
            provider, model_id, prompt_tokens, completion_tokens
        )

    if expected is None:
        # Unknown pricing — we can't evaluate, but we also won't fail.
        return PlausibilityResult(
            row_id=row_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cost_usd=float(cost_usd),
            expected_cost_usd=0.0,
            deviation_pct=0.0,
            ok=cost_usd >= 0,
            reason="unknown-pricing"
            if cost_usd >= 0
            else "negative-cost",
        )

    if prompt_tokens == 0 and completion_tokens == 0:
        # Zero-token edge case — only sanity-check non-negativity.
        return PlausibilityResult(
            row_id=row_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=float(cost_usd),
            expected_cost_usd=0.0,
            deviation_pct=0.0,
            ok=cost_usd >= 0,
            reason="zero-tokens" if cost_usd >= 0 else "negative-cost",
        )

    if cost_usd < 0:
        return PlausibilityResult(
            row_id=row_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cost_usd=float(cost_usd),
            expected_cost_usd=float(expected),
            deviation_pct=-1.0,
            ok=False,
            reason="negative-cost",
        )

    if expected <= 0:
        # Defensive — should be unreachable given the zero-token guard
        # above, but protects against pricing rows that get nuked to 0.
        return PlausibilityResult(
            row_id=row_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cost_usd=float(cost_usd),
            expected_cost_usd=float(expected),
            deviation_pct=0.0,
            ok=cost_usd == 0,
            reason="expected-zero",
        )

    deviation = (cost_usd - expected) / expected
    ok = abs(deviation) <= tolerance
    return PlausibilityResult(
        row_id=row_id,
        provider=provider,
        model_id=model_id,
        prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens),
        cost_usd=float(cost_usd),
        expected_cost_usd=float(expected),
        deviation_pct=float(deviation),
        ok=ok,
        reason="" if ok else "outside-tolerance",
    )


def find_implausible_rows(
    conn: sqlite3.Connection,
    *,
    since: str,
    tolerance: float = DEFAULT_TOLERANCE,
) -> list[PlausibilityResult]:
    """Scan rows since ``since`` and return only those failing the check."""

    failures: list[PlausibilityResult] = []
    for row in iter_rows_since(conn, since):
        result = plausibility_check(
            provider=row["provider"],
            model_id=row["model_id"] or "",
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            cost_usd=float(row["cost_usd"] or 0.0),
            tolerance=tolerance,
            row_id=int(row["id"]),
        )
        if not result.ok:
            failures.append(result)
    return failures


# ---------------------------------------------------------------------------
# Tabular rendering
# ---------------------------------------------------------------------------


_TABLE_HEADERS: tuple[str, ...] = (
    "date",
    "provider",
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
)


def format_table(rows: Sequence[Mapping[str, object]]) -> str:
    """Render :func:`daily_totals` output as a fixed-width table.

    The output always contains the header row even when ``rows`` is
    empty (so the CLI prints a non-empty, human-readable response on
    a fresh database). A trailing ``TOTAL`` row sums ``calls``,
    ``prompt_tokens``, ``completion_tokens`` and ``cost_usd``.
    """

    body: list[list[str]] = []
    total_calls = 0
    total_prompt = 0
    total_completion = 0
    total_cost = 0.0

    for row in rows:
        calls = int(row.get("calls", 0))
        prompt = int(row.get("prompt_tokens", 0))
        completion = int(row.get("completion_tokens", 0))
        cost = float(row.get("cost_usd", 0.0))
        total_calls += calls
        total_prompt += prompt
        total_completion += completion
        total_cost += cost
        body.append([
            str(row.get("day", "")),
            str(row.get("provider", "")),
            str(calls),
            str(prompt),
            str(completion),
            f"{cost:.4f}",
        ])

    if body:
        body.append([
            "TOTAL",
            "-",
            str(total_calls),
            str(total_prompt),
            str(total_completion),
            f"{total_cost:.4f}",
        ])

    columns = list(_TABLE_HEADERS)
    table_rows: list[list[str]] = [list(columns)] + body
    widths = [max(len(r[i]) for r in table_rows) for i in range(len(columns))]
    lines = []
    for r in table_rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r))
        )
    # Insert a separator line under the header for readability.
    sep = "  ".join("-" * w for w in widths)
    lines.insert(1, sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _validate_iso_date(value: str) -> str:
    """Accept ``YYYY-MM-DD`` and return it; raise ``ArgumentTypeError`` else."""

    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be ISO format YYYY-MM-DD; got {value!r}"
        ) from exc
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.llm.cost_report",
        description=(
            "Print per-provider, per-day totals from the llm_cost_ledger "
            "SQLite table. Optional flags surface required-field "
            "integrity issues and cost-plausibility regressions."
        ),
    )
    parser.add_argument(
        "--since",
        required=True,
        type=_validate_iso_date,
        help="Lower bound (YYYY-MM-DD) on DATE(called_at). Required.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DATA_DIR / "alpha_sniper.db",
        help="SQLite database path. Defaults to DATA_DIR/alpha_sniper.db.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="Plausibility tolerance (fraction of expected cost). "
        f"Default {DEFAULT_TOLERANCE}.",
    )
    parser.add_argument(
        "--check-plausibility",
        action="store_true",
        help="Also flag rows whose cost_usd deviates from published "
        "rates by more than --tolerance. Exits non-zero on findings.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any row has a null required field.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON document instead of a fixed-width table.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    db_path: Path = args.db.expanduser()
    if not db_path.exists():
        print(
            f"ERROR: SQLite db not found at {db_path}. Run the M2 migration "
            f"first.",
            file=sys.stderr,
        )
        return 2

    conn = db.connect(db_path)
    try:
        # Don't run migrations here — this is a read-only tool. If the
        # schema is out of date, surface that instead of silently
        # creating tables.
        version = db.current_schema_version(conn)
        if version < 1:
            print(
                "ERROR: llm_cost_ledger table is missing. Run the migration "
                "(python -m biotech_sniper.migrations.migrate_json_to_sqlite) "
                "before invoking cost_report.",
                file=sys.stderr,
            )
            return 3

        totals = daily_totals(conn, args.since)
        null_rows = find_rows_with_null_fields(conn)
        implausible: list[PlausibilityResult] = []
        if args.check_plausibility:
            implausible = find_implausible_rows(
                conn, since=args.since, tolerance=args.tolerance
            )
    finally:
        conn.close()

    if args.json:
        payload = {
            "since": args.since,
            "tolerance": args.tolerance,
            "totals": totals,
            "null_field_violations": [
                {k: row[k] for k in row.keys()} for row in null_rows
            ],
            "implausible_rows": [
                {
                    "row_id": r.row_id,
                    "provider": r.provider,
                    "model_id": r.model_id,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "cost_usd": r.cost_usd,
                    "expected_cost_usd": r.expected_cost_usd,
                    "deviation_pct": r.deviation_pct,
                    "reason": r.reason,
                }
                for r in implausible
            ],
        }
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(f"# llm_cost_ledger totals since {args.since} (db={db_path})")
        print(format_table(totals))
        if not totals:
            print("(no rows)")
        if null_rows:
            print()
            print(f"# {len(null_rows)} row(s) with null required fields:")
            for row in null_rows:
                print(
                    f"  id={row['id']} provider={row['provider']} "
                    f"model_id={row['model_id']!r} "
                    f"prompt_tokens={row['prompt_tokens']} "
                    f"completion_tokens={row['completion_tokens']} "
                    f"latency_ms={row['latency_ms']} "
                    f"cost_usd={row['cost_usd']} "
                    f"called_at={row['called_at']!r}"
                )
        if args.check_plausibility:
            print()
            if implausible:
                print(
                    f"# {len(implausible)} implausible row(s) "
                    f"(tolerance={args.tolerance:.0%}):"
                )
                for r in implausible:
                    print(
                        f"  id={r.row_id} provider={r.provider} "
                        f"model={r.model_id} "
                        f"cost_usd={r.cost_usd:.6f} "
                        f"expected={r.expected_cost_usd:.6f} "
                        f"deviation={r.deviation_pct:+.2%} "
                        f"reason={r.reason}"
                    )
            else:
                print("# plausibility: all rows within tolerance.")

    exit_code = 0
    if args.strict and null_rows:
        exit_code = 4
    if args.check_plausibility and implausible:
        exit_code = 5 if exit_code == 0 else exit_code
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
