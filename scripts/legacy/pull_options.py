#!/usr/bin/env python3
"""Pull live options chain data for biotech catalyst plays."""

import yfinance as yf
from datetime import datetime
import sys

from biotech_sniper.paths import BASE_DIR

output_lines = []

def log(msg=""):
    print(msg)
    output_lines.append(msg)

log(f"Options Chain Pull — {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
log("=" * 80)

tickers = {
    "IDYA": {
        "expiry": "2026-05-15",
        "strikes": [40, 42, 45, 47, 50],
        "type": "calls",
        "note": "Announcement est. late Apr/early May (UNCERTAIN). Stock ~$31.45"
    },
    "TVTX": {
        "expiry": "2026-04-17",
        "strikes": [32.5, 35, 37.5],
        "type": "calls",
        "note": "PDUFA Apr 13 (CERTAIN), 4-day buffer. Stock ~$27.66"
    },
    "AXSM": {
        "expiry": "2026-05-15",
        "strikes": [195, 200, 210, 220, 230],
        "type": "calls",
        "note": "PDUFA Apr 30 (CERTAIN), 16-day buffer. Stock ~$160.50"
    },
    "RGNX_Jul": {
        "ticker": "RGNX",
        "expiry": "2026-07-17",
        "strikes": [10, 12.5],
        "type": "calls",
        "note": "UNCERTAIN early Q2, conservative buffer. Stock ~$7.77"
    },
    "RGNX_May": {
        "ticker": "RGNX",
        "expiry": "2026-05-15",
        "strikes": [10],
        "type": "calls",
        "note": "RGNX May check for $10C"
    },
}

# First, check available expiry dates for each underlying ticker
unique_tickers = set(config.get("ticker", name.split("_")[0]) for name, config in tickers.items())
for sym in sorted(unique_tickers):
    t = yf.Ticker(sym)
    try:
        expiries = t.options
        log(f"\n{sym} available expiries: {', '.join(expiries[:15])}{'...' if len(expiries) > 15 else ''}")
    except Exception as e:
        log(f"{sym} — could not fetch expiries: {e}")

log("\n" + "=" * 80)

for name, config in tickers.items():
    ticker_sym = config.get("ticker", name.split("_")[0])
    target_expiry = config["expiry"]
    
    log(f"\n{'='*60}")
    log(f"  {name} — {ticker_sym} — Target expiry: {target_expiry}")
    log(f"  {config['note']}")
    log(f"{'='*60}")
    
    t = yf.Ticker(ticker_sym)
    
    try:
        # Check available expiries to find best match
        available = t.options
        
        # Try exact match first, then find closest
        if target_expiry in available:
            use_expiry = target_expiry
        else:
            # Find closest expiry on or after target
            target_dt = datetime.strptime(target_expiry, "%Y-%m-%d")
            candidates = []
            for exp in available:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d")
                if abs((exp_dt - target_dt).days) <= 5:  # within 5 days
                    candidates.append((abs((exp_dt - target_dt).days), exp))
            if candidates:
                candidates.sort()
                use_expiry = candidates[0][1]
                log(f"  ** Exact expiry {target_expiry} not found, using closest: {use_expiry}")
            else:
                log(f"  ** ERROR: No expiry within 5 days of {target_expiry}. Available: {available[:10]}")
                continue
        
        chain = t.option_chain(use_expiry)
        df = chain.calls if config["type"] == "calls" else chain.puts
        
        # Filter to requested strikes
        df_filtered = df[df["strike"].isin(config["strikes"])]
        
        if df_filtered.empty:
            log(f"  ** No matching strikes found. Available strikes near target range:")
            target_min = min(config["strikes"]) * 0.7
            target_max = max(config["strikes"]) * 1.3
            nearby = df[(df["strike"] >= target_min) & (df["strike"] <= target_max)]
            log(f"     {sorted(nearby['strike'].tolist())}")
            continue
        
        log(f"  {'Strike':>8}  {'Expiry':>12}  {'Bid':>8}  {'Ask':>8}  {'Mid':>8}  {'Last':>8}  {'IV%':>8}  {'OI':>8}  {'Vol':>8}")
        log(f"  {'-'*8}  {'-'*12}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
        
        for _, row in df_filtered.sort_values("strike").iterrows():
            bid = row.get("bid", 0) or 0
            ask = row.get("ask", 0) or 0
            mid = (bid + ask) / 2
            last = row.get("lastPrice", 0) or 0
            iv = (row.get("impliedVolatility", 0) or 0) * 100
            oi = int(row.get("openInterest", 0) or 0)
            vol = row.get("volume", 0)
            vol_str = str(int(vol)) if vol and vol == vol else "—"
            
            log(f"  ${row.strike:>7.1f}  {use_expiry:>12}  ${bid:>6.2f}  ${ask:>6.2f}  ${mid:>6.2f}  ${last:>6.2f}  {iv:>6.1f}%  {oi:>7d}  {vol_str:>8}")
        
        # Check for missing strikes
        found_strikes = set(df_filtered["strike"].tolist())
        missing = set(config["strikes"]) - found_strikes
        if missing:
            log(f"\n  ** Missing strikes: {sorted(missing)}")
            
    except Exception as e:
        log(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()

log(f"\n{'='*80}")
log(f"Pull complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# Save to file
outpath = str(BASE_DIR / "options_chains/batch_a_fresh.txt")
with open(outpath, "w") as f:
    f.write("\n".join(output_lines))
print(f"\nSaved to {outpath}")
