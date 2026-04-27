#!/usr/bin/env python3
"""
IV CRUSH EXIT RULES
The #1 lesson from TVTX, IDYA: even when direction is RIGHT, IV collapse post-announcement
destroys option value faster than the stock move creates intrinsic value.

THE RULE (hardcoded, mandatory):
  On catalyst announcement day (PDUFA date, topline, etc.):
  - Sell 50% of the position at market OPEN (9:30-9:45 AM ET) regardless of direction
  - This locks in IV premium while it still exists
  - Hold remaining 50% for the directional move to play out

WHY THIS WORKS:
  TVTX example: Stock opened +8% pre-market on approval. IV was 305% at entry.
  If we sold 50% at open, we would have captured remaining time value before IV collapsed.
  Instead holding 100% through 9:45 AM = IV crushed, option dead.

  IDYA example: PFS positive pre-market +12%. IV 188% at entry.
  Selling 50% at open = capture IV premium. Hold 50% for NDA thesis.
  Instead: IV collapsed from 188% to ~40%, option -91%.

IMPLEMENTATION:
  1. Daily cron checks: is today a catalyst day for any active play?
  2. If yes AND IV > 100%: generate sell order alert at 9:30 AM ET open
  3. Alert format: pre-market email with specific exit instructions
  4. Also flags plays approaching expiry (DTE < 10) for roll decisions

INTRADAY RULE:
  If a play gaps >15% pre-market AND IV was >100% at entry:
  - Immediate alert to sell 50% at open
  - Subject: "SNIPER EXIT ALERT — [TICKER] — Sell 50% at open"
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
LEDGER_FILE = BASE_DIR / "state/performance_ledger.json"

# IV threshold above which we apply the 50% exit rule
IV_CRUSH_THRESHOLD = 100  # IV > 100% = rule applies

# Days to expiry below which we send roll warning
DTE_ROLL_WARNING = 10

# Minimum stock gap to trigger immediate exit alert
GAP_TRIGGER_PCT = 15


def load_active_plays() -> dict:
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            return json.load(f).get("active", {})
    return {}


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"plays": {}}


def check_catalyst_days() -> list:
    """
    Returns list of plays where today is (or is within 1 day of) the PDUFA date.
    These need pre-market exit alerts.
    """
    today = datetime.date.today()
    active = load_active_plays()
    alerts = []

    for ticker, play in active.items():
        pdufa = play.get("pdufa_date")
        if not pdufa:
            continue
        try:
            pdufa_date = datetime.date.fromisoformat(pdufa)
            days_to_pdufa = (pdufa_date - today).days
            if 0 <= days_to_pdufa <= 1:
                iv = play.get("iv_pct")
                alerts.append({
                    "ticker": ticker,
                    "pdufa_date": pdufa,
                    "days_to_pdufa": days_to_pdufa,
                    "iv_pct": iv,
                    "apply_exit_rule": iv and iv > IV_CRUSH_THRESHOLD,
                    "direction": play.get("direction"),
                    "strike": play.get("option_strike"),
                    "expiry": play.get("option_expiry"),
                    "reason": "PDUFA today" if days_to_pdufa == 0 else "PDUFA tomorrow",
                })
        except:
            pass

    return alerts


def check_dte_warnings() -> list:
    """Returns plays with DTE < DTE_ROLL_WARNING that need roll decisions."""
    today = datetime.date.today()
    active = load_active_plays()
    warnings = []

    for ticker, play in active.items():
        expiry = play.get("option_expiry")
        if not expiry:
            continue
        try:
            exp_date = datetime.date.fromisoformat(expiry)
            dte = (exp_date - today).days
            if 0 < dte < DTE_ROLL_WARNING:
                pdufa = play.get("pdufa_date")
                catalyst_date = pdufa or play.get("estimated_announcement", "")
                warnings.append({
                    "ticker": ticker,
                    "dte": dte,
                    "expiry": expiry,
                    "catalyst": catalyst_date,
                    "direction": play.get("direction"),
                    "strike": play.get("option_strike"),
                    "lottery_tier": play.get("lottery_tier", False),
                })
        except:
            pass

    return warnings


def check_gap_exit_alerts(current_prices: dict) -> list:
    """
    Given a dict of {ticker: current_price}, check for pre-market gaps
    that should trigger immediate 50% exit alerts.
    current_prices: fetched from yfinance pre-market
    """
    ledger = load_ledger()
    active = load_active_plays()
    alerts = []

    for ticker, current_price in current_prices.items():
        if ticker not in active:
            continue
        play = active[ticker]
        ledger_entry = ledger["plays"].get(ticker, {})
        entry_stock = ledger_entry.get("entry_stock", 0) or 0
        iv_at_entry = ledger_entry.get("entry_iv_pct", 0) or 0
        direction = play.get("direction", "")

        if not entry_stock or not current_price:
            continue

        gap_pct = (current_price - entry_stock) / entry_stock * 100

        # Large gap in the right direction + high IV at entry
        if abs(gap_pct) >= GAP_TRIGGER_PCT and iv_at_entry > IV_CRUSH_THRESHOLD:
            correct_direction = (
                (direction == "LONG_CALLS" and gap_pct > 0) or
                (direction == "LONG_PUTS" and gap_pct < 0)
            )
            alerts.append({
                "ticker": ticker,
                "gap_pct": gap_pct,
                "current_price": current_price,
                "entry_stock": entry_stock,
                "iv_at_entry": iv_at_entry,
                "direction": direction,
                "correct_direction": correct_direction,
                "action": "SELL 50% AT OPEN (9:30-9:45 AM ET)",
                "reason": f"Gap {'matches' if correct_direction else 'WRONG direction but'} + IV={iv_at_entry:.0f}% at entry = IV crush risk",
            })

    return alerts


def format_exit_alert_email(catalyst_alerts: list, dte_warnings: list,
                              gap_alerts: list = None) -> str:
    """
    Format the pre-market exit alert email body.
    Sent when PDUFA is today/tomorrow OR large pre-market gap detected.
    """
    lines = []
    today = datetime.date.today().strftime("%A, %B %d, %Y")

    lines.append(f"ALPHA SNIPER — EXIT ALERT | {today}")
    lines.append("=" * 60)
    lines.append("")

    # Gap alerts (highest urgency)
    if gap_alerts:
        lines.append("*** IMMEDIATE ACTION REQUIRED — PRE-MARKET GAP DETECTED ***")
        lines.append("")
        for a in gap_alerts:
            gap_str = f"+{a['gap_pct']:.1f}%" if a['gap_pct'] > 0 else f"{a['gap_pct']:.1f}%"
            dir_tag = "CORRECT DIRECTION" if a["correct_direction"] else "WRONG DIRECTION"
            lines.append(f"  {a['ticker']}: Stock {gap_str} pre-market | {dir_tag}")
            lines.append(f"  IV at entry: {a['iv_at_entry']:.0f}% — HIGH CRUSH RISK")
            lines.append(f"  ACTION: {a['action']}")
            lines.append(f"  Why: {a['reason']}")
            lines.append("")

    # PDUFA day alerts
    if catalyst_alerts:
        lines.append("PDUFA / CATALYST DAY — IV CRUSH EXIT PROTOCOL:")
        lines.append("")
        for a in catalyst_alerts:
            timing = "TODAY" if a["days_to_pdufa"] == 0 else "TOMORROW"
            iv_str = f"{a['iv_pct']:.0f}%" if a["iv_pct"] else "unknown"
            rule_str = " -- IV CRUSH RULE APPLIES" if a["apply_exit_rule"] else ""
            lines.append(f"  {a['ticker']}: PDUFA {timing} ({a['pdufa_date']}){rule_str}")
            lines.append(f"  Strike: ${a['strike']} | Expiry: {a['expiry']} | IV: {iv_str}")
            if a["apply_exit_rule"]:
                lines.append(f"  ACTION: Sell 50% at OPEN (9:30-9:45 AM ET) to lock in IV premium")
                lines.append(f"  Hold 50% for the directional move to play out")
                lines.append(f"  Why: At IV>{IV_CRUSH_THRESHOLD}%, IV collapse post-announcement often exceeds stock move gain")
            lines.append("")

    # DTE warnings
    if dte_warnings:
        lines.append("DTE WARNING — OPTIONS EXPIRING SOON:")
        lines.append("")
        for w in dte_warnings:
            lottery_tag = " [LOTTERY — let expire or sell now]" if w["lottery_tier"] else ""
            lines.append(f"  {w['ticker']}: {w['dte']} days to expiry ({w['expiry']}){lottery_tag}")
            lines.append(f"  Catalyst: {str(w['catalyst'])[:40]} | Strike: ${w['strike']}")
            if not w["lottery_tier"]:
                lines.append(f"  DECISION: Roll to next expiry, take profit, or let expire?")
            lines.append("")

    lines.append("─" * 60)
    lines.append("These alerts are generated automatically from the IV monitoring system.")
    lines.append("Always execute exits before 9:45 AM ET to capture remaining time value.")

    return "\n".join(lines)


def run_daily_exit_check() -> dict:
    """
    Run all exit checks. Returns results for inclusion in daily email.
    Called as part of the daily cron pipeline.
    """
    catalyst_alerts = check_catalyst_days()
    dte_warnings = check_dte_warnings()

    result = {
        "catalyst_alerts": catalyst_alerts,
        "dte_warnings": dte_warnings,
        "needs_alert_email": bool(catalyst_alerts or dte_warnings),
        "alert_email_body": None,
    }

    if catalyst_alerts or dte_warnings:
        result["alert_email_body"] = format_exit_alert_email(
            catalyst_alerts, dte_warnings
        )

    if catalyst_alerts:
        print(f"  [iv_exit] PDUFA day alerts: {[a['ticker'] for a in catalyst_alerts]}")
    if dte_warnings:
        print(f"  [iv_exit] DTE warnings: {[w['ticker'] for w in dte_warnings]}")

    return result


def format_exit_section_for_email(exit_result: dict) -> str:
    """Compact section for the main daily email."""
    alerts = exit_result.get("catalyst_alerts", [])
    warnings = exit_result.get("dte_warnings", [])

    if not alerts and not warnings:
        return ""

    lines = ["", "─" * 65, "IV CRUSH EXIT RULES", "─" * 65]

    for a in alerts:
        timing = "TODAY" if a["days_to_pdufa"] == 0 else "tomorrow"
        rule = " -- SELL 50% AT OPEN" if a["apply_exit_rule"] else ""
        lines.append(f"  {a['ticker']}: PDUFA {timing}{rule}")

    for w in warnings:
        lottery = " [lottery]" if w["lottery_tier"] else ""
        lines.append(f"  {w['ticker']}: {w['dte']}d to expiry — roll or exit?{lottery}")

    return "\n".join(lines)


if __name__ == "__main__":
    result = run_daily_exit_check()
    if result["alert_email_body"]:
        print(result["alert_email_body"])
    else:
        print("No exit alerts today.")
