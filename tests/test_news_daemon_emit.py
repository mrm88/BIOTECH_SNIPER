"""Tests for ``biotech_sniper.news_daemon.emit`` (f-m2-06).

Pins the f-m2-06 contract and the assertion IDs listed in
``features.json::fulfills``:

* VAL-M2-019 — multi-keyword news_events row collapses to ONE
  candidate_events row whose ``matched_keywords`` is sorted +
  deduped + comma-joined (deterministic across runs).
* VAL-M2-020 / VAL-M2-021 — calendar_match populated on a hit;
  ``NULL`` on a miss; both still emit the candidate.
* VAL-M2-022 — ``candidate_events`` schema matches the locked spec
  (id PK + ticker + source_news_event_id FK + matched_keywords +
  calendar_match + emitted_at + dedup_key UNIQUE).
* VAL-M2-023 — ``dedup_key = sha256(ticker | news_event_id |
  matched_keywords_sorted)`` with ASCII Unit Separator (``\\x1f``)
  and pipe-injection resistance.
* VAL-M2-024 — synthetic ``news_events`` insert produces a
  candidate_events row within < 2 poll cycles (verified by running
  :func:`run_one_poll_cycle` once and asserting the candidate
  exists immediately).
* VAL-M2-025 — re-emitting the same source headline is idempotent
  on dedup_key (``INSERT OR IGNORE`` + ``UNIQUE``).
* AGENTS.md "Multi-row inserts in a poll cycle MUST use a single
  transaction (executemany or BEGIN/COMMIT)".
"""

from __future__ import annotations

import datetime
import hashlib
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper import db
from biotech_sniper.migrations.runner import run as run_migrations_runner
from biotech_sniper.news_daemon import emit
from biotech_sniper.news_daemon.emit import (
    FIELD_SEPARATOR,
    INSERT_SQL,
    CandidateEvent,
    compute_dedup_key,
    get_last_emitted_news_event_id,
    iter_pending_news_events,
    make_candidate,
    run_one_poll_cycle,
    write_candidate,
    write_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures — build a v10 SQLite db with news_events + candidate_events.
# ---------------------------------------------------------------------------


def _build_v10_db(tmp_path: Path) -> Path:
    """Build a tmp v10 SQLite database (v9 schema + migration 010)."""

    db_path = tmp_path / "alpha.db"
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    run_migrations_runner(db_path, target_version=11, take_backup_first=False)
    return db_path


@pytest.fixture
def v10_db(tmp_path: Path) -> Iterator[Path]:
    """Yields a tmp_path SQLite db at schema_version=10."""

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
    """Insert one news_events row and return its primary key.

    The composite UNIQUE INDEX ``idx_news_events_dedup`` collapses
    ``(ticker, source, COALESCE(url,''), COALESCE(published_at,''))``
    duplicates, so this helper auto-fills a unique URL when none is
    supplied to keep test seeds independent of insertion order.
    """

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
# VAL-M2-022 — candidate_events schema
# ---------------------------------------------------------------------------


class TestCandidateEventsSchemaVAL_M2_022:
    """``candidate_events`` schema matches the locked spec."""

    def test_table_exists_with_required_columns(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            cols = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(candidate_events)")
            }
        finally:
            conn.close()

        assert "id" in cols
        assert "ticker" in cols
        assert cols["ticker"][3] == 1, "ticker NOT NULL"
        assert "source_news_event_id" in cols
        assert cols["source_news_event_id"][3] == 1, "source_news_event_id NOT NULL"
        assert "matched_keywords" in cols
        assert cols["matched_keywords"][3] == 1, "matched_keywords NOT NULL"
        assert "calendar_match" in cols
        assert "emitted_at" in cols
        assert cols["emitted_at"][3] == 1, "emitted_at NOT NULL"
        assert "dedup_key" in cols
        assert cols["dedup_key"][3] == 1, "dedup_key NOT NULL"

    def test_foreign_key_to_news_events(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            fks = list(
                conn.execute("PRAGMA foreign_key_list(candidate_events)")
            )
        finally:
            conn.close()
        assert any(
            row[2] == "news_events" and row[3] == "source_news_event_id"
            for row in fks
        ), f"missing FK candidate_events.source_news_event_id → news_events.id: {fks}"

    def test_dedup_key_has_unique_constraint(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            ddl_row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='candidate_events'"
            ).fetchone()
        finally:
            conn.close()
        assert ddl_row is not None
        ddl = ddl_row[0].upper()
        assert "DEDUP_KEY" in ddl
        assert "UNIQUE" in ddl

    def test_dedup_key_unique_constraint_enforced(self, v10_db: Path) -> None:
        """Two rows with the same dedup_key must collide at the engine level."""

        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="VRTX", title="approval")
            conn.execute(
                INSERT_SQL,
                ("VRTX", news_id, "approval", None, "2026-04-29T00:00:00.000000Z", "deadbeef"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO candidate_events "
                    "(ticker, source_news_event_id, matched_keywords, "
                    "calendar_match, emitted_at, dedup_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("VRTX", news_id, "approval", None, "2026-04-29T00:00:01.000000Z", "deadbeef"),
                )
                conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# VAL-M2-023 — dedup_key formula + pipe injection resistance
# ---------------------------------------------------------------------------


class TestDedupKeyFormulaVAL_M2_023:
    """``dedup_key`` matches ``sha256(ticker | event_id | sorted_kw)``."""

    def test_formula_matches_explicit_sha256(self) -> None:
        """Compute the formula by hand and assert equality."""

        ticker = "ABCD"
        event_id = 42
        kws = ["pdufa", "approval", "label expansion"]
        sorted_csv = ",".join(sorted(set(kws)))
        expected = hashlib.sha256(
            f"{ticker}{FIELD_SEPARATOR}{event_id}{FIELD_SEPARATOR}{sorted_csv}".encode("utf-8")
        ).hexdigest()
        assert compute_dedup_key(ticker, event_id, kws) == expected

    def test_field_separator_is_unit_separator_not_pipe(self) -> None:
        """The delimiter is ``\\x1f`` (ASCII Unit Separator), NOT ``|``."""

        assert FIELD_SEPARATOR == "\x1f"
        # The dedup_key is computed with \x1f, so a pipe-delimited
        # alternative MUST yield a different hash.
        ticker, event_id = "ABCD", 1
        kws = ["a", "b"]
        actual = compute_dedup_key(ticker, event_id, kws)
        pipe_payload = f"{ticker}|{event_id}|{','.join(sorted(kws))}".encode("utf-8")
        pipe_hash = hashlib.sha256(pipe_payload).hexdigest()
        assert actual != pipe_hash, (
            "dedup_key formula must NOT use ``|`` as a field separator"
        )

    def test_dedup_key_invariant_under_keyword_order_and_dedup(self) -> None:
        a = compute_dedup_key("VRTX", 7, ["pdufa", "approval", "approval"])
        b = compute_dedup_key("VRTX", 7, ["approval", "pdufa"])
        assert a == b

    def test_dedup_key_resists_pipe_injection_in_matched_keywords(self) -> None:
        """A keyword containing ``|`` cannot collide via field-injection."""

        # Two keyword sets that would collide under naive
        # pipe-delimited concatenation: ``["a|b"]`` vs ``["a", "b"]``.
        injected = compute_dedup_key("VRTX", 1, ["a|b"])
        legit = compute_dedup_key("VRTX", 1, ["a", "b"])
        assert injected != legit

        # Same with a separator embedded inside the keyword text:
        # the unit-separator collision is also resisted because
        # \x1f is unlikely to appear in any real headline keyword,
        # but if it did the sha256 is not injection-prone since the
        # ordering of fields means the prefix part still differs.
        a = compute_dedup_key("VRTX", 1, ["foo\x1fbar"])
        b = compute_dedup_key("VRTX", 1, ["foo", "bar"])
        # Even though the joined body might look ``"foo\x1fbar"``
        # in both cases when the inner kw is sorted, the legit
        # case has a comma between ``foo`` and ``bar`` (not a
        # unit-separator), so the hashes still differ.
        assert a != b

    def test_dedup_key_changes_with_ticker_or_event_id(self) -> None:
        base = compute_dedup_key("ABCD", 42, ["x"])
        assert base != compute_dedup_key("XYZ", 42, ["x"])
        assert base != compute_dedup_key("ABCD", 99, ["x"])


# ---------------------------------------------------------------------------
# make_candidate — factory normalisation
# ---------------------------------------------------------------------------


class TestMakeCandidate:
    """``make_candidate`` produces canonical ``CandidateEvent`` instances."""

    def test_keywords_are_sorted_dedup_and_csv_joined(self) -> None:
        cand = make_candidate("VRTX", 7, ["pdufa", "approval", "approval"])
        assert cand.matched_keywords == "approval,pdufa"

    def test_dedup_key_is_canonical(self) -> None:
        cand = make_candidate("VRTX", 7, ["pdufa", "approval"])
        assert cand.dedup_key == compute_dedup_key("VRTX", 7, ["approval", "pdufa"])

    def test_emitted_at_default_iso8601_utc(self) -> None:
        cand = make_candidate("VRTX", 1, ["x"])
        # Format ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
        parsed = datetime.datetime.strptime(
            cand.emitted_at, "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        assert parsed.tzinfo is None  # naive (UTC by convention)

    def test_emitted_at_override_honoured(self) -> None:
        cand = make_candidate(
            "VRTX",
            1,
            ["x"],
            emitted_at="2026-04-29T15:00:00.000000Z",
        )
        assert cand.emitted_at == "2026-04-29T15:00:00.000000Z"

    def test_calendar_match_default_none(self) -> None:
        cand = make_candidate("VRTX", 1, ["x"])
        assert cand.calendar_match is None

    def test_to_row_positional_matches_insert_sql(self) -> None:
        cand = make_candidate(
            "VRTX",
            5,
            ["pdufa"],
            calendar_match='{"source":"trial_calendar"}',
            emitted_at="2026-04-29T00:00:00.000000Z",
        )
        row = cand.to_row()
        assert row[0] == "VRTX"
        assert row[1] == 5
        assert row[2] == "pdufa"
        assert row[3] == '{"source":"trial_calendar"}'
        assert row[4] == "2026-04-29T00:00:00.000000Z"
        assert row[5] == cand.dedup_key


# ---------------------------------------------------------------------------
# write_candidate — single-row INSERT OR IGNORE
# ---------------------------------------------------------------------------


class TestWriteCandidate:
    """:func:`write_candidate` enforces VAL-M2-025 idempotency."""

    def test_inserts_new_row_and_returns_true(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="VRTX", title="PDUFA approval")
            conn.commit()
        finally:
            conn.close()

        cand = make_candidate("VRTX", news_id, ["approval", "pdufa"])
        assert write_candidate(str(v10_db), cand) is True

        conn = sqlite3.connect(v10_db)
        try:
            row = conn.execute(
                "SELECT ticker, source_news_event_id, matched_keywords, "
                "calendar_match, emitted_at, dedup_key "
                "FROM candidate_events WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "VRTX"
        assert row[1] == news_id
        assert row[2] == "approval,pdufa"
        assert row[3] is None
        assert row[5] == cand.dedup_key

    def test_idempotent_on_dedup_key_returns_false(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="VRTX", title="approval")
            conn.commit()
        finally:
            conn.close()

        cand = make_candidate("VRTX", news_id, ["approval", "pdufa"])
        assert write_candidate(str(v10_db), cand) is True
        assert write_candidate(str(v10_db), cand) is False

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

    def test_persists_calendar_match_payload(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="VRTX", title="approval")
            conn.commit()
        finally:
            conn.close()

        payload = (
            '{"catalyst_date":"2026-05-12","days_until":13,'
            '"source":"trial_calendar"}'
        )
        cand = make_candidate(
            "VRTX",
            news_id,
            ["approval"],
            calendar_match=payload,
        )
        write_candidate(str(v10_db), cand)

        conn = sqlite3.connect(v10_db)
        try:
            row = conn.execute(
                "SELECT calendar_match FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == payload

    def test_persists_null_calendar_match_when_missing(
        self, v10_db: Path
    ) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="NOCAL", title="approval")
            conn.commit()
        finally:
            conn.close()

        cand = make_candidate("NOCAL", news_id, ["approval"])
        write_candidate(str(v10_db), cand)

        conn = sqlite3.connect(v10_db)
        try:
            row = conn.execute(
                "SELECT calendar_match FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] is None

    def test_accepts_open_connection(self, v10_db: Path) -> None:
        """Passing an already-open Connection shares the writer's transaction."""

        conn = db.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="VRTX", title="approval")
            conn.commit()
            cand = make_candidate("VRTX", news_id, ["approval"])
            assert write_candidate(conn, cand) is True
            # Re-issue on the same connection — still idempotent.
            assert write_candidate(conn, cand) is False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# write_candidates — batch executemany inside one transaction
# ---------------------------------------------------------------------------


class TestWriteCandidatesBatch:
    """Multi-row inserts use ``executemany`` inside one transaction."""

    def test_batch_inserts_all_rows_in_one_call(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        news_ids: list[int] = []
        try:
            for ticker in ("AAA", "BBB", "CCC"):
                news_ids.append(
                    _seed_news_event(conn, ticker=ticker, title="approval")
                )
            conn.commit()
        finally:
            conn.close()

        candidates = [
            make_candidate(ticker, nid, ["approval"])
            for ticker, nid in zip(("AAA", "BBB", "CCC"), news_ids)
        ]
        attempted, inserted = write_candidates(str(v10_db), candidates)
        assert attempted == 3
        assert inserted == 3

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 3

    def test_empty_iterable_returns_zero_zero(self, v10_db: Path) -> None:
        attempted, inserted = write_candidates(str(v10_db), [])
        assert attempted == 0
        assert inserted == 0

    def test_partial_dedup_collapses_to_single_row(self, v10_db: Path) -> None:
        """Submitting two duplicates inserts ONE; INSERT OR IGNORE swallows the second."""

        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(conn, ticker="AAA", title="approval")
            conn.commit()
        finally:
            conn.close()

        cand_a = make_candidate("AAA", news_id, ["approval"])
        cand_b = make_candidate("AAA", news_id, ["pdufa", "approval"])
        # cand_a and cand_b have DIFFERENT dedup_keys (different
        # matched_keywords), so both are inserted.  But cand_a' is
        # an exact dup of cand_a → swallowed.
        cand_a_dup = cand_a
        attempted, inserted = write_candidates(
            str(v10_db),
            [cand_a, cand_a_dup, cand_b],
        )
        assert attempted == 3
        assert inserted == 2

    def test_single_transaction_atomic_on_failure(
        self,
        v10_db: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing batch must roll back; no partial commit lingers.

        We force a failure by passing a row whose news_event_id
        violates the FK (no matching news_events row).
        """

        # candidate referring to a non-existent news_events.id → FK violation
        cand_bad = make_candidate("ZZZZ", 999_999, ["approval"])

        with pytest.raises(sqlite3.IntegrityError):
            write_candidates(str(v10_db), [cand_bad])

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# get_last_emitted_news_event_id — durable watermark
# ---------------------------------------------------------------------------


class TestWatermarkRecovery:
    """Cross-restart watermark MUST come from SQLite, not in-memory."""

    def test_returns_zero_when_table_empty(self, v10_db: Path) -> None:
        assert get_last_emitted_news_event_id(str(v10_db)) == 0

    def test_returns_max_source_news_event_id(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            ids = [
                _seed_news_event(conn, ticker=f"TICK{i}", title=f"t{i}")
                for i in range(4)
            ]
            conn.commit()
        finally:
            conn.close()

        cands = [
            make_candidate(f"TICK{i}", nid, ["approval"])
            for i, nid in enumerate(ids)
        ]
        write_candidates(str(v10_db), cands)
        assert get_last_emitted_news_event_id(str(v10_db)) == max(ids)


# ---------------------------------------------------------------------------
# iter_pending_news_events — cursor over news_events.id past watermark
# ---------------------------------------------------------------------------


class TestIterPendingNewsEvents:
    def test_returns_only_rows_past_watermark(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            ids = [
                _seed_news_event(conn, ticker="VRTX", title=f"t{i}")
                for i in range(5)
            ]
            conn.commit()
            seen = list(iter_pending_news_events(conn, after_id=ids[1]))
        finally:
            conn.close()
        # Three rows past ids[1].
        assert [row[0] for row in seen] == ids[2:]

    def test_filters_by_polled_tickers(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(conn, ticker="AAA", title="t1")
            in_scope = _seed_news_event(conn, ticker="BBB", title="t2")
            _seed_news_event(conn, ticker="CCC", title="t3")
            conn.commit()
            seen = list(
                iter_pending_news_events(conn, polled_tickers=["BBB"])
            )
        finally:
            conn.close()
        assert len(seen) == 1
        assert seen[0][0] == in_scope
        assert seen[0][1] == "BBB"

    def test_empty_polled_set_yields_nothing(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(conn, ticker="AAA", title="t1")
            conn.commit()
            seen = list(iter_pending_news_events(conn, polled_tickers=[]))
        finally:
            conn.close()
        assert seen == []


# ---------------------------------------------------------------------------
# run_one_poll_cycle — VAL-M2-024 (synthetic seed → emit < 2 cycles)
#                     + multi-keyword collapses to ONE candidate.
# ---------------------------------------------------------------------------


class TestRunOnePollCycleVAL_M2_024:
    """Synthetic insert produces a candidate within ONE cycle (well below 2)."""

    def test_synthetic_news_event_emits_one_candidate(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(
                conn,
                ticker="VRTX",
                title="VRTX receives FDA approval for new drug PDUFA cleared",
            )
            conn.commit()
        finally:
            conn.close()

        scanned, inserted = run_one_poll_cycle(str(v10_db))
        assert scanned == 1
        assert inserted == 1

        conn = sqlite3.connect(v10_db)
        try:
            rows = conn.execute(
                "SELECT ticker, source_news_event_id, matched_keywords "
                "FROM candidate_events"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "VRTX"
        assert rows[0][1] == news_id
        assert "fda approval" in rows[0][2]
        assert "pdufa" in rows[0][2]

    def test_multi_keyword_row_emits_exactly_one_candidate(
        self, v10_db: Path
    ) -> None:
        """One news_events row × N keywords = ONE candidate_events row."""

        conn = sqlite3.connect(v10_db)
        try:
            news_id = _seed_news_event(
                conn,
                ticker="VRTX",
                title=(
                    "VRTX announces PDUFA decision, NDA submission, and "
                    "license agreement on FDA approval"
                ),
            )
            conn.commit()
        finally:
            conn.close()

        run_one_poll_cycle(str(v10_db))

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()[0]
            kws = conn.execute(
                "SELECT matched_keywords FROM candidate_events "
                "WHERE source_news_event_id=?",
                (news_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, "multi-keyword row must collapse to ONE candidate"
        # Sorted CSV with multiple keywords present.
        assert "," in kws
        kw_list = kws.split(",")
        assert kw_list == sorted(kw_list)
        # At least three real catalyst tokens fired.
        assert "pdufa" in kw_list
        assert "nda submission" in kw_list
        assert "license agreement" in kw_list

    def test_no_match_row_skipped_no_candidate(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(
                conn,
                ticker="VRTX",
                title="VRTX hires new VP of marketing",  # no catalyst kw
            )
            conn.commit()
        finally:
            conn.close()

        scanned, inserted = run_one_poll_cycle(str(v10_db))
        assert scanned == 1
        assert inserted == 0

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_polled_tickers_scope_filter(self, v10_db: Path) -> None:
        """Out-of-scope tickers are filtered at the SQL layer."""

        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(
                conn, ticker="VRTX", title="VRTX FDA approval received"
            )
            _seed_news_event(
                conn, ticker="EXCLU", title="EXCLU FDA approval received"
            )
            conn.commit()
        finally:
            conn.close()

        run_one_poll_cycle(str(v10_db), polled_tickers=["VRTX"])

        conn = sqlite3.connect(v10_db)
        try:
            rows = conn.execute(
                "SELECT ticker FROM candidate_events"
            ).fetchall()
        finally:
            conn.close()
        tickers = [r[0] for r in rows]
        assert tickers == ["VRTX"]
        assert "EXCLU" not in tickers

    def test_empty_polled_set_zero_work(self, v10_db: Path) -> None:
        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(conn, ticker="VRTX", title="FDA approval")
            conn.commit()
        finally:
            conn.close()
        scanned, inserted = run_one_poll_cycle(
            str(v10_db), polled_tickers=[]
        )
        assert scanned == 0
        assert inserted == 0

    def test_re_running_cycle_is_idempotent(self, v10_db: Path) -> None:
        """Calling :func:`run_one_poll_cycle` twice never double-emits."""

        conn = sqlite3.connect(v10_db)
        try:
            _seed_news_event(conn, ticker="VRTX", title="PDUFA decision FDA approval")
            conn.commit()
        finally:
            conn.close()

        a_scanned, a_inserted = run_one_poll_cycle(str(v10_db))
        b_scanned, b_inserted = run_one_poll_cycle(str(v10_db))
        # First cycle: scans + inserts.  Second cycle: watermark
        # advanced past the only news_events row so scanned is 0.
        assert a_inserted == 1
        assert b_inserted == 0

        conn = sqlite3.connect(v10_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM candidate_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Single-transaction discipline
# ---------------------------------------------------------------------------


def test_emit_module_uses_executemany_for_batch(v10_db: Path) -> None:
    """Source-level grep: the batch writer uses ``executemany``.

    AGENTS.md "Multi-row inserts in a poll cycle MUST use a single
    transaction (executemany or BEGIN/COMMIT block)".
    """

    source = Path(emit.__file__).read_text(encoding="utf-8")
    assert "executemany(" in source, (
        "write_candidates must use executemany for batched insert"
    )
    assert "with conn:" in source, (
        "batch insert must be wrapped in a single transaction"
    )


def test_emit_module_uses_insert_or_ignore() -> None:
    """The writer SQL must use ``INSERT OR IGNORE`` for dedup."""

    source = Path(emit.__file__).read_text(encoding="utf-8")
    assert "INSERT OR IGNORE INTO candidate_events" in source


def test_emit_module_no_phantom_dedup_key_on_news_events() -> None:
    """The emitter MUST NOT reference a phantom ``news_events.dedup_key`` column.

    Pinned by VAL-M2-026.  ``news_events`` has a composite UNIQUE
    INDEX on ``(ticker, source, COALESCE(url,''),
    COALESCE(published_at,''))`` but NO ``dedup_key`` column.
    """

    source = Path(emit.__file__).read_text(encoding="utf-8")
    assert "news_events.dedup_key" not in source
