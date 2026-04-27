"""Lock-in regression test for f-m3-22 yfinance residual-imports removal.

VAL-M3-011 failed in user-testing round 1 because two production
modules (``biotech_sniper/new_opportunity_sniper.py`` and
``biotech_sniper/intelligence/bulk_universe_scanner.py``) still
contained ``yfinance``-shaped strings (imports, ``yf.`` references,
or stale comments) after f-m3-02 + f-m3-13 had removed the runtime
usage. The mission contract requires zero yfinance references in
production modules and in the pinned-dependency manifest.

This test is the regression gate that fails loudly if a future
worker reintroduces a yfinance import, ``yf.`` reference, or
``yfinance``-pinned line into either tree::

    git grep -nE 'yfinance|^import yf|from yfinance|yf\\.' \\
        biotech_sniper/ requirements.txt    # must be empty
    grep -E '^yfinance' requirements.txt    # must be empty

The test deliberately operates on the on-disk text of the files —
not on any runtime behaviour — so it remains green even on hosts
where the real ``alpaca-py`` SDK is unavailable, and it also gates
docstrings and comments (which are exactly how the residual
references slipped through previous milestones).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root: this file lives at ``tests/test_no_yfinance.py``.
REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "biotech_sniper"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# The full grep pattern from the feature's verification step. Mirrors
# ``git grep -nE 'yfinance|^import yf|from yfinance|yf\.'``.
_FORBIDDEN_PATTERN = re.compile(
    r"yfinance|^import yf|from yfinance|yf\.",
    flags=re.MULTILINE,
)

# ``^yfinance`` form from ``grep -E '^yfinance' requirements.txt``.
_REQUIREMENTS_LINE_PATTERN = re.compile(r"^yfinance", flags=re.MULTILINE)


def _iter_python_files(root: Path):
    """Yield every committed-source ``.py`` under ``root``.

    Skips ``__pycache__`` and any other generated cache directories
    so the grep is over real source only.
    """
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_pkg_root_exists_for_grep() -> None:
    """Sanity check — the ``biotech_sniper/`` tree must exist for the gate."""
    assert PKG_ROOT.is_dir(), f"missing package root: {PKG_ROOT}"
    assert REQUIREMENTS.is_file(), f"missing requirements file: {REQUIREMENTS}"


def test_no_yfinance_references_in_biotech_sniper_package() -> None:
    """No file under ``biotech_sniper/`` may mention yfinance/yf./import yf.

    Catches both runtime imports (``import yfinance``, ``from yfinance``,
    ``yf.Ticker``) and stale comments / docstrings that historically
    reintroduced VAL-M3-011 regressions.
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_python_files(PKG_ROOT):
        text = path.read_text(encoding="utf-8")
        for match in _FORBIDDEN_PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1]
            offenders.append((str(path.relative_to(REPO_ROOT)), line_no, line))

    assert not offenders, (
        "Forbidden yfinance-shaped references found under biotech_sniper/:\n"
        + "\n".join(f"  {p}:{n}: {l!r}" for p, n, l in offenders)
    )


def test_no_yfinance_references_in_requirements_file() -> None:
    """``requirements.txt`` must contain zero yfinance mentions.

    Covers both the strict ``^yfinance`` pin form and any commentary
    that names yfinance — the same gate the verification step runs.
    """
    text = REQUIREMENTS.read_text(encoding="utf-8")

    pin_match = _REQUIREMENTS_LINE_PATTERN.search(text)
    assert pin_match is None, (
        "requirements.txt still pins yfinance (line begins with 'yfinance')"
    )

    full_match = _FORBIDDEN_PATTERN.search(text)
    assert full_match is None, (
        "requirements.txt still mentions yfinance (e.g. in a comment): "
        f"{text.splitlines()[text.count(chr(10), 0, full_match.start())]!r}"
    )


@pytest.mark.parametrize(
    "module_relpath",
    [
        "new_opportunity_sniper.py",
        "intelligence/bulk_universe_scanner.py",
    ],
)
def test_specific_modules_named_in_feature_are_clean(module_relpath: str) -> None:
    """Targeted gate for the two modules that triggered the f-m3-22 work.

    The feature description names these explicitly; pinning them as
    parametrised cases makes a future regression's failure message
    point straight at the offending file rather than at the
    package-wide gate.
    """
    path = PKG_ROOT / module_relpath
    assert path.is_file(), f"expected production module missing: {path}"
    text = path.read_text(encoding="utf-8")
    match = _FORBIDDEN_PATTERN.search(text)
    assert match is None, (
        f"{module_relpath} still contains a forbidden yfinance reference: "
        f"{text.splitlines()[text.count(chr(10), 0, match.start())]!r}"
    )
