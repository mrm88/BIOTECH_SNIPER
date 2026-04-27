#!/usr/bin/env python3
"""
PERFORMANCE TRACKER
Runs every morning as part of the daily cron (before email).

WHAT IT DOES:
  1. For every active play, snapshots: stock price, option mid, option IV,
     days to expiry, current P&L vs entry fill.
  2. Detects if a catalyst has fired (large price move overnight).
  3. Writes a daily snapshot to state/performance_ledger.json.
  4. Feeds auto_resolver.py which records final outcomes.

DATA MODEL (state/performance_ledger.json):
  {
    "plays": {
      "RVMD": {
        "ticker": "RVMD",
        "direction": "LONG_CALLS",
        "p_success": 90,
        "science_grade": "C",
        "catalyst_type": "READOUT",
        "strike": 125,
        "expiry": "2026-06-18",
        "entry_date": "2026-03-30",
        "entry_stock": 96.50,
        "entry_fill": 3.20,         # option premium paid
        "snapshots": [
          {
            "date": "2026-04-13",
            "stock": 136.30,
            "option_mid": 14.80,
            "iv_pct": 95.0,
            "days_to_exp": 66,
            "pnl_pct": 362.5,       # (14.80 - 3.20) / 3.20 * 100
            "pnl_1k": 3625,         # on $1k invested
            "stock_move_from_entry": 41.2,
          }
        ],
        "status": "ACTIVE",         # ACTIVE | RESOLVED | EXPIRED
        "resolved": null,
      }
    },
    "last_updated": "2026-04-26",
    "total_plays_tracked": 12,
  }
"""

import json
import datetime
import sys
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
LEDGER_FILE = BASE_DIR / "state/performance_ledger.json"
ACTIVE_PLAYS_FILE = BASE_DIR / "state/active_plays.json"
RESOLVED_PLAYS_FILE = BASE_DIR / "state/resolved_plays.json"


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"plays": {}, "last_updated": None, "total_plays_tracked": 0}


def save_ledger(ledger: dict):
    LEDGER_FILE.parent.mkdir(exist_ok=True)
    ledger["last_updated"] = datetime.date.today().isoformat()
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def load_active_plays() -> dict:
    if ACTIVE_PLAYS_FILE.exists():
        with open(ACTIVE_PLAYS_FILE) as f:
            d = json.load(f)
        return d.get("active", {})
    return {}


def fetch_live_data(ticker: str, strike: float, expiry: str, opt_type: str) -> dict:
    """Fetch option mid + IV from the Alpaca options-chain endpoint.

    M3 update (f-m3-02): the legacy vendor-backed price/chain pull is
    replaced by
    :func:`biotech_sniper.options_chains.pull_options.pull_chain`, which
    sources data from Alpaca. The underlying stock price is no longer
    fetched here — downstream callers fall back to None when missing
    and the M3 paper-executor will compute mid directly from the
    chain row's bid/ask.
    """
    try:
        from biotech_sniper.options_chains.pull_options import pull_chain

        stock_price = None
        option_mid = None
        iv_pct = None

        if strike and expiry and opt_type:
            try:
                target_type = "call" if opt_type == "C" else "put"
                chain = pull_chain(ticker, expiry)
                rows = [
                    r for r in chain
                    if (r.get("type") or "").lower() == target_type
                ]
                if rows:
                    try:
                        target_strike = float(strike)
                    except (TypeError, ValueError):
                        target_strike = None
                    if target_strike is not None:
                        rows.sort(
                            key=lambda r: abs(
                                (r.get("strike") or 0.0) - target_strike
                            )
                        )
                        row = rows[0]
                        bid = row.get("bid") or 0
                        ask = row.get("ask") or 0
                        if (bid + ask) > 0:
                            option_mid = round((bid + ask) / 2, 2)
                        iv_raw = row.get("iv")
                        if iv_raw is not None:
                            iv_pct = round(float(iv_raw) * 100, 1)
            except Exception:
                # Option chain may not exist for this expiry; fall through
                # with None values, mirroring the legacy behaviour.
                pass

        return {"stock": stock_price, "option_mid": option_mid, "iv_pct": iv_pct}

    except Exception as e:
        print(f"  [tracker] {ticker} data fetch failed: {e}")
        return {"stock": None, "option_mid": None, "iv_pct": None}


def record_entry(ledger: dict, ticker: str, play: dict, live_data: dict) -> dict:
    """Create initial ledger entry for a new play."""
    today = datetime.date.today().isoformat()
    raw_strike = play.get("option_strike")
    try:
        strike = float(str(raw_strike).split("/")[0]) if raw_strike else None
    except:
        strike = None

    entry = {
        "ticker": ticker,
        "direction": play.get("direction", ""),
        "p_success": play.get("p_success", 0),
        "science_grade": play.get("science_grade", ""),
        "catalyst_type": play.get("catalyst_type", ""),
        "sector": play.get("sector", "BIOTECH"),
        "drug": play.get("drug_or_topic", play.get("drug", "")),
        "indication": play.get("indication", ""),
        "strike": strike,
        "expiry": play.get("option_expiry", ""),
        "opt_type": play.get("option_type", "C"),
        "estimated_announcement": play.get("estimated_announcement", ""),
        "pdufa_date": play.get("pdufa_date"),
        "entry_date": play.get("added_date", today),
        "entry_stock": live_data.get("stock"),
        "entry_fill": live_data.get("option_mid"),
        "entry_iv_pct": live_data.get("iv_pct"),
        "snapshots": [],
        "status": "ACTIVE",
        "resolved": None,
    }
    return entry


def take_snapshot(ledger_entry: dict, live_data: dict, today: str) -> dict:
    """Add a daily price/pnl snapshot to an existing ledger entry."""
    stock = live_data.get("stock")
    option_mid = live_data.get("option_mid")
    iv_pct = live_data.get("iv_pct")

    entry_fill = ledger_entry.get("entry_fill")
    entry_stock = ledger_entry.get("entry_stock")

    # P&L calculation
    pnl_pct = None
    pnl_1k = None
    if option_mid and entry_fill and entry_fill > 0:
        pnl_pct = round((option_mid - entry_fill) / entry_fill * 100, 1)
        contracts_per_1k = max(1, int(1000 / (entry_fill * 100)))
        invested = contracts_per_1k * entry_fill * 100
        current_val = contracts_per_1k * option_mid * 100
        pnl_1k = round(current_val - invested)
    elif ledger_entry.get("direction") == "EQUITY_ONLY" and stock and entry_stock:
        pnl_pct = round((stock - entry_stock) / entry_stock * 100, 1)
        shares = int(1000 / entry_stock)
        pnl_1k = round(shares * (stock - entry_stock))

    stock_move = None
    if stock and entry_stock:
        stock_move = round((stock - entry_stock) / entry_stock * 100, 1)

    # Days to expiry
    days_to_exp = None
    expiry = ledger_entry.get("expiry")
    if expiry:
        try:
            days_to_exp = (datetime.date.fromisoformat(expiry) - datetime.date.today()).days
        except:
            pass

    # Large move detection (catalyst fired?)
    catalyst_signal = None
    if stock_move is not None:
        if stock_move > 20:
            catalyst_signal = f"LARGE_MOVE_UP: stock +{stock_move:.1f}% — catalyst may have fired"
        elif stock_move < -20:
            catalyst_signal = f"LARGE_MOVE_DOWN: stock {stock_move:.1f}% — catalyst may have fired (negative)"
        elif stock_move > 10:
            catalyst_signal = f"MODERATE_MOVE_UP: stock +{stock_move:.1f}%"
        elif stock_move < -10:
            catalyst_signal = f"MODERATE_MOVE_DOWN: stock {stock_move:.1f}%"

    snapshot = {
        "date": today,
        "stock": stock,
        "option_mid": option_mid,
        "iv_pct": iv_pct,
        "days_to_exp": days_to_exp,
        "pnl_pct": pnl_pct,
        "pnl_1k": pnl_1k,
        "stock_move_from_entry": stock_move,
        "catalyst_signal": catalyst_signal,
    }
    return snapshot


def run_tracker() -> dict:
    """
    Main entry point. Called daily before email generation.
    Returns summary dict for inclusion in email.
    """
    today = datetime.date.today().isoformat()
    ledger = load_ledger()
    active = load_active_plays()

    print(f"\n[performance_tracker] Running — {today}")
    print(f"  Active plays: {len(active)} | Ledger entries: {len(ledger['plays'])}")

    results = {
        "date": today,
        "new_entries": [],
        "updated": [],
        "catalyst_signals": [],
        "pnl_summary": [],
        "errors": [],
    }

    for ticker, play in active.items():
        direction = play.get("direction", "")
        raw_strike = play.get("option_strike")
        try:
            strike = float(str(raw_strike).split("/")[0]) if raw_strike else None
        except:
            strike = None
        expiry = play.get("option_expiry", "")
        opt_type = play.get("option_type", "C")

        # Fetch live data
        live = fetch_live_data(ticker, strike, expiry, opt_type)

        if ticker not in ledger["plays"]:
            # New play — create entry
            entry = record_entry(ledger, ticker, play, live)
            ledger["plays"][ticker] = entry
            results["new_entries"].append(ticker)
            print(f"  NEW ENTRY: {ticker} | stock={live.get('stock')} | option_mid={live.get('option_mid')}")

        # Add daily snapshot
        entry = ledger["plays"][ticker]
        snapshot = take_snapshot(entry, live, today)

        # Avoid duplicate snapshots for same date
        existing_dates = {s["date"] for s in entry.get("snapshots", [])}
        if today not in existing_dates:
            entry.setdefault("snapshots", []).append(snapshot)
            results["updated"].append(ticker)

        # Catalyst signals
        if snapshot.get("catalyst_signal"):
            results["catalyst_signals"].append({
                "ticker": ticker,
                "signal": snapshot["catalyst_signal"],
                "pnl_pct": snapshot.get("pnl_pct"),
            })
            print(f"  CATALYST SIGNAL: {ticker} — {snapshot['catalyst_signal']}")

        # P&L summary
        results["pnl_summary"].append({
            "ticker": ticker,
            "pnl_pct": snapshot.get("pnl_pct"),
            "pnl_1k": snapshot.get("pnl_1k"),
            "stock_move": snapshot.get("stock_move_from_entry"),
            "days_to_exp": snapshot.get("days_to_exp"),
            "iv_pct": snapshot.get("iv_pct"),
        })

        ledger["plays"][ticker] = entry

    ledger["total_plays_tracked"] = len(ledger["plays"])
    save_ledger(ledger)
    print(f"  Ledger saved: {len(ledger['plays'])} total tracked plays")

    return results


def format_pnl_table_for_email(tracker_results: dict) -> str:
    """Format P&L table for daily email. Called by email_formatter."""
    lines = []
    lines.append("─" * 65)
    lines.append("LIVE P&L TRACKER")
    lines.append("─" * 65)
    lines.append(f"  {'TICKER':<7} {'P&L%':>7}  {'$1k PNL':>8}  {'STOCK':>8}  {'IV%':>6}  {'DTE':>4}  {'SIGNAL'}")
    lines.append(f"  {'──────':<7} {'────':>7}  {'───────':>8}  {'─────':>8}  {'───':>6}  {'───':>4}")

    pnl_data = sorted(
        tracker_results.get("pnl_summary", []),
        key=lambda x: (x.get("pnl_1k") or 0),
        reverse=True
    )

    total_pnl = 0
    for row in pnl_data:
        ticker = row["ticker"]
        pnl_pct = row.get("pnl_pct")
        pnl_1k = row.get("pnl_1k")
        stock_move = row.get("stock_move")
        iv_pct = row.get("iv_pct")
        dte = row.get("days_to_exp")
        signal = row.get("signal", "")

        pnl_pct_str = f"{pnl_pct:+.0f}%" if pnl_pct is not None else "N/A"
        pnl_1k_str = f"${pnl_1k:+,.0f}" if pnl_1k is not None else "N/A"
        stock_str = f"{stock_move:+.1f}%" if stock_move is not None else "N/A"
        iv_str = f"{iv_pct:.0f}%" if iv_pct is not None else "N/A"
        dte_str = str(dte) if dte is not None else "N/A"

        if pnl_1k:
            total_pnl += pnl_1k

        lines.append(f"  {ticker:<7} {pnl_pct_str:>7}  {pnl_1k_str:>8}  {stock_str:>8}  {iv_str:>6}  {dte_str:>4}")

    lines.append(f"  {'─'*55}")
    lines.append(f"  TOTAL (if $1k per play): ${total_pnl:+,.0f}")

    catalyst_signals = tracker_results.get("catalyst_signals", [])
    if catalyst_signals:
        lines.append("")
        lines.append("CATALYST SIGNALS DETECTED:")
        for cs in catalyst_signals:
            lines.append(f"  {cs['ticker']}: {cs['signal']}")

    return "\n".join(lines)


if __name__ == "__main__":
    results = run_tracker()
    print()
    print(format_pnl_table_for_email(results))
