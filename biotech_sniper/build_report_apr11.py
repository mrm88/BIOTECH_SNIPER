#!/usr/bin/env python3
"""Build Excel report and email body for Alpha Sniper Run #12, April 11 2026."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
options_data = json.load(open(BASE / 'state/options_chains_2026-04-11.json'))
opts = {r['ticker']: r for r in options_data}
plays = json.load(open(BASE / 'state/active_plays.json'))
science = json.load(open(BASE / 'state/science_grades.json'))
sci = {}
for k, v in science.items():
    sci[k.split('_')[0]] = v

today = datetime.date.today().strftime('%B %d, %Y')

# ── COLORS ───────────────────────────────────────────────────────────────────
DARK   = '0D1117'; GBG  = '0D2818'; RBG  = '2D0A0A'; OBG  = '2D1A00'
HDR    = '161B22'; WHT  = 'FFFFFF'; GRN  = '3FB950'; RED  = 'F85149'
ORN    = 'D29922'; YLW  = 'E3B341'; GRY  = '8B949E'; BLU  = '58A6FF'
PRP    = 'BC8CFF'

def cs(cell, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    cell.fill = PatternFill(fill_type='solid', fgColor=bg)
    cell.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    cell.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for c, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True, sz=10)

wb = openpyxl.Workbook()

# ── TAB 1: Qualifying Plays ──────────────────────────────────────────────────
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today}  --  Run #12  --  8 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=13, al='center')
ws1.row_dimensions[1].height = 28

hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    # ticker, direction, strike, expiry, mid, mult, k1, p, grade, oi, catalyst, note
    ('IDYA',  'CALLS', '$35C',        '2026-05-15',  5.35,  3.7,  3700, 96, 'C', 10708, 'Apr 13 2026 CERTAIN',
     'DATE CONFIRMED. Topline OPTIMUM-02 Monday. IV=185%. OI=10,708. Enter before Monday open.'),
    ('TVTX',  'CALLS', '$35C',        '2026-04-17',  2.85,  6.0,  6000, 77, 'C',  8614, 'Apr 13 2026 CERTAIN',
     'PDUFA Monday. IV=305% (binary priced in). Stock at $28.96 vs $35 strike. SAME DATE AS IDYA.'),
    ('AXSM',  'CALLS', '$200C',       '2026-05-15',  8.05, 15.0, 15000, 65, 'D',   573, 'Apr 30 2026 CERTAIN',
     'BEST MULTIPLE 15x. Drug already approved (MDD). Label extension to AD agitation.'),
    ('AGIO',  'PUTS',  '$30P',        '2026-08-21',  2.82,  6.5,  6500,  6, 'F',   691, 'May-Jun 2026',
     'Grade F confirms put. P(fail)=94%. Non-RCT surrogate endpoint wrong patient population.'),
    ('RVMD',  'CALLS', '$125C',       '2026-06-18',  8.10,  6.0,  6000, 61, 'C',    55, 'May-Jun 2026',
     'First multi-RAS inhibitor. Phase 3 enrollment complete. OI=55, use limit orders.'),
    ('NTLA',  'CALLS', '$15C',        '2026-07-17',  2.25,  4.1,  4100, 82, 'C',  1752, 'May-Jun 2026',
     'NEJM data published Apr 2 (31/32 attack-free 3yr). BLA forces disclosure by June.'),
    ('PRAX',  'CALLS', '$300C',       '2027-01-15', 79.00,  3.4,  3400, 70, 'C',    62, 'Sep 27 2026 CERTAIN',
     'First Nav channel SCN2A/8A drug. Rare epilepsy. Confirmed PDUFA. Spread=6% OI=62.'),
    ('MLTX',  'CALLS', '$21C',        '2026-08-21',  4.20,  2.7,  2700,100, 'B',  1041, 'Jun-Jul 2026',
     'Warpspeed P=100%. Grade B best design. RCT triple-blind N=960. Small cap re-rating.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    rbg = RBG if 'PUT' in direc else GBG
    dcol = RED if 'PUT' in direc else GRN
    gcol = {' A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        c = ws1.cell(row=row, column=col, value=val)
        fg = (YLW if col==1 else dcol if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 42
    row += 1

for i, w in enumerate([8,10,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# ── TAB 2: All Active ────────────────────────────────────────────────────────
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:M1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today} -- 10 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=12, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIRECTION','P(WIN)','SCIENCE','STRIKE','EXPIRY',
               'MID','MULTIPLE','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('IDYA', 'Darovasertib+Crizotinib','CALLS', 96,'C','$35C',       '2026-05-15', 5.35,  3.7,10708,'Apr 13 CERTAIN',  2, 'DATE CONFIRMED. IV=185%. OI=10,708.'),
    ('TVTX', 'Sparsentan FILSPARI sNDA','CALLS',77,'C','$35C',       '2026-04-17', 2.85,  6.0, 8614,'Apr 13 CERTAIN',  2, 'PDUFA Monday. IV=305%. Stock $28.96 vs $35C.'),
    ('RGNX', 'RGX-202 gene therapy',   'CALLS', 68,'F','$10C',       '2026-07-17', 2.65,  2.2, 1509,'Apr-May 2026',    4, 'BELOW 2.5x. Grade F = non-RCT (standard for gene therapy). AbbVie $100M.'),
    ('AXSM', 'AXS-05 sNDA',            'CALLS', 65,'D','$200C',      '2026-05-15', 8.05, 15.0,  573,'Apr 30 CERTAIN', 19, 'BEST MULTIPLE 15x. Already-approved drug extension.'),
    ('ARGX', 'Efgartigimod $800/$850C','SPREAD',87,'D','$800/$850C', '2026-05-15',20.35,  1.5,    6,'May 10 CERTAIN', 29, 'BELOW THRESHOLD. Spread widened to $20.35 (was $12). Max payout $50 = 1.5x. Stock at $799.65.'),
    ('RVMD', 'RMC-6236 RAS inhibitor', 'CALLS', 61,'C','$125C',      '2026-06-18', 8.10,  6.0,   55,'May-Jun 2026',   34, 'Phase 3 enrollment complete. OI thin, use limits.'),
    ('AGIO', 'Tebapivat AG-946',       'PUTS',   6,'F','$30P',       '2026-08-21', 2.82,  6.5,  691,'May-Jun 2026',   34, 'Grade F confirms put. P(fail)=94%.'),
    ('NTLA', 'NTLA-2002 CRISPR HAE',  'CALLS', 82,'C','$15C',       '2026-07-17', 2.25,  4.1, 1752,'May-Jun 2026',   34, 'NEJM 3yr data published. BLA forces June disclosure.'),
    ('MLTX', 'Sonelokimab IZAR-1',    'CALLS',100,'B','$21C',       '2026-08-21', 4.20,  2.7, 1041,'Jun-Jul 2026',   65, 'Warpspeed 100%. Grade B. RCT triple-blind N=960.'),
    ('PRAX', 'Relutrigine NDA',       'CALLS', 70,'C','$300C',      '2027-01-15',79.00,  3.4,   62,'Sep 27 CERTAIN',169, 'Confirmed PDUFA. Rare epilepsy. Spread=6%.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker, drug, direc, p, grade, strike, expiry, mid, mult, oi, cat, days, notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    row_vals = [ticker, drug, direc, f'{p}%', f'Grade {grade}', strike, expiry,
                f'${mid:.2f}', f'{mult}x', oi, cat, f'{days}d', notes]
    for col, val in enumerate(row_vals, 1):
        c = ws2.cell(row=i, column=col, value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9 else WHT)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==13))
    ws2.row_dimensions[i].height = 36

for i, w in enumerate([8,28,10,8,10,12,12,9,9,7,14,7,55], 1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# ── TAB 3: ARGX Analysis ─────────────────────────────────────────────────────
ws3 = wb.create_sheet('ARGX Spread Analysis')
ws3.sheet_properties.tabColor = 'F85149'
ws3.merge_cells('A1:D1')
t3 = ws3['A1']
t3.value = 'ARGX $800/$850 Call Spread Analysis -- Apr 11, 2026'
cs(t3, bg=DARK, fg=RED, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['SCENARIO','STOCK PRICE AT MAY 15','SPREAD VALUE','P&L on $12 ENTRY'])

argx_rows = [
    ('Current (weekend)', '$799.65', '$29.65 (lower leg ITM)', 'Spread now worth ~$29.65 if exercised -- BUT $850C worthless. Net ~$0.35'),
    ('PDUFA miss / flat', '$800', '$0 (stock doesnt reach $850)', '-$12 (max loss)'),
    ('Partial move',      '$825', '$25 (of max $50)',              '+$13 = +108%'),
    ('Full approval move','$850+', '$50 (max payout)',              '+$38 = 3.2x on $12 entry'),
    ('Strong approval',   '$900+', '$50 (capped at $50)',           '+$38 = 3.2x (same)'),
]
notes_rows = [
    ('ISSUE', 'Spread debit widened from $12 to $20.35 (fresh entry). OI collapsed: $800C OI=6, $850C OI=3.', '', ''),
    ('STATUS', 'IF entered at $12: current theoretical value ~$20-25. Spread is technically profitable at stock $799.65.', '', ''),
    ('ACTION', 'Hold through May 10 PDUFA. Do not add. OI too thin to exit cleanly before PDUFA.', '', ''),
    ('NOTE', 'ARGX removed from qualifying list (fresh entry now 1.5x). Existing position hold.', '', ''),
]

for i, rd in enumerate(argx_rows+notes_rows, 3):
    for col, val in enumerate(rd, 1):
        c = ws3.cell(row=i, column=col, value=val)
        cs(c, bg=(OBG if i<=len(argx_rows)+2 else DARK), fg=(YLW if col==1 else WHT), wrap=True)
    ws3.row_dimensions[i].height = 36

for i, w in enumerate([18,22,30,30], 1):
    ws3.column_dimensions[get_column_letter(i)].width = w

# ── TAB 4: Monitor ───────────────────────────────────────────────────────────
ws4 = wb.create_sheet('Monitor')
ws4.sheet_properties.tabColor = 'D29922'
ws4.merge_cells('A1:F1')
t4 = ws4['A1']
t4.value = f'MONITOR PLAYS -- {today}'
cs(t4, bg=DARK, fg=ORN, bold=True, sz=12, al='center')
ws4.row_dimensions[1].height = 24
hrow(ws4, 2, ['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])

monitor = [
    ('VERA',  'Atacicept IgAN BLA',    'Jul 7 2026 PDUFA',   '72%',
     'Jan27 $55C @ $8.05 = 2.3x. Just below 2.5x. RCT double-blind N=376. Science Grade C.',
     'When multiple hits 2.5x'),
    ('VRDN',  'Veligrotug TED BLA',    'Jun 30 2026 PDUFA',  '72%',
     'Spreads $0 bid most strikes. Watch for liquidity to build in May.',
     'When spread tightens below 80%'),
    ('RZLT',  'Ersodetug upLIFT',      'H2 2026',            '72%',
     '16-patient trial. No bid on options.',
     'If liquidity builds'),
    ('NAMS',  'Obicetrapib PREVAIL',   'Nov 2026',           '37%',
     'PUT candidate. N=9,541 CV outcomes trial. Too far out.',
     'When within 90 days'),
    ('AMGN',  'Olpasiran OCEAN-a',     '2027',               '71%',
     'Mega cap. 2027 readout.',
     'Remove if still 2027 in 60 days'),
]

for i, rd in enumerate(monitor, 3):
    for col, val in enumerate(rd, 1):
        c = ws4.cell(row=i, column=col, value=val)
        cs(c, bg=DARK, fg=(YLW if col==1 else WHT), wrap=(col in [5,6]))
    ws4.row_dimensions[i].height = 36

for i, w in enumerate([8,28,18,7,60,26], 1):
    ws4.column_dimensions[get_column_letter(i)].width = w

# Save
path = BASE / 'reports/Alpha_Sniper_2026-04-11.xlsx'
wb.save(path)
print(f'Saved: {path}')

# ── EMAIL BODY ────────────────────────────────────────────────────────────────
lines = []
lines.append("ALPHA SNIPER -- April 11, 2026 (Saturday)")
lines.append("Run #12  |  10 active plays  |  8 qualifying (>=2.5x)  |  Best: AXSM 15.0x")
lines.append("=" * 65)
lines.append("")
lines.append("WEEKEND BRIEF: Two catalysts hit Monday April 13")
lines.append("")
lines.append("  IDYA: Topline OPTIMUM-02 confirmed Monday via PRN. $35C May15 @ $5.35 = 3.7x.")
lines.append("  TVTX: PDUFA also April 13. IV=305% (market fully pricing binary). $35C Apr17 @ $2.85 = 6.0x.")
lines.append("")
lines.append("ARGX UPDATE: Spread widened. Net debit now $20.35 (was $12 at entry).")
lines.append("  Max payout still $50. Fresh entry now 1.5x (below threshold).")
lines.append("  Existing position: hold through May 10 PDUFA. Do not add.")
lines.append("")
lines.append("ONE-LINERS (sorted nearest catalyst, >=2.5x):")
lines.append("-" * 65)

one_liners = [
    ("2d",   "IDYA", "$35C  May-15 @ $5.35",  "3.7x",  "P=96%",  "OI=10,708", "Apr 13 CONFIRMED  |  IV=185%  |  Grade C"),
    ("2d",   "TVTX", "$35C  Apr-17 @ $2.85",  "6.0x",  "P=77%",  "OI=8,614",  "Apr 13 PDUFA      |  IV=305%  |  Grade C"),
    ("19d",  "AXSM", "$200C May-15 @ $8.05",  "15.0x", "P=65%",  "OI=573",    "Apr 30 PDUFA      |  Grade D (PDUFA bet, not trial)"),
    ("34d",  "AGIO", "$30P  Aug-21 @ $2.82",  "6.5x",  "P(f)=94%","OI=691",   "May-Jun 2026      |  Grade F confirms puts"),
    ("34d",  "RVMD", "$125C Jun-18 @ $8.10",  "6.0x",  "P=61%",  "OI=55",     "May-Jun 2026      |  Phase 3 done"),
    ("34d",  "NTLA", "$15C  Jul-17 @ $2.25",  "4.1x",  "P=82%",  "OI=1,752",  "May-Jun 2026      |  NEJM data pub"),
    ("169d", "PRAX", "$300C Jan-27 @ $79.00", "3.4x",  "P=70%",  "OI=62",     "Sep 27 PDUFA      |  Rare epilepsy, 6% spread"),
    ("65d",  "MLTX", "$21C  Aug-21 @ $4.20",  "2.7x",  "P=100%", "OI=1,041",  "Jun-Jul 2026      |  Warpspeed 100%, Grade B"),
]

for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f"{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}")

lines.append("")
lines.append("BELOW THRESHOLD (Excel tab for reference):")
lines.append("  RGNX $10C Jul17 @ $2.65 -- 2.2x (non-RCT gene therapy, Grade F, AbbVie milestone pending)")
lines.append("  ARGX $800/$850C May15 -- 1.5x FRESH ENTRY (spread widened; existing position: hold to PDUFA)")
lines.append("")
lines.append("=" * 65)
lines.append("SCIENCE GRADES")
lines.append("-" * 65)
lines.append("")
lines.append("Grade B -- MLTX: RCT triple-blind N=960, ACR50 endpoint. Best designed trial.")
lines.append("")
lines.append("Grade C -- IDYA, TVTX, RVMD, NTLA, PRAX: Adequate design, some concerns.")
lines.append("")
lines.append("Grade D (LONG risk note):")
lines.append("  AXSM: PDUFA bet on already-approved drug -- science grade reflects trial design, not approval odds.")
lines.append("  ARGX: Open-label functional endpoint. Prior data strong. Hold.")
lines.append("")
lines.append("Grade F (context matters):")
lines.append("  RGNX: Non-RCT standard for gene therapy. FDA accepted same design for Elevidys/Zolgensma.")
lines.append("  AGIO: Grade F CONFIRMS put thesis. Non-RCT + surrogate + wrong patient population.")
lines.append("")
lines.append("=" * 65)
lines.append("PLAY CARDS")
lines.append("-" * 65)

cards = [
    ("IDYA", "CALLS", "3.7x", "P=96%", "Grade C", "$35C May-15 @ $5.35 | OI=10,708 | IV=185%",
     "Apr 13 2026 (topline OPTIMUM-02, CONFIRMED Monday)",
     "First 1L therapy for HLA-A2-neg uveal melanoma. DB lock was April 1-15. Announcement confirmed via PRN. IV reflects full binary pricing -- enter before Monday open if conviction high.",
     "OI=10,708 shows significant smart money positioning. First confirmed date in the DAR-UM-2 program. Pre-reg likely fires before press release. $1k -> ~$3,700."),
    ("TVTX", "CALLS", "6.0x", "P=77%", "Grade C", "$35C Apr-17 @ $2.85 | OI=8,614 | IV=305%",
     "Apr 13 2026 (PDUFA CERTAIN, same day as IDYA)",
     "FILSPARI already approved for IgAN. FSGS label expansion with Phase 3 DUPLEX data. Stock at $28.96 vs $35 strike. IV=305% means the market is fully pricing the binary outcome.",
     "Monday is a double-catalyst day (IDYA + TVTX both Apr 13). Already-approved drug = lower regulatory bar for label expansion. $1k -> ~$6,000."),
    ("AXSM", "CALLS", "15.0x", "P=65%", "Grade D", "$200C May-15 @ $8.05 | OI=573 | IV=72%",
     "Apr 30 2026 (PDUFA CERTAIN)",
     "Best multiple in the portfolio at 15x. AXS-05 (Auvelity) is already FDA-approved for MDD. This is a label extension to Alzheimer agitation. BTD + Priority Review. Stock at $178.11.",
     "Not a trial result bet. FDA has seen all the data. Label extension = lower bar. Pre-PDUFA drift historically strong in final 10 days. Grade D reflects trial design not approval probability. $1k -> ~$15,000."),
    ("AGIO", "PUTS", "6.5x", "P(fail)=94%", "Grade F", "$30P Aug-21 @ $2.82 | OI=691 | IV=55%",
     "May-Jun 2026",
     "Phase 2b tebapivat in lower-risk MDS uses 8-week transfusion independence endpoint for the hardest-to-treat patient subset. Phase 2a worked only in the easiest patients.",
     "Science Grade F CONFIRMS the put thesis. Non-randomized, surrogate endpoint, wrong patient population. P(fail)=94%. $1k -> ~$6,500."),
    ("RVMD", "CALLS", "6.0x", "P=61%", "Grade C", "$125C Jun-18 @ $8.10 | OI=55 | IV=101%",
     "May-Jun 2026",
     "RMC-6236 is the first multi-RAS(ON) selective inhibitor targeting KRAS, NRAS, and HRAS. Phase 3 RASolute-301 enrollment complete. NDA submission H2 2026.",
     "KRAS was considered undruggable for decades. Only company with multi-RAS selectivity profile. OI=55 is thin -- use limit orders only. $1k -> ~$6,000."),
    ("NTLA", "CALLS", "4.1x", "P=82%", "Grade C", "$15C Jul-17 @ $2.25 | OI=1,752 | IV=101%",
     "May-Jun 2026",
     "NTLA-2002 CRISPR HAE prophylaxis. HAELO enrollment complete September 2025. 3-year data published April 2 in NEJM: 96% attack reduction, 31/32 attack-free. BLA H2 2026.",
     "Data already published. Market has not fully re-rated. BLA timeline forces final topline disclosure before June. Window closing. $1k -> ~$4,100."),
    ("PRAX", "CALLS", "3.4x", "P=70%", "Grade C", "$300C Jan-27 @ $79.00 | OI=62 | spread=6%",
     "Sep 27 2026 (PDUFA CERTAIN)",
     "Relutrigine (PRAX-562) is the first targeted Nav1.2/Nav1.6 channel blocker for SCN2A/SCN8A developmental and epileptic encephalopathies. Approximately 5,000 US patients with no targeted treatment.",
     "Confirmed PDUFA Sep 27. Jan27 expiry gives 4-month buffer. IV will build as PDUFA approaches. OI=62, spread=6% is unusually tight for this size. Rare disease premium on approval. $1k -> ~$3,400."),
    ("MLTX", "CALLS", "2.7x", "P=100%", "Grade B", "$21C Aug-21 @ $4.20 | OI=1,041 | IV=122%",
     "Jun-Jul 2026",
     "Sonelokimab IZAR-1 Phase 3 PsA readout. Warpspeed consensus P=100%. Triple-blind placebo-controlled RCT, N=960. ACR50 endpoint is FDA gold standard for PsA.",
     "Grade B: best designed trial in portfolio. Warpspeed P=100% is extremely rare. Small cap ($500M) means institutional re-rating on approval is large. $1k -> ~$2,700."),
]

for ticker, direc, mult, prob, grade, option_str, cat, why, edge in cards:
    lines.append(f"\n{'='*55}")
    lines.append(f"  {ticker} -- {direc} -- {mult} -- {prob} -- {grade}")
    lines.append(f"{'='*55}")
    lines.append(f"  OPTION:   {option_str}")
    lines.append(f"  CATALYST: {cat}")
    lines.append(f"  WHY:      {why}")
    lines.append(f"  EDGE:     {edge}")

lines.append("")
lines.append("=" * 65)
lines.append("MONITOR")
lines.append("-" * 65)
lines.append("  VERA  atacicept -- Jul 7 PDUFA  | 2.3x (below threshold). Promote when 2.5x.")
lines.append("  VRDN  veligrotug -- Jun 30 PDUFA | Spreads $0 bid. Watch May for liquidity.")
lines.append("  NAMS  obicetrapib -- Nov 2026     | PUT candidate P=37%. Too far out.")
lines.append("")
lines.append("Auto-remove Monday April 14: TVTX (post-PDUFA)")
lines.append("Monitor Monday April 13: IDYA and TVTX both announce")

body = "\n".join(lines)
with open(BASE / 'reports/email_body_2026-04-11.txt', 'w') as f:
    f.write(body)

print(f"Email body: {len(body)} chars")
print("Done.")
