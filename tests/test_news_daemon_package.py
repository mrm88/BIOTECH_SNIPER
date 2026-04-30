"""Behavioural tests for the f-m2-01 news_daemon package skeleton.

These tests pin the contract for VAL-M2-001 / VAL-M2-002 / VAL-M2-003
and the f-m2-01 expectedBehavior list:

* Package directory contains every required submodule.
* ``python -m biotech_sniper.news_daemon --help`` exits 0 and the
  usage string mentions ``--once``, ``--poll-seconds``, ``--db``.
* ``python -m biotech_sniper.news_daemon --dry-run`` exits 0
  cleanly without DB writes or network egress.
* ``import biotech_sniper.news_daemon`` (and every submodule)
  pulls in zero modules whose name contains ``llm``, no
  Stage-2-provider SDK (``openai``, ``anthropic``,
  ``google.generativeai``), and no non-blocking I/O stack
  (the synchronous-only transport contract is enforced by source
  grep — see :func:`test_no_forbidden_substrings_in_source`).
* No module-level dedup-cache set lives inside the package
  (also grep-verified to keep cross-restart dedup honest).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import biotech_sniper.news_daemon  # noqa: F401  (smoke import for the package)
from biotech_sniper.news_daemon import poll_loop


# ---------------------------------------------------------------------------
# Package layout (VAL-M2-001)
# ---------------------------------------------------------------------------


PACKAGE_DIR = Path(biotech_sniper.news_daemon.__file__).parent

#: Submodules required by the f-m2-01 feature description.  ``emitter.py``
#: is the alias-name required by VAL-M2-001 / VAL-M2-003; the canonical
#: implementation lives in ``emit.py`` per the architecture documents.
REQUIRED_SUBMODULES: tuple[str, ...] = (
    "__init__.py",
    "__main__.py",
    "poll_loop.py",
    "scope.py",
    "matcher.py",
    "emit.py",
    "emitter.py",
    "heartbeat.py",
    "log.py",
)


@pytest.mark.parametrize("filename", REQUIRED_SUBMODULES)
def test_required_submodule_exists(filename: str) -> None:
    """Every required submodule lives under ``biotech_sniper/news_daemon/``."""

    path = PACKAGE_DIR / filename
    assert path.is_file(), f"missing required submodule: {path}"


def test_package_dir_resolves_under_biotech_sniper() -> None:
    """The package lives at ``biotech_sniper/news_daemon/`` (not nested elsewhere)."""

    assert PACKAGE_DIR.parent.name == "biotech_sniper"
    assert PACKAGE_DIR.name == "news_daemon"


def test_no_duplicate_poll_loop_outside_package() -> None:
    """No other source file in ``biotech_sniper/`` defines a poll loop.

    Pins the VAL-M2-001 sub-clause: ``grep -RIn 'def poll_loop\\|def
    run_poll' biotech_sniper/ --include='*.py' | grep -v 'news_daemon/'``
    returns empty.
    """

    repo_biotech_sniper = PACKAGE_DIR.parent
    pattern = re.compile(r"def\s+(poll_loop|run_poll)\b")

    offenders: list[Path] = []
    for source in repo_biotech_sniper.rglob("*.py"):
        if "news_daemon" in source.parts:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        if pattern.search(text):
            offenders.append(source)

    assert offenders == [], f"unexpected poll-loop definitions: {offenders}"


# ---------------------------------------------------------------------------
# Module-level dedup-cache forbidden (feature description invariant)
# ---------------------------------------------------------------------------


def test_no_module_level_dedup_cache_set() -> None:
    """The package contains no module-level ``seen_ids`` / ``_SEEN`` set.

    Cross-restart dedup MUST be sourced from the
    ``candidate_events.dedup_key`` UNIQUE constraint — module-level
    in-memory state would silently degrade dedup on SIGKILL +
    restart.  Pinned by a literal grep over the package source.
    """

    pattern = re.compile(r"\b(seen_ids|_SEEN)\b")
    offenders: list[Path] = []
    for source in PACKAGE_DIR.rglob("*.py"):
        text = source.read_text(encoding="utf-8", errors="replace")
        if pattern.search(text):
            offenders.append(source)

    assert offenders == [], (
        "module-level dedup-cache state is forbidden — see "
        "biotech_sniper/news_daemon/__init__.py for the contract: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# Forbidden imports / substrings — sync stack only, no LLM glue
# ---------------------------------------------------------------------------


_FORBIDDEN_TRANSPORT_RE = re.compile(r"\b(aiohttp|httpx|asyncio)\b")
_FORBIDDEN_LLM_RE = re.compile(
    r"biotech_sniper\.llm"
    r"|api\.perplexity\.ai"
    r"|api\.x\.ai"
    r"|api\.anthropic\.com"
    r"|generativelanguage\.googleapis\.com"
)


def test_no_forbidden_substrings_in_source() -> None:
    """Package source contains no banned transport names or scoring URLs."""

    transport_offenders: list[tuple[Path, str]] = []
    llm_offenders: list[tuple[Path, str]] = []

    for source in PACKAGE_DIR.rglob("*.py"):
        text = source.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_TRANSPORT_RE.search(line):
                transport_offenders.append((source, f"L{line_no}: {line}"))
            if _FORBIDDEN_LLM_RE.search(line):
                llm_offenders.append((source, f"L{line_no}: {line}"))

    assert transport_offenders == [], (
        f"forbidden synchronous-stack violation: {transport_offenders}"
    )
    assert llm_offenders == [], (
        f"forbidden Stage-2-provider reference: {llm_offenders}"
    )


# ---------------------------------------------------------------------------
# Import smoke — no transitive Stage-2 / provider imports (VAL-M2-003)
# ---------------------------------------------------------------------------


def test_import_pulls_no_llm_modules() -> None:
    """Importing the package loads zero modules whose name contains ``llm``.

    Mirrors the f-m2-01 verification step::

        .venv/bin/python -c 'import biotech_sniper.news_daemon; \
            import sys; assert all("llm" not in m for m in sys.modules)'
    """

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import biotech_sniper.news_daemon\n"
                "import biotech_sniper.news_daemon.poll_loop\n"
                "import biotech_sniper.news_daemon.scope\n"
                "import biotech_sniper.news_daemon.matcher\n"
                "import biotech_sniper.news_daemon.emit\n"
                "import biotech_sniper.news_daemon.emitter\n"
                "import biotech_sniper.news_daemon.heartbeat\n"
                "import biotech_sniper.news_daemon.log\n"
                "leaks = [m for m in sys.modules if 'llm' in m]\n"
                "assert leaks == [], leaks\n"
                "stage2_provider_prefixes = ('openai', 'anthropic', "
                "'google.generativeai')\n"
                "providers = [m for m in sys.modules "
                "if m.startswith(stage2_provider_prefixes)]\n"
                "assert providers == [], providers\n"
                "transports = [m for m in sys.modules "
                "if m in ('aiohttp', 'httpx')]\n"
                "assert transports == [], transports\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"import smoke failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# CLI entrypoint (VAL-M2-002)
# ---------------------------------------------------------------------------


def test_module_help_lists_required_flags() -> None:
    """``python -m biotech_sniper.news_daemon --help`` mentions key flags."""

    proc = subprocess.run(
        [sys.executable, "-m", "biotech_sniper.news_daemon", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    for flag in ("--once", "--poll-seconds", "--db", "--dry-run"):
        assert flag in proc.stdout, (
            f"--help output missing required flag {flag!r}; "
            f"stdout={proc.stdout!r}"
        )


def test_module_dry_run_exits_zero() -> None:
    """``python -m biotech_sniper.news_daemon --dry-run`` exits 0 cleanly."""

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "biotech_sniper.news_daemon",
            "--dry-run",
            "--max-cycles",
            "3",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"dry-run exit nonzero: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_build_parser_advertises_dry_run_and_max_cycles() -> None:
    """The argparse parser exposes ``--dry-run`` and ``--max-cycles``."""

    parser = poll_loop.build_parser()
    args = parser.parse_args(
        ["--dry-run", "--max-cycles", "5", "--poll-seconds", "30"]
    )
    assert args.dry_run is True
    assert args.max_cycles == 5
    assert args.poll_seconds == 30


def test_main_dry_run_returns_zero() -> None:
    """:func:`poll_loop.main` returns 0 under ``--dry-run``."""

    rc = poll_loop.main(["--dry-run", "--max-cycles", "1"])
    assert rc == 0


def test_main_non_dry_run_refuses_until_loop_lands() -> None:
    """Skeleton refuses real loop until f-m2-03..f-m2-09 land.

    Documents the f-m2-01 contract that the package skeleton stops
    short of running a real loop; subsequent M2 features fill in
    poll-cadence resolution, scope filter, matcher, emit, heartbeat,
    and resilience handling.  The ``NotImplementedError`` ensures
    callers cannot accidentally exercise the unfinished body
    (which would silently no-op before the wiring lands).
    """

    with pytest.raises(NotImplementedError):
        poll_loop.main([])


# ---------------------------------------------------------------------------
# Public surface — sibling submodules exist with documented symbols
# ---------------------------------------------------------------------------


def test_public_surface_present() -> None:
    """Each submodule exports the f-m2-01 contract symbols.

    The bodies are skeleton stubs filled in by f-m2-03..f-m2-09;
    the symbols themselves are pinned here so subsequent features
    cannot accidentally rename them and break sibling imports.
    """

    from biotech_sniper.news_daemon import emit, heartbeat, matcher, scope, log

    # poll_loop
    assert hasattr(poll_loop, "build_parser")
    assert hasattr(poll_loop, "main")
    assert poll_loop.DEFAULT_POLL_SECONDS == 30
    assert poll_loop.MIN_POLL_SECONDS == 15
    assert poll_loop.MAX_POLL_SECONDS == 90

    # scope
    assert "watch" in scope.ALLOWED_TIERS
    assert "tradeable" in scope.ALLOWED_TIERS
    assert callable(scope.filter_universe)
    assert callable(scope.load_polled_universe)

    # matcher
    assert hasattr(matcher, "MatchResult")
    result = matcher.match_news_row("ABCD", "headline", "body")
    assert isinstance(result, matcher.MatchResult)
    assert result.is_match is False  # skeleton returns no-match

    # emit (canonical) + emitter (alias)
    from biotech_sniper.news_daemon import emitter

    assert emit.FIELD_SEPARATOR == "\x1f"
    assert emitter.FIELD_SEPARATOR == "\x1f"
    assert emit.compute_dedup_key is emitter.compute_dedup_key
    assert emit.CandidateEvent is emitter.CandidateEvent

    # heartbeat
    assert callable(heartbeat.write_heartbeat)
    assert callable(heartbeat.default_heartbeat_path)
    assert hasattr(heartbeat, "Heartbeat")

    # log
    assert log.MAX_LINE_BYTES == 4 * 1024
    assert callable(log.get_news_logger)
    assert callable(log.truncate_field)


def test_dedup_key_uses_unit_separator_and_sorts_keywords() -> None:
    """``compute_dedup_key`` uses ASCII Unit Separator (\\x1f) and sorts kw."""

    from biotech_sniper.news_daemon import emit

    key_unsorted = emit.compute_dedup_key("ABCD", 42, ["pdufa", "approval"])
    key_sorted = emit.compute_dedup_key("ABCD", 42, ["approval", "pdufa"])
    assert key_unsorted == key_sorted, (
        "matched_keywords order must NOT affect dedup_key"
    )

    # Different ticker → different key
    key_other = emit.compute_dedup_key("XYZA", 42, ["approval", "pdufa"])
    assert key_other != key_sorted

    # Different news_event_id → different key
    key_other_event = emit.compute_dedup_key("ABCD", 99, ["approval", "pdufa"])
    assert key_other_event != key_sorted


def test_dedup_key_resists_pipe_injection() -> None:
    """Pipe characters in keywords cannot collide with the field separator.

    The ``|`` character is NOT a field separator — the dedup_key
    formula uses ``\\x1f``.  This test pins that an attacker who
    crafts a keyword containing ``|`` cannot collide with another
    legitimate keyword combination.
    """

    from biotech_sniper.news_daemon import emit

    # Two distinct keyword sets that would collide under a naive
    # pipe-delimited concatenation: ``["a|b"]`` vs ``["a", "b"]``.
    key_pipe = emit.compute_dedup_key("ABCD", 1, ["a|b"])
    key_split = emit.compute_dedup_key("ABCD", 1, ["a", "b"])
    assert key_pipe != key_split


def test_filter_universe_rejects_empty_and_whitespace_tickers() -> None:
    """Scope filter silently drops empty / whitespace-only tickers."""

    from biotech_sniper.news_daemon import scope

    polled = {"ABCD", "EFGH"}
    out = scope.filter_universe(["ABCD", "", "  ", "EFGH", "ZZZZ"], polled)
    assert out == {"ABCD", "EFGH"}
