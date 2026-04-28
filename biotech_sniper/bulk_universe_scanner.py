"""Two-tier universe builder (M2 universe expansion).

This module implements :func:`build_universe`, the entry point that
populates the SQLite ``universe`` table with the watch + tradeable
tiers per VAL-M2-071 and the f-m2-09 feature spec.

Universe construction
---------------------
``build_universe()`` materialises the watch tier from two sources:

1. **SECTORS seed** — the ~421 image-derived tickers in
   :data:`biotech_sniper.state.full_biotech_universe_raw.SECTORS`
   (preserved at ``biotech_sniper/state/`` by f-m1-03; only the
   ``*.json`` state files moved into ``migrations/seed/``).
   Source label: ``'sectors_seed'``.

2. **Auto-discovered NCT-sponsor tickers** — the historical CT.gov
   delta-scan output curated by ``intelligence.master_discovery``.
   The persisted snapshot lives at
   ``migrations/seed/master_ticker_list.json`` (657 unique tickers).
   In production this list is refreshed by the existing
   master_discovery cron; the seed file is the deterministic
   fallback used during the M2 build (and during tests). Source
   label: ``'ct_gov_discovery'``.

After the watch rows are written, the options-chain probe at
:mod:`biotech_sniper.options_chain_probe` is applied to every row.
The seed-backed probe answers ``True`` for the historical 147
options-validated tickers (sourced from
``migrations/seed/universe_stats.json``'s ``tickers_with_options``
list) and ``False`` for everything else. Rows whose probe returns
``True`` have ``has_options_chain=1`` and ``tier='tradeable'``;
the rest stay ``tier='watch'``.

The build is idempotent — re-running on the same inputs upserts each
row in place rather than appending. The ``ticker`` PRIMARY KEY on
the ``universe`` table is the SQL-level guard.

CLI
---

    python -m biotech_sniper.bulk_universe_scanner --build

Prints a JSON summary of the build (counts, source breakdown, db
path). Designed for cron consumption — exits non-zero on hard
failures.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from biotech_sniper import config as _config
from biotech_sniper import db
from biotech_sniper.options_chain_probe import (
    AlpacaBackedProbe,
    OptionsChainProbe,
    SeedBackedProbe,
)
from biotech_sniper.paths import BASE_DIR, DATA_DIR

__all__ = [
    "BuildResult",
    "build_universe",
    "default_db_path",
    "default_probe",
    "load_sectors_seed_tickers",
    "load_ct_gov_discovery_tickers",
    "main",
    "refresh_universe_chains",
    "WATCH_SOURCE_SECTORS",
    "WATCH_SOURCE_CT_GOV",
]


logger = logging.getLogger(__name__)


# Source labels persisted on each row.
WATCH_SOURCE_SECTORS = "sectors_seed"
WATCH_SOURCE_CT_GOV = "ct_gov_discovery"


# Path to the persisted CT.gov-derived ticker list. The file is owned
# by master_discovery; we only consume it here.
_CT_GOV_SEED_PATH = BASE_DIR / "migrations" / "seed" / "master_ticker_list.json"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    """Structured summary of one ``build_universe()`` run."""

    watch_count: int = 0
    tradeable_count: int = 0
    sectors_seed_added: int = 0
    ct_gov_added: int = 0
    duplicates_skipped: int = 0
    probe_calls: int = 0
    db_path: str = ""
    completed_at: str = ""
    sources: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


# ---------------------------------------------------------------------------
# Seed loaders
# ---------------------------------------------------------------------------


def _normalise_ticker(raw: object) -> str | None:
    """Best-effort ticker normalisation; returns ``None`` for junk input."""
    if not isinstance(raw, str):
        return None
    t = raw.strip().upper()
    if not t or len(t) > 6:
        return None
    # Ticker characters: letters, digits, hyphen, period (for class shares).
    cleaned = t.replace("-", "").replace(".", "")
    if not cleaned.isalnum():
        return None
    return t


def load_sectors_seed_tickers() -> set[str]:
    """Return the deduped SECTORS seed tickers.

    Imports the SECTORS dict from
    :mod:`biotech_sniper.state.full_biotech_universe_raw` and
    flattens both the ``featured`` (tuples) and ``grid`` (strings)
    sub-lists. Tickers are upper-cased and validated. Junk strings
    such as empty entries or names with spaces are skipped.
    """

    # Local import keeps module import time low and avoids the seed
    # module's banner ``print`` running unless we actually need the
    # SECTORS dict (e.g. tests that mock the seed loader).
    from biotech_sniper.state.full_biotech_universe_raw import SECTORS

    out: set[str] = set()
    for sector_data in SECTORS.values():
        for entry in sector_data.get("featured", []):
            if isinstance(entry, (list, tuple)) and entry:
                t = _normalise_ticker(entry[0])
                if t:
                    out.add(t)
        for entry in sector_data.get("grid", []):
            t = _normalise_ticker(entry)
            if t:
                out.add(t)
    return out


def load_ct_gov_discovery_tickers(
    seed_path: Path | str = _CT_GOV_SEED_PATH,
) -> set[str]:
    """Return the auto-discovered NCT-sponsor ticker set.

    Reads ``all_tickers`` from ``migrations/seed/master_ticker_list.json``
    — the persisted output of ``intelligence.master_discovery``'s daily
    CT.gov delta-scan. In production the file is refreshed by the
    cron; here we read it as a deterministic seed.

    A missing file returns an empty set so unit tests can run without
    the seed JSON present.
    """

    path = Path(seed_path)
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not parse CT.gov seed at %s", path)
        return set()

    if not isinstance(payload, dict):
        return set()

    raw = payload.get("all_tickers")
    if not isinstance(raw, list):
        return set()

    out: set[str] = set()
    for entry in raw:
        t = _normalise_ticker(entry)
        if t:
            out.add(t)
    return out


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical SQLite path for the project."""
    return DATA_DIR / "alpha_sniper.db"


def default_probe() -> OptionsChainProbe:
    """Return the project-default options-chain probe.

    Prefers :class:`AlpacaBackedProbe` whenever the Alpaca paper-trading
    credentials are configured (per :func:`config.provider_enabled`-style
    lookups). Falls back to :class:`SeedBackedProbe` so smoke imports
    on a host without ALPACA_KEY_ID / ALPACA_SECRET_KEY still build a
    usable universe (every ticker stays at ``tier='watch'`` because
    the seed JSON only flips the historical 147 tickers tradeable).

    The factory is consulted by :func:`build_universe` and by
    :func:`main` (the ``--build`` CLI). Tests bypass the factory by
    passing ``probe=...`` directly so they remain hermetic.
    """
    key = _config.get_alpaca_key_id()
    secret = _config.get_alpaca_secret_key()
    if key and secret:
        try:
            return AlpacaBackedProbe()
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "default_probe: failed to construct AlpacaBackedProbe "
                "(%s); falling back to SeedBackedProbe",
                exc.__class__.__name__,
            )
    return SeedBackedProbe()


# ---------------------------------------------------------------------------
# Core build routine
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _upsert_universe_row(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    tier: str,
    has_options_chain: bool,
    last_chain_check_at: str,
    source: str,
) -> bool:
    """Insert or update one ``universe`` row.

    Returns ``True`` when a new row was inserted (i.e. the ticker did
    not exist), ``False`` when an existing row was updated. The
    boolean lets the caller distinguish "added by this build" rows
    from "merged duplicates" rows so the :class:`BuildResult` summary
    is accurate even on a re-run.
    """

    flag = 1 if has_options_chain else 0
    existing = conn.execute(
        "SELECT 1 FROM universe WHERE ticker = ?", (ticker,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO universe (ticker, tier, has_options_chain, "
            "last_chain_check_at, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, tier, flag, last_chain_check_at, source, _now_iso()),
        )
        return True
    conn.execute(
        "UPDATE universe SET tier = ?, has_options_chain = ?, "
        "last_chain_check_at = ?, source = ?, updated_at = ? "
        "WHERE ticker = ?",
        (tier, flag, last_chain_check_at, source, _now_iso(), ticker),
    )
    return False


def build_universe(
    *,
    db_path: Path | str | None = None,
    sectors_tickers: Iterable[str] | None = None,
    ct_gov_tickers: Iterable[str] | None = None,
    probe: OptionsChainProbe | None = None,
) -> BuildResult:
    """Populate the ``universe`` table with the two-tier universe.

    Parameters
    ----------
    db_path:
        Where to write. ``None`` resolves to :func:`default_db_path`.
    sectors_tickers:
        Override the SECTORS seed (defaults to
        :func:`load_sectors_seed_tickers`). Tests pass a small set
        here.
    ct_gov_tickers:
        Override the CT.gov auto-discovered set (defaults to
        :func:`load_ct_gov_discovery_tickers`). Tests pass a small
        set here.
    probe:
        :class:`OptionsChainProbe` used to decide
        ``has_options_chain`` per ticker. Defaults to a fresh
        :class:`SeedBackedProbe` reading from
        ``migrations/seed/universe_stats.json``.

    Returns
    -------
    BuildResult
        Structured summary suitable for cron logging.

    Behaviour
    ---------
    * Every ticker from ``sectors_tickers`` is inserted/updated with
      ``source='sectors_seed'``.
    * Every ticker from ``ct_gov_tickers`` not already present is
      inserted with ``source='ct_gov_discovery'``. Duplicates with
      the SECTORS seed are skipped (the SECTORS row wins on first
      insert).
    * After the watch rows are written, the probe runs against every
      ticker. Rows where ``probe(ticker)`` returns ``True`` are set
      to ``tier='tradeable'`` with ``has_options_chain=1``;
      everything else remains ``tier='watch'`` with
      ``has_options_chain=0``.
    * Re-running the build is idempotent: row counts after the second
      call match the first.
    """

    target = Path(db_path) if db_path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if sectors_tickers is None:
        sectors_set = load_sectors_seed_tickers()
    else:
        sectors_set = {
            t for t in (_normalise_ticker(x) for x in sectors_tickers) if t
        }

    if ct_gov_tickers is None:
        ct_gov_set = load_ct_gov_discovery_tickers()
    else:
        ct_gov_set = {
            t for t in (_normalise_ticker(x) for x in ct_gov_tickers) if t
        }

    # Dedup CT.gov against SECTORS — SECTORS wins.
    ct_gov_only = ct_gov_set - sectors_set

    if probe is None:
        # Default to the seed-backed probe so smoke imports and the
        # M2 universe-builder tests stay deterministic. The Alpaca-
        # backed probe is opt-in (see :func:`refresh_universe_chains`
        # and the ``--alpaca-probe`` CLI flag).
        probe = SeedBackedProbe()

    result = BuildResult(db_path=str(target))
    now = _now_iso()

    conn = db.connect(target)
    try:
        db.run_migrations(conn)
        with conn:
            # Pass 1: seed both sources at tier='watch'.
            for ticker in sorted(sectors_set):
                inserted = _upsert_universe_row(
                    conn,
                    ticker=ticker,
                    tier="watch",
                    has_options_chain=False,
                    last_chain_check_at=now,
                    source=WATCH_SOURCE_SECTORS,
                )
                if inserted:
                    result.sectors_seed_added += 1
                else:
                    result.duplicates_skipped += 1

            for ticker in sorted(ct_gov_only):
                inserted = _upsert_universe_row(
                    conn,
                    ticker=ticker,
                    tier="watch",
                    has_options_chain=False,
                    last_chain_check_at=now,
                    source=WATCH_SOURCE_CT_GOV,
                )
                if inserted:
                    result.ct_gov_added += 1
                else:
                    result.duplicates_skipped += 1

            # Pass 2: apply the probe and promote tradeables.
            all_rows = conn.execute(
                "SELECT ticker FROM universe"
            ).fetchall()
            for row in all_rows:
                ticker = row["ticker"]
                has_chain = bool(probe.probe(ticker))
                result.probe_calls += 1
                tier = "tradeable" if has_chain else "watch"
                conn.execute(
                    "UPDATE universe SET tier = ?, has_options_chain = ?, "
                    "last_chain_check_at = ?, updated_at = ? "
                    "WHERE ticker = ?",
                    (
                        tier,
                        1 if has_chain else 0,
                        now,
                        _now_iso(),
                        ticker,
                    ),
                )

            # Compute final tier counts inside the transaction so the
            # summary reflects exactly what was committed.
            result.watch_count = conn.execute(
                "SELECT COUNT(*) FROM universe WHERE tier = 'watch'"
            ).fetchone()[0]
            result.tradeable_count = conn.execute(
                "SELECT COUNT(*) FROM universe WHERE tier = 'tradeable'"
            ).fetchone()[0]
            source_rows = conn.execute(
                "SELECT source, COUNT(*) AS c FROM universe GROUP BY source"
            ).fetchall()
            result.sources = {row["source"]: row["c"] for row in source_rows}
    finally:
        conn.close()

    result.completed_at = _now_iso()
    return result


# ---------------------------------------------------------------------------
# Daily / intraday refresh — re-probe every universe row's chain status.
# ---------------------------------------------------------------------------


@dataclass
class RefreshResult:
    """Structured summary of one ``refresh_universe_chains()`` run."""

    rows_checked: int = 0
    flipped_to_tradeable: int = 0
    flipped_to_watch: int = 0
    has_options_chain_count: int = 0
    db_path: str = ""
    completed_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def refresh_universe_chains(
    *,
    db_path: Path | str | None = None,
    probe: OptionsChainProbe | None = None,
) -> RefreshResult:
    """Re-probe every universe row and update ``has_options_chain``.

    Wires the daily/intraday refresh path required by f-m3-08:
    every existing ``universe`` row is re-checked through the
    supplied probe (defaults to :func:`default_probe`, which prefers
    :class:`AlpacaBackedProbe` when ALPACA credentials are configured)
    and its ``has_options_chain`` + ``last_chain_check_at`` columns
    are updated. Tier transitions are honoured: a ticker that loses
    its chain drops to ``tier='watch'``, a ticker that gains one is
    promoted to ``tier='tradeable'``.

    Idempotent — re-running with the same probe leaves row counts
    unchanged (only ``last_chain_check_at`` advances).

    Parameters
    ----------
    db_path:
        Override the SQLite db path. ``None`` resolves to
        :func:`default_db_path`.
    probe:
        :class:`OptionsChainProbe` to consult. ``None`` resolves to
        :func:`default_probe`.

    Returns
    -------
    RefreshResult
        Structured summary of the refresh run.
    """
    target = Path(db_path) if db_path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if probe is None:
        probe = default_probe()

    result = RefreshResult(db_path=str(target))
    now = _now_iso()

    conn = db.connect(target)
    try:
        db.run_migrations(conn)
        with conn:
            rows = conn.execute(
                "SELECT ticker, tier, has_options_chain FROM universe"
            ).fetchall()
            for row in rows:
                ticker = row["ticker"]
                prior_has_chain = bool(row["has_options_chain"])
                try:
                    has_chain = bool(probe.probe(ticker))
                except Exception as exc:  # noqa: BLE001 - defensive
                    logger.warning(
                        "refresh_universe_chains: probe(%s) raised %s; "
                        "treating as no-chain",
                        ticker,
                        exc.__class__.__name__,
                    )
                    has_chain = False

                result.rows_checked += 1
                if has_chain and not prior_has_chain:
                    result.flipped_to_tradeable += 1
                elif not has_chain and prior_has_chain:
                    result.flipped_to_watch += 1

                tier = "tradeable" if has_chain else "watch"
                conn.execute(
                    "UPDATE universe SET tier = ?, has_options_chain = ?, "
                    "last_chain_check_at = ?, updated_at = ? "
                    "WHERE ticker = ?",
                    (
                        tier,
                        1 if has_chain else 0,
                        now,
                        _now_iso(),
                        ticker,
                    ),
                )

            result.has_options_chain_count = conn.execute(
                "SELECT COUNT(*) FROM universe WHERE has_options_chain = 1"
            ).fetchone()[0]
    finally:
        conn.close()

    result.completed_at = _now_iso()
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.bulk_universe_scanner",
        description=(
            "Build the two-tier biotech universe (watch + tradeable) "
            "into the project SQLite db."
        ),
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run the full universe build and write to SQLite.",
    )
    parser.add_argument(
        "--refresh-chains",
        action="store_true",
        help=(
            "Re-probe every existing universe row's options-chain "
            "status (uses the Alpaca-backed probe when ALPACA "
            "credentials are configured)."
        ),
    )
    parser.add_argument(
        "--alpaca-probe",
        action="store_true",
        help=(
            "Use the Alpaca-backed probe for the build / refresh "
            "(otherwise falls back to the seed-backed probe)."
        ),
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=(
            "Override the target SQLite database path "
            "(default: data/alpha_sniper.db)."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not (args.build or args.refresh_chains):
        parser.error("specify --build or --refresh-chains")
        return 2  # pragma: no cover - argparse exits via SystemExit

    db_path = Path(args.db) if args.db else default_db_path()
    probe: OptionsChainProbe | None = (
        AlpacaBackedProbe() if args.alpaca_probe else None
    )

    if args.refresh_chains:
        refresh_result = refresh_universe_chains(db_path=db_path, probe=probe)
        sys.stdout.write(refresh_result.to_json() + "\n")
        return 0

    build_result = build_universe(db_path=db_path, probe=probe)
    sys.stdout.write(build_result.to_json() + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
