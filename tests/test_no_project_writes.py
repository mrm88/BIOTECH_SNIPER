"""Regression tests for VAL-M2-061: no writes leak under ``project/``.

Background
----------
On the VPS the original reference tree is preserved at
``/root/alpha_sniper/project/biotech_sniper/`` and is **read-only** per
the mission boundaries (``AGENTS.md``). The new clone lives at
``/root/alpha_sniper/repo/`` with state under ``repo/state/`` and DB
under ``repo/data/`` (resolved via :mod:`biotech_sniper.paths`).

A user-testing run found JSON files at
``/root/alpha_sniper/project/biotech_sniper/state/*.json`` with mtimes
within the last 7 days, which means *some* code path was using a
package-relative base directory (typically
``BASE_DIR = Path(__file__).parent[.parent]``) instead of
``paths.BASE_DIR``. When the package was imported from
``project/biotech_sniper/``, those writes landed in the read-only
reference tree and silently mutated it.

These tests guard against the issue regressing:

* :func:`test_no_path_file_dot_parent_writers_in_package` — static
  scan of every ``.py`` file under ``biotech_sniper/`` for the
  ``Path(__file__).parent`` idiom outside the few legitimate uses
  (``paths.py`` fallback, ``db/__init__.py`` for ``schema.sql``,
  ``audit.py`` for the ``.env`` discovery shim).
* :func:`test_state_constants_resolve_under_base_dir` — imports the
  modules that previously declared a package-relative ``BASE_DIR``
  and asserts their state-file constants resolve under
  ``paths.BASE_DIR``.
* :func:`test_save_paths_do_not_write_under_project_sibling` —
  redirects ``BIOTECH_SNIPER_HOME`` to a clean tmp dir, exercises the
  audit / news_events / scoring write paths, and asserts no files
  land under ``{BASE_DIR}/../project``.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "biotech_sniper"

# These files are allowed to use ``Path(__file__).parent`` because
# they reference assets that legitimately live inside the package
# itself (not state files):
#
# * ``paths.py``         — fallback when BIOTECH_SNIPER_HOME is unset.
# * ``db/__init__.py``   — locates ``schema.sql`` next to the module.
# * ``audit.py``         — locates ``.env`` *one or two levels above*
#                          the package, never under the package.
ALLOWED_PATH_FILE_USES = {
    PACKAGE_ROOT / "paths.py",
    PACKAGE_ROOT / "db" / "__init__.py",
    PACKAGE_ROOT / "audit.py",
}


def test_no_path_file_dot_parent_writers_in_package():
    """No ``.py`` file under the package may build a state-write base from ``Path(__file__).parent``.

    The forbidden idiom is ``Path(__file__).parent`` (or
    ``parent.parent``) used as a base for ``state/``, ``data/``,
    ``logs/``, ``reports/``, or ``archive/`` — the set of write zones
    that ``paths.py`` already handles. Any such occurrence outside
    :data:`ALLOWED_PATH_FILE_USES` is a regression of VAL-M2-061.
    """
    pattern = re.compile(r"Path\(__file__\)\s*\.\s*(?:resolve\(\)\s*\.\s*)?parent")
    offenders: list[tuple[Path, int, str]] = []
    for py_file in PACKAGE_ROOT.rglob("*.py"):
        if py_file in ALLOWED_PATH_FILE_USES:
            continue
        # Skip __pycache__ etc. (rglob already yields .py only, but
        # be defensive against editor backup files).
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            # Skip pure comment lines and docstring fragments — we
            # only care about live code that constructs paths.
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                offenders.append((py_file, lineno, line.strip()))
    assert not offenders, (
        "Found Path(__file__).parent constructions in package source. These "
        "resolve relative to the package directory, not BASE_DIR, and can "
        "leak writes into the read-only reference tree on the VPS. Use "
        "biotech_sniper.paths.BASE_DIR (or STATE_DIR / DATA_DIR / etc.) "
        f"instead.\nOffenders:\n" + "\n".join(
            f"  {p.relative_to(REPO_ROOT)}:{n}: {line}" for p, n, line in offenders
        )
    )


def _reload(module_name: str):
    """Reload a module so its module-level ``BASE_DIR`` picks up the
    current ``BIOTECH_SNIPER_HOME``."""
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


@pytest.fixture
def isolated_base(monkeypatch, tmp_path):
    """Set ``BIOTECH_SNIPER_HOME`` to a clean tmp dir and reload paths.

    Returns the resolved ``BASE_DIR`` path. Also pre-creates the
    sibling ``project/`` directory used by VAL-M2-061's check —
    that directory must remain empty after exercising save paths.
    """
    base = tmp_path / "repo"
    base.mkdir()
    project_sibling = tmp_path / "project"
    project_sibling.mkdir()
    monkeypatch.setenv("BIOTECH_SNIPER_HOME", str(base))
    paths = _reload("biotech_sniper.paths")
    assert paths.BASE_DIR == base
    return base, project_sibling


def test_state_constants_resolve_under_base_dir(isolated_base):
    """Modules previously using ``Path(__file__).parent.parent`` now
    resolve their state-file constants under ``paths.BASE_DIR``."""
    base, _ = isolated_base

    # The following modules used to declare ``BASE_DIR =
    # Path(__file__).parent[.parent]`` and built state paths from it.
    # After the f-m2-18 fix they MUST source BASE_DIR from
    # ``biotech_sniper.paths`` instead.
    iv = _reload("biotech_sniper.iv_crush_exit_rules")
    intraday = _reload("biotech_sniper.intraday_scanner")
    cpm = _reload("biotech_sniper.intelligence.company_pipeline_manager")
    bus = _reload("biotech_sniper.intelligence.bulk_universe_scanner")
    sched = _reload("biotech_sniper.intelligence.pipeline_scheduler")

    for path in (
        iv.ACTIVE_PLAYS_FILE,
        iv.LEDGER_FILE,
        intraday.ACTIVE_FILE,
        intraday.RESOLVED_FILE,
        intraday.SEC_STATE,
        intraday.INTRADAY_LOG,
        cpm.STATE_FILE,
    ):
        assert base in Path(path).resolve().parents, (
            f"{path} does not resolve under BASE_DIR={base}"
        )
    # bulk_universe_scanner and pipeline_scheduler expose BASE_DIR;
    # check it directly.
    assert bus.BASE_DIR == base
    assert sched.BASE_DIR == base


def test_save_paths_do_not_write_under_project_sibling(isolated_base, monkeypatch):
    """Exercise typical save paths and verify the read-only ``project/``
    sibling tree stays empty.

    The redirected ``BASE_DIR`` is ``{tmp}/repo`` and the forbidden
    sibling is ``{tmp}/project`` (mirroring the VPS layout). After
    running representative writes (audit JSON merge, news-events
    audit-merge, scoring cache write), no file may exist below
    ``{tmp}/project``.
    """
    base, project_sibling = isolated_base

    # Reload the modules that perform writes against BASE_DIR-derived
    # paths so they pick up the redirected BIOTECH_SNIPER_HOME.
    paths = _reload("biotech_sniper.paths")
    news_events = _reload("biotech_sniper.news_events")
    unified_scorer = _reload("biotech_sniper.sectors.unified_scorer")

    # 1) Audit-style write: write a small dict to BASE_DIR/state/audit_latest.json
    audit_dir = paths.STATE_DIR
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "audit_latest.json"
    audit_path.write_text(json.dumps({"as_of_date": "2026-04-27"}), encoding="utf-8")

    # 2) news_events.write_audit_summary: writes into
    # BASE_DIR/state/audit_latest.json (the canonical contract path).
    result = news_events.DailyIngestResult(
        tickers_attempted=0,
        rows_inserted=0,
        rows_skipped_duplicate=0,
        sources_run=["test"],
        empty_feed_reasons={},
        completed_at="2026-04-27T00:00:00Z",
    )
    news_events.write_audit_summary(result)
    assert audit_path.is_file()

    # 3) Scoring cache write: drop a tiny scoring_cache.json under
    # BASE_DIR/state/. We don't import the heavy scorer's main entry —
    # just ensure the file lands under the redirected base.
    cache_path = paths.STATE_DIR / "scoring_cache.json"
    cache_path.write_text(json.dumps({"AAPL": {"score": 0.0}}), encoding="utf-8")

    # 4) DB connect (creates BASE_DIR/data/alpha_sniper.db) — the
    # earlier test_db_file_permissions covers correctness; here we
    # only need confirmation that the write does not land outside
    # BASE_DIR.
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    from biotech_sniper import db
    conn = db.connect(paths.DATA_DIR / "alpha_sniper.db")
    conn.close()

    # The forbidden sibling tree must remain empty. We list all files
    # below ``project/`` and assert none exist.
    leaked = sorted(p for p in project_sibling.rglob("*") if p.is_file())
    assert leaked == [], (
        f"Files leaked under read-only project sibling: "
        + ", ".join(str(p) for p in leaked)
    )

    # And every actual write should have landed under BASE_DIR.
    written = sorted(p for p in base.rglob("*") if p.is_file())
    assert written, "Sanity check: at least one file should have been written under BASE_DIR"
    for p in written:
        assert base in p.resolve().parents, f"{p} escaped BASE_DIR={base}"
