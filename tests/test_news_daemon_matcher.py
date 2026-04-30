"""Unit tests for ``biotech_sniper.news_daemon.matcher``.

Pins the f-m2-05 feature contract and the assertion IDs listed in
``features.json::fulfills``:

* VAL-M2-015 — TIER-1/TIER-2 catalyst vocab is REUSED from
  :mod:`biotech_sniper.intelligence.universal_news_watcher`, never
  redefined locally; ``CATALYST_KEYWORDS`` is a subset of
  ``TIER_1_SIGNALS ∪ TIER_2_SIGNALS``.
* VAL-M2-016 — partnership / collaboration / license keywords match
  WITHOUT any $-threshold (tiny + huge dollar figures both match).
* VAL-M2-017 — M&A rumour keywords match (acquires, to acquire,
  agreed to be acquired, merger, buyout, take-private, take private,
  strategic alternatives).
* VAL-M2-018 — IND / NDA / BLA / sNDA regulatory submission
  keywords match.
* VAL-M2-019 — multiple-keyword-on-one-row collapses to ONE
  :class:`MatchResult` whose ``matched_keywords`` is sorted +
  deduped (deterministic across runs so the dedup_key is stable).
* VAL-M2-020 — calendar match within the configured window
  populates ``calendar_match`` as a JSON payload string.
* VAL-M2-021 — calendar miss / no row / table missing leaves
  ``calendar_match`` as :data:`None` and STILL emits the candidate.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from biotech_sniper.news_daemon import matcher
from biotech_sniper.news_daemon.matcher import (
    CALENDAR_LOOKUP_WINDOW_DAYS,
    CATALYST_KEYWORDS,
    MatchResult,
    match_keywords,
    match_news_row,
)


# ---------------------------------------------------------------------------
# Fixtures — a minimal trial_calendar + plays seed for VAL-M2-020 / VAL-M2-021
# ---------------------------------------------------------------------------


def _ensure_trial_calendar(conn: sqlite3.Connection) -> None:
    """Create a minimal ``trial_calendar`` table for the lookup test."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trial_calendar (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT    NOT NULL,
            catalyst_date TEXT    NOT NULL,
            source        TEXT    NOT NULL CHECK(source IN ('ctgov','pdufa','ema_chmp')),
            source_ref    TEXT,
            fetched_at    TEXT    NOT NULL
        )
        """
    )


def _seed_trial_calendar_row(
    conn: sqlite3.Connection,
    ticker: str,
    catalyst_date: str,
    *,
    source: str = "pdufa",
    source_ref: str | None = "TEST-DRUG",
) -> None:
    conn.execute(
        "INSERT INTO trial_calendar (ticker, catalyst_date, source, "
        "source_ref, fetched_at) VALUES (?, ?, ?, ?, '2026-04-29T00:00:00Z')",
        (ticker, catalyst_date, source, source_ref),
    )


@pytest.fixture
def calendar_db(tmp_path: Path) -> Iterator[Path]:
    """Synthetic SQLite DB with a populated ``trial_calendar`` table."""

    db_path = tmp_path / "calendar.db"
    conn = sqlite3.connect(db_path)
    try:
        _ensure_trial_calendar(conn)
        # Within window
        _seed_trial_calendar_row(conn, "CALX", "2026-05-15", source="pdufa")
        # Far future, OUTSIDE the default 90-day window
        _seed_trial_calendar_row(conn, "FAR", "2027-12-31", source="ctgov")
        conn.commit()
    finally:
        conn.close()
    yield db_path


@pytest.fixture
def empty_calendar_db(tmp_path: Path) -> Iterator[Path]:
    """SQLite DB with the table present but zero rows."""

    db_path = tmp_path / "empty_calendar.db"
    conn = sqlite3.connect(db_path)
    try:
        _ensure_trial_calendar(conn)
        conn.commit()
    finally:
        conn.close()
    yield db_path


@pytest.fixture
def no_calendar_table_db(tmp_path: Path) -> Iterator[Path]:
    """SQLite DB without any ``trial_calendar`` table — M1 not yet run."""

    db_path = tmp_path / "no_table.db"
    conn = sqlite3.connect(db_path)
    try:
        # Some other unrelated table to ensure the DB file exists
        conn.execute("CREATE TABLE _placeholder (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    yield db_path


# ---------------------------------------------------------------------------
# VAL-M2-015 — vocab REUSE, no local redefinition
# ---------------------------------------------------------------------------


class TestCatalystKeywordsReuseVAL_M2_015:
    """``CATALYST_KEYWORDS`` is sourced from universal_news_watcher."""

    def test_catalyst_keywords_is_subset_of_tier1_union_tier2(self) -> None:
        """``CATALYST_KEYWORDS <= (TIER_1_SIGNALS | TIER_2_SIGNALS)``."""

        from biotech_sniper.universal_news_watcher import (
            TIER_1_SIGNALS,
            TIER_2_SIGNALS,
        )

        assert CATALYST_KEYWORDS <= (TIER_1_SIGNALS | TIER_2_SIGNALS)

    def test_catalyst_keywords_equals_intelligence_module_union(self) -> None:
        """The matcher reuses the canonical lists, not a snapshot."""

        from biotech_sniper.intelligence.universal_news_watcher import (
            TIER_1_SIGNALS as canon_tier_1,
            TIER_2_SIGNALS as canon_tier_2,
        )

        assert CATALYST_KEYWORDS == (
            frozenset(canon_tier_1) | frozenset(canon_tier_2)
        )

    def test_no_local_tier_list_literal_in_matcher_source(self) -> None:
        """The matcher source has zero ``TIER_1_SIGNALS = [`` redefinitions.

        Pins VAL-M2-015's grep clause: the matcher imports the
        canonical lists from
        :mod:`biotech_sniper.intelligence.universal_news_watcher`
        rather than redefining them locally.
        """

        source = Path(matcher.__file__).read_text(encoding="utf-8")
        bad = re.search(
            r"^\s*(TIER_1_SIGNALS|TIER_2_SIGNALS)\s*=\s*\[",
            source,
            re.MULTILINE,
        )
        assert bad is None, (
            f"matcher.py must NEVER redefine TIER lists locally: {bad!r}"
        )

    def test_catalyst_keywords_is_frozenset(self) -> None:
        """Frozenset enables ``|`` / ``<=`` set algebra at the call site."""

        assert isinstance(CATALYST_KEYWORDS, frozenset)


# ---------------------------------------------------------------------------
# VAL-M2-016 — partnership/collab/license match without $-threshold
# ---------------------------------------------------------------------------


class TestPartnershipNoThresholdVAL_M2_016:
    """Partnership / collaboration / license vocab matches at any size."""

    @pytest.mark.parametrize(
        "headline",
        [
            "Tiny biotech and BigPharma announce $2M partnership",
            "BigPharma signs $500M licensing agreement with biotech",
            "Smallco and Pharmaco announce $100M license deal",
            "Pharmaco and Smallco sign $1.5B collaboration agreement",
        ],
    )
    def test_partnership_matches_regardless_of_dollar_size(
        self, headline: str
    ) -> None:
        """Tiny and huge $ figures both match — no Stage-1 size filter."""

        result = match_news_row("ABCD", headline, "")
        assert result.is_match, (
            f"partnership headline must match without $-threshold: "
            f"{headline!r} → {result.matched_keywords!r}"
        )

    def test_partnership_keyword_alone_matches(self) -> None:
        """Bare ``partnership`` keyword matches (not gated on $)."""

        result = match_news_row(
            "ABCD",
            "Companies announce strategic partnership",
            "",
        )
        assert "partnership" in result.matched_keywords

    def test_collaboration_matches(self) -> None:
        """Bare ``collaboration`` keyword matches."""

        result = match_news_row(
            "ABCD",
            "ABCD enters collaboration with PharmaCo",
            "",
        )
        assert "collaboration" in result.matched_keywords

    def test_license_agreement_matches_in_body(self) -> None:
        """Keyword in the body (not headline) still matches."""

        result = match_news_row(
            "ABCD",
            "ABCD announces deal",
            "Today ABCD signed a license agreement with PharmaCo.",
        )
        assert "license agreement" in result.matched_keywords


# ---------------------------------------------------------------------------
# VAL-M2-017 — M&A rumour keywords
# ---------------------------------------------------------------------------


class TestMnAKeywordsVAL_M2_017:
    """M&A rumour vocab matches all listed forms."""

    @pytest.mark.parametrize(
        "headline,expected_kw",
        [
            ("PharmaCo to acquire ABCD for $2B", "to acquire"),
            ("PharmaCo acquires ABCD in stock deal", "acquires"),
            ("ABCD agreed to be acquired by PharmaCo", "agreed to be acquired"),
            ("ABCD and Bigco announce merger", "merger"),
            ("ABCD facing buyout pressure", "buyout"),
            ("ABCD considering take-private offer", "take-private"),
            ("ABCD considering take private offer", "take private"),
            ("ABCD exploring strategic alternatives", "strategic alternatives"),
            ("ABCD receives unsolicited acquisition proposal", "acquisition"),
            ("Bigco extends tender offer for ABCD shares", "tender offer"),
        ],
    )
    def test_ma_keyword_matches(self, headline: str, expected_kw: str) -> None:
        """Each M&A rumour keyword is matched with word-boundary semantics."""

        result = match_news_row("ABCD", headline, "")
        assert expected_kw in result.matched_keywords, (
            f"expected {expected_kw!r} in {result.matched_keywords!r} "
            f"for headline={headline!r}"
        )


# ---------------------------------------------------------------------------
# VAL-M2-018 — regulatory submission keywords
# ---------------------------------------------------------------------------


class TestRegulatorySubmissionVAL_M2_018:
    """IND / NDA / BLA / sNDA regulatory submission vocab matches."""

    @pytest.mark.parametrize(
        "headline,expected_kw",
        [
            ("ABCD announces NDA submission for drug-X", "nda submission"),
            ("ABCD: BLA submission accepted by FDA", "bla submission"),
            ("ABCD: sNDA accepted by the FDA", "snda"),
            ("ABCD: IND filing accepted by FDA", "ind filing"),
            (
                "ABCD: Investigational New Drug application granted clearance",
                "investigational new drug",
            ),
            (
                "ABCD: Biologics License Application accepted for review",
                "biologics license application",
            ),
            (
                "ABCD: New Drug Application submitted to FDA",
                "new drug application",
            ),
        ],
    )
    def test_regulatory_keyword_matches(
        self, headline: str, expected_kw: str
    ) -> None:
        """Each regulatory submission keyword matches case-insensitive."""

        result = match_news_row("ABCD", headline, "")
        assert expected_kw in result.matched_keywords, (
            f"expected {expected_kw!r} in {result.matched_keywords!r} "
            f"for headline={headline!r}"
        )

    def test_snda_does_not_match_inside_unrelated_word(self) -> None:
        """``sNDA`` token is bounded by non-word chars; ``asndas`` does NOT match."""

        result = match_news_row("ABCD", "asndas asndas asndas", "")
        assert "snda" not in result.matched_keywords


# ---------------------------------------------------------------------------
# VAL-M2-019 — multi-keyword collapses to ONE result with sorted dedup
# ---------------------------------------------------------------------------


class TestMatchedKeywordsSortedDedupVAL_M2_019:
    """Multiple matches on one row collapse to ONE sorted, deduped tuple."""

    def test_multiple_matches_collapse_to_single_result(self) -> None:
        """One row → one MatchResult regardless of how many keywords fire."""

        result = match_news_row(
            "ABCD",
            "ABCD topline phase 3 results — pivotal trial",
            "PDUFA date set; FDA approval anticipated.",
        )
        # All keywords should be present
        assert "topline" in result.matched_keywords
        assert "phase 3 results" in result.matched_keywords
        assert "pivotal trial" in result.matched_keywords
        assert "pdufa" in result.matched_keywords
        assert "fda approval" in result.matched_keywords

    def test_matched_keywords_are_sorted_ascending(self) -> None:
        """Tuple is sorted ascending so dedup_key is stable across runs."""

        result = match_news_row(
            "ABCD",
            "ABCD pdufa topline pivotal trial",
            "",
        )
        keywords = list(result.matched_keywords)
        assert keywords == sorted(keywords), (
            f"matched_keywords must be sorted ascending: {keywords}"
        )

    def test_matched_keywords_are_deterministic_across_calls(self) -> None:
        """Same input → same matched_keywords tuple (stable dedup_key)."""

        headline = "ABCD topline phase 3 results — partnership announced"
        a = match_news_row("ABCD", headline, "")
        b = match_news_row("ABCD", headline, "")
        assert a.matched_keywords == b.matched_keywords

    def test_matched_keywords_csv_form_is_sorted_comma_joined(self) -> None:
        """``matched_keywords_csv`` is the canonical CSV form for persistence."""

        result = match_news_row(
            "ABCD",
            "ABCD pdufa topline",
            "",
        )
        # pdufa < topline alphabetically
        assert result.matched_keywords_csv == "pdufa,topline"

    def test_repeated_keyword_in_text_dedups_to_one(self) -> None:
        """A keyword appearing twice in the text contributes ONE entry."""

        result = match_news_row(
            "ABCD",
            "topline topline topline",
            "topline",
        )
        assert result.matched_keywords.count("topline") == 1


# ---------------------------------------------------------------------------
# VAL-M2-020 — trial_calendar lookup populates calendar_match within window
# ---------------------------------------------------------------------------


class TestCalendarMatchWithinWindowVAL_M2_020:
    """A trial_calendar row within the lookup window populates calendar_match."""

    def test_calendar_match_within_window_populates_payload(
        self, calendar_db: Path
    ) -> None:
        """JSON payload includes source, catalyst_date, days_until."""

        # Pin "today" so days_until is deterministic
        today = datetime.date(2026, 5, 1)
        result = match_news_row(
            "CALX",
            "CALX topline phase 3 results",
            "",
            db_path=str(calendar_db),
            today=today,
        )

        assert result.is_match
        assert result.calendar_match is not None
        payload = json.loads(result.calendar_match)
        assert payload["source"] == "trial_calendar"
        assert payload["catalyst_date"] == "2026-05-15"
        assert payload["days_until"] == 14

    def test_calendar_match_outside_window_is_none(
        self, calendar_db: Path
    ) -> None:
        """A row OUTSIDE the 90-day window degrades to ``calendar_match=None``."""

        today = datetime.date(2026, 5, 1)
        result = match_news_row(
            "FAR",
            "FAR topline phase 3 results",
            "",
            db_path=str(calendar_db),
            today=today,
        )
        assert result.is_match
        assert result.calendar_match is None

    def test_no_keyword_match_skips_calendar_lookup(
        self, calendar_db: Path
    ) -> None:
        """Cold path: no keyword → no calendar lookup, empty result."""

        result = match_news_row(
            "CALX",
            "Just some unrelated business news",
            "",
            db_path=str(calendar_db),
        )
        assert not result.is_match
        assert result.calendar_match is None


# ---------------------------------------------------------------------------
# VAL-M2-021 — calendar miss still emits candidate (calendar_match=None)
# ---------------------------------------------------------------------------


class TestCalendarMissStillEmitsVAL_M2_021:
    """Calendar LEFT-JOIN miss is NOT an error; candidate still emitted."""

    def test_no_row_for_ticker_emits_with_null_calendar_match(
        self, calendar_db: Path
    ) -> None:
        """Ticker without any calendar row → ``calendar_match=None``."""

        result = match_news_row(
            "NOCAL",
            "NOCAL topline phase 3 results",
            "",
            db_path=str(calendar_db),
        )
        assert result.is_match
        assert result.calendar_match is None

    def test_empty_trial_calendar_table_emits_with_null_calendar_match(
        self, empty_calendar_db: Path
    ) -> None:
        """Table exists but empty → emit with ``calendar_match=None``."""

        result = match_news_row(
            "ABCD",
            "ABCD topline phase 3 results",
            "",
            db_path=str(empty_calendar_db),
        )
        assert result.is_match
        assert result.calendar_match is None

    def test_missing_trial_calendar_table_emits_with_null_calendar_match(
        self, no_calendar_table_db: Path
    ) -> None:
        """Table missing entirely (M1 not run) → still emits the candidate."""

        result = match_news_row(
            "ABCD",
            "ABCD topline phase 3 results",
            "",
            db_path=str(no_calendar_table_db),
        )
        assert result.is_match
        assert result.calendar_match is None

    def test_calendar_lookup_does_not_raise_on_missing_db_file(
        self, tmp_path: Path
    ) -> None:
        """A db_path that does not exist degrades to ``calendar_match=None``."""

        result = match_news_row(
            "ABCD",
            "ABCD topline phase 3 results",
            "",
            db_path=str(tmp_path / "does_not_exist.db"),
        )
        assert result.is_match
        assert result.calendar_match is None


# ---------------------------------------------------------------------------
# Match-result data class
# ---------------------------------------------------------------------------


class TestMatchResult:
    """Pin the public surface of :class:`MatchResult`."""

    def test_default_match_result_is_not_match(self) -> None:
        assert MatchResult().is_match is False

    def test_match_result_with_keyword_is_match(self) -> None:
        assert MatchResult(matched_keywords=("pdufa",)).is_match is True

    def test_match_result_csv_empty_when_no_keywords(self) -> None:
        assert MatchResult().matched_keywords_csv == ""

    def test_match_result_is_frozen(self) -> None:
        """Frozen dataclass — assignment raises."""

        result = MatchResult(matched_keywords=("pdufa",))
        with pytest.raises(Exception):
            result.matched_keywords = ("topline",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# match_keywords helper
# ---------------------------------------------------------------------------


class TestMatchKeywordsHelper:
    """Direct tests for :func:`match_keywords` (no calendar lookup)."""

    def test_empty_string_returns_empty_tuple(self) -> None:
        assert match_keywords("") == ()

    def test_none_returns_empty_tuple(self) -> None:
        assert match_keywords(None) == ()  # type: ignore[arg-type]

    def test_no_keywords_returns_empty_tuple(self) -> None:
        assert match_keywords("just some normal news today") == ()

    def test_case_insensitive_matching(self) -> None:
        assert "pdufa" in match_keywords("PDUFA Date Confirmed")
        assert "pdufa" in match_keywords("Pdufa date confirmed")
        assert "pdufa" in match_keywords("pdufa date confirmed")

    def test_returns_sorted_unique_tuple(self) -> None:
        out = match_keywords("topline pdufa topline pdufa")
        assert out == ("pdufa", "topline")


# ---------------------------------------------------------------------------
# Defensive — malformed inputs do not raise
# ---------------------------------------------------------------------------


class TestMatcherDoesNotRaise:
    """The matcher must NEVER raise on malformed input — daemon must idle."""

    def test_blank_ticker_returns_no_match(self) -> None:
        result = match_news_row("", "topline phase 3 results", "")
        # Keywords still match on the text, but the ticker is blank
        assert result.is_match is True
        # No calendar lookup should be performed for a blank ticker
        assert result.calendar_match is None

    def test_non_string_headline_returns_no_match(self) -> None:
        result = match_news_row("ABCD", None, "")  # type: ignore[arg-type]
        assert result.is_match is False

    def test_non_string_body_treated_as_empty(self) -> None:
        result = match_news_row(
            "ABCD",
            "topline phase 3 results",
            None,  # type: ignore[arg-type]
        )
        assert result.is_match is True


# ---------------------------------------------------------------------------
# Window default + bound
# ---------------------------------------------------------------------------


def test_calendar_window_default_is_90_days() -> None:
    """Default window is the documented 90-day forward window."""

    assert CALENDAR_LOOKUP_WINDOW_DAYS == 90
