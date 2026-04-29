"""Lock-in test: no ``datetime.utcnow()`` usage in the project tree.

``datetime.datetime.utcnow()`` is deprecated in Python 3.12 and silently
produces a naive datetime that callers routinely treat as UTC. The
canonical replacement is ``datetime.datetime.now(datetime.timezone.utc)``,
which yields a tz-aware UTC datetime.

f-misc-04 swept the project tree to remove every prior occurrence (4 in
``biotech_sniper/intelligence/universal_news_watcher.py``, 2 in
``biotech_sniper/llm/llm_debate.py``, plus a docstring mention in
``biotech_sniper/backtest.py``). This test pins that invariant: any
future regression that re-introduces ``datetime.utcnow`` or a bare
``utcnow()`` call inside ``biotech_sniper/`` or ``tests/`` (other than
this lock-in test itself) fails the suite.

The grep mirrors the verification step in the feature description:

    grep -nE 'datetime\\.utcnow|^.*utcnow\\(\\)' biotech_sniper/ tests/

Empty output ⇒ pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = (REPO_ROOT / "biotech_sniper", REPO_ROOT / "tests")

# Lock-in pattern. Matches:
#  * The dotted form ``datetime.utcnow`` (covers ``datetime.utcnow()``
#    and ``datetime.datetime.utcnow()``).
#  * A bare ``utcnow()`` call anywhere on a line.
_UTCNOW_PATTERN = re.compile(r"datetime\.utcnow|utcnow\(\)")

# This file legitimately mentions the literal pattern in its docstring
# and inside the regex above. Skip it during the scan so the lock-in
# test does not flag itself.
_SELF_PATH = Path(__file__).resolve()


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path.resolve() == _SELF_PATH:
                continue
            files.append(path)
    return files


def test_no_utcnow_in_project_tree() -> None:
    """No remaining ``datetime.utcnow`` / ``utcnow()`` usage in the tree."""
    offenders: list[str] = []
    for path in _iter_python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _UTCNOW_PATTERN.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.rstrip()}")

    assert not offenders, (
        "Found deprecated datetime.utcnow / bare utcnow() usage. Replace "
        "with datetime.datetime.now(datetime.timezone.utc):\n  "
        + "\n  ".join(offenders)
    )


def test_files_with_known_replacements_use_timezone_utc() -> None:
    """Sanity check the post-refactor files actually use the canonical form.

    Pins the rewrite in
    :mod:`biotech_sniper.intelligence.universal_news_watcher` and
    :mod:`biotech_sniper.llm.llm_debate` so a future "clean up imports"
    pass that drops the timezone-aware ``now()`` cannot regress to
    naive UTC without first failing this assertion.
    """
    expected = (
        REPO_ROOT
        / "biotech_sniper"
        / "intelligence"
        / "universal_news_watcher.py",
        REPO_ROOT / "biotech_sniper" / "llm" / "llm_debate.py",
    )
    for path in expected:
        if not path.is_file():
            pytest.skip(f"{path} missing — skipping replacement check")
        text = path.read_text(encoding="utf-8")
        assert "datetime.now(datetime.timezone.utc)" in text, (
            f"{path.relative_to(REPO_ROOT)} no longer references "
            "datetime.now(datetime.timezone.utc); did the canonical "
            "replacement regress?"
        )
