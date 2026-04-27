"""Tests for :mod:`biotech_sniper.llm.llm_debate` (M2 feature f-m2-12).

These tests are fully hermetic: no live LLM API is ever hit. We
hand-roll three "invoke" callables (Claude / Gemini / Grok) that
replay cassettes from ``tests/fixtures/cassettes/debate/``. Each
cassette is a JSON file with one entry per round (claude, gemini,
grok); the invoke callables consume them in order and return the
SDK-shaped payload (``text``, ``prompt_tokens``, ``completion_tokens``,
``cost_usd``, ``latency_ms``, ``model_id``).

Coverage targets the f-m2-12 spec + VAL-M2-079..084:

* trigger validation (raises on unknown values)
* divergence trigger persists ≥ 2 rounds (VAL-M2-080)
* round cap MAX(round_index) ≤ 3 (VAL-M2-081)
* daily cost cap honoured (VAL-M2-082)
* short-circuit reason logged in audit JSON (VAL-M2-083)
* rotation trigger plumbed through identically
* play_card_formatter overrides ``grade`` with debate ``final_grade``
  (VAL-M2-084)
* schema check — ``llm_debate`` table created with the contract columns
  and FK
* default-invoker construction surfaces a typed error when keys are
  missing
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from biotech_sniper import db
from biotech_sniper.llm import llm_debate
from biotech_sniper.llm.llm_debate import (
    DebateError,
    DebateInvalidTrigger,
    LLM_DEBATE_DAILY_USD_CAP,
    LLM_DEBATE_MAX_ROUNDS,
    VALID_TRIGGERS,
    run_debate,
)


CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes" / "debate"


# ---------------------------------------------------------------------------
# Cassette → invoke factories
# ---------------------------------------------------------------------------


def _load_cassette(name: str) -> dict[str, Any]:
    return json.loads((CASSETTE_DIR / name).read_text(encoding="utf-8"))


def _make_invokers(cassette: dict[str, Any]) -> dict[str, Any]:
    """Wrap cassette ``rounds`` into per-provider invoke callables.

    Each callable mirrors :data:`llm_debate.InvokeFn` — it accepts the
    prompt string and returns the SDK-shaped payload dict.
    """
    rounds = cassette["rounds"]
    state: dict[str, list[str]] = {"claude": [], "gemini": [], "grok": []}

    def _make(name: str):
        payload = dict(rounds[name])

        def invoke(prompt: str) -> dict[str, Any]:
            state[name].append(prompt)
            return dict(payload)

        return invoke

    return {
        "claude_invoke": _make("claude"),
        "gemini_invoke": _make("gemini"),
        "grok_invoke": _make("grok"),
        "_calls": state,
    }


# ---------------------------------------------------------------------------
# Test fixtures — DB setup
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "alpha.db"


@pytest.fixture
def temp_audit_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "audit_latest.json"


def _seed_scoring_cache(
    db_path: Path,
    *,
    ticker: str = "IDYA",
    as_of_date: str = "2026-04-27",
    divergence: bool = True,
    claude_grade: str = "A",
    gemini_grade: str = "B-",
    science_grade: str = "A-",
    ensemble_score: float = 0.62,
) -> int:
    """Seed a single scoring_cache row and return its id."""
    conn = db.connect(db_path)
    try:
        db.run_migrations(conn)
        payload = json.dumps(
            {
                "model_breakdown": {
                    "claude": {
                        "letter_grade": claude_grade,
                        "rationale": "Replicated P2 ORR strongly supports the thesis.",
                        "citations": [
                            {"source": "NCT05123456", "quote_or_url": "https://example/ct"}
                        ],
                        "model_id": "claude-opus-4-1-20250805",
                    },
                    "gemini": {
                        "letter_grade": gemini_grade,
                        "rationale": "Open-label PFS-only design is a material risk.",
                        "citations": [
                            {"source": "8-K 2026-04-15", "quote_or_url": "DSMB review"}
                        ],
                        "model_id": "gemini-2.5-pro",
                    },
                    "grok": {
                        "score": 0.55,
                        "rationale": "Mid-cap with low float favours an outsized move.",
                    },
                },
                "active_weights": {"grok": 0.4, "claude": 0.3, "gemini": 0.3},
            },
            sort_keys=True,
        )
        cursor = conn.execute(
            "INSERT INTO scoring_cache ("
            "ticker, as_of_date, grok_rank, grok_score, "
            "claude_grade, claude_probability, gemini_grade, gemini_probability, "
            "science_grade, ensemble_score, divergence_flag, payload"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticker,
                as_of_date,
                1,
                0.55,
                claude_grade,
                0.7,
                gemini_grade,
                0.42,
                science_grade,
                ensemble_score,
                int(bool(divergence)),
                payload,
            ),
        )
        cache_id = int(cursor.lastrowid)
        conn.commit()
        return cache_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Schema sanity (VAL-M2-079)
# ---------------------------------------------------------------------------


def test_schema_creates_llm_debate_table(temp_db_path: Path):
    conn = db.connect(temp_db_path)
    try:
        db.run_migrations(conn)
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='llm_debate'"
        ).fetchone()
        assert ddl_row is not None, "llm_debate table missing"
        ddl = ddl_row[0]
        # All columns required by VAL-M2-079.
        for col in (
            "id",
            "scoring_cache_id",
            "trigger",
            "round_index",
            "model",
            "prompt",
            "response",
            "latency_ms",
            "cost_usd",
            "final_grade",
            "transcript_complete_at",
        ):
            assert col in ddl, f"llm_debate missing column {col}"
        # CHECK constraint on trigger.
        assert "trigger IN" in ddl
        # FK to scoring_cache.
        assert "FOREIGN KEY" in ddl and "scoring_cache" in ddl

        # CHECK constraint actually rejects bogus triggers.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO llm_debate "
                "(trigger, round_index, model) VALUES (?,?,?)",
                ("bogus", 1, "claude"),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. trigger validation
# ---------------------------------------------------------------------------


def test_run_debate_rejects_unknown_trigger(temp_db_path):
    cache_id = _seed_scoring_cache(temp_db_path)
    with pytest.raises(DebateInvalidTrigger):
        run_debate(cache_id, "bogus", db_path=temp_db_path)


def test_valid_triggers_constant_matches_spec():
    assert VALID_TRIGGERS == frozenset({"divergence", "rotation"})


# ---------------------------------------------------------------------------
# 3. Happy path — three rounds persisted, final_grade returned
# ---------------------------------------------------------------------------


def test_divergence_trigger_persists_three_rounds(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path)
    invokers = _make_invokers(_load_cassette("three_round_success.json"))

    result = run_debate(
        cache_id,
        "divergence",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    assert result["short_circuited"] is False
    assert result["trigger"] == "divergence"
    assert result["scoring_cache_id"] == cache_id
    assert len(result["rounds"]) == 3
    assert result["final_grade"] == "B+"
    # Each round received a prompt.
    assert len(invokers["_calls"]["claude"]) == 1
    assert len(invokers["_calls"]["gemini"]) == 1
    assert len(invokers["_calls"]["grok"]) == 1
    # Round 1 prompt cites Gemini's position.
    assert "gemini_position" in invokers["_calls"]["claude"][0]
    # Round 2 prompt carries Claude's critique.
    assert "claude_critique" in invokers["_calls"]["gemini"][0]
    # Round 3 prompt has both transcripts.
    assert "round1_claude_critique" in invokers["_calls"]["grok"][0]
    assert "round2_gemini_rebuttal" in invokers["_calls"]["grok"][0]

    # DB rows persisted: model, prompt, response, latency_ms, cost_usd
    # populated for each round; only Round 3 has final_grade.
    conn = db.connect(temp_db_path)
    try:
        rows = conn.execute(
            "SELECT round_index, model, prompt, response, latency_ms, "
            "cost_usd, final_grade FROM llm_debate "
            "WHERE scoring_cache_id = ? ORDER BY round_index",
            (cache_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    rounds_by_idx = {r["round_index"]: r for r in rows}
    assert rounds_by_idx[1]["model"] == "claude-opus-4-1-20250805"
    assert rounds_by_idx[1]["final_grade"] is None
    assert rounds_by_idx[2]["model"] == "gemini-2.5-pro"
    assert rounds_by_idx[2]["final_grade"] is None
    assert rounds_by_idx[3]["model"] == "grok-4-0709"
    assert rounds_by_idx[3]["final_grade"] == "B+"
    assert rounds_by_idx[3]["cost_usd"] == 0.0057


# ---------------------------------------------------------------------------
# 4. Round cap MAX(round_index) ≤ 3 (VAL-M2-081)
# ---------------------------------------------------------------------------


def test_max_round_index_capped_at_three(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path)
    invokers = _make_invokers(_load_cassette("three_round_success.json"))

    run_debate(
        cache_id,
        "divergence",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    conn = db.connect(temp_db_path)
    try:
        max_idx = conn.execute(
            "SELECT MAX(round_index) FROM llm_debate "
            "WHERE scoring_cache_id = ?",
            (cache_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert max_idx <= LLM_DEBATE_MAX_ROUNDS == 3


# ---------------------------------------------------------------------------
# 5. Daily cost cap short-circuits when prior spend already at cap
#    (VAL-M2-082 + VAL-M2-083)
# ---------------------------------------------------------------------------


def test_short_circuit_when_daily_cap_already_exceeded(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path)

    # Pre-populate today's spend at the cap.
    conn = db.connect(temp_db_path)
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO llm_debate "
            "(scoring_cache_id, trigger, round_index, model, "
            "cost_usd, transcript_complete_at) VALUES (?,?,?,?,?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (cache_id, "divergence", 1, "saturator", LLM_DEBATE_DAILY_USD_CAP),
        )
        conn.commit()
    finally:
        conn.close()

    # Build invokers but they should NEVER be called.
    invokers = _make_invokers(_load_cassette("three_round_success.json"))

    result = run_debate(
        cache_id,
        "divergence",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    assert result["short_circuited"] is True
    assert result["reason"] == "daily_cap_exceeded"
    assert result["final_grade"] is None
    assert result["rounds"] == []
    # static_ensemble surfaced for fallback display
    assert result["static_ensemble"]["ticker"] == "IDYA"

    # No invokers called.
    assert invokers["_calls"]["claude"] == []
    assert invokers["_calls"]["gemini"] == []
    assert invokers["_calls"]["grok"] == []

    # audit JSON has the short-circuit block.
    assert temp_audit_path.is_file()
    audit = json.loads(temp_audit_path.read_text(encoding="utf-8"))
    block = audit.get("llm_debate_short_circuit")
    assert isinstance(block, dict)
    assert block["reason"] == "daily_cap_exceeded"
    assert block["count"] >= 1


def test_short_circuit_count_increments_on_repeat(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path)
    conn = db.connect(temp_db_path)
    try:
        db.run_migrations(conn)
        conn.execute(
            "INSERT INTO llm_debate "
            "(scoring_cache_id, trigger, round_index, model, "
            "cost_usd, transcript_complete_at) VALUES (?,?,?,?,?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (cache_id, "divergence", 1, "saturator", LLM_DEBATE_DAILY_USD_CAP),
        )
        conn.commit()
    finally:
        conn.close()

    for _ in range(3):
        run_debate(
            cache_id,
            "divergence",
            db_path=temp_db_path,
            audit_path=temp_audit_path,
        )

    audit = json.loads(temp_audit_path.read_text(encoding="utf-8"))
    block = audit["llm_debate_short_circuit"]
    assert block["count"] >= 3


# ---------------------------------------------------------------------------
# 6. Daily cap is summed correctly across the day's debate rows
#    (VAL-M2-082)
# ---------------------------------------------------------------------------


def test_daily_total_cost_does_not_exceed_cap_after_normal_run(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path)
    invokers = _make_invokers(_load_cassette("three_round_success.json"))
    run_debate(
        cache_id,
        "divergence",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    conn = db.connect(temp_db_path)
    try:
        total = conn.execute(
            "SELECT ROUND(COALESCE(SUM(cost_usd),0),4) "
            "FROM llm_debate WHERE date(transcript_complete_at) = date('now')"
        ).fetchone()[0]
    finally:
        conn.close()
    assert 0.0 < float(total) <= LLM_DEBATE_DAILY_USD_CAP


# ---------------------------------------------------------------------------
# 7. Rotation trigger flows through (M3 wiring point — f-m3-10)
# ---------------------------------------------------------------------------


def test_rotation_trigger_persists_three_rounds(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id = _seed_scoring_cache(temp_db_path, ticker="RVMD")
    invokers = _make_invokers(_load_cassette("rotation_trigger.json"))

    result = run_debate(
        cache_id,
        "rotation",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    assert result["trigger"] == "rotation"
    assert result["final_grade"] == "A-"
    conn = db.connect(temp_db_path)
    try:
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT trigger FROM llm_debate "
                "WHERE scoring_cache_id = ?",
                (cache_id,),
            )
        }
    finally:
        conn.close()
    assert triggers == {"rotation"}


# ---------------------------------------------------------------------------
# 8. play_card_formatter overrides ``grade`` with debate ``final_grade``
#    (VAL-M2-084)
# ---------------------------------------------------------------------------


def test_play_card_uses_debate_final_grade_when_present(
    temp_db_path: Path, tmp_path: Path
):
    from biotech_sniper import play_card_formatter

    # Two candidates: one divergent, one not.
    _seed_scoring_cache(
        temp_db_path,
        ticker="IDYA",
        divergence=True,
        claude_grade="A",
        gemini_grade="B-",
        science_grade="A-",
        ensemble_score=0.74,
    )
    _seed_scoring_cache(
        temp_db_path,
        ticker="RVMD",
        divergence=False,
        claude_grade="B+",
        gemini_grade="B+",
        science_grade="B+",
        ensemble_score=0.62,
    )

    invokers_full = _make_invokers(_load_cassette("three_round_success.json"))
    debate_invokers = {
        "claude": invokers_full["claude_invoke"],
        "gemini": invokers_full["gemini_invoke"],
        "grok": invokers_full["grok_invoke"],
    }

    written = play_card_formatter.emit_play_cards(
        as_of_date="2026-04-27",
        n=10,
        base_dir=tmp_path,
        db_path=temp_db_path,
        debate_invokers=debate_invokers,
    )
    paths = {p.name: p for p in written}
    idya_card = json.loads(paths["IDYA.json"].read_text())
    rvmd_card = json.loads(paths["RVMD.json"].read_text())

    # IDYA was divergent → grade overridden by debate's final_grade ('B+').
    assert idya_card["divergence_flag"] is True
    assert idya_card["debate_final_grade"] == "B+"
    assert idya_card["grade"] == "B+"
    assert idya_card["science_grade"] == "A-"  # original preserved

    # RVMD non-divergent → grade stays as static science_grade.
    assert rvmd_card["divergence_flag"] is False
    assert rvmd_card["debate_final_grade"] is None
    assert rvmd_card["grade"] == "B+"
    assert rvmd_card["science_grade"] == "B+"


# ---------------------------------------------------------------------------
# 9. run_debates_for_divergent skips non-divergent candidates
# ---------------------------------------------------------------------------


def test_run_debates_for_divergent_skips_non_divergent(
    temp_db_path: Path, temp_audit_path: Path
):
    cache_id_div = _seed_scoring_cache(temp_db_path, ticker="IDYA", divergence=True)
    cache_id_nondiv = _seed_scoring_cache(temp_db_path, ticker="RVMD", divergence=False)

    candidates = [
        {"id": cache_id_div, "ticker": "IDYA", "divergence_flag": True},
        {"id": cache_id_nondiv, "ticker": "RVMD", "divergence_flag": False},
    ]
    invokers = _make_invokers(_load_cassette("three_round_success.json"))

    out = llm_debate.run_debates_for_divergent(
        candidates,
        db_path=temp_db_path,
        audit_path=temp_audit_path,
        claude_invoke=invokers["claude_invoke"],
        gemini_invoke=invokers["gemini_invoke"],
        grok_invoke=invokers["grok_invoke"],
    )

    assert cache_id_div in out
    assert cache_id_nondiv not in out
    # exactly one debate fired
    assert len(invokers["_calls"]["grok"]) == 1


# ---------------------------------------------------------------------------
# 10. Default invoker construction surfaces a typed error when keys missing
# ---------------------------------------------------------------------------


def test_default_invoker_raises_when_provider_disabled(monkeypatch):
    # All three provider_enabled checks return False — calling
    # _build_default_invoker for any of them must raise DebateError.
    monkeypatch.setattr(
        "biotech_sniper.llm.llm_debate.config.provider_enabled",
        lambda name: False,
    )
    for provider in ("anthropic", "gemini", "xai"):
        with pytest.raises(DebateError):
            llm_debate._build_default_invoker(provider)


# ---------------------------------------------------------------------------
# 11. Missing scoring_cache row raises DebateError (defensive)
# ---------------------------------------------------------------------------


def test_run_debate_raises_when_scoring_cache_id_unknown(temp_db_path: Path):
    # Create the schema but no rows.
    conn = db.connect(temp_db_path)
    try:
        db.run_migrations(conn)
    finally:
        conn.close()
    with pytest.raises(DebateError, match="scoring_cache.id=999"):
        run_debate(999, "divergence", db_path=temp_db_path)


# ---------------------------------------------------------------------------
# 12. f-m2-13 fix #6 — default invokers must use public client.chat() methods
# ---------------------------------------------------------------------------


def test_default_invokers_route_through_public_chat_methods(
    temp_db_path: Path, temp_audit_path: Path, monkeypatch
):
    """Each default invoker must call ``<client>.chat`` (public, ledger-writing).

    The earlier implementation reached into the private SDK objects
    (``client._client.messages.create``,
    ``client._client.models.generate_content``,
    ``client._chat_completion``) which bypassed the cost-ledger
    write. This test asserts:

    1. After ``run_debate`` completes, ``llm_cost_ledger`` contains
       exactly 3 new rows (one per round, one per provider).
    2. The default invoker functions invoke the *public* ``.chat``
       method on each client (verified by stubbing the chat method
       and asserting it was called).
    """
    cache_id = _seed_scoring_cache(temp_db_path)

    # Stub config.provider_enabled to allow building the default
    # invokers without real API keys.
    monkeypatch.setattr(
        "biotech_sniper.llm.llm_debate.config.provider_enabled",
        lambda name: True,
    )

    # Build payloads each chat() call will return.
    claude_payload = {
        "text": '{"letter_grade": "A-", "rationale": "Strong P3 design."}',
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "cost_usd": 0.0035,
        "latency_ms": 250,
        "model_id": "claude-opus-4-1-20250805",
        "request_id": "claude-1",
    }
    gemini_payload = {
        "text": '{"letter_grade": "B", "rationale": "Open-label is a risk."}',
        "prompt_tokens": 110,
        "completion_tokens": 28,
        "cost_usd": 0.0009,
        "latency_ms": 220,
        "model_id": "gemini-2.5-pro",
        "request_id": "gemini-1",
    }
    grok_payload = {
        "text": '{"final_grade": "B+", "rationale": "Splits the difference."}',
        "prompt_tokens": 130,
        "completion_tokens": 25,
        "cost_usd": 0.0011,
        "latency_ms": 180,
        "model_id": "grok-4-0709",
        "request_id": "grok-1",
    }

    chat_calls: dict[str, int] = {"claude": 0, "gemini": 0, "grok": 0}

    # Patch the SDK constructors so building each client never hits a
    # real provider, then patch the public ``.chat`` method to write a
    # llm_cost_ledger row directly (the same side-effect the real
    # public method has).
    def _record_ledger(provider: str, payload: dict[str, Any]) -> None:
        conn = db.connect(temp_db_path)
        try:
            db.run_migrations(conn)
            with conn:
                conn.execute(
                    "INSERT INTO llm_cost_ledger ("
                    "provider, model_id, purpose, prompt_tokens, "
                    "completion_tokens, latency_ms, cost_usd, request_id"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        provider,
                        payload["model_id"],
                        "debate",
                        payload["prompt_tokens"],
                        payload["completion_tokens"],
                        payload["latency_ms"],
                        payload["cost_usd"],
                        payload["request_id"],
                    ),
                )
        finally:
            conn.close()

    # Patch ClaudeClient.__init__/chat
    from biotech_sniper.llm import claude_client as _cc

    def _claude_init(self, *args, **kwargs):
        self._db_path = temp_db_path

    def _claude_chat(self, prompt, *, system=None, model=None, purpose="debate"):
        chat_calls["claude"] += 1
        _record_ledger("anthropic", claude_payload)
        return claude_payload

    monkeypatch.setattr(_cc.ClaudeClient, "__init__", _claude_init)
    monkeypatch.setattr(_cc.ClaudeClient, "chat", _claude_chat)

    # Patch GeminiClient
    from biotech_sniper.llm import gemini_client as _gc

    def _gemini_init(self, *args, **kwargs):
        self._db_path = temp_db_path

    def _gemini_chat(self, prompt, *, system=None, model=None, purpose="debate", json_mode=True):
        chat_calls["gemini"] += 1
        _record_ledger("gemini", gemini_payload)
        return gemini_payload

    monkeypatch.setattr(_gc.GeminiClient, "__init__", _gemini_init)
    monkeypatch.setattr(_gc.GeminiClient, "chat", _gemini_chat)

    # Patch XAIClient
    from biotech_sniper.llm import xai_client as _xc

    def _xai_init(self, *args, **kwargs):
        self._db_path = temp_db_path

    def _xai_chat(self, messages, *, model=None, purpose="debate", json_mode=True):
        chat_calls["grok"] += 1
        _record_ledger("xai", grok_payload)
        return grok_payload

    monkeypatch.setattr(_xc.XAIClient, "__init__", _xai_init)
    monkeypatch.setattr(_xc.XAIClient, "chat", _xai_chat)

    # Snapshot the ledger row count BEFORE the debate (zero, fresh db).
    conn = db.connect(temp_db_path)
    try:
        db.run_migrations(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM llm_cost_ledger"
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == 0

    # Run the debate using DEFAULT invokers (no claude/gemini/grok
    # invoker passed) — this exercises the production wiring.
    result = run_debate(
        cache_id,
        "divergence",
        db_path=temp_db_path,
        audit_path=temp_audit_path,
    )

    assert result["short_circuited"] is False
    assert chat_calls == {"claude": 1, "gemini": 1, "grok": 1}, chat_calls

    conn = db.connect(temp_db_path)
    try:
        rows = conn.execute(
            "SELECT provider FROM llm_cost_ledger ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    providers = [r["provider"] for r in rows]
    assert providers == ["anthropic", "gemini", "xai"], providers


def test_llm_debate_module_does_not_call_private_sdk_methods():
    """f-m2-13 fix #6 hygiene check: no private SDK access in default invokers.

    A static check of the module source: post-fix, the default
    invokers route through ``client.chat(...)`` and must not reach
    into ``client._client.messages.create``,
    ``client._client.models.generate_content``, or
    ``client._chat_completion``.
    """
    src = Path(llm_debate.__file__).read_text(encoding="utf-8")
    forbidden = [
        "_client.messages.create",
        "_client.models.generate_content",
        "_chat_completion",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"llm_debate.py still references {needle!r} — default invokers "
            "must use the public ``.chat`` methods instead."
        )
