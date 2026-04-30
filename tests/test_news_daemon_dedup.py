"""Cross-restart dedup tests for ``biotech_sniper.news_daemon.emit`` (f-m2-06).

Pins the assertion IDs listed in ``features.json::fulfills``:

* VAL-M2-026 — daemon rides existing news_events composite dedup
  index — NO phantom column reference.  The package source contains
  zero ``news_events.dedup_key`` references.
* VAL-M2-027 — same news_events row never double-emits after
  ``kill -9`` + restart.  Simulated in-process by re-running
  :func:`run_one_poll_cycle` after a synthetic seed.
* VAL-M2-028 — daemon recovers durable state from SQLite.  The
  watermark comes from
  :func:`get_last_emitted_news_event_id` (i.e. ``MAX(source_news_event_id)``
  over ``candidate_events``), NOT from any module-level in-memory set.

The tests use a tmp_path SQLite db built via the migration runner
(v9 schema → v10 reading-B foundations) so the schema is identical
to production.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon import emit
from biotech_sniper.news_daemon.emit import (
    compute_dedup_key,
    get_last_emitted_news_event_id,
    make_candidate,
    run_one_poll_cycle,
    write_candidate,
    write_candidates,
)


_PACKAGE_DIR = Path(emit.__file__).parent


def _build_v10_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=10, take_backup_first=False)
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    yield _build_v10_db(tmp_path)


_NEWS_EVENT_COUNTER = 0


def _seed_news_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    title: str,
    source: str = "test_source",
    url: str | None = None,
    published_at: str | None = None,
) -> int:
    """Insert one news_events row with a unique URL by default."""

    global _NEWS_EVENT_COUNTER
    if url is None:
        _NEWS_EVENT_COUNTER += 1
        url = f"https://example.com/news/{_NEWS_EVENT_COUNTER}"
    cursor = conn.execute(
        "INSERT INTO news_events (ticker, source, published_at, title, url) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, source, published_at, title, url),
    )
    new_id = cursor.lastrowid
    assert new_id is not None
    return int(new_id)


# ---------------------------------------------------------------------------
# VAL-M2-026 — no phantom news_events.dedup_key reference
# ---------------------------------------------------------------------------


class TestNoPhantomDedupKeyVAL_M2_026:
    """Daemon rides existing news_events composite UNIQUE index."""

    def test_no_phantom_dedup_key_in_news_daemon_source(self) -> None:
        """``news_events.dedup_key`` literal is absent from the package."""

        pattern = re.compile(r"news_events\s*\.\s*dedup_key", re.IGNORECASE)
        offenders: list[Path] = []
        for source in _PACKAGE_DIR.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(source)
        assert offenders == [], (
            f"news_daemon source MUST NOT reference news_events.dedup_key: "
            f"{offenders}"
        )

    def test_news_events_table_has_no_dedup_key_column(
        self, v10_db: Path
    ) -> None:
        """Schema confirms no ``news_events.dedup_key`` column exists."""

        conn = sqlite3.connect(v10_db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(news_events)")
            }
        finally:
            conn.close()
        assert "dedup_key" not in cols, (
            "news_events.dedup_key column must NOT exist (composite "
            "UNIQUE index idx_news_events_dedup is the source of truth)"
        )

    def test_news_events_composite_dedup_index_exists(
        self, v10_db: Path
    ) -> None:
        """The existing composite UNIQUE INDEX is what the daemon rides."""

        conn = sqlite3.connect(v10_db)
        try:
            indexes = list(
                conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='news_events'"
                )
            )
        finally:
            conn.close()
        names = {row[0] for row in indexes}
        assert "idx_news_events_dedup" in names

        ddl = next(
            row[1] for row in indexes if row[0] == "idx_news_events_dedup"
        )
        upper = ddl.upper()
        assert "UNIQUE" in upper
        # Composite over the four canonical fields.
        assert "TICKER" in upper
        assert "SOURCE" in upper
        assert "URL" in upper
        assert "PUBLISHED_AT" in upper

    def test_emit_uses_news_events_id_cursor(self) -> None:
        """The cursor uses ``news_events.id > watermark``, NOT a dedup_key."""

        text = (_PACKAGE_DIR / "emit.py").read_text(encoding="utf-8")
        # The cursor is keyed off ``id > ?``.
        assert "id > ?" in text or "id >\n" in text or re.search(
            r"id\s*>\s*\?", text
        ), text[:400]


# ---------------------------------------------------------------------------
# VAL-M2-027 — same news_events row never double-emits after kill -9
# ---------------------------------------------------------------------------


class TestNoDoubleEmitAfterRestartVAL_M2_027:
    """Restart simulation: re-run cycle after seeding, expect 1 candidate."""

    def test_re_run_after_seed_emits_exactly_one(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(
                conn, ticker="VRTX", title="VRTX PDUFA approval"
            )
            conn.commit()
        finally:
            conn.close()

        # First cycle commits the candidate.
        run_one_poll_cycle(str(v10_db))
        # Simulate kill -9 + restart by simply re-invoking
        # :func:`run_one_poll_cycle` — it must source the watermark
        # from SQLite, NOT from in-memory state.
        run_one_poll_cycle(str(v10_db))

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, (
            "after restart simulation, the same news_events row must "
            "produce AT MOST one candidate_events row"
        )

    def test_dedup_key_unique_blocks_re_emit_even_with_zero_watermark(
        self, v10_db: Path
    ) -> None:
        """Even if the watermark is reset to 0, the UNIQUE constraint holds.

        This pins the durability anchor: the cross-restart guarantee
        does NOT depend on the watermark alone — the
        ``candidate_events.dedup_key`` UNIQUE constraint is the
        primary protection.  An attacker who tampers with the
        watermark (e.g. by deleting state) cannot induce double
        emission because the dedup_key is recomputed identically and
        the second insert is silently swallowed by ``INSERT OR IGNORE``.
        """

        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(
                conn, ticker="VRTX", title="VRTX FDA approval received PDUFA"
            )
            conn.commit()
        finally:
            conn.close()

        # First cycle: write a candidate.
        run_one_poll_cycle(str(v10_db))
        # Force the cycle to scan from id=0 again (as if the
        # watermark had been corrupted to zero).
        run_one_poll_cycle(str(v10_db), after_id=0)

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_subprocess_restart_simulation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: spawn a subprocess to re-emit after a fresh import.

        This simulates kill -9 + systemd restart more faithfully than
        an in-process re-call: a new Python interpreter is started,
        all modules are freshly imported (no in-memory caches survive),
        and the watermark must come from SQLite.
        """

        db_path = _build_v10_db(tmp_path)
        # Seed one news_events row.
        conn = sqlite3.connect(db_path)
        try:
            _seed_news_event(
                conn, ticker="VRTX", title="VRTX PDUFA approval"
            )
            conn.commit()
        finally:
            conn.close()

        # Helper script: import + run one cycle.
        script = (
            "from biotech_sniper.news_daemon.emit import run_one_poll_cycle\n"
            f"scanned, inserted = run_one_poll_cycle(r'{db_path}')\n"
            "print(f'scanned={scanned} inserted={inserted}')\n"
        )

        repo_root = Path(emit.__file__).resolve().parents[2]

        # First subprocess: emits the candidate.
        result1 = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "inserted=1" in result1.stdout

        # Second subprocess: fresh interpreter, no in-memory state.
        # MUST read watermark from SQLite and skip the seeded row.
        result2 = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "inserted=0" in result2.stdout, result2.stdout

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# VAL-M2-028 — daemon recovers state from SQLite, NOT from in-memory.
# ---------------------------------------------------------------------------


class TestStartupReadsWatermarkFromDbVAL_M2_028:
    """Watermark recovery is rooted in SQLite state, not Python state."""

    def test_watermark_helper_returns_max_source_news_event_id(
        self, v10_db: Path
    ) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            ids = [
                _seed_news_event(conn, ticker=f"T{i}", title="approval")
                for i in range(3)
            ]
            conn.commit()
        finally:
            conn.close()

        cands = [
            make_candidate(f"T{i}", nid, ["approval"])
            for i, nid in enumerate(ids)
        ]
        write_candidates(str(v10_db), cands)
        assert get_last_emitted_news_event_id(str(v10_db)) == max(ids)

    def test_no_module_level_seen_set_in_package(self) -> None:
        """No module-level ``seen_ids`` / ``_SEEN`` / set-based dedup state."""

        pattern = re.compile(
            r"^\s*(seen_ids|_SEEN|_seen_ids|SEEN_IDS|_emitted_ids|EMITTED_IDS)"
            r"\s*[:=]",
            re.MULTILINE,
        )
        offenders: list[Path] = []
        for source in _PACKAGE_DIR.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(source)
        assert offenders == [], (
            f"news_daemon must rely on candidate_events.dedup_key UNIQUE, "
            f"NOT module-level dedup state: {offenders}"
        )

    def test_run_one_poll_cycle_reads_watermark_from_db_each_call(
        self, v10_db: Path
    ) -> None:
        """The watermark is recomputed from SQLite on every cycle."""

        conn = sqlite3.connect(v10_db)
        try:
            id1 = _seed_news_event(conn, ticker="A", title="A PDUFA decision")
            conn.commit()
        finally:
            conn.close()

        run_one_poll_cycle(str(v10_db))
        # After cycle 1, watermark = id1.
        assert get_last_emitted_news_event_id(str(v10_db)) == id1

        conn = sqlite3.connect(v10_db)
        try:
            id2 = _seed_news_event(conn, ticker="B", title="B PDUFA decision")
            conn.commit()
        finally:
            conn.close()

        run_one_poll_cycle(str(v10_db))
        # After cycle 2, watermark must advance to id2 (recovered from DB).
        assert get_last_emitted_news_event_id(str(v10_db)) == id2


# ---------------------------------------------------------------------------
# Pipe-injection resistance under realistic poll cycles
# ---------------------------------------------------------------------------


class TestPipeInjectionResistanceUnderEmit:
    """End-to-end pipe-injection resistance via the real writer path."""

    def test_two_keyword_sets_with_pipe_collide_distinctly(
        self, v10_db: Path
    ) -> None:
        """``["a|b"]`` and ``["a", "b"]`` produce distinct dedup_keys."""

        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(
                conn, ticker="VRTX", title="approval"
            )
            conn.commit()
        finally:
            conn.close()

        cand_pipe = make_candidate("VRTX", news_id, ["a|b"])
        cand_split = make_candidate("VRTX", news_id, ["a", "b"])
        assert cand_pipe.dedup_key != cand_split.dedup_key

        # Both can coexist.
        assert write_candidate(str(v10_db), cand_pipe) is True
        assert write_candidate(str(v10_db), cand_split) is True

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_dedup_key_field_separator_is_unit_separator(self) -> None:
        """A direct probe: rebuild the key with explicit \\x1f separator."""

        # If someone refactors compute_dedup_key to switch to ``|``
        # this assertion fires.
        ticker, eid = "VRTX", 42
        kws = ["pdufa", "approval"]
        sorted_csv = ",".join(sorted(kws))
        actual = compute_dedup_key(ticker, eid, kws)
        import hashlib

        with_us = hashlib.sha256(
            f"{ticker}\x1f{eid}\x1f{sorted_csv}".encode("utf-8")
        ).hexdigest()
        with_pipe = hashlib.sha256(
            f"{ticker}|{eid}|{sorted_csv}".encode("utf-8")
        ).hexdigest()

        assert actual == with_us
        assert actual != with_pipe
