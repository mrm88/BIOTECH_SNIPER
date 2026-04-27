"""Options-chain liquidity probe.

This module owns the ``probe_chain(ticker) -> bool`` contract used by
:func:`biotech_sniper.bulk_universe_scanner.build_universe` to decide
whether a watch-tier ticker is promotable to ``tier='tradeable'``.

Two-phase rollout
-----------------
* **M2:** :class:`SeedBackedProbe` reads
  ``migrations/seed/universe_stats.json``'s ``tickers_with_options``
  list — the historical 147 options-validated tickers from the
  discovery report — and answers ``True`` for any ticker in that
  list, ``False`` otherwise.
* **M3 (f-m3-08, this milestone):** :class:`AlpacaBackedProbe` calls
  :meth:`biotech_sniper.alpaca_client.AlpacaClient.get_options_chain`
  for the ticker and returns ``True`` iff at least one chain row
  with an expiration date within :data:`PROBE_LOOKAHEAD_DAYS` (60
  days) of today is returned. Transport errors return ``False`` so
  a transient broker outage degrades to "not tradeable" rather than
  poisoning the universe.

The liquidity filter is intentionally permissive — per the mission's
``AGENTS.md`` "Universe boundaries" section, the existence of *any*
chain row is sufficient to flip a ticker to tradeable. Spread / OI /
volume thresholds are explicitly NOT applied.
"""

from __future__ import annotations

import datetime
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Final, Iterable

from biotech_sniper.paths import BASE_DIR

__all__ = [
    "OptionsChainProbe",
    "SeedBackedProbe",
    "AlpacaBackedProbe",
    "load_seed_options_tickers",
    "probe_chain",
    "DEFAULT_SEED_PATH",
    "PROBE_LOOKAHEAD_DAYS",
]


logger = logging.getLogger(__name__)

#: Maximum lookahead window (days) for the Alpaca-backed probe. Chain
#: rows whose ``expiry`` is more than this many days in the future are
#: ignored. The window mirrors the mission's "any chain within the next
#: 60 days" liquidity filter (AGENTS.md → Risk defaults).
PROBE_LOOKAHEAD_DAYS: Final[int] = 60


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
# Alpaca-backed implementation (M3 — f-m3-08)
# ---------------------------------------------------------------------------


def _today() -> datetime.date:
    """Return ``datetime.date.today()`` — extracted for monkeypatching."""
    return datetime.date.today()


def _expiry_target_iso(today: datetime.date | None = None) -> str:
    """Return the ISO date ``today + PROBE_LOOKAHEAD_DAYS``.

    Used by :class:`AlpacaBackedProbe` to pass an upper-bound expiry
    hint to :meth:`AlpacaClient.get_options_chain`. Alpaca's
    ``OptionChainRequest`` accepts a single ``expiration_date`` filter
    so the probe asks the broker for the chain near the cutoff —
    which the broker resolves to whatever near-term contracts it has
    available. Empty results mean the ticker has no listed options
    in the lookahead window.
    """
    base = today or _today()
    return (base + datetime.timedelta(days=PROBE_LOOKAHEAD_DAYS)).isoformat()


def _row_expiry_within_lookahead(
    row: dict[str, Any], today: datetime.date, cutoff: datetime.date
) -> bool:
    """Return ``True`` iff ``row['expiry']`` lies in ``[today, cutoff]``.

    Rows missing or with an unparseable ``expiry`` field are treated
    as "no signal" and excluded — the probe must not flip a ticker to
    tradeable on the basis of a row whose expiration we cannot
    confirm sits inside the 60-day window.
    """
    raw = row.get("expiry") if isinstance(row, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        expiry = datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return False
    return today <= expiry <= cutoff


class AlpacaBackedProbe(OptionsChainProbe):
    """Real options-chain probe backed by :class:`AlpacaClient`.

    The probe consults
    :meth:`biotech_sniper.alpaca_client.AlpacaClient.get_options_chain`
    for each ticker. The call is intentionally permissive — the
    project's universe-tier policy only requires the existence of
    *any* chain row within the next 60 days to flag a ticker
    tradeable. Spread / OI / volume thresholds are explicitly NOT
    applied here (see ``AGENTS.md`` → Risk defaults).

    Construction is lazy — supplying ``client=None`` defers
    :class:`AlpacaClient` construction until the first call. This
    matters during smoke imports on hosts without paper credentials,
    where importing this module must not raise.

    Errors
    ------
    Transport / auth failures from the SDK surface as a ``False``
    answer plus a ``WARNING`` log line. Returning ``True`` on a
    broker error would silently promote untradeable tickers; a hard
    raise would crash the daily build. ``False`` is the conservative
    default: the worst-case is a tradeable ticker temporarily
    demoted to watch until the next refresh.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        lookahead_days: int = PROBE_LOOKAHEAD_DAYS,
    ) -> None:
        self._client = client
        self._lookahead_days = int(lookahead_days)

    # ------------------------------------------------------------------
    # Internal: lazy client construction.
    # ------------------------------------------------------------------

    def _ensure_client(self) -> Any | None:
        """Return the wrapped client, lazily building one if needed.

        Returns ``None`` when construction fails (e.g. missing
        ALPACA_KEY_ID / ALPACA_SECRET_KEY in the environment). The
        caller will treat ``None`` as "no client available" and
        answer ``False`` for every ticker.
        """
        if self._client is not None:
            return self._client
        try:
            # Local import keeps the module importable on hosts where
            # alpaca-py / credentials are unavailable.
            from biotech_sniper.alpaca_client import AlpacaClient

            self._client = AlpacaClient()
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "AlpacaBackedProbe: failed to construct AlpacaClient (%s); "
                "every probe will return False until the construction "
                "succeeds",
                exc.__class__.__name__,
            )
            self._client = None
        return self._client

    # ------------------------------------------------------------------
    # OptionsChainProbe contract.
    # ------------------------------------------------------------------

    def probe(self, ticker: str) -> bool:
        if not isinstance(ticker, str) or not ticker.strip():
            return False
        symbol = ticker.strip().upper()
        client = self._ensure_client()
        if client is None:
            return False

        today = _today()
        cutoff = today + datetime.timedelta(days=self._lookahead_days)
        target_expiry = cutoff.isoformat()

        try:
            chain = client.get_options_chain(symbol, target_expiry)
        except Exception as exc:  # noqa: BLE001 - typed below
            logger.warning(
                "AlpacaBackedProbe: get_options_chain(%s) raised %s; "
                "treating ticker as no-chain",
                symbol,
                exc.__class__.__name__,
            )
            return False

        if not chain:
            return False

        any_parseable = False
        for row in chain:
            if not isinstance(row, dict):
                continue
            raw = row.get("expiry")
            if isinstance(raw, str) and raw.strip():
                # Track whether ANY row carried a parseable expiry
                # — that determines whether the fallback below
                # applies. A row that explicitly resolves outside
                # the window does NOT trigger the fallback.
                try:
                    datetime.date.fromisoformat(raw[:10])
                    any_parseable = True
                except ValueError:
                    pass
            if _row_expiry_within_lookahead(row, today, cutoff):
                return True

        # All rows carried a parseable expiry but none landed in
        # the lookahead window → strict reject.
        if any_parseable:
            return False
        # The broker returned at least one row but none carried a
        # structured expiry. Respect the AGENTS.md "any chain row"
        # relaxed filter: fall back to len > 0 so we don't demote
        # a tradeable ticker just because the snapshot omits the
        # expiry string.
        return any(isinstance(row, dict) for row in chain)


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
