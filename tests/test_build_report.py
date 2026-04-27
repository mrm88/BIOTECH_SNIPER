"""Unit tests for ``biotech_sniper.build_report``.

The build_report module replaces the per-date ``build_report_aprNN.py``
proliferation with a single CLI accepting ``--date YYYY-MM-DD``. These
tests cover:

* Strict ISO date parsing and rejection of malformed values.
* Dispatch to the matching dated archive script when one exists.
* Placeholder workbook fallback when no dated archive script exists.
* End-to-end CLI behaviour: ``--date 2026-04-25`` returns 0 and writes
  ``reports/Alpha_Sniper_2026-04-25.xlsx``; ``--date 2026/04/25`` exits
  non-zero and emits a stderr message naming the expected format.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_build_report():
    """Re-import build_report after a BIOTECH_SNIPER_HOME change."""
    for name in ("biotech_sniper.build_report", "biotech_sniper.paths"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    return importlib.import_module("biotech_sniper.build_report")


@pytest.fixture
def isolated_home(monkeypatch, tmp_path: Path):
    """Point BIOTECH_SNIPER_HOME at an empty temp dir so dispatch falls back
    to the placeholder workbook path (no dated scripts present)."""
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


def test_parse_date_accepts_iso():
    br = _reload_build_report()
    assert br._parse_date("2026-04-25") == _dt.date(2026, 4, 25)


@pytest.mark.parametrize(
    "value",
    [
        "2026/04/25",
        "apr25",
        "20260425",
        "2026-4-25",  # not strict ISO (single-digit month)
        "2026-13-01",  # invalid month
        "",
        "not-a-date",
    ],
)
def test_parse_date_rejects_malformed(value):
    import argparse

    br = _reload_build_report()
    with pytest.raises(argparse.ArgumentTypeError) as exc:
        br._parse_date(value)
    # The message must name the expected format so callers know how to fix it.
    assert "YYYY-MM-DD" in str(exc.value)


# ---------------------------------------------------------------------------
# _archive_script_for / build_report dispatch
# ---------------------------------------------------------------------------


def test_archive_script_resolves_existing_dated_file():
    """The repo ships ``archive/build_report/build_report_apr25.py``, so the
    helper must locate it for date 2026-04-25."""
    br = _reload_build_report()
    script = br._archive_script_for(_dt.date(2026, 4, 25))
    assert script is not None
    assert script.name == "build_report_apr25.py"
    assert script.is_file()


def test_archive_script_returns_none_when_missing(isolated_home: Path):
    """With BIOTECH_SNIPER_HOME pointing at an empty directory there is no
    archive/ subtree, so the lookup must return ``None``."""
    br = _reload_build_report()
    assert br._archive_script_for(_dt.date(2030, 1, 1)) is None


def test_build_report_emits_placeholder_for_unknown_date(isolated_home: Path):
    """``build_report`` falls back to a placeholder workbook when no dated
    archive script exists for the requested date."""
    br = _reload_build_report()
    out = br.build_report(_dt.date(2030, 1, 1))
    assert out.exists()
    assert out.name == "Alpha_Sniper_2030-01-01.xlsx"
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


def test_cli_happy_path_writes_workbook(tmp_path: Path, monkeypatch, capsys):
    """``--date 2026-04-25`` exits 0 and writes the expected workbook.

    We point BIOTECH_SNIPER_HOME at the actual repo root so the dispatcher
    can find the archived dated script. The dated script saves the workbook
    to ``BASE/reports/Alpha_Sniper_2026-04-25.xlsx``.
    """
    repo_root = Path(__file__).resolve().parent.parent
    # Use the real package directory as BASE_DIR so the archived dated
    # script's hard-coded ``BASE / 'reports/...'`` lookups land in a
    # writable location and the produced workbook can be inspected.
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(repo_root / "biotech_sniper"))
    br = _reload_build_report()

    rc = br.main(["--date", "2026-04-25"])
    assert rc == 0

    output = repo_root / "biotech_sniper" / "reports" / "Alpha_Sniper_2026-04-25.xlsx"
    assert output.exists() and output.stat().st_size > 0


def test_cli_rejects_malformed_date(monkeypatch, capsys):
    """A malformed date triggers a non-zero exit with an ISO-format message
    on stderr (``argparse`` converts ``ArgumentTypeError`` into ``SystemExit``
    with code 2)."""
    br = _reload_build_report()
    with pytest.raises(SystemExit) as exc:
        br.main(["--date", "2026/04/25"])
    assert exc.value.code != 0

    captured = capsys.readouterr()
    # argparse prints the error to stderr, prefixed by usage info.
    assert "YYYY-MM-DD" in captured.err


def test_cli_requires_date_argument(capsys):
    """Missing ``--date`` exits non-zero (argparse 'required' enforcement)."""
    br = _reload_build_report()
    with pytest.raises(SystemExit) as exc:
        br.main([])
    assert exc.value.code != 0
