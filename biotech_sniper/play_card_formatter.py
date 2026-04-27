#!/usr/bin/env python3
"""
PLAY CARD FORMATTER — ALL DYNAMIC, NOTHING HARDCODED
Every card is generated fresh at runtime by passing live data to Claude/Gemini.
The formatter builds the structured prompt — the cron agent calls the model.

No hardcoded strings. No static edge lines. No cached why-lines.
Every field computed from: live price + live options chain + latest scoring data.
"""

import json, re, datetime
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE


# ── MULTIPLIER CALCULATOR ────────────────────────────────────────────────────

def calculate_multiple(option_fill: float, option_strike: float, option_type: str,
                        stock_price: float, success_move_pct: float, 
                        total_risk: float = 1000) -> dict:
    """
    Calculate realistic $1k multiple from live fill + expected move.
    All math, no hardcoding.
    """
    if not all([option_fill, option_strike, stock_price, success_move_pct]):
        return {"multiple": None, "contracts": None, "invested": None,
                "option_value_on_success": None, "break_even_pct": None}

    contracts = max(1, int(total_risk / (option_fill * 100)))
    invested = contracts * option_fill * 100

    # Stock price on success
    if option_type == "C":
        new_stock = stock_price * (1 + success_move_pct / 100)
        intrinsic = max(0, new_stock - option_strike)
        break_even_pct = ((option_strike + option_fill - stock_price) / stock_price) * 100
    else:  # P
        new_stock = stock_price * (1 - success_move_pct / 100)
        intrinsic = max(0, option_strike - new_stock)
        break_even_pct = ((stock_price - option_strike + option_fill) / stock_price) * 100

    # Add ~15% time value estimate if expiry > 30 days out
    time_value_factor = 1.15
    option_value = intrinsic * time_value_factor

    total_return = contracts * option_value * 100
    multiple = round(total_return / invested, 1) if invested > 0 else 0

    return {
        "multiple": multiple,
        "contracts": contracts,
        "invested": round(invested),
        "option_value_on_success": round(option_value, 2),
        "break_even_pct": round(break_even_pct, 1),
        "total_return_on_success": round(total_return)
    }


def calculate_equity_multiple(stock_price: float, success_move_pct: float,
                               total_risk: float = 1000) -> dict:
    shares = int(total_risk / stock_price)
    invested = shares * stock_price
    new_price = stock_price * (1 + success_move_pct / 100)
    total_return = shares * new_price
    multiple = round(total_return / invested, 1)
    return {"multiple": multiple, "shares": shares, "invested": round(invested)}


# ── CARD PROMPT BUILDER ──────────────────────────────────────────────────────

def build_card_generation_prompt(play: dict, live_price: float, option_fill: float,
                                  option_oi: int, option_spread_quality: str,
                                  success_move_pct: float, failure_move_pct: float,
                                  mult_data: dict, latest_news: str = "") -> str:
    """
    Builds the prompt for Claude/Gemini to generate the play card text.
    The AI writes the 💡 and 🔑 lines fresh from current data — never from cache.
    """
    ticker = play.get("ticker", "?")
    sector = play.get("sector", "BIOTECH")
    p = play.get("p_success", 0)
    direction = play.get("direction", "")
    ann = play.get("estimated_announcement", "")
    certainty = play.get("announcement_certainty", "ESTIMATED")
    drug = play.get("drug_or_topic", "")
    indication = play.get("indication", "")
    notes = play.get("notes", "")
    strike = play.get("option_strike")
    expiry = play.get("option_expiry", "")
    opt_type = play.get("option_type", "C")

    multiple = mult_data.get("multiple", "??")
    contracts = mult_data.get("contracts", "??")
    invested = mult_data.get("invested", "??")
    be_pct = mult_data.get("break_even_pct", "??")

    sector_context = {
        "BIOTECH": "This is a binary clinical trial catalyst. The drug either hits the primary endpoint or misses.",
        "CONTRACT": "This is a government contract award. Either the company wins the contract or loses it to a competitor.",
        "ADCOM": "This is an FDA Advisory Committee vote. The panel votes yes or no on the drug. FDA follows ~75% of the time."
    }.get(sector, "Binary event.")

    prompt = f"""You are generating a tweet-style play card for a binary options trade. 
Write EXACTLY two lines — nothing more. Use plain language, no jargon.

PLAY DATA (use this, do not make up numbers):
Ticker: {ticker}
Sector: {sector}
Event: {drug}
Indication/Contract: {indication}
P(success/win/yes): {p}%
Direction: {direction}
Expected stock move on WIN: +{success_move_pct}%
Expected stock move on LOSS: -{abs(failure_move_pct)}%
$1k multiple on win: ~{multiple}x ({contracts} contracts at ${option_fill} fill)
Break-even move needed: +{be_pct}%
Live price: ${live_price}
Strike: ${strike}{opt_type} expiry {expiry}
OI: {option_oi} | Spread: {option_spread_quality}
Announcement: {ann} ({certainty})
Notes/context: {notes}
Latest news/context: {latest_news or 'None'}

{sector_context}

LINE 1 — 💡 WHY TRADE THIS (max 140 chars):
Explain in plain English why this is an attractive trade RIGHT NOW.
Use the actual data above — mention the probability, the multiple, the timing.
Do NOT say "the model predicts" or "AI says". Just state the fact.
Example: "96% trial success probability, ~9x on $1k calls, data dropping in ~15 days. Almost nothing scores this high with options available."

LINE 2 — 🔑 THE EDGE (max 140 chars):
What specific thing do most traders NOT know about this trade?
This should be the information asymmetry — why you have an edge.
Reference the actual notes/context above. Be specific.
Example: "Primary endpoint is a biomarker (expression ≥10%), not NSAA. 100% of Phase 1/2 patients cleared it. Street is treating this like a functional endpoint trial."

Output format — exactly two lines, no headers, no labels:
[why line]
[edge line]"""

    return prompt


# ── STATIC CARD ASSEMBLER ────────────────────────────────────────────────────

def assemble_card(play: dict, live_price: float, option_fill: float,
                  option_oi: int, option_spread_quality: str,
                  success_move_pct: float, failure_move_pct: float,
                  why_line: str, edge_line: str) -> str:
    """
    Assembles the final card from pre-computed components.
    Called after the AI has generated why_line and edge_line.
    Everything else is computed from live data — no static strings.
    """
    ticker = play.get("ticker", "?")
    sector = play.get("sector", "BIOTECH")
    p = play.get("p_success", 0)
    direction = play.get("direction", "")
    ann = play.get("estimated_announcement", "")
    certainty = play.get("announcement_certainty", "ESTIMATED")
    strike = play.get("option_strike")
    expiry = play.get("option_expiry", "")
    opt_type = play.get("option_type", "C")

    # Sector emoji
    sector_emoji = {"BIOTECH": "🧬", "CONTRACT": "🏛", "ADCOM": "⚖️"}.get(sector, "📊")

    # Direction display
    dir_emoji = {"LONG_CALLS": "📈", "LONG_PUTS": "📉", "CALL_SPREAD": "📊", "EQUITY_ONLY": "💵"}.get(direction, "")
    dir_label = {"LONG_CALLS": "LONG CALLS", "LONG_PUTS": "LONG PUTS",
                 "CALL_SPREAD": "CALL SPREAD", "EQUITY_ONLY": "EQUITY"}.get(direction, direction)

    # Days to announcement
    days_out = _estimate_days(play)
    days_str = f"~{days_out}d" if days_out < 999 else "~??d"
    cert_str = "✓" if certainty == "CERTAIN" else "~"

    # Compute multiple
    if direction == "EQUITY_ONLY":
        mult_data = calculate_equity_multiple(live_price, success_move_pct)
        multiple = mult_data["multiple"]
        contracts = mult_data["shares"]
        invested = mult_data["invested"]
        be_pct = "N/A"
    else:
        mult_data = calculate_multiple(option_fill, strike, opt_type,
                                        live_price, success_move_pct)
        multiple = mult_data["multiple"]
        contracts = mult_data["contracts"]
        invested = mult_data["invested"]
        be_pct = mult_data["break_even_pct"]

    # Move displays
    if direction == "LONG_PUTS":
        move_win = f"Stock drops -{abs(success_move_pct):.0f}%"
        move_lose = f"Stock rises +{abs(failure_move_pct):.0f}%"
        move_emoji_win = "📉"
        move_emoji_lose = "📈"
    else:
        move_win = f"+{success_move_pct:.0f}%"
        move_lose = f"-{abs(failure_move_pct):.0f}%"
        move_emoji_win = "📈"
        move_emoji_lose = "📉"

    mult_str = f"~{multiple}x on $1k" if multiple else "??x on $1k"

    # Option line
    spread_icon = {"TIGHT": "🟢", "MODERATE": "🟡", "WIDE": "🟠",
                   "UNTRADEABLE": "🔴", "NO BID": "🔴"}.get(option_spread_quality, "⚪")

    if direction == "EQUITY_ONLY":
        option_line = f"⚙️  Buy {contracts} shares @ ${live_price:.2f} = ${invested} | No options market"
    elif direction == "CALL_SPREAD":
        option_line = f"⚙️  ${strike} spread | {expiry} | ~${option_fill:.2f} debit | {spread_icon} {option_spread_quality}"
    else:
        option_line = (f"⚙️  ${strike}{opt_type} {expiry} @ ${option_fill:.2f} fill | "
                       f"{contracts}c = ${invested} | OI {option_oi} | {spread_icon} {option_spread_quality} | "
                       f"BE: +{be_pct}%")

    # Decoy orders (2 lines only in card — full ladder in email body)
    if direction not in ("EQUITY_ONLY", "CALL_SPREAD") and option_fill:
        decoy1 = f"BUY TO OPEN {ticker} {expiry} ${strike}{opt_type} | LIMIT ${option_fill:.2f} | 1c | GTC"
        decoy2 = f"BUY TO OPEN {ticker} {expiry} ${strike}{opt_type} | LIMIT ${option_fill-0.03:.2f} | 1c | GTC"
        orders_line = f"📋 {decoy1}\n   {decoy2}  [+ ladder — see full email]"
    else:
        orders_line = ""

    # Calibration warnings (IV crush, science grade, spread recommendation)
    from biotech_sniper.sectors.unified_scorer import (
        check_iv_crush_risk, apply_science_size_modifier, should_use_spread,
        detect_catalyst_type
    )
    warnings = []

    # IV crush check
    iv_pct = play.get("iv_pct")  # may be None if not pulled
    if iv_pct:
        iv_check = check_iv_crush_risk(iv_pct)
        if iv_check["crush_risk"]:
            warnings.append(f"RISK: {iv_check['warning']}")

    # Science grade size modifier check
    science_grade = play.get("science_grade", "")
    if science_grade:
        size_mod = apply_science_size_modifier(science_grade, direction, p)
        if size_mod.get("mismatch_alert"):
            warnings.append(f"MISMATCH ALERT: Grade {science_grade} science on LONG play. {size_mod.get('note', '')} HALF SIZE max.")
        elif size_mod.get("note") and size_mod["size_multiplier"] < 1.0:
            warnings.append(f"SIZE: {size_mod['note']}")

    # Spread recommendation
    catalyst_type = play.get("catalyst_type") or detect_catalyst_type(
        play.get("notes", ""), play.get("drug_or_topic", ""), play.get("indication", ""), sector
    )
    spread_rec = should_use_spread(p, catalyst_type, science_grade or "C")
    if spread_rec["use_spread"] and direction == "LONG_CALLS":
        warnings.append(f"SPREAD RECOMMENDED: {spread_rec['reason']}")

    # Assemble
    lines = [
        f"{sector_emoji} {ticker} [{sector}] | {days_str} | P={p}% | {dir_label} {dir_emoji}",
        f"{move_emoji_win} Win: {move_win} -> {mult_str}  |  {move_emoji_lose} Fail: {move_lose}",
        f"WHY: {why_line}",
        f"EDGE: {edge_line}",
        option_line,
    ]
    if orders_line:
        lines.append(orders_line)
    for w in warnings:
        lines.append(f"*** {w}")
    lines.append(f"EST: {ann}" if certainty != "CERTAIN" else f"CERTAIN: {ann}")

    return "\n".join(lines)


def _estimate_days(play: dict) -> int:
    today = datetime.date.today()
    pdufa = play.get("pdufa_date")
    if pdufa:
        try: return max(0, (datetime.date.fromisoformat(pdufa) - today).days)
        except: pass
    est = play.get("estimated_announcement", "").lower()
    q = re.search(r'q([1-4])\s*(20\d\d)', est)
    if q:
        m = {1:2,2:5,3:8,4:11}[int(q.group(1))]
        try: return max(0,(datetime.date(int(q.group(2)),m,15)-today).days)
        except: pass
    h = re.search(r'h([12])\s*(20\d\d)', est)
    if h:
        m = 5 if int(h.group(1))==1 else 11
        try: return max(0,(datetime.date(int(h.group(2)),m,15)-today).days)
        except: pass
    mid = re.search(r'(?:mid|early|late)\s*(20\d\d)', est)
    if mid and not any(x in est for x in ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]):
        try: return max(0,(datetime.date(int(mid.group(1)),6,15)-today).days)
        except: pass
    mmap = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,
            "september":9,"october":10,"november":11,"december":12,
            "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    found = [mmap[re.sub(r'[^a-z]','',w)] for w in re.split(r'[\s,/\-]+',est) if re.sub(r'[^a-z]','',w) in mmap]
    if found:
        yr = re.search(r'20\d\d', est)
        year = int(yr.group()) if yr else today.year
        day = 10 if "early" in est else (25 if "late" in est else 15)
        try:
            t = datetime.date(year, min(found), day)
            if t < today: t = datetime.date(year, max(found), day)
            return max(0,(t-today).days)
        except: pass
    iso = re.search(r'(20\d\d-\d{2}-\d{2})', play.get("estimated_announcement",""))
    if iso:
        try: return max(0,(datetime.date.fromisoformat(iso.group(1))-today).days)
        except: pass
    return 999


# ── REMOVAL ALERT FORMATTER ──────────────────────────────────────────────────

def format_removal_alert(ticker: str, reason: str, sector: str = "BIOTECH",
                          detail: str = "") -> str:
    """Tweet-style removal alert. Detail is fresh text from the trigger."""
    emoji = {"BIOTECH":"🧬","CONTRACT":"🏛","ADCOM":"⚖️"}.get(sector,"📊")
    icons = {
        "PDUFA_PASSED": "✅ RESOLVED — PDUFA passed",
        "TOPLINE_8K": "🔴 DATA DROPPED — 8-K filed",
        "AWARD_POSTED": "🏆 CONTRACT AWARDED",
        "ADCOM_VOTED": "⚖️ ADCOM VOTED",
        "WINDOW_EXPIRED": "⏰ WINDOW EXPIRED",
        "PROB_DROPPED": "📉 PROBABILITY DOWNGRADED",
        "TRIAL_STOPPED": "🚨 TRIAL SUSPENDED/CANCELLED",
        "OPTIONS_DEAD": "💧 OPTIONS ILLIQUID",
    }
    label = icons.get(reason, f"🗑 REMOVED — {reason}")
    return f"{emoji} {ticker} — {label}\n{detail}\nAction: Removed from active plays. Check for open positions."


# ── NEW PLAY ALERT FORMATTER ─────────────────────────────────────────────────

def format_new_play_alert(card_text: str, sector: str) -> str:
    """Wrap a generated play card as a new play alert."""
    emoji = {"BIOTECH":"🧬","CONTRACT":"🏛","ADCOM":"⚖️"}.get(sector,"📊")
    return f"🆕 NEW {emoji} {sector} PLAY DISCOVERED\n{'─'*55}\n{card_text}"


# ── CRON INSTRUCTIONS ────────────────────────────────────────────────────────

CARD_GENERATION_INSTRUCTIONS = """
HOW TO GENERATE PLAY CARDS IN THE CRON:

For each qualifying play, generate the card as follows:

STEP A — Gather live data:
  1. Pull live stock price via finance_quotes tool
  2. Pull live options chain via Yahoo Finance for correct expiry/strike
  3. Get bid/ask/mid/IV/OI/volume
  4. Calculate realistic fill = mid (TIGHT/MODERATE) or mid-$0.10 (WIDE)
  5. Calculate $1k multiple using calculate_multiple() from play_card_formatter.py

STEP B — Generate why_line and edge_line via Claude or Gemini:
  1. Call build_card_generation_prompt() with all live data
  2. Run through claude_opus_4_6 or gemini_3_1_pro  
  3. Extract the two output lines (why + edge)
  4. These must reflect LATEST NEWS — before calling the model, search:
     "[TICKER] news today" and "[TICKER] [drug/contract] update [current month]"
     Pass the top 2-3 results as latest_news parameter

STEP C — Assemble the card:
  1. Call assemble_card() with all data + AI-generated lines
  2. Include in email under the play's section

CRITICAL: why_line and edge_line must NEVER be hardcoded or cached.
They are regenerated fresh every single run using latest data + news.
The calculate_multiple() function handles all math automatically.
"""

if __name__ == "__main__":
    # Demo: show what the card looks like when AI lines are provided externally
    import sys
    sys.path.insert(0, str(BASE))

    plays = json.load(open(BASE / "state/active_plays.json"))

    print("PLAY CARD FORMATTER — DYNAMIC VERSION")
    print("Cards are assembled from live data + AI-generated lines")
    print("="*65)

    # Simulate what the cron would do — AI lines provided as if freshly generated
    test_data = [
        ("IDYA",  31.45, 2.50, 74,   "WIDE",     60.0, 65.0,
         "96% AI ensemble says this hits — DB lock was 1H April, data is days away. Best biotech probability score active right now.",
         "DB lock confirmed March 22 PR. Webcast pre-reg not opened yet — the day it does, announcement date is CERTAIN. Watch ir.ideayabio.com/events daily."),

        ("TVTX",  27.66, 2.73, 7607, "TIGHT",    50.0, 50.0,
         "PDUFA April 13 — 77% win probability, ~3x on $1k, tightest options spread of all active plays. Clean binary in 13 days.",
         "Stock was $34 before January delay — fell on CMC extension, not efficacy concern. Clean Phase 3 DUPLEX data intact. Street re-rated wrong event."),

        ("NTLA",  11.65, 1.65, 1734, "TIGHT",    75.0, 50.0,
         "82% probability, ~5x on $1k, first-ever in vivo CRISPR Phase 3 — if this works it's a landmark. Phase 1/2 showed 95% attack reduction in NEJM.",
         "Primary endpoint is attack rate reduction — HAE patients average 3-12 attacks/year. Phase 3 has 60 patients and is likely overpowered given how large the Ph1/2 effect was."),

        ("AGIO",  29.61, 1.25, 98,   "NO BID",   60.0, 30.0,
         "6% success probability — this trial is almost certainly failing. Puts pay 5.8x on $1k if stock drops 60%. Patient limit entry needed (no bid).",
         "8-week transfusion independence in LR-MDS is an unrealistic endpoint — takes months of treatment. Warpspeed independently scores this at 6%. Street is pricing 50/50."),

        ("VRDN",  18.53, 1.78, 856,  "MODERATE", 80.0, 40.0,
         "Stock crashed 32% on REVEAL-1 which ACTUALLY SUCCEEDED — now trades at cash value with REVEAL-2 coming Q2. Asymmetry score 8/10.",
         "REVEAL-1 beat primary with p<0.0001 but market expected 65% PRR, got 54%. Stock at $875M cash / 51M shares = $17/share cash floor. REVEAL-2 still incoming."),

        # Contract example
        ("RKLB",  21.40, 1.85, 2840, "MODERATE", 45.0, 20.0,
         "J&A sole-source on SAM.gov = government already picked Rocket Lab, just filing paperwork. 87% historical win rate on J&A filings. ~8x on $1k calls.",
         "SAM.gov J&A filed March 28 names Neutron as only vehicle meeting sub-24hr reusable turnaround spec. Zero analyst coverage of SAM.gov for equities — this is pure edge."),

        # AdCom example
        ("AXSM",  160.50, 3.62, 535, "MODERATE", 28.0, 32.0,
         "AdCom vote April 30, 78% yes probability. Two trades available: vote day (+28% stock) then PDUFA ~90d later. FDA briefing docs drop 48h before.",
         "REXULTI approved for exact same indication in 2023 — FDA already validated the endpoint. AUVELITY already approved as AXSM's own drug for MDD. Path is proven."),
    ]

    for row in test_data:
        ticker, price, fill, oi, spread, win_mv, fail_mv, why, edge = row
        if ticker not in plays["active"]:
            # Create minimal play object for demo
            play = {"ticker": ticker, "sector": "CONTRACT" if ticker in ("RKLB","KTOS") else "ADCOM" if ticker in ("AXSM_ADC",) else "BIOTECH",
                    "p_success": 80, "direction": "LONG_CALLS", "estimated_announcement": "May 2026",
                    "announcement_certainty": "ESTIMATED", "option_strike": 25, "option_expiry": "2026-07-17", "option_type": "C",
                    "drug_or_topic": "Demo", "indication": "Demo", "notes": "Demo"}
            if ticker == "RKLB":
                play.update({"sector":"CONTRACT","p_success":87,"option_strike":25,"option_expiry":"2026-07-17","estimated_announcement":"May - June 2026"})
            elif ticker == "AXSM":
                play.update({"sector":"ADCOM","p_success":78,"option_strike":185,"option_expiry":"2026-05-16","pdufa_date":"2026-04-30","announcement_certainty":"CERTAIN","estimated_announcement":"April 30 2026"})
        else:
            play = plays["active"][ticker]

        card = assemble_card(play, price, fill, oi, spread, win_mv, fail_mv, why, edge)
        print(f"\n{card}")
        print("─"*65)
