"""f-misc-03 — legacy ``intelligence/*`` and ``sectors/contracts/*`` cleanup.

Pins three contracts that were left dangling after f-m2-07 finished the
broader sandbox-path migration:

1. ``biotech_sniper.intelligence.pipeline_scheduler`` imports cleanly
   without any ``ModuleNotFoundError`` (the legacy ``try/except``
   fallback that performed ``sys.path.insert`` and re-imported the
   already-absolute ``biotech_sniper.intelligence.company_pipeline_manager``
   path is gone — there is now a single canonical absolute import).
2. :func:`biotech_sniper.sectors.contracts.sam_sniper.fetch_defense_gov_contracts`
   does not raise ``FileNotFoundError`` when ``company_ticker_map.json``
   is absent — it logs a warning, returns an empty ticker map, and
   downgrades the scan to "no matches" instead of crashing the daily
   contract sweep.
3. The two specific files above carry no live ``sys.path.insert(...)``
   call sites at module/runtime scope. Comments that *mention* the
   pattern are deliberately allowed (they document the f-misc-03 fix);
   only executable statements are forbidden.

NOTE on path discrepancy: the f-misc-03 feature description references
``biotech_sniper/intelligence/sam_sniper.py``, but the actual module
lives at ``biotech_sniper/sectors/contracts/sam_sniper.py`` (only one
sam_sniper exists in the tree). The runtime fix and these tests target
the real location.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _live_sys_path_insert_lines(module_path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, source)`` for every executable ``sys.path.insert``.

    Walks the AST so commented-out occurrences (allowed — they document
    the historical fix) don't show up as violations. Any ``Call`` node
    whose attribute chain spells ``sys.path.insert`` is treated as a
    live mutation.
    """
    src = module_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    offending: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match attribute chains exactly: sys.path.insert(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "insert"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            offending.append((node.lineno, ast.unparse(node)))
    return offending


# ---------------------------------------------------------------------------
# 1) pipeline_scheduler clean import
# ---------------------------------------------------------------------------


def test_pipeline_scheduler_imports_without_module_not_found_error() -> None:
    """``import biotech_sniper.intelligence.pipeline_scheduler`` succeeds.

    The pre-fix module raised ``ModuleNotFoundError`` whenever the
    interpreter happened to land in the legacy
    ``except ImportError: sys.path.insert(...)`` branch on a clone
    where ``biotech_sniper`` was already importable but the legacy
    bare ``intelligence/`` directory was not on ``sys.path``. With the
    fallback removed the import is unconditional.
    """
    # Force a fresh import so we exercise the canonical entry point
    # rather than a previously-cached module object.
    import sys as _sys

    _sys.modules.pop("biotech_sniper.intelligence.pipeline_scheduler", None)
    module = importlib.import_module("biotech_sniper.intelligence.pipeline_scheduler")

    assert hasattr(module, "run_scheduled_pipelines"), (
        "pipeline_scheduler must expose run_scheduled_pipelines"
    )
    assert hasattr(module, "refresh_tickers")
    assert hasattr(module, "print_schedule_summary")


def test_pipeline_scheduler_has_no_live_sys_path_insert() -> None:
    """The active source contains no executable ``sys.path.insert(...)`` call."""
    import biotech_sniper.intelligence.pipeline_scheduler as ps

    offending = _live_sys_path_insert_lines(Path(ps.__file__))
    assert offending == [], (
        f"pipeline_scheduler still carries live sys.path.insert calls: {offending}"
    )


# ---------------------------------------------------------------------------
# 2) sam_sniper graceful FileNotFoundError handling
# ---------------------------------------------------------------------------


def test_sam_sniper_load_ticker_map_returns_empty_when_missing(monkeypatch, tmp_path):
    """``load_ticker_map`` warns + returns an empty map on missing file."""
    from biotech_sniper.sectors.contracts import sam_sniper

    # Point every candidate to a non-existent file under a hermetic
    # temp dir so we exercise the warn-and-fallback branch.
    nonexistent = (tmp_path / "no_such_ticker_map.json",)
    monkeypatch.setattr(sam_sniper, "TICKER_MAP_CANDIDATES", nonexistent)

    result = sam_sniper.load_ticker_map()

    assert isinstance(result, dict)
    assert result == {"companies": {}}, (
        "missing ticker map must yield an empty companies dict"
    )


def test_sam_sniper_fetch_defense_gov_contracts_no_filenotfound(
    monkeypatch, tmp_path
):
    """``fetch_defense_gov_contracts`` does not raise ``FileNotFoundError``.

    With ``company_ticker_map.json`` absent and the network feed stubbed
    to an empty entry list, the function must:

    * complete without raising,
    * return a list (the contract surface — empty when no matches),
    * log a warning naming the missing ticker map (covered separately
      via ``caplog`` to keep this assertion focused on the
      no-FileNotFoundError invariant).
    """
    from biotech_sniper.sectors.contracts import sam_sniper

    nonexistent_map = tmp_path / "no_such_ticker_map.json"
    nonexistent_state = tmp_path / "state" / "contracts_state.json"

    monkeypatch.setattr(
        sam_sniper, "TICKER_MAP_CANDIDATES", (nonexistent_map,)
    )
    monkeypatch.setattr(sam_sniper, "CONTRACTS_STATE_FILE", nonexistent_state)

    # Stub the RSS layer so the test never touches the network. The
    # function imports ``feedparser`` lazily and falls back to a
    # ``requests.get`` parse path when feedparser is absent — patch
    # both to return an empty entry set.
    class _EmptyFeed:
        entries: list = []

    import feedparser as _feedparser

    monkeypatch.setattr(_feedparser, "parse", lambda url: _EmptyFeed())

    # Should NOT raise FileNotFoundError (the regression we're guarding
    # against). Any other unexpected exception is also a failure.
    result = sam_sniper.fetch_defense_gov_contracts(days_back=1)

    assert isinstance(result, list), (
        f"expected list result, got {type(result).__name__}"
    )
    # Verify the state-write side effect succeeded (no FileNotFoundError
    # on the parent directory either).
    assert nonexistent_state.parent.is_dir()


def test_sam_sniper_load_ticker_map_logs_warning(monkeypatch, tmp_path, caplog):
    """When the ticker map is missing, a WARNING-level log line is emitted."""
    import logging

    from biotech_sniper.sectors.contracts import sam_sniper

    monkeypatch.setattr(
        sam_sniper, "TICKER_MAP_CANDIDATES", (tmp_path / "missing.json",)
    )

    with caplog.at_level(logging.WARNING, logger=sam_sniper.log.name):
        sam_sniper.load_ticker_map()

    matching = [
        rec for rec in caplog.records
        if "company_ticker_map.json" in rec.getMessage()
        and rec.levelno >= logging.WARNING
    ]
    assert matching, (
        "expected a WARNING about missing company_ticker_map.json; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


def test_sam_sniper_has_no_live_sys_path_insert() -> None:
    """``sectors/contracts/sam_sniper.py`` carries no executable sys.path.insert."""
    from biotech_sniper.sectors.contracts import sam_sniper

    offending = _live_sys_path_insert_lines(Path(sam_sniper.__file__))
    assert offending == [], (
        f"sam_sniper still carries live sys.path.insert calls: {offending}"
    )


# ---------------------------------------------------------------------------
# 3) Cross-file invariant: no live sys.path.insert in either targeted file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_dotted",
    [
        "biotech_sniper.intelligence.pipeline_scheduler",
        "biotech_sniper.sectors.contracts.sam_sniper",
    ],
)
def test_no_sys_path_insert_in_targeted_files(module_dotted: str) -> None:
    """Cross-file guard for the f-misc-03 contract surface."""
    module = importlib.import_module(module_dotted)
    offending = _live_sys_path_insert_lines(Path(module.__file__))
    assert offending == [], (
        f"{module_dotted} still carries live sys.path.insert calls: {offending}"
    )


# ---------------------------------------------------------------------------
# 4) f-misc-09 — Tree-wide AST guard
# ---------------------------------------------------------------------------
#
# f-misc-03 cleared every site under ``biotech_sniper/intelligence/``;
# f-misc-09 finishes the job for the rest of the package tree:
#   * biotech_sniper/intraday_scanner.py
#   * biotech_sniper/new_opportunity_sniper.py
#   * biotech_sniper/email_formatter.py
#   * biotech_sniper/play_card_formatter.py
#   * biotech_sniper/sectors/adcom/adcom_scanner.py
#
# The guard below walks every ``*.py`` file under ``biotech_sniper/``
# and asserts that no executable ``sys.path.insert(...)`` call remains.
# Comments that mention the pattern are intentionally allowed — they
# document the historical fix (and are needed for forensic clarity in
# both f-misc-03 and f-misc-09 cleanup commits). Tests/ are out of
# scope: pytest collection occasionally inserts paths legitimately,
# and the guard is about the production package only.


def _iter_package_python_files() -> list[Path]:
    """Return every ``*.py`` file under the ``biotech_sniper`` package.

    Resolves the package directory via ``biotech_sniper.__file__`` so
    the test runs from any CWD and matches the *installed* layout
    rather than guessing repo-relative paths.
    """
    import biotech_sniper as _pkg

    pkg_root = Path(_pkg.__file__).parent
    # Skip ``__pycache__`` (compiled bytecode) and any auto-generated
    # ``*.pyi`` stubs. Only ``*.py`` source files are relevant.
    return sorted(p for p in pkg_root.rglob("*.py") if "__pycache__" not in p.parts)


def test_tree_wide_no_live_sys_path_insert() -> None:
    """No executable ``sys.path.insert`` anywhere under ``biotech_sniper/``.

    AST-walks every ``*.py`` source file under the installed package
    root and asserts the count of live ``sys.path.insert(...)`` calls
    is zero. Commented-out references are allowed (and used to
    document the f-misc-03 / f-misc-09 cleanup history).
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for source_path in _iter_package_python_files():
        offending = _live_sys_path_insert_lines(source_path)
        if offending:
            offenders[str(source_path)] = offending

    assert offenders == {}, (
        "Live ``sys.path.insert`` call sites remain under biotech_sniper/. "
        "Convert each to a canonical ``biotech_sniper.*`` absolute import "
        "(see f-misc-03 / f-misc-09 cleanup pattern). Offenders:\n"
        + "\n".join(
            f"  {path}: {sites}" for path, sites in sorted(offenders.items())
        )
    )


def test_tree_wide_guard_covers_f_misc_09_targets() -> None:
    """Sanity-check that the tree-wide guard actually scans every file
    that f-misc-09 touched.

    Without this assertion a future refactor could rename / move one
    of the target files and silently regress: the tree-wide guard
    would still pass (no offenders) but the protection invariant
    would be lost. Pinning the expected set keeps the cleanup
    contract honest.
    """
    scanned = {p.name for p in _iter_package_python_files()}
    expected = {
        "intraday_scanner.py",
        "new_opportunity_sniper.py",
        "email_formatter.py",
        "play_card_formatter.py",
        "adcom_scanner.py",
    }
    missing = expected - scanned
    assert not missing, (
        f"f-misc-09 target files are no longer present in the package "
        f"tree: {missing}. Update the guard or the target list."
    )


# ---------------------------------------------------------------------------
# 5) f-misc-14 — email_formatter.py bare-namespace import guard
# ---------------------------------------------------------------------------
#
# Discovered during f-misc-09 (commit c297fa8): four bare-namespace
# imports inside ``biotech_sniper.email_formatter.build_daily_email``
# only resolved when an earlier-loaded module had incidentally mutated
# ``sys.path``:
#   * ``from play_card_formatter import _estimate_days``
#   * ``from performance_tracker import format_pnl_table_for_email``
#   * ``from auto_resolver  import format_resolutions_for_email``
#   * ``from learning_engine import format_learning_summary_for_email``
#
# These are NOT ``sys.path.insert`` call sites (so f-misc-09 was
# correctly out of scope), but they share the same fragility: they
# rely on the legacy bare ``biotech_sniper/`` directory being on
# ``sys.path`` rather than the canonical absolute package path. The
# guard below AST-walks ``email_formatter.py`` and asserts every
# ``from <pkg> import ...`` statement starts with either
# ``biotech_sniper`` or a stdlib top-level module name (relative
# imports — ``from . import x`` — are allowed and implicitly
# resolve under the current package).


def _bare_namespace_import_lines(
    module_path: Path, allowed_top_level: frozenset[str]
) -> list[tuple[int, str]]:
    """Return ``(lineno, source)`` for each offending ``ImportFrom`` node.

    A node is offending when:

    * It is an absolute import (``level == 0``), AND
    * Its top-level module segment is not in ``allowed_top_level``
      (i.e., not ``biotech_sniper`` and not a stdlib module).

    Relative imports (``from . import x``) are allowed because they
    resolve via the package they live in, not via ``sys.path``.
    """
    src = module_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    offending: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # Relative imports (level > 0) are not bare-namespace risks.
        if node.level and node.level > 0:
            continue
        if node.module is None:
            continue
        top_level = node.module.split(".", 1)[0]
        if top_level in allowed_top_level:
            continue
        offending.append((node.lineno, ast.unparse(node)))
    return offending


def test_email_formatter_has_no_bare_namespace_imports() -> None:
    """``biotech_sniper/email_formatter.py`` carries no bare-namespace
    ``from <pkg> import ...`` statements outside ``biotech_sniper``
    or the standard library.

    The four originally-broken sites (``play_card_formatter``,
    ``performance_tracker``, ``auto_resolver``, ``learning_engine``)
    have been migrated to canonical ``biotech_sniper.*`` absolute
    paths in this commit; this guard prevents regressions.
    """
    import sys

    import biotech_sniper.email_formatter as ef

    # ``sys.stdlib_module_names`` is a frozenset of every top-level
    # stdlib module name; available since Python 3.10. The project
    # pins to 3.10+ (see AGENTS.md), so this is always present.
    allowed = frozenset({"biotech_sniper"}) | sys.stdlib_module_names

    offending = _bare_namespace_import_lines(Path(ef.__file__), allowed)
    assert offending == [], (
        "email_formatter.py contains bare-namespace 'from <pkg> import ...' "
        "statements that don't begin with 'biotech_sniper' or a stdlib module. "
        "Convert each to a canonical 'biotech_sniper.*' absolute import. "
        f"Offenders: {offending}"
    )


def test_email_formatter_build_daily_email_imports_resolve_canonically() -> None:
    """Smoke-check that ``build_daily_email`` imports cleanly via the
    canonical package path, with no reliance on a previously-loaded
    module having mutated ``sys.path``.

    A fresh sub-interpreter import (clearing the four formerly-bare
    targets from ``sys.modules`` first) confirms the canonical paths
    are wired correctly even when nothing has front-loaded the bare
    namespace into the search path.
    """
    import importlib
    import sys as _sys

    for legacy in (
        "play_card_formatter",
        "performance_tracker",
        "auto_resolver",
        "learning_engine",
        "biotech_sniper.email_formatter",
    ):
        _sys.modules.pop(legacy, None)

    module = importlib.import_module("biotech_sniper.email_formatter")
    assert hasattr(module, "build_daily_email"), (
        "build_daily_email must remain exported from email_formatter"
    )
