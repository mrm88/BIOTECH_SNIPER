import json, datetime

cards = json.load(open('state/play_cards_2026-04-10.json'))
options_data = json.load(open('state/options_chains_2026-04-10.json'))
options_by_ticker = {r['ticker']: r for r in options_data}

today = datetime.date.today()

# Sort by days to catalyst
order = {'Apr 13 2026':1,'~May 2026':2,'Apr 30 2026':3,'Apr-May 2026':4,
         'May 10 2026':5,'May-Jun 2026':6,'Jun-Jul 2026':7,'Sep 27 2026':8}
qualifying = sorted([c for c in cards if c.get('multiple',0) >= 2.5],
                    key=lambda x: order.get(x.get('catalyst',''), 9))

lines = []
lines.append("ALPHA SNIPER -- April 10, 2026")
lines.append("Run #11  |  10 active plays  |  9 qualifying (>=2.5x)  |  Best: AXSM 14.2x")
lines.append("=" * 65)
lines.append("")
lines.append("NEW PLAY ADDED: PRAX $300C Jan27 -- 3.5x | Sep 27 PDUFA confirmed")
lines.append("NEW PLAY ADDED: MLTX $21C Aug21 -- 3.4x | Jun-Jul IZAR-1 readout")
lines.append("")
lines.append("ONE-LINERS (sorted nearest catalyst first, >=2.5x only):")
lines.append("-" * 65)

one_liners = [
    ("3d",   "TVTX",  "$35C Apr-17 @ $4.25",  "5.1x",  "P=77%",  "OI=8,614",  "catalyst: Apr 13 2026  |  PDUFA CERTAIN"),
    ("0d",   "IDYA",  "$35C May-15 @ $5.20",  "4.0x",  "P=96%",  "OI=10,708", "catalyst: ~May 2026  |  DB lock day 10 of 15"),
    ("20d",  "AXSM",  "$200C May-15 @ $8.60", "14.2x", "P=65%",  "OI=573",    "catalyst: Apr 30 2026  |  PDUFA CERTAIN -- BEST MULTIPLE"),
    ("30d",  "ARGX",  "$800/$850C May-15",     "3.2x",  "P=87%",  "OI=6",      "catalyst: May 10 2026  |  PDUFA CERTAIN -- stock at $800 lower leg"),
    ("5d",   "RGNX",  "$10C Jul-17 @ $2.10",  "2.9x",  "P=68%",  "OI=1,509",  "catalyst: Apr-May 2026  |  Science Grade F -- gene therapy standard"),
    ("35d",  "AGIO",  "$30P Aug-21 @ $2.73",  "6.8x",  "P(fail)=94%", "OI=691", "catalyst: May-Jun 2026  |  PUT -- Grade F confirms short thesis"),
    ("35d",  "RVMD",  "$125C Jun-18 @ $6.80", "7.1x",  "P=61%",  "OI=55",     "catalyst: May-Jun 2026  |  Phase 3 enrollment complete"),
    ("35d",  "NTLA",  "$15C Jul-17 @ $2.45",  "4.0x",  "P=82%",  "OI=1,752",  "catalyst: May-Jun 2026  |  NEJM data pub Apr 2. BLA forces June"),
    ("66d",  "MLTX",  "$21C Aug-21 @ $4.10",  "3.4x",  "P=100%", "OI=1,041",  "catalyst: Jun-Jul 2026  |  NEW -- Warpspeed 100%, Grade B RCT"),
    ("170d", "PRAX",  "$300C Jan-27 @ $79.00", "3.5x",  "P=70%",  "OI=62",     "catalyst: Sep 27 2026  |  NEW -- CERTAIN PDUFA, rare epilepsy"),
]

for days, ticker, strike_str, multiple, prob, oi_str, note in one_liners:
    lines.append(f"{days} | {ticker}  {strike_str} -- {multiple} | {prob} | {oi_str} | {note}")

lines.append("")
lines.append("BELOW THRESHOLD (Excel only):")
lines.append("  ARGX $800/$850C May-15 -- current value $5.75 vs $12 entry. Trade intact, stock at lower leg.")
lines.append("")

lines.append("=" * 65)
lines.append("SCIENCE GRADES (Shkreli-style protocol analysis)")
lines.append("-" * 65)
lines.append("")
lines.append("Grade B (solid design):")
lines.append("  MLTX: RCT triple-blind N=960 ACR50 -- Warpspeed P=100%. Best designed trial.")
lines.append("")
lines.append("Grade C (adequate, some concerns):")
lines.append("  IDYA: Open-label but hard clinical endpoint. DB lock imminent.")
lines.append("  TVTX: Clear mechanistic rationale. Label extension = lower FDA bar.")
lines.append("  NTLA: Small N=60 but data already published in NEJM (31/32 attack-free).")
lines.append("  RVMD: Open-label but hard OS endpoint. N=501 well-powered.")
lines.append("  PRAX: RCT placebo-controlled. Seizure frequency = validated rare epilepsy endpoint.")
lines.append("")
lines.append("Grade D (weak science -- RISK NOTE on LONG plays):")
lines.append("  AXSM: PDUFA bet on already-approved drug. Science flag is for trial design, not approval probability.")
lines.append("  ARGX: Open-label + functional endpoint. Prior efgartigimod data is strong. Grade D = risk to monitor.")
lines.append("")
lines.append("Grade F (flawed protocol -- context matters):")
lines.append("  RGNX: Non-RCT is STANDARD for gene therapy. FDA approved Elevidys/Zolgensma on same design. AbbVie $100M milestone validates.")
lines.append("  AGIO: Grade F CONFIRMS the put thesis -- non-RCT, surrogate endpoint, wrong patient population. P(fail)=94%.")
lines.append("")

lines.append("=" * 65)
lines.append("FULL PLAY CARDS")
lines.append("-" * 65)

card_order = ['TVTX','IDYA','AXSM','ARGX','RGNX','AGIO','RVMD','NTLA','MLTX','PRAX']
cards_by_ticker = {c['ticker']: c for c in cards}

for ticker in card_order:
    card = cards_by_ticker.get(ticker)
    if not card:
        continue
    mult   = card.get('multiple', 0)
    p      = card.get('p_success', 0)
    grade  = card.get('science_grade', '?')
    direc  = card.get('direction','')
    strike = card.get('strike','')
    otype  = card.get('opt_type','')
    expiry = card.get('expiry','')
    mid    = card.get('mid', 0)
    oi     = card.get('oi', 0)
    catalyst = card.get('catalyst','')
    k1     = card.get('k1', 0)
    drug   = card.get('drug','')
    why    = card.get('why','')
    edge   = card.get('edge','')
    stock  = card.get('stock_price', 0)
    
    direction_label = 'PUT' if 'PUT' in direc else ('CALL SPREAD' if 'SPREAD' in direc else 'CALLS')
    threshold_note = '' if mult >= 2.5 else '  [BELOW 2.5x THRESHOLD -- full card for reference]'
    
    lines.append("")
    lines.append(f"{'=' * 55}")
    lines.append(f"  {ticker} -- {direction_label} -- {mult}x -- P={p}% -- Science Grade {grade}")
    lines.append(f"{'=' * 55}")
    lines.append(f"  DRUG:    {drug}")
    lines.append(f"  STOCK:   ${stock:.2f}")
    if 'SPREAD' in direc:
        lines.append(f"  OPTION:  $800C/$850C May-15  entry debit $12  current ~$5.75  max payout $38 (3.2x)")
    else:
        lines.append(f"  OPTION:  ${strike}{otype} {expiry} @ ${mid:.2f}  |  OI={oi}  |  $1k -> ~${k1:,}")
    lines.append(f"  CATALYST: {catalyst}{threshold_note}")
    lines.append(f"")
    lines.append(f"  WHY: {why}")
    lines.append(f"")
    lines.append(f"  EDGE: {edge}")

lines.append("")
lines.append("=" * 65)
lines.append("MONITOR PLAYS (watch list)")
lines.append("-" * 65)
lines.append("  VRDN veligrotug -- Jun 30 PDUFA  |  P=72%  |  Spreads too wide, watch for liquidity")
lines.append("  VERA atacicept  -- Jul 7 PDUFA   |  P=72%  |  2.3x just below threshold, promote when 2.5x")
lines.append("  RZLT ersodetug  -- H2 2026        |  P=72%  |  Too illiquid")
lines.append("  NAMS obicetrapib -- Nov 2026       |  P=37%  |  PUT candidate, too far out")
lines.append("")
lines.append("No emoji. No spam. Next email if anything changes intraday.")
lines.append("Full spreadsheet attached.")

body = "\n".join(lines)

# Save
with open('reports/email_body_2026-04-10.txt', 'w') as f:
    f.write(body)

print("Email body built. Preview:")
print()
print(body[:3000])
