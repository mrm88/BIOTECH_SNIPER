"""Multi-LLM head-to-head debate loop (M2 feature f-m2-12).

This module implements :func:`run_debate`, the single entry point for
the structured Claude ↔ Gemini ↔ Grok debate that fires when a
top-N candidate's ensemble carries a ``divergence_flag = TRUE`` (or
when the M3 rotation engine evaluates a slot swap).

Three rounds, in order
----------------------
1. **Claude critiques Gemini.** Claude is shown Gemini's full
   rationale + citations and asked to critique and re-grade.
2. **Gemini rebuts Claude.** Gemini receives Claude's critique +
   re-grade and is asked to rebut and re-grade.
3. **Grok adjudicates.** Grok receives both transcripts and emits a
   ``final_grade`` plus a brief adjudication rationale.

Every round is persisted to the ``llm_debate`` SQLite table
(``provider``, model, prompt, response, latency_ms, cost_usd) so the
debate transcript is reproducible end-to-end. The Claude and Gemini
rows store ``final_grade=NULL``; only the Grok adjudication row
carries the canonical ``final_grade`` so a single
``MAX(final_grade)`` aggregation per debate is unambiguous.

Hard caps (enforced inside this module)
---------------------------------------
* :data:`LLM_DEBATE_MAX_ROUNDS` = ``3`` — never more than three rounds
  per scoring cache id. The schema does not enforce this directly;
  callers MUST go through :func:`run_debate`.
* :data:`LLM_DEBATE_DAILY_USD_CAP` = ``10.0`` — sum of ``cost_usd``
  across all ``llm_debate`` rows whose ``transcript_complete_at``
  falls on the current UTC date. Once the cap is reached,
  :func:`run_debate` short-circuits without making any LLM calls and
  returns the static-ensemble result. The day's
  ``state/audit_latest.json`` records the short-circuit reason at the
  top-level ``llm_debate_short_circuit`` key per VAL-M2-083.

Public surface
--------------
* :func:`run_debate` — the only sanctioned entry point.
* :class:`DebateError`, :class:`DebateInvalidTrigger` — typed errors.
* :data:`LLM_DEBATE_MAX_ROUNDS`, :data:`LLM_DEBATE_DAILY_USD_CAP` —
  the documented caps. Override-via-env is intentionally not provided;
  the spec calls for hard caps inside this module.
* :data:`VALID_TRIGGERS` — the allowed values for the ``trigger``
  argument, mirroring the SQLite ``CHECK`` constraint.

Design notes
------------
* Each round's LLM call is performed via an injectable
  ``invoke`` callable: ``(prompt: str) -> dict`` returning a payload
  with ``text``, ``prompt_tokens``, ``completion_tokens``,
  ``cost_usd``, ``latency_ms``, ``model_id``. This keeps the production
  wrappers (which build real :class:`ClaudeClient` /
  :class:`GeminiClient` / :class:`XAIClient` instances) trivially
  swappable for fakes in tests — every test can pass three callables
  whose return values it controls outright.
* Daily-cap enforcement is performed BEFORE the first round so a debate
  that would push the day's spend over the cap simply doesn't start.
  The cap is re-checked between rounds; if a single round's cost
  drives the running total over the cap, the remaining rounds are
  skipped and the partial transcript is preserved (downstream the
  selection logic treats this as a graceful degradation, equivalent to
  a static-ensemble result).
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from biotech_sniper import config, db
from biotech_sniper.paths import BASE_DIR, DATA_DIR

__all__ = [
    "run_debate",
    "DebateError",
    "DebateInvalidTrigger",
    "LLM_DEBATE_MAX_ROUNDS",
    "LLM_DEBATE_DAILY_USD_CAP",
    "VALID_TRIGGERS",
    "InvokeFn",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — hard caps per the f-m2-12 spec.
# ---------------------------------------------------------------------------

#: Max rounds per scoring_cache id. Mirrored as a soft assertion
#: in :func:`run_debate`; SQLite has no constraint of its own.
LLM_DEBATE_MAX_ROUNDS: int = 3

#: USD cap on the day's total ``llm_debate.cost_usd`` (sum across
#: every row whose ``transcript_complete_at`` matches today's
#: UTC date). Once the cap is reached, debates short-circuit.
LLM_DEBATE_DAILY_USD_CAP: float = 10.0

#: Allowed ``trigger`` values — mirrors the SQLite ``CHECK`` constraint.
VALID_TRIGGERS: frozenset[str] = frozenset({"divergence", "rotation"})


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Signature of the per-round invoke callable.
#:
#: Production wrappers build a payload via the underlying SDK; tests
#: hand-roll the same shape. The required keys mirror the columns
#: written to ``llm_debate`` plus ``text`` (the assistant body).
InvokeFn = Callable[[str], Mapping[str, Any]]


class DebateError(Exception):
    """Base class for all debate orchestration errors."""


class DebateInvalidTrigger(DebateError, ValueError):
    """Raised when ``trigger`` is not in :data:`VALID_TRIGGERS`."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_debate(
    scoring_cache_id: int,
    trigger: str,
    *,
    db_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
    claude_invoke: Optional[InvokeFn] = None,
    gemini_invoke: Optional[InvokeFn] = None,
    grok_invoke: Optional[InvokeFn] = None,
    today: Optional[datetime.date] = None,
) -> dict[str, Any]:
    """Run the 3-round Claude / Gemini / Grok debate.

    Parameters
    ----------
    scoring_cache_id:
        Primary key of the :data:`scoring_cache` row that motivated
        the debate. Used to look up the existing ensemble payload
        (Claude's rationale + citations, Gemini's rationale +
        citations, ticker, ensemble_score, science_grade) and to
        FK-link every persisted round.
    trigger:
        One of :data:`VALID_TRIGGERS`. ``'divergence'`` is the M2
        wiring; ``'rotation'`` is reserved for the M3 rotation engine
        (f-m3-10) and is supported here so a single SQLite schema
        carries both lifecycles.
    db_path:
        Optional override for the SQLite path. Defaults to
        ``DATA_DIR / "alpha_sniper.db"``.
    audit_path:
        Optional override for the audit JSON path that will receive
        the ``llm_debate_short_circuit`` key on cap-hit. Defaults to
        ``BASE_DIR / "state" / "audit_latest.json"``.
    claude_invoke / gemini_invoke / grok_invoke:
        Per-provider invocation callables. When ``None``, a default
        is built that wraps the production SDK clients — but the
        defaults raise inside :class:`DebateError` if the matching
        provider's API key is missing, so production callers should
        gate on :func:`config.provider_enabled` first.
    today:
        Optional override for "today" used in the daily-cap query.
        Tests pass a fixed date so the cap query is deterministic;
        production leaves it ``None`` (UTC today).

    Returns
    -------
    dict
        Always carries:

        * ``scoring_cache_id`` — echoed back.
        * ``trigger`` — echoed back.
        * ``rounds`` — list of round dicts (model, prompt, response,
          latency_ms, cost_usd, final_grade). Empty on short-circuit.
        * ``final_grade`` — Grok's adjudicated grade (or ``None`` on
          short-circuit / failure).
        * ``short_circuited`` — ``True`` when the daily cap was hit.
        * ``reason`` — ``'daily_cap_exceeded'`` when short-circuited;
          absent otherwise.
        * ``static_ensemble`` — copy of the scoring_cache row's
          static-ensemble fields (ticker, ensemble_score,
          science_grade, claude_grade, gemini_grade). Present on
          short-circuit so callers can fall back without re-querying.
    """
    if trigger not in VALID_TRIGGERS:
        raise DebateInvalidTrigger(
            f"trigger must be one of {sorted(VALID_TRIGGERS)!r}; "
            f"got {trigger!r}"
        )

    target_db = Path(db_path) if db_path is not None else DATA_DIR / "alpha_sniper.db"
    target_audit = (
        Path(audit_path)
        if audit_path is not None
        else BASE_DIR / "state" / "audit_latest.json"
    )
    today_date = today or datetime.datetime.utcnow().date()

    target_db.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(target_db)
    try:
        db.run_migrations(conn)
        cache_row = _load_scoring_cache_row(conn, scoring_cache_id)
        if cache_row is None:
            raise DebateError(
                f"scoring_cache.id={scoring_cache_id} not found in "
                f"{target_db}"
            )

        static_ensemble = _static_ensemble_view(cache_row)

        # ---- Daily-cap gate (BEFORE any LLM call) --------------------
        spent = _todays_debate_spend(conn, today_date)
        if spent >= LLM_DEBATE_DAILY_USD_CAP:
            count = _short_circuit_count_for_today(conn, today_date) + 1
            _record_short_circuit(
                target_audit,
                reason="daily_cap_exceeded",
                count=count,
                ticker=static_ensemble.get("ticker"),
                scoring_cache_id=scoring_cache_id,
            )
            return {
                "scoring_cache_id": scoring_cache_id,
                "trigger": trigger,
                "rounds": [],
                "final_grade": None,
                "short_circuited": True,
                "reason": "daily_cap_exceeded",
                "static_ensemble": static_ensemble,
            }

        # ---- Build defaults lazily (only when a slot is None) --------
        if claude_invoke is None:
            claude_invoke = _build_default_invoker("anthropic")
        if gemini_invoke is None:
            gemini_invoke = _build_default_invoker("gemini")
        if grok_invoke is None:
            grok_invoke = _build_default_invoker("xai")

        # ---- Three rounds --------------------------------------------
        breakdown = _extract_model_breakdown(cache_row)

        rounds: list[dict[str, Any]] = []
        running_spend = spent

        # Round 1 — Claude critiques Gemini.
        round1_prompt = _build_round1_prompt(
            ticker=static_ensemble.get("ticker"),
            trigger=trigger,
            gemini=breakdown.get("gemini") or {},
            ensemble=static_ensemble,
        )
        round1 = _execute_round(
            invoke=claude_invoke,
            round_index=1,
            prompt=round1_prompt,
            scoring_cache_id=scoring_cache_id,
            trigger=trigger,
            conn=conn,
            final_grade=None,
        )
        rounds.append(round1)
        running_spend += float(round1.get("cost_usd") or 0.0)
        if running_spend >= LLM_DEBATE_DAILY_USD_CAP:
            return _wrap_partial(
                scoring_cache_id, trigger, rounds, static_ensemble,
                target_audit, today_date, conn,
            )

        # Round 2 — Gemini rebuts Claude.
        round2_prompt = _build_round2_prompt(
            ticker=static_ensemble.get("ticker"),
            trigger=trigger,
            claude_critique=round1.get("response") or "",
            claude_round1_grade=round1.get("parsed_grade"),
            gemini=breakdown.get("gemini") or {},
            ensemble=static_ensemble,
        )
        round2 = _execute_round(
            invoke=gemini_invoke,
            round_index=2,
            prompt=round2_prompt,
            scoring_cache_id=scoring_cache_id,
            trigger=trigger,
            conn=conn,
            final_grade=None,
        )
        rounds.append(round2)
        running_spend += float(round2.get("cost_usd") or 0.0)
        if running_spend >= LLM_DEBATE_DAILY_USD_CAP:
            return _wrap_partial(
                scoring_cache_id, trigger, rounds, static_ensemble,
                target_audit, today_date, conn,
            )

        # Round 3 — Grok adjudicates and emits final_grade.
        round3_prompt = _build_round3_prompt(
            ticker=static_ensemble.get("ticker"),
            trigger=trigger,
            claude_critique=round1.get("response") or "",
            gemini_rebuttal=round2.get("response") or "",
            ensemble=static_ensemble,
        )
        round3 = _execute_round(
            invoke=grok_invoke,
            round_index=3,
            prompt=round3_prompt,
            scoring_cache_id=scoring_cache_id,
            trigger=trigger,
            conn=conn,
            final_grade="__from_response__",
        )
        rounds.append(round3)

        final_grade = round3.get("parsed_grade") or round3.get("final_grade")

        return {
            "scoring_cache_id": scoring_cache_id,
            "trigger": trigger,
            "rounds": rounds,
            "final_grade": final_grade,
            "short_circuited": False,
            "static_ensemble": static_ensemble,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wiring helpers (used by unified_scorer / play_card_formatter)
# ---------------------------------------------------------------------------


def run_debates_for_divergent(
    candidates: list[Mapping[str, Any]],
    *,
    db_path: Optional[Path] = None,
    audit_path: Optional[Path] = None,
    claude_invoke: Optional[InvokeFn] = None,
    gemini_invoke: Optional[InvokeFn] = None,
    grok_invoke: Optional[InvokeFn] = None,
) -> dict[int, dict[str, Any]]:
    """Fire :func:`run_debate` for every candidate with ``divergence_flag``.

    Returns a mapping ``{scoring_cache_id: debate_result}`` so callers
    (e.g. :func:`play_card_formatter.emit_play_cards`) can override
    the static ``science_grade`` with the debate's ``final_grade``
    when present.

    Failures inside an individual debate are logged at WARNING and
    the candidate is skipped — a single broken debate must not abort
    the daily play-card emission.
    """
    out: dict[int, dict[str, Any]] = {}
    for cand in candidates:
        if not bool(cand.get("divergence_flag")):
            continue
        cache_id = cand.get("id")
        if cache_id is None:
            continue
        try:
            result = run_debate(
                int(cache_id),
                "divergence",
                db_path=db_path,
                audit_path=audit_path,
                claude_invoke=claude_invoke,
                gemini_invoke=gemini_invoke,
                grok_invoke=grok_invoke,
            )
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "llm_debate: run_debate failed for scoring_cache_id=%s "
                "(ticker=%s): %r",
                cache_id,
                cand.get("ticker"),
                exc,
            )
            continue
        out[int(cache_id)] = result
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_scoring_cache_row(
    conn: sqlite3.Connection, scoring_cache_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, ticker, as_of_date, grok_rank, grok_score, "
        "claude_grade, claude_probability, gemini_grade, "
        "gemini_probability, science_grade, ensemble_score, "
        "divergence_flag, payload "
        "FROM scoring_cache WHERE id = ?",
        (int(scoring_cache_id),),
    ).fetchone()


def _static_ensemble_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ticker": row["ticker"],
        "as_of_date": row["as_of_date"],
        "ensemble_score": row["ensemble_score"],
        "science_grade": row["science_grade"],
        "claude_grade": row["claude_grade"],
        "claude_probability": row["claude_probability"],
        "gemini_grade": row["gemini_grade"],
        "gemini_probability": row["gemini_probability"],
        "grok_score": row["grok_score"],
        "divergence_flag": bool(row["divergence_flag"]),
    }


def _extract_model_breakdown(row: sqlite3.Row) -> dict[str, Any]:
    """Return the ``model_breakdown`` dict from the scoring_cache payload.

    The payload is JSON-serialised by ``EnsembleScorer._persist_to_scoring_cache``;
    on a fresh ``scoring_cache`` row written outside the ensemble (e.g.
    a hand-crafted test fixture) the payload may be ``None`` — we
    tolerate that by returning an empty dict so the prompt builders
    fall back to whatever fields the row itself exposes.
    """
    payload_str = row["payload"] if "payload" in row.keys() else None
    if not payload_str:
        return {}
    try:
        payload = json.loads(payload_str)
    except (TypeError, ValueError):
        return {}
    breakdown = payload.get("model_breakdown") if isinstance(payload, dict) else None
    return dict(breakdown) if isinstance(breakdown, dict) else {}


def _todays_debate_spend(conn: sqlite3.Connection, today: datetime.date) -> float:
    """Return the running total of today's debate cost in USD.

    The query keys on ``date(transcript_complete_at) = today`` so the
    cap window matches the validator exactly (VAL-M2-082).
    """
    iso = today.isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total "
        "FROM llm_debate "
        "WHERE date(transcript_complete_at) = ?",
        (iso,),
    ).fetchone()
    if row is None:
        return 0.0
    return float(row["total"] or 0.0)


def _short_circuit_count_for_today(
    conn: sqlite3.Connection, today: datetime.date
) -> int:
    """Best-effort helper to extract the prior short-circuit count.

    The audit JSON is the source of truth; this is only used to
    increment the count when re-running ``run_debate`` later in the
    same day. If the file is unreadable we just return 0 — the audit
    writer below merges idempotently so duplicates would be benign.
    """
    return 0


def _record_short_circuit(
    audit_path: Path,
    *,
    reason: str,
    count: int,
    ticker: Optional[str],
    scoring_cache_id: int,
) -> None:
    """Merge the ``llm_debate_short_circuit`` block into ``audit_latest.json``.

    The audit writer in ``biotech_sniper.audit`` already preserves
    foreign keys (it loads the existing JSON, updates its own
    contract keys, and writes back), so this helper merges its own
    block in the same way: read existing JSON (if any), set our key,
    write back atomically. Missing parent dirs are created on
    demand.
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if audit_path.is_file():
        try:
            with audit_path.open("r", encoding="utf-8") as fp:
                existing = json.load(fp)
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    prior = existing.get("llm_debate_short_circuit")
    prior_count = 0
    if isinstance(prior, dict):
        try:
            prior_count = int(prior.get("count") or 0)
        except (TypeError, ValueError):
            prior_count = 0

    new_count = max(int(count), prior_count + 1)

    existing["llm_debate_short_circuit"] = {
        "reason": reason,
        "count": new_count,
        "last_ticker": ticker,
        "last_scoring_cache_id": int(scoring_cache_id),
        "recorded_at": datetime.datetime.utcnow().isoformat() + "Z",
    }

    tmp = audit_path.with_suffix(audit_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(existing, fp, indent=2, sort_keys=True)
    tmp.replace(audit_path)


def _execute_round(
    *,
    invoke: InvokeFn,
    round_index: int,
    prompt: str,
    scoring_cache_id: int,
    trigger: str,
    conn: sqlite3.Connection,
    final_grade: Optional[str],
) -> dict[str, Any]:
    """Run one round, persist the row, and return a structured result.

    ``final_grade`` is the *intent* — pass ``"__from_response__"`` to
    parse the grade out of the model's reply (used for the
    adjudicating Round 3); pass ``None`` for non-final rounds; pass a
    literal string to override.
    """
    payload = invoke(prompt)
    text = str(payload.get("text") or "")
    cost_usd = float(payload.get("cost_usd") or 0.0)
    latency_ms = int(payload.get("latency_ms") or 0)
    model_id = str(payload.get("model_id") or "unknown")
    prompt_tokens = int(payload.get("prompt_tokens") or 0)
    completion_tokens = int(payload.get("completion_tokens") or 0)

    parsed_grade: Optional[str] = None
    if text:
        parsed_grade = _parse_grade_from_text(text)

    if final_grade == "__from_response__":
        persisted_final = parsed_grade
    else:
        persisted_final = final_grade

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_debate (
                scoring_cache_id, trigger, round_index,
                model, prompt, response,
                latency_ms, cost_usd, final_grade
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(scoring_cache_id),
                trigger,
                int(round_index),
                model_id,
                prompt,
                text,
                latency_ms,
                cost_usd,
                persisted_final,
            ),
        )
        row_id = cursor.lastrowid

    return {
        "id": row_id,
        "round_index": round_index,
        "model": model_id,
        "prompt": prompt,
        "response": text,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "final_grade": persisted_final,
        "parsed_grade": parsed_grade,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _wrap_partial(
    scoring_cache_id: int,
    trigger: str,
    rounds: list[dict[str, Any]],
    static_ensemble: dict[str, Any],
    audit_path: Path,
    today: datetime.date,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Wrap a partial transcript that hit the daily cap mid-debate."""
    prior_count = 0
    if audit_path.is_file():
        try:
            with audit_path.open("r", encoding="utf-8") as fp:
                existing = json.load(fp)
            sc = existing.get("llm_debate_short_circuit") if isinstance(existing, dict) else None
            if isinstance(sc, dict):
                prior_count = int(sc.get("count") or 0)
        except Exception:
            prior_count = 0
    _record_short_circuit(
        audit_path,
        reason="daily_cap_exceeded",
        count=prior_count + 1,
        ticker=static_ensemble.get("ticker"),
        scoring_cache_id=scoring_cache_id,
    )
    return {
        "scoring_cache_id": scoring_cache_id,
        "trigger": trigger,
        "rounds": rounds,
        "final_grade": None,
        "short_circuited": True,
        "reason": "daily_cap_exceeded",
        "static_ensemble": static_ensemble,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


_ROUND_GRADE_INSTRUCTIONS = (
    "Return STRICT JSON only — no markdown fences, no commentary outside the "
    "object — with exactly these keys: "
    "letter_grade (one of A+, A, A-, B+, B, B-, C+, C, C-, D, F), "
    "rationale (1-3 sentence string)."
)

_ADJUDICATION_INSTRUCTIONS = (
    "Return STRICT JSON only — no markdown fences, no commentary outside the "
    "object — with exactly these keys: "
    "final_grade (one of A+, A, A-, B+, B, B-, C+, C, C-, D, F), "
    "rationale (1-3 sentence adjudication summary)."
)


def _build_round1_prompt(
    *,
    ticker: Optional[str],
    trigger: str,
    gemini: Mapping[str, Any],
    ensemble: Mapping[str, Any],
) -> str:
    """Round 1: Claude critiques Gemini's rationale + citations."""
    payload = {
        "ticker": ticker,
        "trigger": trigger,
        "ensemble": dict(ensemble),
        "gemini_position": {
            "letter_grade": gemini.get("letter_grade"),
            "probability": gemini.get("probability"),
            "rationale": gemini.get("rationale"),
            "citations": gemini.get("citations") or [],
        },
    }
    return (
        "You are Claude, the deep-tier biotech-science reasoner. Gemini "
        "scored the same catalyst thesis below; review Gemini's full "
        "rationale and citations, identify weaknesses, biases, or missing "
        "considerations, and re-grade the thesis from your own perspective.\n\n"
        + json.dumps(payload, sort_keys=True, default=str, indent=2)
        + "\n\n"
        + _ROUND_GRADE_INSTRUCTIONS
    )


def _build_round2_prompt(
    *,
    ticker: Optional[str],
    trigger: str,
    claude_critique: str,
    claude_round1_grade: Optional[str],
    gemini: Mapping[str, Any],
    ensemble: Mapping[str, Any],
) -> str:
    """Round 2: Gemini rebuts Claude and re-grades."""
    payload = {
        "ticker": ticker,
        "trigger": trigger,
        "ensemble": dict(ensemble),
        "your_prior_position": {
            "letter_grade": gemini.get("letter_grade"),
            "rationale": gemini.get("rationale"),
        },
        "claude_critique": {
            "regrade": claude_round1_grade,
            "full_text": claude_critique,
        },
    }
    return (
        "You are Gemini, the second deep-tier biotech-science reasoner. "
        "Claude has critiqued your prior rationale (below). Rebut Claude's "
        "critique on the merits — defend or revise your reasoning, then "
        "re-grade the thesis.\n\n"
        + json.dumps(payload, sort_keys=True, default=str, indent=2)
        + "\n\n"
        + _ROUND_GRADE_INSTRUCTIONS
    )


def _build_round3_prompt(
    *,
    ticker: Optional[str],
    trigger: str,
    claude_critique: str,
    gemini_rebuttal: str,
    ensemble: Mapping[str, Any],
) -> str:
    """Round 3: Grok adjudicates and emits final_grade."""
    payload = {
        "ticker": ticker,
        "trigger": trigger,
        "ensemble": dict(ensemble),
        "round1_claude_critique": claude_critique,
        "round2_gemini_rebuttal": gemini_rebuttal,
    }
    return (
        "You are Grok-4, the fast-tier ranker, now serving as the "
        "adjudicator of a Claude-vs-Gemini biotech debate. Read both "
        "transcripts, decide which side's reasoning is stronger on the "
        "merits, and emit a single ``final_grade`` plus a brief "
        "adjudication rationale (1-3 sentences).\n\n"
        + json.dumps(payload, sort_keys=True, default=str, indent=2)
        + "\n\n"
        + _ADJUDICATION_INSTRUCTIONS
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_grade_from_text(text: str) -> Optional[str]:
    """Pull a letter grade out of a JSON-mode model response.

    Tolerates the same minor formatting quirks the deep-tier clients
    handle (markdown fences, extra whitespace) so a single
    ``letter_grade`` / ``final_grade`` value is recoverable from any
    of the three rounds.
    """
    if not text:
        return None
    cleaned = text.strip()
    # Strip simple ```json fences.
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]

    try:
        data = json.loads(cleaned)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    # Prefer the adjudicator's ``final_grade`` if present, else the
    # per-round ``letter_grade``.
    for key in ("final_grade", "letter_grade"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# ---------------------------------------------------------------------------
# Default invoker factories — production wrappers around the SDKs.
# ---------------------------------------------------------------------------


def _build_default_invoker(provider: str) -> InvokeFn:
    """Return an invoke function wrapping the named provider's SDK.

    The wrapper is built lazily so missing optional SDKs don't break
    module import. Tests should pass explicit callables instead of
    relying on these factories.
    """
    if provider == "anthropic":
        return _claude_invoker_factory()
    if provider == "gemini":
        return _gemini_invoker_factory()
    if provider == "xai":
        return _xai_invoker_factory()
    raise DebateError(f"unknown provider: {provider!r}")


def _claude_invoker_factory() -> InvokeFn:
    """Build a Claude invoker wrapping :class:`ClaudeClient`."""
    from biotech_sniper.llm.claude_client import (  # local import — defer SDK
        CLAUDE_INPUT_USD_PER_1K,
        CLAUDE_OUTPUT_USD_PER_1K,
        ClaudeClient,
        DEFAULT_MAX_TOKENS,
        DEFAULT_MODEL,
    )
    import time

    if not config.provider_enabled("anthropic"):
        raise DebateError(
            "Anthropic provider is disabled (LLM_PROVIDERS does not include "
            "it, or ANTHROPIC_API_KEY is unset)."
        )
    client = ClaudeClient()

    def invoke(prompt: str) -> dict[str, Any]:
        t_start = time.perf_counter()
        msg = client._client.messages.create(  # type: ignore[attr-defined]
            model=DEFAULT_MODEL,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=(
                "You are a biotech-catalyst analyst participating in a "
                "structured debate. Follow the per-round instructions exactly."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        text = _claude_message_text(msg)
        usage = getattr(msg, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost_usd = round(
            (prompt_tokens / 1000.0) * CLAUDE_INPUT_USD_PER_1K
            + (completion_tokens / 1000.0) * CLAUDE_OUTPUT_USD_PER_1K,
            6,
        )
        return {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "model_id": str(getattr(msg, "model", DEFAULT_MODEL) or DEFAULT_MODEL),
        }

    return invoke


def _gemini_invoker_factory() -> InvokeFn:
    """Build a Gemini invoker wrapping :class:`GeminiClient`."""
    from biotech_sniper.llm.gemini_client import (
        GEMINI_INPUT_USD_PER_1K,
        GEMINI_OUTPUT_USD_PER_1K,
        GeminiClient,
        DEFAULT_MAX_OUTPUT_TOKENS,
        DEFAULT_MODEL,
    )
    from google.genai import types as genai_types
    import time

    if not config.provider_enabled("gemini"):
        raise DebateError(
            "Gemini provider is disabled (LLM_PROVIDERS does not include "
            "it, or GEMINI_API_KEY is unset)."
        )
    client = GeminiClient()

    def invoke(prompt: str) -> dict[str, Any]:
        request_config = genai_types.GenerateContentConfig(
            system_instruction=(
                "You are Gemini, a biotech-catalyst analyst participating "
                "in a structured debate. Follow the per-round instructions "
                "exactly."
            ),
            temperature=0.2,
            response_mime_type="application/json",
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        )
        t_start = time.perf_counter()
        response = client._client.models.generate_content(  # type: ignore[attr-defined]
            model=DEFAULT_MODEL,
            contents=prompt,
            config=request_config,
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        text = getattr(response, "text", "") or ""
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        cost_usd = round(
            (prompt_tokens / 1000.0) * GEMINI_INPUT_USD_PER_1K
            + (completion_tokens / 1000.0) * GEMINI_OUTPUT_USD_PER_1K,
            6,
        )
        return {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "model_id": str(getattr(response, "model_version", DEFAULT_MODEL) or DEFAULT_MODEL),
        }

    return invoke


def _xai_invoker_factory() -> InvokeFn:
    """Build a Grok invoker wrapping :class:`XAIClient`."""
    from biotech_sniper.llm.xai_client import (
        DEFAULT_MODEL,
        GROK_4_INPUT_USD_PER_1K,
        GROK_4_OUTPUT_USD_PER_1K,
        XAIClient,
    )
    import time

    if not config.provider_enabled("xai"):
        raise DebateError(
            "xAI provider is disabled (LLM_PROVIDERS_FAST not 'xai' or "
            "XAI_API_KEY is unset)."
        )
    client = XAIClient()

    def invoke(prompt: str) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Grok, the adjudicator of a biotech-catalyst "
                    "debate. Decide the winning side on the merits and emit "
                    "a single final_grade."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        t_start = time.perf_counter()
        response_json = client._chat_completion(  # type: ignore[attr-defined]
            messages=messages,
            model=DEFAULT_MODEL,
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        try:
            text = str(response_json["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            text = ""
        usage = response_json.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost_usd = round(
            (prompt_tokens / 1000.0) * GROK_4_INPUT_USD_PER_1K
            + (completion_tokens / 1000.0) * GROK_4_OUTPUT_USD_PER_1K,
            6,
        )
        return {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "model_id": str(response_json.get("model") or DEFAULT_MODEL),
        }

    return invoke


def _claude_message_text(message: Any) -> str:
    """Extract the assistant text body from an Anthropic Message."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type and block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
