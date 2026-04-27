#!/usr/bin/env python3
"""
AUTO RESOLVER
Runs daily. Detects when a catalyst has resolved and records the final outcome.

WHAT IT DOES:
  1. Checks every active play for resolution signals:
     - PDUFA date passed (auto)
     - Stock moved >25% overnight (catalyst fired)
     - Option expired worthless (DTE = 0)
     - 8-K filed mentioning the drug (from sec_8k_state.json)
     - Option gain >150% (clear winner, record and optionally remove)
  2. When resolved, writes the final outcome to:
     - state/resolved_plays.json  (existing)
     - state/performance_ledger.json (adds "resolved" block to play entry)
  3. Passes resolved data to learning_engine.py for immediate recalibration.

RESOLUTION CRITERIA:
  OUTCOME_WIN:   option gain >100% AND stock moved in correct direction >15%
  OUTCOME_LOSS:  option lost >80% OR expired worthless
  OUTCOME_MIXED: correct direction but option still lost (IV crush, wrong strike)
  OUTCOME_PENDING: catalyst may have fired but outcome unclear

LEARNING DATA STORED PER RESOLVED TRADE:
  - Was direction correct? (stock move in predicted direction)
  - Was option profitable?
  - What was actual stock move on catalyst day?
  - What was IV at entry vs exit?
  - Was science grade predictive?
  - Was P(success) accurate? (if P=70% and we've resolved 10+ plays, Brier score)
"""

import json
import datetime
import sys
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
LEDGER_FILE = BASE_DIR / "state/performance_ledger.json"
RESOLVED_FILE = BASE_DIR / "state/resolved_plays.json"
ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
SEC_8K_FILE = BASE_DIR / "state/sec_8k_state.json"


def load_ledger():
    if LEDGER_FILE.exists():
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"plays": {}}


def save_ledger(ledger):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def load_resolved():
    if RESOLVED_FILE.exists():
        with open(RESOLVED_FILE) as f:
            d = json.load(f)
        return d.get("resolved", [])
    return []


def save_resolved(resolved_list):
    with open(RESOLVED_FILE, "w") as f:
        json.dump({"resolved": resolved_list}, f, indent=2)


def load_active():
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            d = json.load(f)
        return d.get("active", {}), d.get("monitor", {})
    return {}, {}


def get_recent_8k_mentions(ticker: str) -> list:
    """Check sec_8k_state for recent 8-K filings mentioning this ticker."""
    if not SEC_8K_FILE.exists():
        return []
    try:
        with open(SEC_8K_FILE) as f:
            state = json.load(f)
        alerts = state.get("alerts_sent", {})
        return [a for k, a in alerts.items() if ticker in k]
    except:
        return []


def classify_resolution(ledger_entry: dict) -> dict | None:
    """
    Look at the most recent snapshots and determine if a play has resolved.
    Returns resolution dict or None if still active.
    """
    snapshots = ledger_entry.get("snapshots", [])
    if len(snapshots) < 1:
        return None

    latest = snapshots[-1]
    direction = ledger_entry.get("direction", "")
    pdufa_date = ledger_entry.get("pdufa_date")
    expiry = ledger_entry.get("expiry")
    entry_fill = ledger_entry.get("entry_fill", 0) or 0
    entry_stock = ledger_entry.get("entry_stock", 0) or 0
    today = datetime.date.today()
    ticker = ledger_entry.get("ticker", "?")

    option_mid = latest.get("option_mid", 0) or 0
    stock_move = latest.get("stock_move_from_entry", 0) or 0
    pnl_pct = latest.get("pnl_pct", 0) or 0
    days_to_exp = latest.get("days_to_exp", 999) or 999
    iv_pct = latest.get("iv_pct") or ledger_entry.get("entry_iv_pct")

    # ------------------------------------------------------------------
    # RULE 1: Expired worthless
    # ------------------------------------------------------------------
    if days_to_exp <= 0 and entry_fill > 0:
        is_correct_direction = (
            (direction == "LONG_CALLS" and stock_move > 0) or
            (direction == "LONG_PUTS" and stock_move < 0)
        )
        if option_mid < 0.05:
            return {
                "outcome": "LOSS_EXPIRED",
                "direction_correct": is_correct_direction,
                "option_pnl_pct": -100.0,
                "stock_move_pct": stock_move,
                "iv_entry": ledger_entry.get("entry_iv_pct"),
                "iv_exit": iv_pct,
                "resolve_trigger": "EXPIRED_WORTHLESS",
                "notes": f"Option expired. Stock moved {stock_move:+.1f}% from entry. Direction {'correct' if is_correct_direction else 'wrong'}.",
            }

    # ------------------------------------------------------------------
    # RULE 2: Massive option gain (>150%) — catalyst fired, record win
    # ------------------------------------------------------------------
    if pnl_pct > 150 and entry_fill > 0:
        is_correct_direction = True  # By definition if option up >150%
        return {
            "outcome": "WIN",
            "direction_correct": True,
            "option_pnl_pct": pnl_pct,
            "stock_move_pct": stock_move,
            "iv_entry": ledger_entry.get("entry_iv_pct"),
            "iv_exit": iv_pct,
            "resolve_trigger": "OPTION_UP_150PCT",
            "notes": f"Option +{pnl_pct:.0f}%. Stock +{stock_move:.1f}% from entry. Clear win.",
        }

    # ------------------------------------------------------------------
    # RULE 3: Large stock move (>25%) + catalyst signal
    # ------------------------------------------------------------------
    catalyst_signal = latest.get("catalyst_signal", "")
    if "LARGE_MOVE" in (catalyst_signal or ""):
        is_correct_direction = (
            (direction == "LONG_CALLS" and stock_move > 15) or
            (direction == "LONG_PUTS" and stock_move < -15)
        )
        if is_correct_direction and pnl_pct > 50:
            outcome = "WIN"
        elif is_correct_direction and pnl_pct > 0:
            outcome = "WIN_PARTIAL"
        elif is_correct_direction and pnl_pct < -50:
            outcome = "MIXED_IV_CRUSH"  # Direction right but IV crush killed option
        elif not is_correct_direction and pnl_pct < -50:
            outcome = "LOSS_WRONG_DIRECTION"
        else:
            outcome = "PENDING_REVIEW"

        return {
            "outcome": outcome,
            "direction_correct": is_correct_direction,
            "option_pnl_pct": pnl_pct,
            "stock_move_pct": stock_move,
            "iv_entry": ledger_entry.get("entry_iv_pct"),
            "iv_exit": iv_pct,
            "resolve_trigger": "LARGE_STOCK_MOVE",
            "notes": f"Stock moved {stock_move:+.1f}% from entry. Option {pnl_pct:+.0f}%. Signal: {catalyst_signal}",
        }

    # ------------------------------------------------------------------
    # RULE 4: PDUFA date passed
    # ------------------------------------------------------------------
    if pdufa_date:
        try:
            pdufa_d = datetime.date.fromisoformat(pdufa_date)
            if today > pdufa_d + datetime.timedelta(days=1):
                # PDUFA passed — stock should have moved
                is_correct_direction = (
                    (direction == "LONG_CALLS" and stock_move > 5) or
                    (direction == "LONG_PUTS" and stock_move < -5)
                )
                if pnl_pct > 0:
                    outcome = "WIN" if pnl_pct > 50 else "WIN_PARTIAL"
                elif is_correct_direction and pnl_pct < 0:
                    outcome = "MIXED_IV_CRUSH"
                else:
                    outcome = "LOSS_WRONG_DIRECTION" if not is_correct_direction else "LOSS_WRONG_STRIKE"

                return {
                    "outcome": outcome,
                    "direction_correct": is_correct_direction,
                    "option_pnl_pct": pnl_pct,
                    "stock_move_pct": stock_move,
                    "iv_entry": ledger_entry.get("entry_iv_pct"),
                    "iv_exit": iv_pct,
                    "resolve_trigger": "PDUFA_DATE_PASSED",
                    "notes": f"PDUFA {pdufa_date} passed. Stock {stock_move:+.1f}%. Option {pnl_pct:+.0f}%.",
                }
        except:
            pass

    # ------------------------------------------------------------------
    # RULE 5: Option lost >85% — likely dead
    # ------------------------------------------------------------------
    if pnl_pct < -85 and entry_fill > 0 and days_to_exp > 5:
        return {
            "outcome": "LOSS_OPTION_DEAD",
            "direction_correct": False,
            "option_pnl_pct": pnl_pct,
            "stock_move_pct": stock_move,
            "iv_entry": ledger_entry.get("entry_iv_pct"),
            "iv_exit": iv_pct,
            "resolve_trigger": "OPTION_DOWN_85PCT",
            "notes": f"Option lost {pnl_pct:.0f}% with {days_to_exp}d remaining. Effectively dead.",
        }

    return None  # Still active


def build_resolved_record(ticker: str, ledger_entry: dict, resolution: dict,
                            active_play: dict) -> dict:
    """Build the resolved trade record for storage."""
    today = datetime.date.today().isoformat()
    return {
        "ticker": ticker,
        "resolved_date": today,
        "resolve_trigger": resolution.get("resolve_trigger"),
        "outcome": resolution.get("outcome"),

        # Direction and magnitude
        "direction_correct": resolution.get("direction_correct"),
        "option_pnl_pct": resolution.get("option_pnl_pct"),
        "stock_move_pct": resolution.get("stock_move_pct"),
        "notes": resolution.get("notes", ""),

        # Entry conditions (what we predicted)
        "entry_p_success": ledger_entry.get("p_success"),
        "entry_science_grade": ledger_entry.get("science_grade"),
        "entry_catalyst_type": ledger_entry.get("catalyst_type"),
        "entry_direction": ledger_entry.get("direction"),
        "entry_fill": ledger_entry.get("entry_fill"),
        "entry_stock": ledger_entry.get("entry_stock"),
        "entry_iv_pct": ledger_entry.get("entry_iv_pct"),
        "entry_date": ledger_entry.get("entry_date"),
        "expiry": ledger_entry.get("expiry"),
        "strike": ledger_entry.get("strike"),

        # IV crush diagnosis
        "iv_entry": resolution.get("iv_entry"),
        "iv_exit": resolution.get("iv_exit"),
        "iv_crush_suspected": (
            resolution.get("outcome") in ("MIXED_IV_CRUSH",) or
            (
                resolution.get("direction_correct") and
                (resolution.get("option_pnl_pct") or 0) < 0 and
                (resolution.get("iv_entry") or 0) > 130
            )
        ),

        # Full snapshot history for analysis
        "snapshots": ledger_entry.get("snapshots", []),

        # Original play dict for reference
        "original_play": {
            k: v for k, v in active_play.items()
            if k not in ("snapshots",)
        },
    }


def run_auto_resolver() -> dict:
    """
    Main entry point. Detects resolved plays, writes outcomes, triggers learning.
    Returns summary dict.
    """
    today = datetime.date.today().isoformat()
    ledger = load_ledger()
    resolved_list = load_resolved()
    active, monitor = load_active()

    print(f"\n[auto_resolver] Running — {today}")
    print(f"  Ledger entries: {len(ledger['plays'])} | Existing resolved: {len(resolved_list)}")

    already_resolved = {r["ticker"] for r in resolved_list}
    new_resolutions = []

    for ticker, entry in ledger["plays"].items():
        if entry.get("status") == "RESOLVED":
            continue
        if ticker in already_resolved:
            # Update status in ledger
            entry["status"] = "RESOLVED"
            continue

        resolution = classify_resolution(entry)
        if resolution:
            print(f"  RESOLVED: {ticker} — {resolution['outcome']} | option {resolution.get('option_pnl_pct', 0):+.0f}% | stock {resolution.get('stock_move_pct', 0):+.1f}%")

            active_play = active.get(ticker, monitor.get(ticker, {}))
            record = build_resolved_record(ticker, entry, resolution, active_play)

            resolved_list.append(record)
            new_resolutions.append(record)

            # Update ledger entry
            entry["status"] = "RESOLVED"
            entry["resolved"] = resolution
            ledger["plays"][ticker] = entry

    if new_resolutions:
        save_resolved(resolved_list)
        save_ledger(ledger)
        print(f"  Saved {len(new_resolutions)} new resolutions")

        # Trigger learning engine immediately
        try:
            from learning_engine import run_learning_cycle
            learning_result = run_learning_cycle()
            print(f"  Learning cycle complete: {learning_result.get('summary', '')}")
        except Exception as e:
            print(f"  Learning engine error: {e}")
    else:
        print(f"  No new resolutions today")

    return {
        "date": today,
        "new_resolutions": len(new_resolutions),
        "total_resolved": len(resolved_list),
        "resolutions": new_resolutions,
    }


def format_resolutions_for_email(resolver_result: dict) -> str:
    """Format any new resolutions for inclusion in the daily email."""
    resolutions = resolver_result.get("resolutions", [])
    if not resolutions:
        return ""

    lines = ["", "─" * 65, "TRADES RESOLVED TODAY", "─" * 65]
    for r in resolutions:
        ticker = r["ticker"]
        outcome = r["outcome"]
        opt_pnl = r.get("option_pnl_pct", 0) or 0
        stock_move = r.get("stock_move_pct", 0) or 0
        direction_ok = r.get("direction_correct", False)

        outcome_icons = {
            "WIN": "WIN",
            "WIN_PARTIAL": "WIN (partial)",
            "MIXED_IV_CRUSH": "DIRECTION RIGHT / IV CRUSH",
            "LOSS_WRONG_DIRECTION": "LOSS — wrong direction",
            "LOSS_WRONG_STRIKE": "LOSS — wrong strike",
            "LOSS_EXPIRED": "LOSS — expired",
            "LOSS_OPTION_DEAD": "LOSS — option dead",
            "PENDING_REVIEW": "PENDING REVIEW",
        }
        label = outcome_icons.get(outcome, outcome)

        lines.append(f"  {ticker}: {label}")
        lines.append(f"    Option P&L: {opt_pnl:+.0f}% | Stock move: {stock_move:+.1f}% | Direction: {'correct' if direction_ok else 'wrong'}")
        if r.get("iv_crush_suspected"):
            lines.append(f"    IV CRUSH: IV at entry was {r.get('iv_entry', '?')}%. Option lost despite correct direction.")
        lines.append(f"    {r.get('notes', '')}")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run_auto_resolver()
    print(f"\nResolved {result['new_resolutions']} plays. Total: {result['total_resolved']}")
