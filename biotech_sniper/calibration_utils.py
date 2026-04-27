#!/usr/bin/env python3
"""
CALIBRATION UTILITIES — Applied Apr 26 2026
Utility functions implementing all recalibration rules from the accuracy analysis.

Rules derived from 6 resolved plays:
  - LPCN 62% LONG: failed. Raised LONG minimum to 65%.
  - VRDN $27C: 43% OTM too far for mid-stage readout. Use catalyst-type-specific OTM.
  - TVTX $35C: Label extension = modest move. ATM to 5% OTM for sBLA/sNDA.
  - IDYA $35C: IV=188% crushed. P=96% was on primary only, market priced joint P.

USED BY: play_card_formatter.py (assemble_card), master_unified_run.py (Step 3 options pull)
"""

import json
import datetime
from pathlib import Path

from biotech_sniper.paths import BASE_DIR


def fetch_iv_for_play(ticker: str, strike: float, expiry: str, option_type: str = "C") -> float | None:
    """
    Fetch implied volatility for the specific option contract from the broker.
    Returns IV as a percentage (e.g., 188.0 for 188%), or None if unavailable.

    This is called during Step 3 (live options chain pull) in the daily cron.
    The result is stored in active_plays.json as play["iv_pct"].

    M3 update (f-m3-02): backed by the Alpaca options-chain endpoint via
    :func:`biotech_sniper.options_chains.pull_options.pull_chain`.
    """
    try:
        # Local import keeps this module import-safe when the Alpaca SDK
        # is not configured (e.g. fresh checkouts without paper keys);
        # the call below will surface a typed error and we degrade to
        # ``None`` just like the legacy implementation did on missing
        # data.
        from biotech_sniper.options_chains.pull_options import pull_chain

        target_type = "call" if option_type == "C" else "put"
        chain = pull_chain(ticker, expiry)

        rows = [r for r in chain if (r.get("type") or "").lower() == target_type]
        if not rows:
            return None

        # Closest strike match.
        try:
            target_strike = float(strike)
        except (TypeError, ValueError):
            return None
        rows.sort(key=lambda r: abs((r.get("strike") or 0.0) - target_strike))
        iv_raw = rows[0].get("iv")
        if iv_raw is None:
            return None
        return round(float(iv_raw) * 100, 1)

    except Exception as e:
        print(f"  [calibration_utils] IV fetch failed for {ticker}: {e}")
        return None


def update_play_iv(ticker: str) -> dict:
    """
    Fetch and store the latest IV for an active play.
    Updates active_plays.json with play["iv_pct"].
    Returns {"ticker": ..., "iv_pct": ..., "iv_check": ...}
    """
    from sectors.unified_scorer import check_iv_crush_risk, load_active_plays, save_active_plays
    
    plays = load_active_plays()
    active = plays.get("active", {})
    
    if ticker not in active:
        return {"ticker": ticker, "error": "Not in active plays"}
    
    play = active[ticker]
    strike = play.get("option_strike")
    expiry = play.get("option_expiry")
    opt_type = play.get("option_type", "C")
    
    if not strike or not expiry:
        return {"ticker": ticker, "error": "No strike or expiry in play"}
    
    iv_pct = fetch_iv_for_play(ticker, strike, expiry, opt_type)
    
    if iv_pct is not None:
        play["iv_pct"] = iv_pct
        play["iv_updated"] = datetime.date.today().isoformat()
        active[ticker] = play
        save_active_plays({"active": active, "monitor": plays.get("monitor", {})})
        
        iv_check = check_iv_crush_risk(iv_pct)
        return {
            "ticker": ticker,
            "iv_pct": iv_pct,
            "iv_check": iv_check,
        }
    
    return {"ticker": ticker, "iv_pct": None, "error": "Could not fetch IV"}


def update_all_play_ivs() -> list:
    """
    Update IV for all active plays. Call this during Step 3 (options chain pull).
    Returns list of results.
    """
    from sectors.unified_scorer import load_active_plays
    plays = load_active_plays()
    active = plays.get("active", {})
    
    results = []
    for ticker in active:
        play = active[ticker]
        direction = play.get("direction", "")
        if direction == "EQUITY_ONLY":
            continue
        result = update_play_iv(ticker)
        results.append(result)
        iv = result.get("iv_pct")
        iv_check = result.get("iv_check", {})
        if iv:
            severity = iv_check.get("severity", "")
            if severity in ("HIGH", "EXTREME"):
                print(f"  *** IV CRUSH WARNING: {ticker} IV={iv:.0f}% — {severity}. Reduce size.")
            else:
                print(f"  [IV] {ticker}: {iv:.0f}%")
    
    return results


def compute_expected_move_for_catalyst(catalyst_type: str, p_success: float,
                                        sector: str = "BIOTECH") -> tuple:
    """
    Compute (win_move_pct, loss_move_pct) based on catalyst type and probability.
    
    CALIBRATION (Apr 26 2026):
      These are the historical average moves for each catalyst type.
      Used by play_card_formatter to show realistic win/loss scenarios.
    
    Returns (success_move_pct, failure_move_pct)
    """
    moves = {
        # (success, failure) as positive percentages
        "PDUFA":     (45, 30),   # FDA approval gaps 30-60%, failure drops 25-40%
        "READOUT":   (20, 25),   # Phase 3 data: 10-30% on win, 20-35% on failure
        "LABEL_EXT": (10, 12),   # Label extension: 5-15% on approval, 10-15% on rejection
        "ADCOM":     (25, 20),   # AdCom yes: 15-35% up, AdCom no: 15-25% down
        "CONTRACT":  (15, 10),   # Contract win: 10-20% up, loss: 5-15% down
        "DEFAULT":   (25, 20),
    }
    return moves.get(catalyst_type, moves["DEFAULT"])


def get_joint_probability_warning(play: dict) -> str | None:
    """
    Check if a play has dual endpoints and warn if P was set on primary alone.
    Returns a warning string if applicable, or None.
    
    CALIBRATION NOTE (Apr 26 2026 — IDYA lesson):
      When a trial has co-primary endpoints (PFS AND OS), the OPTION should be
      sized on joint probability, not the easier primary endpoint alone.
      IDYA: P(PFS)=90%, P(OS)=46%, P(joint)~42%. We used P=96%.
    """
    notes = (play.get("notes", "") + " " + play.get("drug_or_topic", "")).lower()
    
    # Signals of dual/co-primary endpoints
    dual_signals = [
        "co-primary", "co primary", "dual endpoint", "dual primary",
        "pfs and os", "pfs+os", "both pfs", "pfs/os",
        "two primary", "primary and secondary pfs",
        "efs and os", "rfs and os",
    ]
    
    if any(sig in notes for sig in dual_signals):
        return (
            "DUAL ENDPOINT: P shown is for primary endpoint only. "
            "Joint P (both endpoints) is lower. Size accordingly."
        )
    
    return None


def format_calibration_summary_for_email() -> str:
    """
    Format the calibration rules as a brief footer note for daily emails.
    Reminds reader of the key risk rules in effect.
    """
    lines = [
        "",
        "─" * 65,
        "CALIBRATION RULES IN EFFECT (updated Apr 26 2026):",
        "  P minimum for LONG: >=65% | P maximum for SHORT: <=40%",
        "  Strike: PDUFA 10-25% OTM | Readout 5-15% OTM | Label ext ATM-5% OTM",
        "  IV >150%: HALF SIZE (crush risk) | P>=80% PDUFA: spread recommended",
        "  Grade D/F LONG: HALF SIZE | Grade F PUT: full size (Shkreli strategy)",
        "  Dual endpoints: use joint P for option sizing, not primary alone",
        "─" * 65,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "update_ivs":
        print("Updating IVs for all active plays...")
        results = update_all_play_ivs()
        print(f"Done. {len(results)} plays updated.")
    else:
        print("Usage: python3 calibration_utils.py update_ivs")
        print("\nCalibration utility loaded. Functions:")
        print("  fetch_iv_for_play(ticker, strike, expiry, option_type)")
        print("  update_play_iv(ticker)")
        print("  update_all_play_ivs()")
        print("  compute_expected_move_for_catalyst(catalyst_type, p_success)")
        print("  get_joint_probability_warning(play)")
        print("  format_calibration_summary_for_email()")
