#!/usr/bin/env python3
"""
ALPHA SNIPER — EMAIL FORMATTER
Assembles the final daily email body from all components.
All text is dynamic. Nothing hardcoded. Called by the daily cron.

Usage:
    from email_formatter import build_daily_email, build_subject_line
"""

import datetime
import json
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE


# ── SUBJECT LINE ────────────────────────────────────────────────────────────

def build_subject_line(active_plays: dict, best_ticker: str = None,
                        best_multiple: float = None) -> str:
    """
    Build the email subject dynamically.
    Format: Alpha Sniper — DATE | N Plays (B🧬 C🏛 A⚖️) | Best: TICKER ~Xx on $1k
    """
    today = datetime.date.today().strftime("%b %d, %Y")

    bio_plays   = [p for p in active_plays.values() if p.get("sector", "BIOTECH") == "BIOTECH"]
    con_plays   = [p for p in active_plays.values() if p.get("sector") == "CONTRACT"]
    adcom_plays = [p for p in active_plays.values() if p.get("sector") == "ADCOM"]

    n = len(active_plays)
    sector_summary = f"{len(bio_plays)}🧬"
    if con_plays:
        sector_summary += f" {len(con_plays)}🏛"
    if adcom_plays:
        sector_summary += f" {len(adcom_plays)}⚖️"

    if best_ticker and best_multiple:
        best_str = f" | Best: {best_ticker} ~{best_multiple}x on $1k"
    else:
        best_str = ""

    return f"Alpha Sniper — {today} | {n} Plays ({sector_summary}){best_str}"


# ── FULL ORDER LADDER ────────────────────────────────────────────────────────

def build_full_ladder(play: dict, option_fill: float) -> str:
    """
    Build the full copy-paste order ladder for a play.
    All math is dynamic from live fill price.
    """
    ticker    = play.get("ticker", "?")
    expiry    = play.get("option_expiry", "?")
    strike    = play.get("option_strike", "?")
    opt_type  = play.get("option_type", "C")
    direction = play.get("direction", "LONG_CALLS")

    if direction in ("EQUITY_ONLY", "CALL_SPREAD") or not option_fill or not strike:
        return f"=== {ticker} ===\n[No standard ladder — see option_line in play card above]"

    # Contracts for $1k risk
    contracts = max(1, int(1000 / (option_fill * 100)))
    bulk = max(1, contracts // 4)
    total_inv = round(contracts * option_fill * 100)

    lines = [f"=== {ticker} ==="]
    lines.append(f"DECOY 1:  BUY TO OPEN {ticker} {expiry} ${strike}{opt_type} | LIMIT ${option_fill:.2f}      | 1 contract  | GTC")
    lines.append(f"DECOY 2:  BUY TO OPEN {ticker} {expiry} ${strike}{opt_type} | LIMIT ${option_fill-0.03:.2f}  | 1 contract  | GTC")

    for i, offset in enumerate([0.05, 0.10, 0.15, 0.20], 1):
        price = max(0.05, round(option_fill - offset, 2))
        lines.append(f"LADDER {i}: BUY TO OPEN {ticker} {expiry} ${strike}{opt_type} | LIMIT ${price:.2f}      | {bulk} contract(s) | GTC")

    avg_fill = round(option_fill - 0.10, 2)
    lines.append(f"Total: {contracts}c avg ${avg_fill:.2f}, risk ~${total_inv}")
    lines.append("Refresh: Cancel and re-ladder $0.03 lower after 90 min no fills.")
    lines.append("Exit: Sell 40-60% at open on announcement day before 9:45 AM ET.")
    return "\n".join(lines)


# ── INTELLIGENCE SIGNALS BLOCK ───────────────────────────────────────────────

def format_signals_block(signals_json: dict) -> str:
    """
    Format the intelligence signals from unified_master_signals.json.
    Shown at the top of the email — critical signals first.
    """
    lines = []
    lines.append("━" * 55)
    lines.append("INTELLIGENCE — ALL SECTORS")
    lines.append("━" * 55)

    critical = signals_json.get("critical", [])
    high     = signals_json.get("high", [])
    needs    = signals_json.get("needs_dual_model_scoring", [])

    if not critical and not high and not needs:
        lines.append("✓ All clear. No new signals across Biotech, Contracts, or AdCom.")
        return "\n".join(lines)

    if critical:
        lines.append(f"\n🚨 CRITICAL ({len(critical)}):")
        for s in critical:
            lines.append(f"  {s.get('icon','')} {s.get('ticker','?')}: {s.get('detail', s.get('title',''))[:120]}")
            if s.get("implication"):
                lines.append(f"    → {s['implication'][:120]}")

    if high:
        lines.append(f"\n🟠 HIGH ({len(high)}):")
        for s in high:
            lines.append(f"  {s.get('ticker','?')}: {s.get('detail', s.get('title',''))[:100]}")

    if needs:
        lines.append(f"\n🔬 NEW CANDIDATES BEING SCORED ({len(needs)}):")
        for s in needs[:5]:
            lines.append(f"  {s.get('ticker','?')}: {s.get('title', s.get('topic',''))[:100]}")

    return "\n".join(lines)


# ── FULL EMAIL BODY ──────────────────────────────────────────────────────────

def build_daily_email(
    active_plays: dict,         # dict of ticker → play dict
    play_cards: list,           # list of (ticker, card_text, option_fill) tuples — already assembled
    signals_json: dict,         # from unified_master_signals.json
    lifecycle_actions: list,    # removed/graduated tickers from lifecycle manager
    best_ticker: str = None,
    best_multiple: float = None,
    science_section: str = "",  # optional — Shkreli-style science grades section
    tracker_results: dict = None,   # from performance_tracker.run_tracker()
    resolver_results: dict = None,  # from auto_resolver.run_auto_resolver()
    learning_results: dict = None,  # from learning_engine.run_learning_cycle()
) -> tuple:
    """
    Assembles the complete daily email.
    Returns (subject, body) — both fully dynamic, nothing hardcoded.

    play_cards: list of (ticker, assembled_card_text, option_fill)
    """
    today_str = datetime.date.today().strftime("%A, %B %d, %Y")
    now_str   = datetime.datetime.now().strftime("%I:%M %p PT")

    lines = []

    # ── HEADER ──────────────────────────────────────────────────────────────
    lines.append(f"ALPHA SNIPER DAILY REPORT — {today_str} | {now_str}")
    lines.append("=" * 65)
    lines.append("")

    # ── QUICK SUMMARY TABLE ──────────────────────────────────────────────────
    lines.append("ACTIVE PLAYS — QUICK VIEW")
    lines.append("─" * 55)
    lines.append(f"  {'TICKER':<7} {'DAYS':>4}  {'P%':>4}  DIRECTION          ANNOUNCEMENT")
    lines.append(f"  {'──────':<7} {'────':>4}  {'──':>4}  ─────────────────  ────────────────────")

    from play_card_formatter import _estimate_days
    sorted_plays = sorted(
        active_plays.items(),
        key=lambda x: _estimate_days(x[1])
    )
    for ticker, p in sorted_plays:
        days   = _estimate_days(p)
        days_s = f"{days}d" if days < 999 else "??d"
        prob   = p.get("p_success", 0)
        direc  = {
            "LONG_CALLS":  "LONG CALLS  📈",
            "LONG_PUTS":   "LONG PUTS   📉",
            "CALL_SPREAD": "CALL SPREAD 📊",
            "EQUITY_ONLY": "EQUITY ONLY 💵",
        }.get(p.get("direction", ""), p.get("direction", ""))
        cert   = "✓" if p.get("announcement_certainty") == "CERTAIN" else "~"
        ann    = p.get("estimated_announcement", "?")[:22]
        lines.append(f"  {ticker:<7} {days_s:>4}  {prob:>3}%  {direc:<18} {cert}{ann}")

    lines.append("")

    # ── LIFECYCLE ACTIONS ────────────────────────────────────────────────────
    removed   = [a for a in lifecycle_actions if a.get("type") in ("REMOVED", "RESOLVED_DATA_DROPPED")]
    graduated = [a for a in lifecycle_actions if a.get("type") == "GRADUATED"]
    upgraded  = [a for a in lifecycle_actions if a.get("type") == "UPGRADED_TO_CERTAIN"]

    if removed or graduated or upgraded:
        lines.append("📋 WATCHLIST CHANGES TODAY")
        lines.append("─" * 55)
        for a in removed:
            lines.append(f"  🗑 REMOVED: {a['ticker']} — {a.get('reason','')}")
        for a in graduated:
            lines.append(f"  🎓 GRADUATED: {a['ticker']} entered 60-day window → now ACTIVE")
        for a in upgraded:
            lines.append(f"  ✅ DATE CONFIRMED: {a['ticker']} upgraded ESTIMATED → CERTAIN")
        lines.append("")

    # ── INTELLIGENCE SIGNALS ─────────────────────────────────────────────────
    lines.append(format_signals_block(signals_json))
    lines.append("")

    # ── LIVE P&L TRACKER ────────────────────────────────────────────────────
    if tracker_results:
        try:
            from performance_tracker import format_pnl_table_for_email
            pnl_block = format_pnl_table_for_email(tracker_results)
            if pnl_block:
                lines.append(pnl_block)
                lines.append("")
        except Exception:
            pass

    # ── RESOLVED TRADES TODAY ────────────────────────────────────────────────
    if resolver_results and resolver_results.get("new_resolutions", 0) > 0:
        try:
            from auto_resolver import format_resolutions_for_email
            res_block = format_resolutions_for_email(resolver_results)
            if res_block:
                lines.append(res_block)
                lines.append("")
        except Exception:
            pass

    # ── LEARNING ENGINE SUMMARY ──────────────────────────────────────────────
    if learning_results and learning_results.get("n", 0) > 0:
        try:
            from learning_engine import format_learning_summary_for_email
            learn_block = format_learning_summary_for_email(learning_results)
            if learn_block:
                lines.append(learn_block)
                lines.append("")
        except Exception:
            pass

    # ── SCIENCE GRADES (Shkreli-style) ──────────────────────────────────────
    if science_section:
        lines.append(science_section)
        lines.append("")

    # ── PLAY CARDS (tiered: profit alerts → main → lottery → monitor) ──────────
    main_cards = []
    profit_alert_cards = []
    lottery_cards = []
    monitor_only_cards = []

    for ticker, card_text, option_fill in play_cards:
        play = active_plays.get(ticker, {})
        n_str = play.get("notes", "").upper()
        if play.get("lottery_tier"):
            lottery_cards.append((ticker, card_text, option_fill))
        elif play.get("below_threshold") or play.get("below_multiple_threshold"):
            monitor_only_cards.append((ticker, card_text, option_fill))
        elif "TAKE PARTIAL PROFIT" in n_str:
            profit_alert_cards.append((ticker, card_text, option_fill))
        else:
            main_cards.append((ticker, card_text, option_fill))

    if profit_alert_cards:
        lines.append("=" * 55)
        lines.append("ACTION REQUIRED — TAKE PARTIAL PROFIT")
        lines.append("=" * 55)
        for ticker, card_text, option_fill in profit_alert_cards:
            lines.append("")
            lines.append(f"*** PROFIT ALERT: {ticker} — Sell 50% at open Monday before 9:45AM ET ***")
            lines.append(card_text)
            lines.append("─" * 55)
        lines.append("")

    n = len(main_cards)
    lines.append(f"{'━'*55}")
    lines.append(f"{n} MAIN PLAYS — SORTED BY DAYS TO ANNOUNCEMENT")
    lines.append(f"{'━'*55}")

    for ticker, card_text, option_fill in main_cards:
        lines.append("")
        lines.append(card_text)
        lines.append("─" * 55)

    if lottery_cards:
        lines.append("")
        lines.append(f"{'━'*55}")
        lines.append("SPECULATIVE TIER (lottery tickets — 1-2 contracts max)")
        lines.append(f"{'━'*55}")
        for ticker, card_text, option_fill in lottery_cards:
            lines.append("")
            lines.append(f"NOTE: {ticker} is a lottery play. Expiry may precede main catalyst. Size = 1-2 contracts max.")
            lines.append(card_text)
            lines.append("─" * 55)

    if monitor_only_cards:
        lines.append("")
        lines.append(f"{'━'*55}")
        lines.append("MONITOR ONLY (below 2.5x threshold or Grade F mismatch — track, do not size up)")
        lines.append(f"{'━'*55}")
        for ticker, card_text, option_fill in monitor_only_cards:
            lines.append("")
            lines.append(card_text)
            lines.append("─" * 55)

    play_cards_for_orders = profit_alert_cards + main_cards + lottery_cards

    # ── FULL COPY-PASTE ORDERS ───────────────────────────────────────────────
    lines.append("")
    lines.append("━" * 55)
    lines.append("FULL COPY-PASTE ORDERS")
    lines.append("━" * 55)
    lines.append("Enter decoys first. Ladder fills as IV compresses pre-announcement.")
    lines.append("")

    for ticker, card_text, option_fill in play_cards_for_orders:
        play = active_plays.get(ticker, {})
        if play.get("direction") in ("EQUITY_ONLY", "CALL_SPREAD"):
            # Still show a note for these
            lines.append(f"=== {ticker} ===")
            if play.get("direction") == "EQUITY_ONLY":
                price = play.get("notes", "").split("$")
                lines.append(f"BUY {ticker} SHARES — see equity line in play card above")
            elif play.get("direction") == "CALL_SPREAD":
                lines.append(f"BUY CALL SPREAD {play.get('option_strike','')} {play.get('option_expiry','')} — see play card above")
            lines.append("")
        elif option_fill and option_fill > 0:
            lines.append(build_full_ladder(play, option_fill))
            lines.append("")

    # ── IV CRUSH + JOINT PROBABILITY WARNINGS
    try:
        # f-misc-09: replaced ``sys.path.insert(0, str(BASE))`` +
        # bare ``from calibration_utils ...`` / ``from sectors.unified_scorer ...``
        # with canonical ``biotech_sniper.*`` absolute imports.
        from biotech_sniper.calibration_utils import (
            get_joint_probability_warning,
        )
        from biotech_sniper.sectors.unified_scorer import check_iv_crush_risk
        risk_warnings = []
        for ticker, play in active_plays.items():
            iv_pct = play.get('iv_pct')
            if iv_pct:
                iv_check = check_iv_crush_risk(iv_pct)
                if iv_check.get('severity') in ('HIGH', 'EXTREME'):
                    risk_warnings.append(f'  {ticker}: {iv_check["warning"]}')
            jp_warn = get_joint_probability_warning(play)
            if jp_warn:
                risk_warnings.append(f'  {ticker}: {jp_warn}')
        if risk_warnings:
            lines.append('')
            lines.append('─' * 55)
            lines.append('RISK ALERTS:')
            for w in risk_warnings:
                lines.append(w)
    except Exception:
        pass

    # ── CALIBRATION RULES FOOTER
    try:
        # f-misc-09: bare ``from calibration_utils import ...``
        # only worked because the previous block had inserted
        # ``BASE`` into ``sys.path``. Use the canonical absolute path
        # directly.
        from biotech_sniper.calibration_utils import (
            format_calibration_summary_for_email,
        )
        lines.append(format_calibration_summary_for_email())
    except Exception:
        pass

    # ── FOOTER ───────────────────────────────────────────────────────────────
    best_str = f"Best multiple: {best_ticker} ({active_plays.get(best_ticker,{}).get('sector','BIOTECH')}) ~{best_multiple}x on $1k." if best_ticker and best_multiple else ""
    lines.append("─" * 55)
    if best_str:
        lines.append(best_str)
    lines.append("Next full report: Tomorrow 6:00 AM PT.")
    lines.append("Intraday alerts active every hour during market hours.")
    lines.append("Risk only money you can light on fire.")

    subject = build_subject_line(active_plays, best_ticker, best_multiple)
    body    = "\n".join(lines)

    return subject, body


if __name__ == "__main__":
    # Quick smoke test
    # f-misc-09: removed the legacy ``sys.path.insert(0, str(BASE))``
    # — running this module via ``python -m biotech_sniper.email_formatter``
    # already places the package on ``sys.path``, so the mutation was
    # a no-op for the supported entry point and a side-effect for
    # everyone else.

    plays = json.load(open(BASE / "state/active_plays.json"))
    signals = json.load(open(BASE / "intelligence/unified_master_signals.json"))
    active = plays["active"]

    # Simulate with dummy cards
    dummy_cards = [
        (t, f"[CARD FOR {t}]", 2.50) for t in list(active.keys())[:3]
    ]

    subject, body = build_daily_email(
        active_plays=active,
        play_cards=dummy_cards,
        signals_json=signals,
        lifecycle_actions=[],
        best_ticker="IDYA",
        best_multiple=9.0,
    )

    print(f"SUBJECT: {subject}")
    print()
    print(body[:3000])
