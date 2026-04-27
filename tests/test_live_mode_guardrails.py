"""Tests for the LIVE_MODE two-gate guardrail (feature f-m3-06).

Re-establishes the contract that ``AlpacaClient`` only constructs
against the live Alpaca URL (``https://api.alpaca.markets``) when
**both** of the following are true at the same time:

1. The environment variable ``LIVE_MODE`` is truthy
   (the boolean is loaded by :mod:`biotech_sniper.config` and
   exposed as ``config.LIVE_MODE``).
2. A confirmation marker file
   ``i-understand-this-trades-real-money`` exists at
   :data:`biotech_sniper.paths.BASE_DIR` (i.e. under
   ``BIOTECH_SNIPER_HOME`` when set, otherwise the repo root).

If **either** gate is closed, construction MUST raise
:class:`LiveTradingBlockedError` BEFORE any SDK construction or
network call. Production code MUST NOT create the confirmation
file — only the user, manually.

Validation contract assertions exercised
----------------------------------------

* **VAL-M3-035** — the four-cell matrix below:

  =========  =========  =================================
  LIVE_MODE  file       outcome
  =========  =========  =================================
  0          missing    block (LiveTradingBlockedError)
  1          missing    block (LiveTradingBlockedError)
  0          present    block (LiveTradingBlockedError)
  1          present    construct successfully + WARNING
  =========  =========  =================================

* **VAL-M3-036** — when both gates open, a WARNING-level log line
  ``"LIVE TRADING ENABLED — orders will trade real money."`` is
  emitted exactly once on construction.
* **VAL-M3-037** — production code (everything outside ``tests/``,
  ``docs/``, and ``README.md``) never writes / touches the
  confirmation file. The grep audit at the bottom of this module
  verifies that no production source file invokes
  ``Path.touch``, ``open(..., 'w')``, or ``write_text(...)``
  against the confirmation filename.
* **VAL-M3-038** — implicit, verified at deploy-time: the VPS
  ``/root/alpha_sniper/.env`` ships ``LIVE_MODE=0`` and the
  confirmation file is absent. We pin ``ALPACA_BASE_URL`` to the
  paper endpoint in the project's ``.env.example`` for the same
  reason, and the local ``LIVE_MODE`` bool from
  :func:`biotech_sniper.config` defaults to ``False``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import alpaca_client as ac
from biotech_sniper.alpaca_client import (
    AlpacaClient,
    LIVE_BASE_URL,
    LIVE_CONFIRMATION_FILE,
    LiveTradingBlockedError,
    PAPER_BASE_URL,
)


# ---------------------------------------------------------------------------
# Minimal SDK doubles: we never let alpaca-py construct a real client
# in these tests. Both the trading client and the option-data client
# expose only the bare surface the wrapper touches at construction.
# ---------------------------------------------------------------------------


class _NullTradingClient:
    """Stand-in for :class:`alpaca.trading.client.TradingClient`."""

    def get_account(self) -> Any:  # pragma: no cover - never called here
        raise AssertionError(
            "TradingClient must not be invoked in LIVE_MODE guardrail tests"
        )


class _NullOptionsDataClient:
    """Stand-in for :class:`OptionHistoricalDataClient`."""

    def get_option_chain(self, request: Any) -> Any:  # pragma: no cover
        raise AssertionError(
            "OptionHistoricalDataClient must not be invoked in LIVE_MODE "
            "guardrail tests"
        )


# ---------------------------------------------------------------------------
# Matrix helpers: the four cells of the LIVE_MODE × file table.
# ---------------------------------------------------------------------------


def _attempt_live_construct() -> AlpacaClient:
    """Try to construct an :class:`AlpacaClient` against the live URL.

    Returns the constructed client on success; raises whatever the
    constructor raises (typically :class:`LiveTradingBlockedError`)
    on failure. Pre-populates injected SDK doubles so a successful
    construction does not need real credentials and never makes a
    real network call.
    """
    return AlpacaClient(
        api_key="k",
        secret_key="s",
        base_url=LIVE_BASE_URL,
        trading_client=_NullTradingClient(),
        option_data_client=_NullOptionsDataClient(),
    )


@pytest.fixture
def patched_gates(monkeypatch):
    """Patch the LIVE_MODE bool and the confirmation-file probe.

    Returns a setter ``set_gates(live_mode: bool, file_present: bool)``
    that the test can call to position the guardrail at any of the
    four matrix cells. The patches are scoped to the test via
    ``monkeypatch`` so no global state leaks between tests.
    """

    def _set_gates(live_mode: bool, file_present: bool) -> None:
        # ``config.LIVE_MODE`` is a ``Final[bool]`` cached at import
        # time; we mutate the module attribute directly so the
        # alpaca_client guardrail (which reads ``config.LIVE_MODE``
        # via attribute lookup) sees the new value.
        monkeypatch.setattr(ac.config, "LIVE_MODE", live_mode)
        # The confirmation file probe is a single private helper we
        # can substitute cleanly; tests do not touch the real file
        # system so a paving check stays deterministic across CI
        # and laptop runs.
        monkeypatch.setattr(
            ac, "_confirmation_file_present", lambda: file_present
        )

    return _set_gates


# ---------------------------------------------------------------------------
# VAL-M3-035 — Matrix of four cells.
# ---------------------------------------------------------------------------


def test_block_when_live_mode_off_and_file_missing(patched_gates) -> None:
    """(LIVE_MODE=0, no file) → block."""
    patched_gates(live_mode=False, file_present=False)
    with pytest.raises(LiveTradingBlockedError) as excinfo:
        _attempt_live_construct()
    msg = str(excinfo.value)
    assert "LIVE_MODE" in msg
    assert LIVE_CONFIRMATION_FILE in msg


def test_block_when_live_mode_on_but_file_missing(patched_gates) -> None:
    """(LIVE_MODE=1, no file) → block.

    Verifies that turning on the env var alone is INSUFFICIENT — the
    confirmation file must also be present. This is the second
    independent gate.
    """
    patched_gates(live_mode=True, file_present=False)
    with pytest.raises(LiveTradingBlockedError) as excinfo:
        _attempt_live_construct()
    assert "LIVE_MODE" in str(excinfo.value)


def test_block_when_file_present_but_live_mode_off(patched_gates) -> None:
    """(LIVE_MODE=0, file present) → block.

    Mirror of the previous case: the confirmation file alone is
    INSUFFICIENT. Both gates must open simultaneously.
    """
    patched_gates(live_mode=False, file_present=True)
    with pytest.raises(LiveTradingBlockedError) as excinfo:
        _attempt_live_construct()
    assert "LIVE_MODE" in str(excinfo.value)


def test_allow_when_live_mode_on_and_file_present(
    patched_gates, caplog
) -> None:
    """(LIVE_MODE=1, file present) → construct + WARNING (VAL-M3-036).

    The wrapper still constructs successfully (no exception) and
    emits a single WARNING-level log line containing the literal
    phrase ``LIVE TRADING ENABLED``. We assert both behaviours in
    one test so a regression to either branch is caught here.
    """
    patched_gates(live_mode=True, file_present=True)
    caplog.set_level(
        logging.WARNING, logger="biotech_sniper.alpaca_client"
    )

    client = _attempt_live_construct()
    assert client.base_url == LIVE_BASE_URL

    warns = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(
        "LIVE TRADING ENABLED" in line for line in warns
    ), (
        "expected the loud 'LIVE TRADING ENABLED' WARNING log on "
        "successful live construction (VAL-M3-036)"
    )


# ---------------------------------------------------------------------------
# Block-message hygiene — the error names BOTH gates so the operator
# immediately sees which side of the guardrail tripped.
# ---------------------------------------------------------------------------


def test_block_message_names_both_gates(patched_gates) -> None:
    """The :class:`LiveTradingBlockedError` message must reference
    both ``LIVE_MODE`` and the literal confirmation filename so an
    operator never has to guess which gate is closed."""
    patched_gates(live_mode=False, file_present=False)
    with pytest.raises(LiveTradingBlockedError) as excinfo:
        _attempt_live_construct()
    msg = str(excinfo.value)
    assert "LIVE_MODE" in msg
    assert LIVE_CONFIRMATION_FILE in msg
    # Echoes the live URL we attempted so logs are self-explanatory.
    assert LIVE_BASE_URL in msg


def test_paper_url_is_default_and_constructs_silently(
    patched_gates, caplog
) -> None:
    """Default-construction (paper URL) does NOT emit the live
    WARNING, even if both gates happen to be open — the warning is
    keyed on the URL choice, not on the gate state.
    """
    patched_gates(live_mode=True, file_present=True)
    caplog.set_level(
        logging.WARNING, logger="biotech_sniper.alpaca_client"
    )
    client = AlpacaClient(
        api_key="k",
        secret_key="s",
        base_url=PAPER_BASE_URL,
        trading_client=_NullTradingClient(),
        option_data_client=_NullOptionsDataClient(),
    )
    assert client.base_url == PAPER_BASE_URL
    warns = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert not any(
        "LIVE TRADING ENABLED" in line for line in warns
    ), (
        "the LIVE TRADING ENABLED warning must only fire for live URL "
        "construction, never for paper construction"
    )


# ---------------------------------------------------------------------------
# VAL-M3-037 — Production code never writes the confirmation file.
#
# Source-level audit: the only thing production code is ever allowed
# to do with the confirmation filename is **read** it (or compare
# strings against it). Any write site (``Path.touch``,
# ``open(..., 'w')``, ``write_text``) within ``biotech_sniper/``
# would unblock LIVE_MODE without the user's explicit knowledge,
# defeating the guardrail.
# ---------------------------------------------------------------------------


# Patterns that would represent a write to the confirmation file. We
# match against substrings on each occurrence line so the audit is
# robust to formatter changes (one-line vs. multi-line calls).
_WRITE_PATTERNS: tuple[str, ...] = (
    ".touch(",
    'open(',
    ".write_text(",
    ".write_bytes(",
    "shutil.copy",
    "subprocess.run(['touch'",
    'subprocess.run(["touch"',
)


def _project_root() -> Path:
    """Return the repository root (parent of the ``biotech_sniper`` package)."""
    # ``__file__`` is ``<repo>/tests/test_live_mode_guardrails.py``
    # so two parents up is the repository root.
    return Path(__file__).resolve().parent.parent


def _iter_production_python_files() -> list[Path]:
    """Walk ``biotech_sniper/`` collecting ``*.py`` files for the audit."""
    root = _project_root() / "biotech_sniper"
    return [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_production_code_never_writes_confirmation_file() -> None:
    """Static audit: every occurrence of the confirmation filename in
    production code is on a non-write line. We do NOT enumerate read
    sites here (those are explicitly allowed and even required by
    the guardrail itself). The audit short-circuits the moment it
    finds a write site, naming the offending file:line so the fix
    is obvious.
    """
    offenders: list[tuple[Path, int, str]] = []
    for source_file in _iter_production_python_files():
        try:
            text = source_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if LIVE_CONFIRMATION_FILE not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LIVE_CONFIRMATION_FILE not in line:
                continue
            for pattern in _WRITE_PATTERNS:
                if pattern in line:
                    offenders.append((source_file, lineno, line.strip()))
                    break
    assert not offenders, (
        "Production code must never write/touch the live-trading "
        "confirmation file (VAL-M3-037). Offending sites:\n"
        + "\n".join(
            f"  {path}:{lineno}  {snippet}"
            for path, lineno, snippet in offenders
        )
    )


def test_grep_audit_returns_only_read_sites() -> None:
    """Mirror the verificationStep grep:

    ``grep -RnE 'i-understand-this-trades-real-money' biotech_sniper/
    | grep -vE 'tests/|README|docs/'`` must surface only known
    read sites. We pin the expected set so a future regression that
    sneaks in a new reference (e.g. a CLI helper that "marks" the
    file) is caught at test time, not deploy time.
    """
    occurrences: dict[Path, list[int]] = {}
    for source_file in _iter_production_python_files():
        try:
            text = source_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LIVE_CONFIRMATION_FILE in line:
                occurrences.setdefault(source_file, []).append(lineno)

    # Every source file that mentions the filename must be one of the
    # known read-only sites. The current tree has two: alpaca_client
    # (the constant + docstrings) and config (a comment). Both are
    # read-only by inspection.
    project = _project_root()
    expected_relative = {
        Path("biotech_sniper/alpaca_client.py"),
        Path("biotech_sniper/config.py"),
    }
    actual_relative = {
        path.relative_to(project) for path in occurrences
    }
    unexpected = actual_relative - expected_relative
    assert not unexpected, (
        "Unexpected files reference the live-trading confirmation "
        "filename. New references should be reviewed against the "
        "VAL-M3-037 'no production write site' invariant. Files: "
        f"{sorted(str(p) for p in unexpected)}"
    )
