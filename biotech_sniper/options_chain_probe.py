"""Options-chain liquidity probe.

This module owns the ``probe_chain(ticker) -> bool`` contract used by
:func:`biotech_sniper.bulk_universe_scanner.build_universe` to decide
whether a watch-tier ticker is promotable to ``tier='tradeable'``.

Two-phase rollout
-----------------
* **M2 (this milestone):** :class:`SeedBackedProbe` reads
  ``migrations/seed/universe_stats.json``'s ``tickers_with_options``
  list — the historical 147 options-validated tickers from the
  discovery report — and answers ``True`` for any ticker in that
  list, ``False`` otherwise.
* **M3 (f-m3-08):** :class:`AlpacaBackedProbe` will replace the seed
  implementation by hitting the Alpaca options-chain endpoint. The
  abstract base class :class:`OptionsChainProbe` and the module-level
  :func:`probe_chain` entry point keep call-sites stable across the
  swap.

The liquidity filter is intentionally permissive — per the mission's
``AGENTS.md`` "Universe boundaries" section, the existence of *any*
chain row is sufficient to flip a ticker to tradeable. Spread / OI /
volume thresholds are explicitly NOT applied.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Final, Iterable

from biotech_sniper.paths import BASE_DIR

__all__ = [
    "OptionsChainProbe",
    "SeedBackedProbe",
    "load_seed_options_tickers",
    "probe_chain",
    "DEFAULT_SEED_PATH",
]


# Path to the seed-data JSON shipped by f-m1-03. The file ships with
# both aggregate counts (``has_options``, ``passed_filters`` etc.) and
# — for f-m2-09 — an explicit ``tickers_with_options`` list of 147
# tickers seeded from the historical discovery report. That list is
# the source of truth for the M2 seed-backed probe.
DEFAULT_SEED_PATH: Final[Path] = (
    BASE_DIR / "migrations" / "seed" / "universe_stats.json"
)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class OptionsChainProbe(ABC):
    """Abstract probe interface.

    Concrete implementations decide whether a given ticker has any
    tradeable options chain. The decision must be cheap (the probe is
    called once per watch-pool ticker on every ``build_universe`` run)
    and deterministic so that re-running the build does not flap the
    ``tier`` between watch and tradeable.
    """

    @abstractmethod
    def probe(self, ticker: str) -> bool:
        """Return ``True`` if ``ticker`` has any options chain."""


# ---------------------------------------------------------------------------
# Seed-backed implementation (M2)
# ---------------------------------------------------------------------------


def load_seed_options_tickers(
    seed_path: Path | str = DEFAULT_SEED_PATH,
) -> set[str]:
    """Return the set of options-validated tickers from the seed JSON.

    The seed file at ``migrations/seed/universe_stats.json`` is
    expected to contain a ``tickers_with_options`` list. When the file
    is missing OR the field is absent (legacy snapshots), an empty
    set is returned — the probe will then answer ``False`` for every
    ticker, which is the documented "stub everything else as FALSE"
    behaviour for the M2 seed implementation.

    Tickers are uppercased and stripped to make the lookup
    case-insensitive against caller-supplied symbols.
    """

    path = Path(seed_path)
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    raw = payload.get("tickers_with_options") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().upper() for t in raw if isinstance(t, str) and t.strip()}


class SeedBackedProbe(OptionsChainProbe):
    """``probe_chain`` implementation backed by the seed JSON.

    The probe pre-loads the options-validated ticker list from
    ``migrations/seed/universe_stats.json`` once at construction
    time. Subsequent :meth:`probe` calls do an O(1) set membership
    check.

    Use ``seed_path`` to inject a different JSON file in tests.
    """

    def __init__(self, seed_path: Path | str = DEFAULT_SEED_PATH) -> None:
        self._seed_path = Path(seed_path)
        self._tickers: set[str] = load_seed_options_tickers(seed_path)

    @property
    def seed_path(self) -> Path:
        """Return the seed file path used by this probe (for diagnostics)."""
        return self._seed_path

    @property
    def known_tickers(self) -> frozenset[str]:
        """Immutable view of the set of options-validated seed tickers."""
        return frozenset(self._tickers)

    def probe(self, ticker: str) -> bool:
        """Return ``True`` iff ``ticker`` is in the seed options list."""
        if not isinstance(ticker, str):
            return False
        return ticker.strip().upper() in self._tickers


# ---------------------------------------------------------------------------
# Default singleton + module-level entry point
# ---------------------------------------------------------------------------


_DEFAULT_PROBE: SeedBackedProbe | None = None


def _default_probe() -> SeedBackedProbe:
    """Return the lazily-constructed module-level probe singleton."""
    global _DEFAULT_PROBE
    if _DEFAULT_PROBE is None:
        _DEFAULT_PROBE = SeedBackedProbe()
    return _DEFAULT_PROBE


def probe_chain(ticker: str) -> bool:
    """Return ``True`` if ``ticker`` has any options chain.

    Module-level convenience wrapper around the default
    :class:`SeedBackedProbe` singleton. Callers that need to override
    the probe (tests, future Alpaca-backed implementation in f-m3-08)
    should construct their own ``SeedBackedProbe`` /
    ``AlpacaBackedProbe`` and call ``probe(ticker)`` directly, OR
    pass an injected probe to ``build_universe(probe=...)``.
    """
    return _default_probe().probe(ticker)


def reset_default_probe() -> None:
    """Reset the module-level probe (used by tests after seed mutation)."""
    global _DEFAULT_PROBE
    _DEFAULT_PROBE = None


def iter_seed_tickers(probe: OptionsChainProbe | None = None) -> Iterable[str]:
    """Yield the options-validated seed tickers known to ``probe``.

    Convenience iterator for diagnostics / audit reporting. When
    ``probe`` is ``None`` the module-level singleton is used. The
    caller receives an empty iterator if the active probe is not
    seed-backed.
    """
    probe = probe or _default_probe()
    if isinstance(probe, SeedBackedProbe):
        yield from sorted(probe.known_tickers)
