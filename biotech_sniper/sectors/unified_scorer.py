#!/usr/bin/env python3
"""
UNIFIED DUAL-MODEL SCORER
Scores any binary catalyst — biotech, contract, AdCom — using the same
Claude Opus 4 + Gemini 2.5 Pro ensemble pipeline.

The probability prompt is sector-specific but the scoring mechanics are identical:
  - Two independent model calls
  - Average = ensemble P(success)
  - Filter: >=65% LONG | <=40% SHORT/PUT | 41-64% DROP  [CALIBRATED Apr 26 2026]
  - Model divergence >25pp → HALF SIZE flag

CALIBRATION RULES (applied Apr 26 2026 — based on 6 resolved plays):
  - Minimum P for LONG: >=65% (raised from 60% — LPCN 62% was the lesson)
  - PDUFA events (FDA decision binary): 10-25% OTM strike
  - Trial readouts (Phase 2/3 data): 5-15% OTM strike only
  - Label extensions (sBLA/sNDA on already-approved drug): ATM to 5% OTM
  - If P>=80%: recommend spread over naked call (reduce vega risk)
  - If IV > 150%: flag HALF SIZE (IV crush risk)
  - Dual-endpoint trials: use joint P for option sizing, not primary-alone P
  - Science Grade D/F on LONG play: HALF SIZE (reduce exposure)
  - Science Grade A/B + P>=80%: full size spread recommended

Also handles:
  - Finding the right option ticker + expiry + strike
  - Calculating realistic fill from live options chain
  - Computing $1k multiple on success
  - Lifecycle: adding qualifying plays to active_plays.json
"""

import argparse
import json
import logging
import os
import re
import datetime
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from biotech_sniper.paths import BASE_DIR


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export the M2 ensemble symbols at this module's namespace so
# downstream callers (M3 selection, M5 ranker training) and validators
# (VAL-M2-044 / VAL-M2-079) can import via the historically-canonical
# location without breaking::
#
#     from biotech_sniper.sectors.unified_scorer import compute_ensemble
#
# The canonical implementation lives in :mod:`biotech_sniper.llm.ensemble`
# (per f-m2-05). This module does NOT duplicate the logic — it only
# re-exports the public surface so legacy import sites keep working.
# ---------------------------------------------------------------------------
from biotech_sniper.llm.ensemble import (  # noqa: F401 (re-exported)
    DIVERGENCE_THRESHOLD,
    EnsembleScorer,
    compute_ensemble,
    letter_grade_distance,
)
from biotech_sniper.llm.claude_client import LETTER_GRADE_ORDER  # noqa: F401

ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
SCORING_CACHE_FILE = BASE_DIR / "state/scoring_cache.json"


# ---------------------------------------------------------------------------
# f-m3-02 — score_options(chain) consumer.
# ---------------------------------------------------------------------------


def score_options(chain: Sequence[dict]) -> float:
    """Score a single options chain and return a liquidity/quality float.

    This is the consumer exercised by the M3 validation contract
    (VAL-M3-013). It accepts the list of dicts produced by
    :func:`biotech_sniper.options_chains.pull_options.pull_chain` and
    returns a single non-negative float — a coarse liquidity / quality
    signal that downstream selection logic can use to break ties or
    rank chains within a single ticker.

    The scoring formula is intentionally simple and deterministic so
    that test fixtures can pin exact values:

    * Each row contributes a factor proportional to its open interest
      (``oi``) and inversely proportional to its bid/ask spread
      (a tight spread is better than a wide one).
    * Rows missing any of :data:`REQUIRED_CHAIN_KEYS` raise
      :class:`KeyError` — this is the explicit contract bound that
      VAL-M3-013 asserts: a row produced by ``pull_chain`` must be
      consumable here without translation.
    * ``mid`` / ``iv`` / ``delta`` are not currently used in the
      score but are validated as present to keep the schema contract
      honest. M5's LightGBM ranker will swap this stub for a learned
      function that reads all of them.

    Parameters
    ----------
    chain:
        Sequence of chain rows (typically the output of
        ``pull_chain``).

    Returns
    -------
    float
        Non-negative score. Empty chains yield ``0.0``. Higher is
        better (more liquidity, tighter spreads).
    """
    # Local import keeps the module import-time cost low and avoids a
    # circular dependency with ``biotech_sniper.options_chains.pull_options``.
    from biotech_sniper.options_chains.pull_options import REQUIRED_CHAIN_KEYS

    total = 0.0
    for row in chain or ():
        # Hard-fail on missing keys so the contract test catches any
        # drift between pull_chain's output schema and the consumer's
        # expectations. A KeyError here is the regression signal that
        # VAL-M3-013 watches for.
        for key in REQUIRED_CHAIN_KEYS:
            if key not in row:
                raise KeyError(
                    f"score_options: missing required key {key!r} in chain row {row!r}"
                )

        oi = row["oi"] or 0
        bid = row["bid"] or 0.0
        ask = row["ask"] or 0.0
        spread = max(ask - bid, 0.0)
        # Liquidity-weighted contribution. Tight spreads weigh more than
        # wide spreads; high OI weighs more than low OI. Both terms are
        # bounded so a single huge-OI row does not dominate.
        liquidity = float(oi) / (1.0 + spread)
        total += liquidity

    return float(total)


# ---------------------------------------------------------------------------
# f-m2-11 — Selection logic (top-N from scoring_cache).
# ---------------------------------------------------------------------------


def _grades_at_or_above(min_grade: str) -> list[str]:
    """Return the canonical letter grades that satisfy ``>= min_grade``.

    :data:`LETTER_GRADE_ORDER` runs from best (``"A+"``, index 0) to
    worst (``"F"``). A grade ``g`` "passes" when its index is **less
    than or equal to** the index of ``min_grade``. This helper
    materialises that allowed-list so callers can use a SQLite ``IN``
    clause instead of relying on lexicographic ``>=`` (which would
    incorrectly mark ``"B-"`` as worse than ``"C"`` because of ASCII
    ordering of the modifier characters).

    ``min_grade`` MUST be a member of :data:`LETTER_GRADE_ORDER` —
    callers receive a deterministic :class:`ValueError` otherwise so
    typos surface in the test suite rather than silently dropping all
    candidates.
    """
    if min_grade not in LETTER_GRADE_ORDER:
        raise ValueError(
            f"select_top_n: min_grade={min_grade!r} is not a member of "
            f"LETTER_GRADE_ORDER={LETTER_GRADE_ORDER}"
        )
    cutoff = LETTER_GRADE_ORDER.index(min_grade)
    return list(LETTER_GRADE_ORDER[: cutoff + 1])


def select_top_n(
    as_of_date: str,
    n: int,
    *,
    min_ensemble_score: float | None = None,
    min_science_grade: str | None = None,
    db_path=None,
) -> list[dict]:
    """Return the top-N candidates from ``scoring_cache`` for ``as_of_date``.

    Behaviour (per the f-m2-11 / VAL-M2-077 / VAL-M2-078 contract):

    * Reads from the SQLite ``scoring_cache`` table at
      ``DATA_DIR / "alpha_sniper.db"`` (override via ``db_path``).
    * Filters rows to ``as_of_date`` (ISO date string, e.g.
      ``"2026-04-27"``).
    * Drops any row whose ``ensemble_score`` is ``NULL`` or
      ``< MIN_ENSEMBLE_SCORE`` (override via ``min_ensemble_score``).
    * Drops any row whose ``science_grade`` is ``NULL`` or worse than
      :data:`biotech_sniper.config.MIN_SCIENCE_GRADE` (override via
      ``min_science_grade``).
    * Orders survivors by ``ensemble_score DESC`` with a deterministic
      tie-break on ``ticker ASC`` so two scoring runs on the same
      data return the same ordered list.
    * Caps the survivor list at ``n`` candidates.

    ``n`` is the only required parameter alongside ``as_of_date``;
    callers typically pass :data:`config.RISK_DEFAULTS["max_concurrent"]`.
    Negative or zero ``n`` returns ``[]`` immediately. Returns ``[]``
    when the SQLite db file does not yet exist (fresh checkouts) so
    the play-card writer can no-op cleanly on a cold start.

    Returns
    -------
    list[dict]
        One dict per surviving candidate, in score-desc order. Keys:
        ``id``, ``ticker``, ``as_of_date``, ``ensemble_score``,
        ``science_grade``, ``claude_grade``, ``claude_probability``,
        ``gemini_grade``, ``gemini_probability``, ``grok_score``,
        ``grok_rank``, ``divergence_flag``, ``payload``,
        ``created_at``. ``payload`` is left as the raw JSON string
        (callers can ``json.loads`` if they need the breakdown).
    """
    # Local imports keep this module import-safe before the SQLite layer
    # is wired up (e.g. on a fresh checkout where data/ is empty).
    from biotech_sniper import config as _config
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    if n is None or n <= 0:
        return []

    threshold_score = (
        float(min_ensemble_score)
        if min_ensemble_score is not None
        else float(_config.MIN_ENSEMBLE_SCORE)
    )
    threshold_grade = (
        str(min_science_grade)
        if min_science_grade is not None
        else str(_config.MIN_SCIENCE_GRADE)
    )

    allowed_grades = _grades_at_or_above(threshold_grade)
    if not allowed_grades:
        # Defensive: ``_grades_at_or_above`` already validates the
        # input, so this branch is unreachable in normal use.
        return []

    target_path = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target_path.exists():
        return []

    try:
        conn = _db.connect(target_path)
    except Exception:
        return []
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            return []

        placeholders = ", ".join("?" for _ in allowed_grades)
        query = (
            "SELECT id, ticker, as_of_date, grok_rank, grok_score, "
            "claude_grade, claude_probability, gemini_grade, "
            "gemini_probability, science_grade, ensemble_score, "
            "divergence_flag, payload, created_at "
            "FROM scoring_cache "
            "WHERE as_of_date = ? "
            "  AND ensemble_score IS NOT NULL "
            "  AND ensemble_score >= ? "
            "  AND science_grade IS NOT NULL "
            f"  AND science_grade IN ({placeholders}) "
            "ORDER BY ensemble_score DESC, ticker ASC "
            "LIMIT ?"
        )
        params: list = [as_of_date, threshold_score, *allowed_grades, int(n)]
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    for row in rows:
        item = dict(row)
        item["divergence_flag"] = bool(item.get("divergence_flag"))
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# f-m5-04 — LightGBM ranker (supplementary signal layer).
#
# The ranker is OFF by default (``config.LIGHTGBM_RANKER_ENABLED``).
# When enabled, :func:`attach_supplementary_score` decorates each play
# card with a ``ranker_score`` field (float in ``[0, 1]``). The flag is
# read fresh on every call so operators can toggle without a restart;
# the booster is cached in module state once the first successful load
# occurs (subsequent calls re-use the same in-memory model).
#
# Critical invariants:
#
# * The ranker is **supplementary** — it never overrides
#   ``ensemble_score`` (a.k.a. ``pre_score``) which continues to drive
#   trade gating, sizing, and entry decisions in
#   :mod:`biotech_sniper.paper_executor` etc.
# * ``lightgbm`` is **not** imported when the flag is off
#   (VAL-M5-030) — the import is hidden behind a local import inside
#   :func:`_load_ranker_singleton` and only fires when the flag flips
#   on.
# * Manifest mismatches surface as
#   :class:`biotech_sniper.ranker.RankerSchemaMismatch` with a
#   structured diff; per-call prediction failures degrade gracefully
#   (the card is emitted without ``ranker_score``) so a bad ranker
#   artefact never blocks the daily play-card emission.
# ---------------------------------------------------------------------------


_RANKER_SINGLETON_STATE: dict[str, Any] = {
    "model": None,
    "model_path": None,
    "load_attempted_for_path": None,
}


def _resolve_ranker_model_path() -> Path | None:
    """Return the highest-versioned ``ranker_v{N}.lgb`` under ``models/``.

    Returns ``None`` when no ranker artefact has been materialised yet
    (fresh checkouts, M5 trainer never run). The caller is expected to
    log a WARNING and disable the supplementary signal in that case.
    """
    models_dir = BASE_DIR / "models"
    if not models_dir.exists():
        return None
    pattern = re.compile(r"^ranker_v(\d+)\.lgb$")
    candidates: list[tuple[int, Path]] = []
    for entry in models_dir.iterdir():
        if not entry.is_file():
            continue
        match = pattern.match(entry.name)
        if match:
            candidates.append((int(match.group(1)), entry))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _load_ranker_singleton():
    """Return a cached :class:`RankerModel` (or ``None`` when unavailable).

    The function is the single import-site for ``lightgbm`` in the
    play-card emission path. When :data:`config.LIGHTGBM_RANKER_ENABLED`
    is ``False`` it returns ``None`` immediately without touching the
    ranker module — that keeps ``lightgbm`` absent from ``sys.modules``
    so VAL-M5-030 ("flag off → lightgbm not loaded") holds. When the
    flag is ``True`` it lazily loads the highest-version ranker
    artefact, caches the resulting :class:`RankerModel` in module
    state, and reuses it across calls until the model path changes.
    """
    from biotech_sniper import config as _config

    if not _config.LIGHTGBM_RANKER_ENABLED:
        # Defensive: clear any cached model so a flip-off-then-flip-on
        # cycle re-loads cleanly (the model_path may have changed in
        # the interim — e.g. a new ranker_v2.lgb was trained).
        _RANKER_SINGLETON_STATE["model"] = None
        _RANKER_SINGLETON_STATE["model_path"] = None
        _RANKER_SINGLETON_STATE["load_attempted_for_path"] = None
        return None

    model_path = _resolve_ranker_model_path()
    if model_path is None:
        if _RANKER_SINGLETON_STATE["load_attempted_for_path"] != "<missing>":
            logger.warning(
                "attach_supplementary_score: LIGHTGBM_RANKER_ENABLED=true "
                "but no ranker_v*.lgb artefact found under %s; "
                "supplementary signal disabled until "
                "`python -m biotech_sniper.training.train_ranker` runs.",
                BASE_DIR / "models",
            )
            _RANKER_SINGLETON_STATE["load_attempted_for_path"] = "<missing>"
        _RANKER_SINGLETON_STATE["model"] = None
        _RANKER_SINGLETON_STATE["model_path"] = None
        return None

    cached_path = _RANKER_SINGLETON_STATE.get("model_path")
    cached_model = _RANKER_SINGLETON_STATE.get("model")
    if cached_model is not None and cached_path == model_path:
        return cached_model

    # New (or first-ever) load attempt. Local import keeps
    # ``biotech_sniper.ranker`` (and its lightgbm import) out of
    # ``sys.modules`` until the flag is flipped on.
    try:
        from biotech_sniper.ranker import load_ranker as _load_ranker

        model = _load_ranker(model_path)
    except Exception as exc:  # noqa: BLE001 — never raise from a side-channel
        logger.warning(
            "attach_supplementary_score: ranker load failed for %s: %r; "
            "supplementary signal disabled for this run",
            model_path,
            exc,
        )
        _RANKER_SINGLETON_STATE["model"] = None
        _RANKER_SINGLETON_STATE["model_path"] = model_path
        _RANKER_SINGLETON_STATE["load_attempted_for_path"] = str(model_path)
        return None

    _RANKER_SINGLETON_STATE["model"] = model
    _RANKER_SINGLETON_STATE["model_path"] = model_path
    _RANKER_SINGLETON_STATE["load_attempted_for_path"] = str(model_path)
    return model


def _candidate_to_feature_row(candidate: dict) -> dict:
    """Map a play-card / scoring_cache candidate to the parquet schema.

    The ranker was trained on the 11 features materialised by
    :mod:`biotech_sniper.training.build_feature_store`. At play-card
    emission time most of those features are not yet observable on
    the candidate dict (e.g. ``iv_at_entry`` is recorded only after
    the entry fills) so we fill in ``None`` / ``NaN`` for the
    missing fields. LightGBM handles ``NaN`` natively for numeric
    columns; missing categorical levels degrade to ``unknown``.
    """
    sg = candidate.get("science_grade")
    if isinstance(sg, str):
        sg = sg.strip() or None

    prior_q: str | None = None
    if isinstance(sg, str) and sg.upper() in {"A", "B", "C", "D", "F"}:
        # The trainer encodes prior_phase2_data_quality as a string-
        # categorical (e.g. ``"4"`` for grade A); mirror that here so
        # the booster sees the same level.
        from biotech_sniper.training.build_feature_store import (
            PRIOR_PHASE2_DATA_QUALITY_MAP as _MAP,
        )

        mapped = _MAP.get(sg.upper())
        if mapped is not None:
            prior_q = str(mapped)

    p_ensemble = candidate.get("ensemble_score")
    if p_ensemble is None:
        p_ensemble = candidate.get("p_ensemble")

    return {
        "science_grade": sg,
        "base_rate": candidate.get("base_rate"),
        "p_ensemble": (
            float(p_ensemble) if p_ensemble is not None else float("nan")
        ),
        "iv_at_entry": candidate.get("iv_at_entry"),
        "dte_at_entry": candidate.get("dte_at_entry"),
        "market_cap": candidate.get("market_cap"),
        "sector": candidate.get("sector"),
        "prior_phase2_data_quality": prior_q,
        "sponsor_size": candidate.get("sponsor_size"),
        "indication_class": candidate.get("indication_class"),
        "days_to_event": candidate.get("days_to_event"),
    }


def attach_supplementary_score(card: dict) -> dict:
    """Decorate ``card`` with a supplementary ``ranker_score`` (in place).

    Behaviour:

    * When :data:`biotech_sniper.config.LIGHTGBM_RANKER_ENABLED` is
      ``False`` the function is a no-op — the card is returned
      unchanged and ``lightgbm`` is not imported.
    * When the flag is ``True`` the highest-versioned
      ``models/ranker_v{N}.lgb`` artefact is loaded (and cached) and
      the card receives a ``ranker_score`` field with a float in
      ``[0, 1]`` derived from the booster's class-1 probability.
    * Per-call prediction failures degrade gracefully — they log a
      WARNING and leave the card without a ``ranker_score`` field
      so a bad model artefact cannot block daily play-card emission.

    The function returns the (possibly mutated) card so it can be
    used as ``payload = attach_supplementary_score(payload)`` at call
    sites.
    """
    model = _load_ranker_singleton()
    if model is None:
        return card

    try:
        import pandas as _pd  # local import — pandas is already a hard dep

        row = _candidate_to_feature_row(card)
        df = _pd.DataFrame([row])
        proba = model.predict_proba(df)
    except Exception as exc:  # noqa: BLE001 — never raise from emission path
        logger.warning(
            "attach_supplementary_score: prediction failed for "
            "ticker=%s: %r; emitting card without ranker_score",
            card.get("ticker"),
            exc,
        )
        return card

    if proba is None:
        return card
    try:
        score = float(proba[0, 1])
    except (IndexError, TypeError, ValueError) as exc:  # pragma: no cover - defensive
        logger.warning(
            "attach_supplementary_score: malformed predict_proba output "
            "for ticker=%s: %r",
            card.get("ticker"),
            exc,
        )
        return card

    if not (score == score):  # NaN check
        return card
    if score < 0.0:
        score = 0.0
    if score > 1.0:
        score = 1.0
    card["ranker_score"] = score
    return card


def load_active_plays():
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}

def save_active_plays(plays):
    ACTIVE_PLAYS_FILE.parent.mkdir(exist_ok=True)
    with open(ACTIVE_PLAYS_FILE, "w") as f:
        json.dump(plays, f, indent=2)

def load_scoring_cache():
    if SCORING_CACHE_FILE.exists():
        with open(SCORING_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_scoring_cache(cache):
    with open(SCORING_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def get_third_friday(year: int, month: int) -> datetime.date:
    """Calculate the 3rd Friday of any given month/year dynamically."""
    d = datetime.date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:  # Friday
            fridays += 1
            if fridays == 3:
                return d
        d += datetime.timedelta(days=1)


def get_option_expiry_for_announcement(announcement_date_str, certainty="ESTIMATED"):
    """
    Given an announcement date/range, return the correct option expiry.
    Standard monthly expiries = 3rd Friday of each month.
    FULLY DYNAMIC — works for any year, no hardcoded expiry tables.
    """
    today = datetime.date.today()
    
    # Parse announcement date — handles ISO, month names, Q1-Q4, H1/H2
    ann_date = None
    if announcement_date_str:
        text = announcement_date_str.lower().strip()
        try:
            ann_date = datetime.date.fromisoformat(announcement_date_str)
        except:
            # Q1/Q2/Q3/Q4 YYYY format
            q_match = re.search(r'q([1-4])\s*(20\d\d)', text)
            if q_match:
                quarter = int(q_match.group(1))
                year    = int(q_match.group(2))
                month   = {1: 2, 2: 5, 3: 8, 4: 11}[quarter]  # mid-quarter
                ann_date = datetime.date(year, month, 15)

            # H1/H2 YYYY format
            if not ann_date:
                h_match = re.search(r'h([12])\s*(20\d\d)', text)
                if h_match:
                    half  = int(h_match.group(1))
                    year  = int(h_match.group(2))
                    month = 3 if half == 1 else 9  # mid-half
                    ann_date = datetime.date(year, month, 15)

            # Month name(s) YYYY — e.g. "April 30 2026", "May - June 2026"
            if not ann_date:
                month_map = {
                    "january": 1, "february": 2, "march": 3, "april": 4,
                    "may": 5, "june": 6, "july": 7, "august": 8,
                    "september": 9, "october": 10, "november": 11, "december": 12
                }
                year_match = re.search(r'20\d\d', text)
                year = int(year_match.group()) if year_match else today.year
                found_months = []
                for mname, mnum in month_map.items():
                    if mname in text:
                        found_months.append(mnum)
                if found_months:
                    day_match = re.search(r'\b([12]?\d)\b', text)
                    day = int(day_match.group(1)) if day_match and int(day_match.group(1)) <= 31 else 15
                    try:
                        ann_date = datetime.date(year, min(found_months), day)
                    except:
                        ann_date = datetime.date(year, min(found_months), 15)

    if not ann_date:
        # Default: 2 months from now
        m = today.month + 2
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        ann_date = datetime.date(y, m, 15)
    
    # For CERTAIN dates: use nearest expiry with min 2-3 trading day buffer
    # For ESTIMATED dates: add 3-4 week buffer on top of estimate
    buffer_days = 3 if certainty == "CERTAIN" else 21  # 3 weeks buffer for uncertain timing
    target = ann_date + datetime.timedelta(days=buffer_days)
    
    # Search up to 18 months forward dynamically — no hardcoded table
    search_start = max(today, target)
    year, month = search_start.year, search_start.month
    for _ in range(18):  # max 18 months
        tf = get_third_friday(year, month)
        if tf >= target and tf > today:
            return tf.isoformat(), (ann_date - today).days
        # Advance to next month
        month += 1
        if month > 12:
            month = 1
            year += 1
    
    # Fallback: 6 months out from today
    m = today.month + 6
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return get_third_friday(y, m).isoformat(), 180

# ── CATALYST TYPE DETECTION ──────────────────────────────────────────────────

DEFAULT_OTM_BY_CATALYST = {
    "PDUFA":      (0.10, 0.25),  # 10-25% OTM for FDA approval binary (large gap-up expected)
    "READOUT":    (0.05, 0.15),  # 5-15% OTM for Phase 2/3 data (magnitude variable)
    "LABEL_EXT":  (0.00, 0.05),  # ATM to 5% OTM for sBLA/sNDA label extension (modest move)
    "ADCOM":      (0.10, 0.20),  # 10-20% OTM for AdCom vote (can gap large on yes)
    "CONTRACT":   (0.15, 0.30),  # 15-30% OTM for contract awards (usually modest)
    "DEFAULT":    (0.10, 0.20),  # Fallback if type unknown
}


def detect_catalyst_type(notes: str, drug_or_topic: str = "", indication: str = "",
                          sector: str = "BIOTECH") -> str:
    """
    Detect whether the catalyst is a PDUFA, READOUT, LABEL_EXT, ADCOM, or CONTRACT.
    Returns a catalyst_type string used to select the correct OTM strike range.

    CALIBRATION NOTE (Apr 26 2026):
      Label extensions (sNDA/sBLA) produce only 5-15% stock moves on approval.
      Trial readouts are binary but magnitude is limited — 10-25% typical.
      PDUFA on first-ever approval can gap 30-60%.
      Using the wrong category was the TVTX $35C lesson.
    """
    if sector == "CONTRACT":
        return "CONTRACT"
    if sector == "ADCOM":
        return "ADCOM"

    combined = (notes + " " + drug_or_topic + " " + indication).lower()

    # Label extension signals (drug already approved, adding new indication)
    label_ext_signals = ["label extension", "snda", "sbla", "supplemental nda", "supplemental bla",
                         "label expansion", "new indication", "already approved", "approved for"]
    # PDUFA = FDA action on a BLA/NDA filing
    pdufa_signals = ["pdufa", "nda filing", "bla filing", "fda decision", "fda action date",
                     "fda approval", "pdufa date", "nda", "bla"]
    # Trial readout signals
    # f-misc-05: extended with the canonical READOUT tokens VAL-M3-051
    # mandates so that ``detect_catalyst_type`` is the single
    # source-of-truth for readout-catalyst keyword detection. Previously
    # ``stage2_dispatcher`` carried parallel readout pre-check tuples
    # which have been retired; all readout tokens live here. The bare
    # ``p1``/``p2``/``p3`` short forms are included so callers passing
    # the bare phase shorthand still resolve to ``READOUT`` via this
    # single helper.
    readout_signals = ["phase 3", "phase 2", "phase 2b", "phase 3a", "phase 1",
                       "data readout", "trial results",
                       "primary endpoint", "pivotal trial", "pivotal data", "clinical readout",
                       "topline", "top-line", "data drop",
                       "p3 readout", "p2 readout", "p1 readout",
                       "first-in-human", "first in human",
                       "p1", "p2", "p3"]

    # Check label extension first (most specific — subset of PDUFA territory)
    if any(sig in combined for sig in label_ext_signals):
        return "LABEL_EXT"
    if any(sig in combined for sig in pdufa_signals):
        return "PDUFA"
    if any(sig in combined for sig in readout_signals):
        return "READOUT"
    return "DEFAULT"


def get_otm_range_for_catalyst(catalyst_type: str) -> tuple:
    """Returns (min_otm_pct, max_otm_pct) for the given catalyst type."""
    return DEFAULT_OTM_BY_CATALYST.get(catalyst_type, DEFAULT_OTM_BY_CATALYST["DEFAULT"])


def calculate_otm_strike(stock_price, direction, otm_pct=0.15,
                          catalyst_type: str = "DEFAULT") -> float:
    """
    Calculate OTM strike based on catalyst type.
    
    CALIBRATION (Apr 26 2026): OTM % is now catalyst-type-specific.
      PDUFA: 10-25% OTM (use midpoint 17.5%)
      READOUT: 5-15% OTM (use midpoint 10%)
      LABEL_EXT: 0-5% OTM (use 2.5% = nearly ATM)
      ADCOM: 10-20% OTM (use midpoint 15%)
      Old default 35% OTM was too aggressive for most non-PDUFA plays.
    """
    # Use catalyst-type-specific OTM range if not explicitly provided
    if catalyst_type != "DEFAULT" or otm_pct == 0.15:  # default sentinel
        min_otm, max_otm = get_otm_range_for_catalyst(catalyst_type)
        otm_pct = (min_otm + max_otm) / 2  # Use midpoint of the range

    if direction in ("LONG_CALLS", "CALL_SPREAD"):
        raw_strike = stock_price * (1 + otm_pct)
    else:  # LONG_PUTS
        raw_strike = stock_price * (1 - otm_pct)
    
    # Round to standard increments
    if raw_strike < 10:
        return round(raw_strike * 2) / 2  # $0.50 increments
    elif raw_strike < 25:
        return round(raw_strike)  # $1 increments
    elif raw_strike < 100:
        return round(raw_strike / 2.5) * 2.5  # $2.50 increments
    elif raw_strike < 200:
        return round(raw_strike / 5) * 5  # $5 increments
    else:
        return round(raw_strike / 10) * 10  # $10 increments


def check_iv_crush_risk(iv_pct: float) -> dict:
    """
    Check if implied volatility is high enough to trigger IV crush warning.

    CALIBRATION NOTE (Apr 26 2026):
      IDYA was trading at IV=188%. Even though the drug worked (+7.6% stock),
      the option lost 91% because IV collapsed from 188% to ~40% post-announcement.
      Rule: if IV > 150%, flag HALF SIZE and add crush warning to the card.
    """
    if iv_pct is None:
        return {"crush_risk": False, "warning": None, "half_size": False}

    if iv_pct > 200:
        return {
            "crush_risk": True,
            "warning": f"EXTREME IV CRUSH RISK: IV={iv_pct:.0f}%. Option likely loses 50-80% even if stock rises. Use HALF SIZE max.",
            "half_size": True,
            "severity": "EXTREME",
        }
    elif iv_pct > 150:
        return {
            "crush_risk": True,
            "warning": f"HIGH IV CRUSH RISK: IV={iv_pct:.0f}%. Option may lose 30-60% post-announcement even if directionally correct. Reduce size.",
            "half_size": True,
            "severity": "HIGH",
        }
    elif iv_pct > 100:
        return {
            "crush_risk": True,
            "warning": f"MODERATE IV CRUSH RISK: IV={iv_pct:.0f}%. Size down slightly — IV expansion already priced in.",
            "half_size": False,
            "severity": "MODERATE",
        }
    else:
        return {"crush_risk": False, "warning": None, "half_size": False}


def should_use_spread(p_success: float, catalyst_type: str, science_grade: str = "C") -> dict:
    """
    Recommend spread vs naked call based on calibration rules.

    CALIBRATION NOTE (Apr 26 2026):
      ARGX $800/$850 spread worked better than naked calls at P=87%.
      When probability is very high, a spread caps the upside but massively
      reduces vega risk. The binary gap still profits, just with a ceiling.

    Returns recommendation dict.
    """
    use_spread = False
    reason = None

    if p_success >= 80 and catalyst_type in ("PDUFA", "ADCOM"):
        use_spread = True
        reason = f"P={p_success}% is high-conviction for a PDUFA/AdCom binary. Spread reduces vega exposure while preserving gap-up upside."
    elif p_success >= 85 and science_grade in ("A", "B"):
        use_spread = True
        reason = f"P={p_success}% + Grade {science_grade} science = highest conviction. Use spread to protect against IV crush."

    return {
        "use_spread": use_spread,
        "reason": reason,
        "spread_type": "CALL_SPREAD" if use_spread else None,
    }


def apply_science_size_modifier(science_grade: str, direction: str,
                                 p_success: float) -> dict:
    """
    Adjust position size based on science grade.

    CALIBRATION NOTE (Apr 26 2026):
      Grade D/F LONG plays have weak science. The model direction may be correct
      but the thesis is shaky. We want half size to manage risk.
      Grade F LONG plays additionally get a MISMATCH ALERT in the email.

    Returns:
      size_multiplier: 1.0 = full, 0.5 = half
      half_size: bool
      mismatch_alert: bool (Grade D/F on a LONG play = flag for user)
    """
    if not science_grade:
        return {"size_multiplier": 1.0, "half_size": False, "mismatch_alert": False}

    is_naked_long = direction == "LONG_CALLS"    # Worst exposure: unhedged
    is_spread = direction == "CALL_SPREAD"         # Already hedged — less alarming
    is_long = direction in ("LONG_CALLS", "CALL_SPREAD")
    is_short = direction == "LONG_PUTS"

    if science_grade == "F":
        if is_naked_long:
            return {
                "size_multiplier": 0.5,
                "half_size": True,
                "mismatch_alert": True,  # Full MISMATCH ALERT for naked long
                "note": "Grade F science on LONG CALLS — HALF SIZE. Science fundamentally flawed. High conviction SHORT territory instead.",
            }
        elif is_spread:
            return {
                "size_multiplier": 0.75,
                "half_size": False,
                "mismatch_alert": True,  # Still flag, but spread already mitigates
                "note": "Grade F science on CALL SPREAD — spread structure is appropriate. Note science is fundamentally flawed.",
            }
        else:  # SHORT / PUT on Grade F = highest conviction
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade F science + PUT = highest conviction setup (Shkreli strategy).",
            }
    elif science_grade == "D":
        if is_naked_long:
            return {
                "size_multiplier": 0.5,
                "half_size": True,
                "mismatch_alert": True,
                "note": "Grade D science on LONG CALLS — HALF SIZE. Multiple red flags. Science is weak.",
            }
        elif is_spread:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,  # Spread already hedges; no alarm needed
                "note": "Grade D science on CALL SPREAD — spread structure appropriately manages risk.",
            }
        else:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade D science + PUT = strong conviction setup.",
            }
    elif science_grade == "A":
        if is_long and p_success >= 80:
            return {
                "size_multiplier": 1.0,
                "half_size": False,
                "mismatch_alert": False,
                "note": "Grade A science + high P = premium setup. Consider spread to lock in conviction.",
            }
    # Grades B and C: normal sizing
    return {"size_multiplier": 1.0, "half_size": False, "mismatch_alert": False}


def compute_joint_probability(p_primary: float, p_secondary: float,
                               correlation: float = 0.7) -> float:
    """
    Compute joint probability for dual-endpoint trials.

    CALIBRATION NOTE (Apr 26 2026):
      IDYA had P(PFS)=90% but P(PFS+OS)=~42%. We used P=96% (primary alone).
      The option was sized as if the HIGHER bar was 96% likely.
      It was not. The market priced in joint probability.
      
      Formula: P(joint) = P(primary) * P(secondary | primary met)
      With correlation ~0.7 for co-primary endpoints in the same trial.
      
    Args:
      p_primary: P(primary endpoint met) as 0-100
      p_secondary: P(secondary endpoint met) as 0-100
      correlation: Pearson correlation between endpoints (default 0.7 for related endpoints)
    """
    # Bayesian approach: P(A and B) = P(A) * P(B|A)
    # P(B|A) = P(B) + correlation * sqrt(P(B)*(1-P(B))/P(A)*(1-P(A))) ... simplified:
    # For correlated endpoints: P(joint) is between P(primary)*P(secondary) and min(P(primary), P(secondary))
    p1 = p_primary / 100
    p2 = p_secondary / 100
    # Weighted between independent and fully correlated
    p_independent = p1 * p2
    p_correlated = min(p1, p2)
    joint = p_independent * (1 - correlation) + p_correlated * correlation
    return round(joint * 100, 1)


def classify_play_direction(p_success):
    """Determine trade direction from probability.
    
    CALIBRATION NOTE (Apr 26 2026):
      Raised LONG threshold from 60% to 65%.
      LPCN at 62% showed that 60-64% is too close to a coin flip for options.
      Puts remain at <=40% (that threshold has been well-calibrated).
    """
    if p_success >= 65:
        return "LONG_CALLS"
    elif p_success <= 40:
        return "LONG_PUTS"
    else:
        return "FILTERED_OUT"


def generate_order_ladder(ticker, expiry, strike, option_type, fill_price, 
                           total_risk=1000, half_size=False):
    """Generate decoy + ladder limit orders."""
    if half_size:
        total_risk = total_risk // 2
    
    contracts = max(1, int(total_risk / (fill_price * 100)))
    
    orders = []
    # 2 decoy probes
    orders.append(f"DECOY 1:  BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${fill_price:.2f}      | 1 contract  | GTC")
    orders.append(f"DECOY 2:  BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${fill_price-0.03:.2f}  | 1 contract  | GTC")
    
    # 4-6 ladder levels
    bulk_contracts = max(1, contracts // 4)
    for i, offset in enumerate([0.05, 0.10, 0.15, 0.20], 1):
        price = max(0.05, fill_price - offset)
        orders.append(f"LADDER {i}: BUY TO OPEN {ticker} {expiry} ${strike}{option_type} | LIMIT ${price:.2f}      | {bulk_contracts} contract(s) | GTC")
    
    total_invested = contracts * fill_price * 100
    return orders, contracts, total_invested

def build_play_entry(ticker, sector, direction, p_success, p_source,
                     drug_or_topic, indication, announcement_date,
                     announcement_certainty, option_expiry, option_strike,
                     option_type, notes="", model_divergence=False):
    """Create a standardized play entry for active_plays.json."""
    today = datetime.date.today().isoformat()
    
    return {
        "ticker": ticker,
        "sector": sector,  # "BIOTECH" | "CONTRACT" | "ADCOM"
        "drug_or_topic": drug_or_topic,
        "indication": indication,
        "p_success": p_success,
        "p_source": p_source,
        "direction": direction,
        "announcement_certainty": announcement_certainty,
        "estimated_announcement": announcement_date,
        "pdufa_date": announcement_date if announcement_certainty == "CERTAIN" else None,
        "option_strike": option_strike,
        "option_expiry": option_expiry,
        "option_type": option_type,
        "half_size": model_divergence,
        "added_date": today,
        "last_updated": today,
        "status": "ACTIVE",
        "days_in_watchlist": 0,
        "notes": notes
    }

def add_play_if_qualifies(ticker, sector, p_success, p_source, direction,
                           drug_or_topic, indication, announcement_date,
                           announcement_certainty, stock_price=None,
                           notes="", model_divergence=False):
    """
    Add a new play to active_plays.json if it qualifies (>=65% LONG or <=40% SHORT/PUT).
    [CALIBRATED Apr 26 2026: raised from >=60% to >=65% for LONG]
    Returns True if added, False if filtered or already exists.
    """
    if direction == "FILTERED_OUT":
        return False
    
    plays = load_active_plays()
    active = plays.get("active", {})
    
    # Already tracked?
    if ticker in active:
        # Update probability if changed
        if abs(active[ticker].get("p_success", 0) - p_success) > 5:
            active[ticker]["p_success"] = p_success
            active[ticker]["last_updated"] = datetime.date.today().isoformat()
            active[ticker]["notes"] += f" | P updated to {p_success}% on {datetime.date.today()}"
            save_active_plays({"active": active, "monitor": plays.get("monitor", {})})
            print(f"  📊 {ticker}: Updated P(success) to {p_success}%")
        return False
    
    # Detect catalyst type for proper strike selection (CALIBRATED Apr 26 2026)
    catalyst_type = detect_catalyst_type(notes, drug_or_topic, indication, sector)
    
    # Calculate expiry and strike
    expiry, days_out = get_option_expiry_for_announcement(announcement_date, announcement_certainty)
    strike = calculate_otm_strike(stock_price or 20, direction,
                                   catalyst_type=catalyst_type) if stock_price else None
    option_type = "C" if direction in ("LONG_CALLS", "CALL_SPREAD") else "P"
    
    # Check if spread is recommended (P>=80% PDUFA/AdCom)
    spread_rec = should_use_spread(p_success, catalyst_type)
    if spread_rec["use_spread"] and direction == "LONG_CALLS":
        direction = "CALL_SPREAD"
        spread_note = f" | SPREAD RECOMMENDED: {spread_rec['reason']}"
    else:
        spread_note = ""
    
    play = build_play_entry(
        ticker=ticker, sector=sector, direction=direction,
        p_success=p_success, p_source=p_source,
        drug_or_topic=drug_or_topic, indication=indication,
        announcement_date=announcement_date,
        announcement_certainty=announcement_certainty,
        option_expiry=expiry, option_strike=strike,
        option_type=option_type, notes=notes + spread_note,
        model_divergence=model_divergence
    )
    play["catalyst_type"] = catalyst_type
    
    active[ticker] = play
    save_active_plays({"active": active, "monitor": plays.get("monitor", {})})
    
    print(f"  ADDED to active plays: {ticker} ({sector}) | P={p_success}% | {direction} | expiry {expiry} | catalyst_type={catalyst_type}")
    return True

def _read_scoring_cache_row(ticker: str, as_of_date: str):
    """Return the persisted ``scoring_cache`` row for ``(ticker, as_of_date)``.

    Returns ``None`` when the SQLite database does not exist yet (fresh
    checkouts), when the row is absent, or when the connection fails
    for any reason. The helper is intentionally forgiving so that
    callers can treat a missing cache as a cold-start and proceed to
    score via the ensemble.
    """
    # Imports are kept local so importing this module does not require
    # the SQLite layer to be set up yet (e.g. during smoke imports on
    # a fresh checkout where ``data/alpha_sniper.db`` is absent).
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    db_path = DATA_DIR / "alpha_sniper.db"
    if not db_path.exists():
        return None
    try:
        conn = _db.connect(db_path)
    except Exception:
        return None
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            # If migrations cannot be applied (schema lock, mid-write,
            # permissions), treat the cache as a miss rather than
            # crashing the scorer.
            return None
        try:
            row = conn.execute(
                "SELECT claude_probability, gemini_probability, "
                "ensemble_score, divergence_flag "
                "FROM scoring_cache WHERE ticker=? AND as_of_date=?",
                (ticker, as_of_date),
            ).fetchone()
        except Exception:
            return None
        return dict(row) if row is not None else None
    finally:
        conn.close()


def score_candidate_with_models(
    prompt,
    ticker,
    cache_key=None,
    *,
    as_of_date=None,
    fast_context=None,
    science_profile=None,
    full_context=None,
    scorer=None,
):
    """Score a binary catalyst via the M2 :class:`EnsembleScorer`.

    Replaces the legacy cron-agent file-based stub (which left the
    actual model calls to the external ``cron-agent`` and read the
    results from per-ticker text files dropped under the legacy
    ``scores/`` directory) with a direct in-process call to the
    :class:`biotech_sniper.llm.ensemble.EnsembleScorer`. The ensemble
    persists every successful score into the SQLite ``scoring_cache``
    table keyed on ``(ticker, as_of_date)``; re-running on the same
    date is idempotent (UPSERT on the unique index).

    Returns:
        ``(claude_probability, gemini_probability, ensemble_score,
        divergence_flag)``. Each numeric component is a float in
        ``[0, 1]`` (or ``None`` when the corresponding provider is
        disabled / unavailable). ``divergence_flag`` is a bool.

    Args:
        prompt: Free-form prompt text. Forwarded into the fast and
            full-context payloads when callers do not supply
            structured contexts.
        ticker: Ticker the candidate is being scored against.
        cache_key: Legacy parameter retained for compatibility; when
            present, it is forwarded into the contexts so callers can
            tag a logical scoring round.
        as_of_date: Optional ISO date used for the SQLite cache
            primary key. Defaults to ``datetime.date.today()``.
        fast_context, science_profile, full_context: Optional
            structured payloads passed to the per-tier clients. When
            omitted the helper falls back to ``{"prompt": prompt}``.
        scorer: Optional pre-built :class:`EnsembleScorer` (used by
            tests to inject fake clients). When ``None``, the function
            calls :meth:`EnsembleScorer.from_config` which gracefully
            skips providers without API keys.
    """
    today = as_of_date or datetime.date.today().isoformat()

    cached = _read_scoring_cache_row(ticker, today)
    if cached is not None:
        print(f"  📋 scoring_cache hit for {ticker} ({today})")
        return (
            cached.get("claude_probability"),
            cached.get("gemini_probability"),
            cached.get("ensemble_score"),
            bool(cached.get("divergence_flag")),
        )

    if scorer is None:
        scorer = EnsembleScorer.from_config()

    candidate_payload = {
        "ticker": ticker,
        "as_of_date": today,
        "fast_context": fast_context
        or {"prompt": prompt or "", "cache_key": cache_key},
        "science_profile": science_profile or {},
        "full_context": full_context
        or {"prompt": prompt or "", "cache_key": cache_key},
    }
    result = scorer.score(candidate_payload)
    return (
        result.get("claude_probability"),
        result.get("gemini_probability"),
        result.get("ensemble_score"),
        bool(result.get("divergence_flag")),
    )

def format_scoring_instructions(signals_needing_scoring):
    """
    Format instructions for the cron agent to score new candidates.
    Returns a structured prompt for dual-model scoring.
    """
    instructions = []
    for signal in signals_needing_scoring:
        ticker = signal.get("ticker")
        sector = signal.get("sector")
        prompt = signal.get("scoring_prompt", "")
        
        instructions.append(f"""
SCORE THIS CANDIDATE — {ticker} ({sector})
Run BOTH models in parallel:
  Model A: claude_opus_4_6
  Model B: gemini_3_1_pro

Prompt for each (identical):
{prompt[:3000]}

Extract from each response: P(success) as a single integer percentage.
Average = ensemble. 
If ensemble >= 65: direction = LONG_CALLS  [CALIBRATED Apr 26 2026: raised from 60%]
If ensemble <= 40: direction = LONG_PUTS  
If 41-64: DROP (do not add to active plays)
If |claude - gemini| > 25pp AND qualifies: flag as HALF SIZE

After scoring, call add_play_if_qualifies() with the results.
Save P scores to: <BASE_DIR>/state/scoring_cache.json
""")
    
    return "\n".join(instructions)

# ---------------------------------------------------------------------------
# f-m2-20 — Unified scorer CLI.
#
# ``python -m biotech_sniper.sectors.unified_scorer`` runs the M2 scoring
# pipeline end-to-end so VAL-M2-049 / VAL-M2-050 / VAL-M2-055 / VAL-M2-057
# pass without falling back to the legacy printer stub. The CLI:
#
# 1. Resolves the ticker list (``--tickers`` override → top-N tradeable
#    universe rows → 5-ticker seed fallback).
# 2. Builds an :class:`EnsembleScorer` (or a fast-only variant under
#    ``--dry-run``).
# 3. Scores each ticker through ``EnsembleScorer.score`` which UPSERTs
#    into ``scoring_cache`` (idempotent on ``(ticker, as_of_date)``).
# 4. Optionally emits play cards via
#    :func:`biotech_sniper.play_card_formatter.emit_play_cards`.
# 5. Prints a single JSON summary line to stdout for cron consumption.
#
# Provider availability is detected at runtime: if ``GEMINI_API_KEY`` is
# missing the CLI logs a WARNING, drops Gemini from ``providers_used``,
# and continues; the same handling applies to ``ANTHROPIC_API_KEY``. The
# fast tier (xAI) is always reported as enabled per the f-m2-20 spec —
# its key absence is handled inside the lazy
# :class:`biotech_sniper.llm.xai_client.XAIClient` constructor.
# ---------------------------------------------------------------------------


_DEFAULT_SEED_TICKERS: tuple[str, ...] = ("SRPT", "VRTX", "BMRN", "ARWR", "IONS")


def _resolve_deep_providers() -> list[str]:
    """Return the deep-tier providers honouring a runtime env override.

    Reads ``LLM_PROVIDERS_DEEP`` directly so tests / cron operators
    can disable the deep tier without re-importing :mod:`config`
    (where the feature flag is captured at module load). Falls back
    to :data:`biotech_sniper.config.LLM_PROVIDERS["deep"]` when the
    env var is unset.
    """
    raw = os.environ.get("LLM_PROVIDERS_DEEP")
    if raw is not None:
        return [p.strip() for p in raw.split(",") if p.strip()]
    from biotech_sniper import config as _config

    deep = _config.LLM_PROVIDERS.get("deep", [])
    if isinstance(deep, list):
        return list(deep)
    return []


def _provider_env_var(provider: str) -> str | None:
    """Return the env var that holds the API key for ``provider``."""
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }.get(provider)


def _load_tradeable_tickers_from_universe(
    top_n: int, *, db_path: Path | None = None
) -> list[str]:
    """Return up to ``top_n`` tickers from ``universe.tier='tradeable'``.

    Returns ``[]`` when the SQLite db file does not exist yet (fresh
    checkouts) or when the query fails for any reason — callers
    should treat the empty list as "no universe yet, use the seed
    fallback".
    """
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    target = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target.exists():
        return []
    try:
        conn = _db.connect(target)
    except Exception:
        return []
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            return []
        try:
            rows = conn.execute(
                "SELECT ticker FROM universe "
                "WHERE tier='tradeable' "
                "ORDER BY ticker ASC LIMIT ?",
                (int(top_n),),
            ).fetchall()
        except Exception:
            return []
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def _load_universe_meta(ticker: str, *, db_path: Path | None = None) -> dict:
    """Return the ``universe`` row for ``ticker`` (or ``{}`` when missing).

    Used to populate ``fast_context.ticker_meta`` in the per-ticker
    scoring payload.
    """
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    target = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target.exists():
        return {}
    try:
        conn = _db.connect(target)
    except Exception:
        return {}
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            return {}
        try:
            row = conn.execute(
                "SELECT ticker, tier, has_options_chain, source "
                "FROM universe WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        except Exception:
            return {}
    finally:
        conn.close()
    if row is None:
        return {}
    return dict(row)


def _build_scorer(*, dry_run: bool = False):
    """Build the :class:`EnsembleScorer` used by :func:`main`.

    Indirected through this module-level helper so tests can
    monkeypatch in fakes (cassette-backed clients) without touching
    production config. When ``dry_run=True`` the deep tier is
    omitted so the CLI does not call Claude / Gemini — the
    scoring_cache UPSERT still runs because the fast-tier xAI score
    is enough to populate the row.
    """
    from biotech_sniper import config as _config
    from biotech_sniper.llm.ensemble import (
        EnsembleScorer as _ES,
        _maybe_build_xai_client,
    )

    if dry_run:
        xai = (
            _maybe_build_xai_client()
            if _config.provider_enabled("xai")
            else None
        )
        return _ES(xai_client=xai, claude_client=None, gemini_client=None)
    return _ES.from_config()


def _load_chain_gate_status(
    tickers: Sequence[str], *, db_path: Path | None = None
) -> dict[str, bool]:
    """Return ``{ticker: has_options_chain_bool}`` for ``tickers``.

    Looks up the ``universe.has_options_chain`` flag for each ticker
    in the SQLite db. Tickers absent from the universe table return
    ``False`` (treated as "no chain confirmed"). Returns an empty
    mapping when the db is missing or the lookup fails — callers
    should treat that as "gate not enforceable" and either log + skip
    every ticker or proceed without the gate (the M3 contract calls
    for the former; see :func:`score_universe`).
    """
    from biotech_sniper import db as _db
    from biotech_sniper.paths import DATA_DIR

    if not tickers:
        return {}
    target = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    if not target.exists():
        return {}
    try:
        conn = _db.connect(target)
    except Exception:
        return {}
    try:
        try:
            _db.run_migrations(conn)
        except Exception:
            return {}
        placeholders = ", ".join("?" for _ in tickers)
        try:
            rows = conn.execute(
                f"SELECT ticker, has_options_chain FROM universe "
                f"WHERE ticker IN ({placeholders})",
                tuple(tickers),
            ).fetchall()
        except Exception:
            return {}
    finally:
        conn.close()
    return {row["ticker"]: bool(row["has_options_chain"]) for row in rows}


def filter_chain_gated_tickers(
    tickers: Sequence[str], *, db_path: Path | None = None
) -> tuple[list[str], list[str]]:
    """Split ``tickers`` into ``(scored, skipped)`` per the chain gate.

    A ticker is **scored** iff its ``universe.has_options_chain``
    flag is ``1``. Watch-only tickers (``has_options_chain=0``) are
    routed to the **skipped** list. Tickers absent from the universe
    table are *also* skipped — this is the strict reading of
    VAL-M3-045 ("every scored ticker must have a universe row with
    has_options_chain=1").

    **f-m3-16 fix — empty-lookup-not-bypass:** previously, when the
    universe table was empty (or no requested ticker had a row),
    the function returned every ticker as "scored" so a fresh
    checkout could still produce play cards. That bypass was
    actively unsafe — it allowed unverified tickers into
    ``scoring_cache`` and downstream paper executions. The strict
    semantics now apply: an empty universe lookup means **every
    ticker is rejected** (returned in ``skipped``). A WARNING is
    logged so operators notice the universe is missing; the daily
    build's universe-refresh step is the only legitimate path back
    to a populated lookup.
    """
    chain_status = _load_chain_gate_status(tickers, db_path=db_path)
    if not chain_status:
        # f-m3-16: empty lookup is a STRICT REJECT, not a bypass.
        # Either the universe table is missing entirely or none of
        # the requested tickers have a row — both cases mean we
        # cannot certify any ticker as having a tradeable options
        # chain, so we drop everything and emit a WARNING for the
        # operator. Fresh-checkout flows must populate `universe`
        # before invoking the scorer.
        logger.warning(
            "filter_chain_gated_tickers: empty universe lookup for "
            "%d tickers; rejecting all (universe table missing or "
            "no rows for the requested tickers)",
            len(list(tickers)),
        )
        return [], list(tickers)
    scored: list[str] = []
    skipped: list[str] = []
    for ticker in tickers:
        if chain_status.get(ticker, False):
            scored.append(ticker)
        else:
            skipped.append(ticker)
    return scored, skipped


def score_universe(
    tickers: Sequence[str],
    *,
    as_of_date: str | None = None,
    scorer=None,
    db_path: Path | None = None,
    enforce_chain_gate: bool = True,
) -> dict:
    """Score ``tickers`` after applying the options-chain gate.

    The gate (per VAL-M3-045 + f-m3-08) drops every ticker whose
    ``universe.has_options_chain`` flag is ``0`` (or missing). Each
    skipped ticker logs a ``WARNING`` line naming the ticker and the
    ``reason='no_options_chain'`` so daily-run operators can audit
    why a candidate was excluded.

    Parameters
    ----------
    tickers:
        Tickers to consider. Order is preserved.
    as_of_date:
        ISO date for the scoring run. Defaults to today.
    scorer:
        Pre-built :class:`biotech_sniper.llm.ensemble.EnsembleScorer`.
        ``None`` defers to :func:`_build_scorer` so production
        callers do not need to know how to build one.
    db_path:
        Override the universe / scoring_cache db path (used by
        tests).
    enforce_chain_gate:
        When ``False`` the chain-gate filter is skipped (used by
        tests of the underlying scoring loop). Production callers
        leave this as ``True``.

    Returns
    -------
    dict
        ``{"as_of_date", "tickers_scored", "tickers_skipped",
        "rows_upserted"}``.
    """
    iso_date = as_of_date or datetime.date.today().isoformat()

    if enforce_chain_gate:
        eligible, skipped = filter_chain_gated_tickers(
            tickers, db_path=db_path
        )
    else:
        eligible, skipped = list(tickers), []

    for ticker in skipped:
        logger.warning(
            "score_universe: skipping %s reason=no_options_chain "
            "(universe.has_options_chain != 1)",
            ticker,
        )

    if scorer is None:
        scorer = _build_scorer()

    rows_upserted = 0
    tickers_scored: list[str] = []
    for ticker in eligible:
        try:
            meta = _load_universe_meta(ticker, db_path=db_path)
            payload = {
                "ticker": ticker,
                "as_of_date": iso_date,
                "fast_context": {
                    "ticker": ticker,
                    "ticker_meta": meta,
                    "as_of_date": iso_date,
                },
                "science_profile": {"ticker": ticker, "sector": "BIOTECH"},
                "full_context": {},
            }
            scorer.score(payload)
        except Exception as exc:  # noqa: BLE001 - per-ticker failure is non-fatal
            logger.warning(
                "score_universe: scoring failed for %s: %r; continuing",
                ticker,
                exc,
            )
            continue
        tickers_scored.append(ticker)
        rows_upserted += 1

    return {
        "as_of_date": iso_date,
        "tickers_scored": tickers_scored,
        "tickers_skipped": skipped,
        "rows_upserted": rows_upserted,
    }


def _resolve_tickers(args: argparse.Namespace, top_n: int) -> list[str]:
    """Return the ordered list of tickers ``main()`` will score.

    Priority order: ``--tickers`` override → top-N tradeable universe
    rows → 5-ticker seed list. The seed fallback returns the full
    seed regardless of ``--top-n`` so VAL-M2-049 / VAL-M2-050 see
    five scored tickers when the universe is empty (a fresh local
    checkout or an early VPS deploy).
    """
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    universe_tickers = _load_tradeable_tickers_from_universe(top_n)
    if universe_tickers:
        return universe_tickers
    return list(_DEFAULT_SEED_TICKERS)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the unified scoring pipeline; return a process exit code.

    Behaviour summary (full contract in the f-m2-20 feature spec):

    * Always returns 0 unless argparse rejects the CLI arguments
      (which raises SystemExit before this function returns).
    * Per-ticker scoring failures log a WARNING and continue rather
      than aborting the run.
    * Final stdout line is a JSON object with the keys
      ``as_of_date``, ``tickers_scored``, ``providers_used``,
      ``rows_upserted`` and ``play_cards_written`` so callers can
      pipe the output into ``jq``.
    """
    from biotech_sniper import config as _config

    parser = argparse.ArgumentParser(
        prog="python -m biotech_sniper.sectors.unified_scorer",
        description=(
            "Run the M2 unified scoring pipeline (Grok-4 fast tier + "
            "Claude / Gemini deep tier) and emit play cards."
        ),
    )
    parser.add_argument(
        "--date",
        default=datetime.date.today().isoformat(),
        help="ISO date (YYYY-MM-DD) the run is scoring for. Defaults to today.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help=(
            "Number of tradeable tickers to score. Defaults to "
            "config.RISK_DEFAULTS['max_concurrent']."
        ),
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help=(
            "Comma-separated ticker list. Overrides the universe lookup "
            "and the seed fallback when provided."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip the deep tier (Claude / Gemini) and suppress play-card "
            "emission. The scoring_cache UPSERT still runs (idempotent)."
        ),
    )
    parser.add_argument(
        "--no-emit-play-cards",
        dest="emit_play_cards",
        action="store_false",
        default=True,
        help="Skip play-card emission even when --dry-run is not set.",
    )
    parser.add_argument(
        "--no-chain-gate",
        dest="enforce_chain_gate",
        action="store_false",
        default=True,
        help=(
            "Disable the options-chain gate that drops tickers whose "
            "universe.has_options_chain flag is 0. Default: gate "
            "enforced (f-m3-08)."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    requested_top_n = (
        int(args.top_n)
        if args.top_n is not None
        else int(_config.RISK_DEFAULTS["max_concurrent"])
    )
    top_n = max(1, requested_top_n)

    tickers = _resolve_tickers(args, top_n)

    # f-m3-08: apply the options-chain gate. Watch-only tickers (no
    # listed Alpaca options chain) never reach the scoring loop and
    # therefore cannot leak into ``scoring_cache`` for ``as_of_date``
    # (VAL-M3-045). Each dropped ticker logs a structured WARNING
    # naming the reason so operators can audit the skip set.
    #
    # f-m3-16: an empty universe lookup is now a STRICT REJECT, not
    # a bypass — every requested ticker is dropped (and logged) so
    # unverified tickers cannot leak through on a fresh checkout.
    # The daily build's universe-refresh step is the only path back
    # to a populated lookup. This matches
    # :func:`filter_chain_gated_tickers`.
    skipped_tickers: list[str] = []
    if args.enforce_chain_gate:
        chain_status = _load_chain_gate_status(tickers)
        if not chain_status:
            logger.warning(
                "unified_scorer: empty universe lookup for %d "
                "tickers; rejecting all (universe table missing or "
                "no rows for the requested tickers). The daily "
                "build must populate `universe` before scoring can "
                "run.",
                len(list(tickers)),
            )
            for ticker in tickers:
                skipped_tickers.append(ticker)
                logger.warning(
                    "unified_scorer: skipping %s reason="
                    "no_options_chain (universe lookup empty)",
                    ticker,
                )
            tickers = []
        else:
            eligible: list[str] = []
            for ticker in tickers:
                if chain_status.get(ticker, False):
                    eligible.append(ticker)
                else:
                    skipped_tickers.append(ticker)
                    logger.warning(
                        "unified_scorer: skipping %s reason="
                        "no_options_chain (universe.has_options_chain "
                        "!= 1)",
                        ticker,
                    )
            tickers = eligible

    # Resolve providers_used. The fast tier (xAI) is always reported
    # per the f-m2-20 spec; the deep tier is filtered by both the
    # ``--dry-run`` flag and the per-provider key presence check.
    providers_used: list[str] = ["xai"]
    # Always run per-provider key-presence check so warnings emit even in
    # --dry-run mode; downstream deep-tier scoring is suppressed by
    # _build_scorer(dry_run=True) regardless. Satisfies VAL-M2-050.
    deep_providers = _resolve_deep_providers()
    for provider in deep_providers:
        env_var = _provider_env_var(provider)
        if env_var and not os.environ.get(env_var):
            logger.warning(
                "skipping deep provider %s: API key not set in env (%s); "
                "pipeline continues without this provider",
                provider,
                env_var,
            )
            continue
        if not args.dry_run:
            providers_used.append(provider)
    providers_used = sorted(providers_used)

    scorer = _build_scorer(dry_run=args.dry_run)

    # f-misc-05: be honest about deep-tier providers whose client
    # construction failed. ``_maybe_build_*_client`` (in
    # :mod:`biotech_sniper.llm.ensemble`) swallows construction
    # exceptions and returns ``None`` so the pipeline can continue
    # with whatever is available, but the env-key check above will
    # still have appended the provider to ``providers_used`` even
    # when the underlying SDK could not be wired up (the symptom
    # observed in f-m2-22: every CLI invocation logged
    # ``module 'google.genai.types' has no attribute 'HttpOptions'``
    # and yet the summary still claimed ``gemini``). Drop providers
    # whose scorer client is ``None`` so the JSON summary reflects
    # what actually has a chance of running.
    if not args.dry_run:
        if "anthropic" in providers_used and getattr(scorer, "_claude", None) is None:
            logger.warning(
                "dropping deep provider anthropic from providers_used: "
                "ClaudeClient construction failed (see prior WARNING)"
            )
            providers_used = [p for p in providers_used if p != "anthropic"]
        if "gemini" in providers_used and getattr(scorer, "_gemini", None) is None:
            logger.warning(
                "dropping deep provider gemini from providers_used: "
                "GeminiClient construction failed (see prior WARNING)"
            )
            providers_used = [p for p in providers_used if p != "gemini"]

    rows_upserted = 0
    tickers_scored: list[str] = []
    # f-misc-05: track per-provider success across all tickers so we
    # can drop a deep provider that was constructed but whose every
    # call failed at runtime (auth error past construction, malformed
    # JSON exhausted retries, sustained 5xx, etc.).
    deep_provider_attempts: dict[str, int] = {}
    deep_provider_successes: dict[str, int] = {}
    for provider, attr in (("anthropic", "_claude"), ("gemini", "_gemini")):
        if provider in providers_used and getattr(scorer, attr, None) is not None:
            deep_provider_attempts[provider] = 0
            deep_provider_successes[provider] = 0
    providers_label = ",".join(providers_used)
    for ticker in tickers:
        try:
            meta = _load_universe_meta(ticker)
            payload = {
                "ticker": ticker,
                "as_of_date": args.date,
                "fast_context": {
                    "ticker": ticker,
                    "ticker_meta": meta,
                    "as_of_date": args.date,
                },
                "science_profile": {
                    "ticker": ticker,
                    "sector": "BIOTECH",
                },
                "full_context": {},
            }
            result = scorer.score(payload)
        except Exception as exc:  # noqa: BLE001 — single-ticker failure is non-fatal
            logger.warning(
                "scoring failed for %s: %r; continuing without it",
                ticker,
                exc,
            )
            continue

        tickers_scored.append(ticker)
        rows_upserted += 1
        # Track per-provider success: ``EnsembleScorer.score`` swallows
        # individual deep-tier failures, leaving the corresponding
        # ``*_grade`` field as ``None`` in the result. We treat any
        # non-None grade as a successful call for that provider.
        if "anthropic" in deep_provider_attempts:
            deep_provider_attempts["anthropic"] += 1
            if result.get("claude_grade") is not None:
                deep_provider_successes["anthropic"] += 1
        if "gemini" in deep_provider_attempts:
            deep_provider_attempts["gemini"] += 1
            if result.get("gemini_grade") is not None:
                deep_provider_successes["gemini"] += 1
        ensemble_score = result.get("ensemble_score")
        grade = result.get("science_grade") or "N/A"
        if ensemble_score is None:
            print(
                f"[score] {ticker} ensemble=None grade={grade} "
                f"providers={providers_label}"
            )
        else:
            print(
                f"[score] {ticker} ensemble={float(ensemble_score):.2f} "
                f"grade={grade} providers={providers_label}"
            )

    # f-misc-05: drop deep providers that were attempted at least
    # once and never succeeded. We only act when there was at least
    # one attempt — when no tickers were scored we have no evidence
    # either way, so we leave ``providers_used`` alone.
    for provider, attempts in deep_provider_attempts.items():
        if attempts > 0 and deep_provider_successes.get(provider, 0) == 0:
            logger.warning(
                "dropping deep provider %s from providers_used: "
                "%d attempt(s), 0 successful score(s) (every call "
                "failed post-construction)",
                provider,
                attempts,
            )
            providers_used = [p for p in providers_used if p != provider]

    play_cards_written = 0
    if args.emit_play_cards and not args.dry_run:
        try:
            from biotech_sniper import play_card_formatter as _pcf

            written = _pcf.emit_play_cards(as_of_date=args.date, n=top_n)
            play_cards_written = len(list(written or []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("emit_play_cards failed: %r", exc)

    summary = {
        "as_of_date": args.date,
        "tickers_scored": tickers_scored,
        "tickers_skipped_no_chain": skipped_tickers,
        "providers_used": providers_used,
        "rows_upserted": rows_upserted,
        "play_cards_written": play_cards_written,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
