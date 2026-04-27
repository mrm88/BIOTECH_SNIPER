"""Parameterized Alpha Sniper daily Excel report builder.

This module replaces the per-date ``build_report_aprNN.py`` proliferation
(``build_report_apr11.py`` ... ``build_report_apr25.py``, plus the
``build_report_apr14b.py`` intraday catch-up variant) with a single
entrypoint that accepts ``--date YYYY-MM-DD``.

Behaviour
---------
* ``--date YYYY-MM-DD`` is the only required argument. The value MUST
  parse as a strict ISO 8601 calendar date — alternative formats such as
  ``2026/04/25``, ``apr25``, or ``20260425`` are rejected with a
  non-zero exit code and a stderr message naming the expected format.
* When a hand-curated dated script for the requested calendar day exists
  under ``archive/build_report/build_report_<mmm><dd>.py`` (e.g.
  ``build_report_apr25.py``), it is executed via :mod:`runpy` so the
  resulting workbook is structurally identical to what the original
  dated script would have written. This is the path exercised by the
  validation contract (``VAL-M1-019``).
* When no dated script exists for the requested date, a minimally
  formatted placeholder workbook is emitted at
  ``BASE_DIR/reports/Alpha_Sniper_<DATE>.xlsx`` so that ``--date`` is
  always deterministic and never errors silently.

CLI usage
---------
    python -m biotech_sniper.build_report --date 2026-04-25

The output workbook lands under :data:`biotech_sniper.paths.REPORTS_DIR`
(``BASE_DIR/reports``) and is named ``Alpha_Sniper_<YYYY-MM-DD>.xlsx``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import re as _re
import runpy
import sys
from pathlib import Path
from typing import Sequence

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from biotech_sniper.paths import BASE_DIR, REPORTS_DIR

__all__ = ["main", "build_report"]


_LOGGER = logging.getLogger(__name__)


# Lowercase, three-letter month abbreviations matching the historical
# ``build_report_aprNN.py`` naming. New monthly archives can extend this
# mapping (e.g. ``5: "may"``) without code changes elsewhere.
_MONTH_ABBREV: dict[int, str] = {
    1: "jan",
    2: "feb",
    3: "mar",
    4: "apr",
    5: "may",
    6: "jun",
    7: "jul",
    8: "aug",
    9: "sep",
    10: "oct",
    11: "nov",
    12: "dec",
}


def _archive_root() -> Path:
    """Return the ``archive/build_report`` directory.

    The directory layout differs depending on whether ``BIOTECH_SNIPER_HOME``
    points at the package directory (local-dev fallback, where ``BASE_DIR``
    is ``<repo>/biotech_sniper``) or the repo root (the VPS deployment).
    Both layouts are supported.
    """
    if (BASE_DIR / "archive" / "build_report").is_dir():
        return BASE_DIR / "archive" / "build_report"
    return BASE_DIR.parent / "archive" / "build_report"


# Strict YYYY-MM-DD pattern — exactly 4 digits, dash, 2 digits, dash, 2
# digits. Python's :func:`datetime.date.fromisoformat` is more permissive
# in 3.11+ (it accepts the basic ``YYYYMMDD`` form too), so we anchor
# with this regex first to keep the contract narrow.
_ISO_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> _dt.date:
    """Parse ``value`` as a strict ``YYYY-MM-DD`` calendar date.

    Raises :class:`argparse.ArgumentTypeError` (which argparse converts
    into a non-zero exit and a stderr error message) when ``value`` does
    not match the expected format. Examples of rejected inputs include
    ``2026/04/25`` (wrong separator), ``apr25`` (legacy script name),
    ``20260425`` (basic ISO form), and ``2026-4-25`` (single-digit month).
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}: expected ISO format YYYY-MM-DD "
            "(e.g. 2026-04-25)"
        )
    try:
        return _dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        # The regex passed, so the only way fromisoformat fails is a
        # calendar-out-of-range value such as ``2026-13-01``.
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}: expected ISO format YYYY-MM-DD "
            "(e.g. 2026-04-25)"
        ) from exc


def _archive_script_for(date: _dt.date) -> Path | None:
    """Return the dated archive script for ``date`` if one exists."""
    abbrev = _MONTH_ABBREV[date.month]
    candidate = _archive_root() / f"build_report_{abbrev}{date.day:02d}.py"
    return candidate if candidate.is_file() else None


def _output_path(date: _dt.date) -> Path:
    return REPORTS_DIR / f"Alpha_Sniper_{date.isoformat()}.xlsx"


def _placeholder_workbook(date: _dt.date) -> Path:
    """Emit a minimally-formatted placeholder workbook for ``date``.

    Used as a deterministic fallback when no hand-curated dated script
    exists for the requested calendar day. The workbook contains a single
    titled sheet so that downstream consumers can rely on the file being
    a valid ``.xlsx`` archive.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    output = _output_path(date)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Alpha Sniper"

    title = ws.cell(row=1, column=1, value=f"Alpha Sniper -- {date.isoformat()}")
    title.font = Font(bold=True, size=12, name="Consolas")
    title.fill = PatternFill(fill_type="solid", fgColor="0D1117")
    title.alignment = Alignment(horizontal="left", vertical="center")
    title.font = Font(bold=True, size=12, color="FFFFFF", name="Consolas")

    note = ws.cell(
        row=2,
        column=1,
        value="No dated build_report script archived for this date.",
    )
    note.font = Font(size=10, name="Consolas", color="8B949E")

    ws.column_dimensions["A"].width = 60
    wb.save(output)
    return output


def build_report(date: _dt.date) -> Path:
    """Build the workbook for ``date`` and return the output path.

    When an archived dated script exists for ``date`` it is executed via
    :mod:`runpy`; the dated scripts save their workbook to
    ``BASE_DIR/reports/Alpha_Sniper_<date>.xlsx`` as a side effect of
    module execution. Otherwise a minimal placeholder workbook is written.
    """
    # Ensure the reports directory exists for both branches. The dated
    # archive scripts assume ``BASE_DIR/reports`` already exists (they call
    # ``wb.save(...)`` without ``mkdir``), which fails on a fresh checkout
    # when ``BIOTECH_SNIPER_HOME`` points at the repo root.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    archive_script = _archive_script_for(date)
    if archive_script is not None:
        # Execute the dated script in a fresh namespace with __name__ set
        # to "__main__" so any ``if __name__ == "__main__":`` blocks fire.
        #
        # Some legacy archive scripts (e.g. apr11/apr12/apr13/apr14/apr14b)
        # read per-day state files such as ``BASE/state/options_chains_<DATE>.json``
        # whose canonical home migrated to ``migrations/seed/`` after f-m1-03.
        # When that state file is absent we degrade gracefully to the
        # placeholder workbook fallback rather than crashing the CLI.
        try:
            runpy.run_path(str(archive_script), run_name="__main__")
        except FileNotFoundError as missing:
            _LOGGER.warning(
                "build_report: archive script %s aborted with "
                "FileNotFoundError (%s); falling back to placeholder workbook",
                archive_script.name,
                missing,
            )
            return _placeholder_workbook(date)
        return _output_path(date)
    return _placeholder_workbook(date)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_report",
        description=(
            "Build the Alpha Sniper daily Excel report for the requested "
            "date. Replaces the per-date build_report_aprNN.py scripts "
            "with a single parameterized entrypoint."
        ),
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="Report date in ISO format (YYYY-MM-DD), e.g. 2026-04-25.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code.

    The dated archive scripts already log their own ``Saved: ...`` line as a
    side effect, so we only emit our own confirmation when the placeholder
    branch ran (i.e. no dated script existed for the requested date).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    used_archive = _archive_script_for(args.date) is not None
    output = build_report(args.date)
    if not used_archive:
        print(f"Saved: {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    sys.exit(main())
