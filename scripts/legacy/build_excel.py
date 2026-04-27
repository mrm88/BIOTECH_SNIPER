#!/usr/bin/env python3
import json, datetime
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

cards = json.load(open('state/play_cards_2026-04-10.json'))
active_plays = json.load(open('state/active_plays.json'))
science_grades = json.load(open('state/science_grades.json'))

today = datetime.date.today().strftime('%B %d, %Y')
wb = openpyxl.Workbook()

DARK_BG   = '0D1117'
GREEN_BG  = '0D2818'
RED_BG    = '2D0A0A'
ORANGE_BG = '2D1A00'
HEADER_BG = '161B22'
WHITE     = 'FFFFFF'
GREEN     = '3FB950'
RED_C     = 'F85149'
ORANGE    = 'D29922'
YELLOW    = 'E3B341'
GREY      = '8B949E'
BLUE      = '58A6FF'
PURPLE    = 'BC8CFF'

def cs(cell, bg=DARK_BG, fg=WHITE, bold=False, size=10, align='left', wrap=False):
    cell.fill = PatternFill(fill_type='solid', fgColor=bg)
    cell.font = Font(color=fg, bold=bold, size=size, name='Consolas')
    cell.alignment = Alignment(horizontal=align, vertical='center', wrap_text=wrap)

def hrow(ws, row, values, bg=HEADER_BG):
    for col, val in enumerate(values, 1):
        c = ws.cell(row=row, column=col, value=val)
        cs(c, bg=bg, fg=YELLOW, bold=True, size=10)

# ── TAB 1: 2.5x+ Plays ──────────────────────────────────────────────────────
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'

ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = 'ALPHA SNIPER  --  ' + today + '  --  9 QUALIFYING PLAYS (>=2.5x)'
cs(t, bg=DARK_BG, fg=GREEN, bold=True, size=13, align='center')
ws1.row_dimensions[1].height = 28

hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN','P(WIN)%','SCIENCE','OI','CATALYST DATE','CATALYST NOTE'])
ws1.row_dimensions[2].height = 20

order = {'Apr 13 2026':1,'~May 2026':2,'Apr 30 2026':3,'Apr-May 2026':4,
         'May 10 2026':5,'May-Jun 2026':6,'Jun-Jul 2026':7,'Sep 27 2026':8}
qualifying = sorted([c for c in cards if c.get('multiple',0) >= 2.5],
                    key=lambda x: order.get(x.get('catalyst',''), 9))

row = 3
for card in qualifying:
    mult  = card.get('multiple', 0)
    p     = card.get('p_success', 0)
    grade = card.get('science_grade', '?')
    direc = card.get('direction','')
    rbg   = RED_BG if 'PUT' in direc else GREEN_BG
    dcol  = RED_C  if 'PUT' in direc else GREEN
    gcol  = {'A':GREEN,'B':GREEN,'C':YELLOW,'D':ORANGE,'F':RED_C}.get(grade, WHITE)
    opt_char = 'P' if 'PUT' in direc else 'C'
    strike_val = str(card.get('strike',''))
    
    vals = [
        card.get('ticker',''),
        direc.replace('LONG_','').replace('_',' '),
        '$' + strike_val + opt_char,
        card.get('expiry',''),
        '$' + str(round(card.get('mid',0),2)),
        str(mult) + 'x',
        '$' + str(card.get('k1',0)),
        str(p) + '%',
        'Grade ' + grade,
        card.get('oi', 0),
        card.get('catalyst',''),
        (card.get('why','') or '')[:120],
    ]
    for col, val in enumerate(vals, 1):
        c = ws1.cell(row=row, column=col, value=val)
        fg = (YELLOW if col==1 else dcol if col==2 else (ORANGE if mult>=5 else BLUE) if col==6
              else (GREEN if p>=60 else RED_C) if col==8 else gcol if col==9 else WHITE)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 44 if len(str(vals[11]))>80 else 22
    row += 1

for i,w in enumerate([8,12,10,12,10,10,12,9,10,7,18,70], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# ── TAB 2: All Active ────────────────────────────────────────────────────────
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:M1')
t2 = ws2['A1']
t2.value = 'ALL ACTIVE PLAYS -- ' + today + ' -- 10 plays'
cs(t2, bg=DARK_BG, fg=BLUE, bold=True, size=12, align='center')
ws2.row_dimensions[1].height = 24

hrow(ws2, 2, ['TICKER','DRUG','DIRECTION','P(WIN)','SCIENCE','STRIKE','EXPIRY','MID','MULTIPLE','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('IDYA',  'Darovasertib+Crizotinib',    'LONG CALLS', 96,  'C', '$35C',       '2026-05-15',  5.20,  4.0, 10708, '~May 2026',     0,  'DB lock day 10. OI +61% overnight to 10,708. Pre-reg imminent.'),
    ('TVTX',  'Sparsentan FILSPARI sNDA',   'LONG CALLS', 77,  'C', '$35C',       '2026-04-17',  4.25,  5.1,  8614, 'Apr 13 2026',   3,  'PDUFA in 3 days. CERTAIN. sNDA label expansion.'),
    ('RGNX',  'RGX-202 gene therapy',        'LONG CALLS', 68,  'F', '$10C',       '2026-07-17',  2.10,  2.9,  1509, 'Apr-May 2026',  5,  'Grade F = non-RCT but standard for gene therapy. AbbVie $100M milestone.'),
    ('AXSM',  'AXS-05 sNDA',                 'LONG CALLS', 65,  'D', '$200C',      '2026-05-15',  8.60, 14.2,   573, 'Apr 30 2026',  20,  'BEST MULTIPLE 14.2x. CERTAIN PDUFA. Already approved for MDD.'),
    ('ARGX',  'Efgartigimod $800/$850 spread','CALL SPREAD',87, 'D', '$800/$850C', '2026-05-15', 12.00,  3.2,     6, 'May 10 2026',  30,  'Stock moved to $800.50 = lower leg. Spread value $5.75 vs $12 entry. Needs 6% more.'),
    ('RVMD',  'RMC-6236 RAS inhibitor',       'LONG CALLS', 61,  'C', '$125C',      '2026-06-18',  6.80,  7.1,    55, 'May-Jun 2026', 35,  'Stock +150% since entry. Phase 3 enrollment complete. OI thin.'),
    ('AGIO',  'Tebapivat AG-946',             'LONG PUTS',   6,  'F', '$30P',       '2026-08-21',  2.73,  6.8,   691, 'May-Jun 2026', 35,  'P(fail)=94%. Grade F confirms put thesis. Best risk/reward in portfolio.'),
    ('NTLA',  'NTLA-2002 CRISPR HAE',        'LONG CALLS', 82,  'C', '$15C',       '2026-07-17',  2.45,  4.0,  1752, 'May-Jun 2026', 35,  'NEJM 3yr data published Apr 2. 31/32 attack-free. BLA forces disclosure by June.'),
    ('MLTX',  'Sonelokimab IZAR-1 PsA',      'LONG CALLS',100,  'B', '$21C',       '2026-08-21',  4.10,  3.4,  1041, 'Jun-Jul 2026', 66,  'Warpspeed P=100%. RCT triple-blind N=960. Grade B best designed trial.'),
    ('PRAX',  'Relutrigine NDA DEE',         'LONG CALLS', 70,  'C', '$300C',      '2027-01-15', 79.00,  3.5,    62, 'Sep 27 2026', 170,  'CERTAIN Sep27 PDUFA. First Nav channel SCN2A/8A blocker. Rare. Spread=6% OI=62.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker, drug, direction, p, grade, strike, expiry, mid, mult, oi, catalyst, days, notes = rd
    rbg = RED_BG if 'PUT' in direction else (GREEN_BG if mult>=2.5 else ORANGE_BG)
    gcol = {'A':GREEN,'B':GREEN,'C':YELLOW,'D':ORANGE,'F':RED_C}.get(grade, WHITE)
    row_vals = [ticker, drug, direction, str(p)+'%', 'Grade '+grade, strike, expiry, '$'+str(mid), str(mult)+'x', oi, catalyst, str(days)+'d', notes]
    for col, val in enumerate(row_vals, 1):
        c = ws2.cell(row=i, column=col, value=val)
        fg = (YELLOW if col==1 else (GREEN if p>=60 else RED_C) if col==4
              else gcol if col==5 else (ORANGE if mult>=5 else BLUE if mult>=2.5 else GREY) if col==9 else WHITE)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==13))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,28,12,8,10,12,12,9,9,7,14,7,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# ── TAB 3: Science Grades ────────────────────────────────────────────────────
ws3 = wb.create_sheet('Science Grades')
ws3.sheet_properties.tabColor = 'BC8CFF'
ws3.merge_cells('A1:H1')
t3 = ws3['A1']
t3.value = 'SCIENCE GRADES (Shkreli-style protocol analysis) -- ' + today
cs(t3, bg=DARK_BG, fg=PURPLE, bold=True, size=12, align='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','GRADE','DESIGN SCORE','ADJ P%','BASE RATE','ENDPOINT','MISMATCH?','KEY FLAG'])

sci_rows = [
    ('MLTX', 'B', '+4', '95%', '45%', 'COMPOSITE',  'No',  'RCT triple-blind ACR50 endpoint. Grade B = best designed trial.'),
    ('IDYA', 'C',  '0', '95%', '52%', 'CLINICAL',   'No',  'Open-label but hard OS/PFS endpoint mitigates bias risk.'),
    ('TVTX', 'C', '+1', '77%', '45%', 'CLINICAL',   'No',  'Already approved drug extension. Clear mechanistic rationale.'),
    ('NTLA', 'C',  '0', '82%', '45%', 'CLINICAL',   'No',  'Small N=60 but 3yr NEJM data published. BLA forcing disclosure.'),
    ('RVMD', 'C', '+2', '61%', '15%', 'CLINICAL',   'No',  'Open-label but hard OS endpoint. N=501 well-powered.'),
    ('PRAX', 'C', '+2', '70%', '45%', 'FUNCTIONAL', 'No',  'RCT placebo-controlled. Seizure frequency validated endpoint.'),
    ('AXSM', 'D', '+2', '55%', '18%', 'FUNCTIONAL', 'YES', 'LONG but PDUFA bet on already-approved drug, not trial result.'),
    ('ARGX', 'D', '-3', '74%', '45%', 'FUNCTIONAL', 'YES', 'LONG but open-label + functional endpoint. Prior efgartigimod data strong.'),
    ('RGNX', 'F', '-7', '44%', '45%', 'UNKNOWN',    'YES', 'Non-RCT but STANDARD for gene therapy. FDA accepted Elevidys/Zolgensma same design.'),
    ('AGIO', 'F', '-8',  '5%', '45%', 'SURROGATE',  'N/A', 'Grade F CONFIRMS put thesis. Non-RCT surrogate endpoint wrong patient pop.'),
]

for i, rd in enumerate(sci_rows, 3):
    ticker, grade, score, adj_p, base, endpoint, mismatch, flag = rd
    gbg = {'A':GREEN_BG,'B':GREEN_BG,'C':DARK_BG,'D':ORANGE_BG,'F':RED_BG}.get(grade, DARK_BG)
    gcol = {'A':GREEN,'B':GREEN,'C':YELLOW,'D':ORANGE,'F':RED_C}.get(grade, WHITE)
    row_vals = [ticker, grade, score, adj_p, base, endpoint, mismatch, flag]
    for col, val in enumerate(row_vals, 1):
        c = ws3.cell(row=i, column=col, value=val)
        fg = (YELLOW if col==1 else gcol if col==2 else RED_C if (mismatch=='YES' and col==7) else WHITE)
        cs(c, bg=gbg, fg=fg, bold=(col in [1,2]), wrap=(col==8))
    ws3.row_dimensions[i].height = 36

for i,w in enumerate([8,8,12,9,10,12,11,70],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

# ── TAB 4: Monitor ───────────────────────────────────────────────────────────
ws4 = wb.create_sheet('Monitor')
ws4.sheet_properties.tabColor = 'D29922'
ws4.merge_cells('A1:F1')
t4 = ws4['A1']
t4.value = 'MONITOR PLAYS -- Below threshold or too far out -- ' + today
cs(t4, bg=DARK_BG, fg=ORANGE, bold=True, size=12, align='center')
ws4.row_dimensions[1].height = 24
hrow(ws4, 2, ['TICKER','DRUG','CATALYST','P%','STATUS / NOTES','GRADUATE WHEN'])

mon_rows = [
    ('VRDN', 'Veligrotug veligrotug', 'Jun 30 2026 PDUFA', '72%', 'Spreads too wide (bid $0). Watch for liquidity building as Jun 30 approaches.', 'When spread tightens below 80%'),
    ('VERA', 'Atacicept IgAN BLA',    'Jul 7 2026 PDUFA',  '72%', 'Jan27 $55C mid=$8.05 = 2.3x -- just below 2.5x. Science Grade C. RCT double-blind N=376.', 'When multiple reaches 2.5x'),
    ('RZLT', 'Ersodetug upLIFT',      'H2 2026',           '72%', 'Very illiquid. 16-patient trial. No bid on options.', 'If liquidity builds'),
    ('NAMS', 'Obicetrapib PREVAIL',   'Nov 2026',          '37%', 'PUT candidate P=37%. N=9,541 CV outcomes trial. Primary completion Nov 2026, too far.', 'When within 90 days'),
    ('AMGN', 'Olpasiran OCEAN-a',     '2027',              '71%', 'Mega cap. Readout 2027. No edge for options.', 'Remove if still 2027 in 90 days'),
]

for i, rd in enumerate(mon_rows, 3):
    for col, val in enumerate(rd, 1):
        c = ws4.cell(row=i, column=col, value=val)
        cs(c, bg=DARK_BG, fg=(YELLOW if col==1 else WHITE), wrap=(col in [5,6]))
    ws4.row_dimensions[i].height = 36

for i,w in enumerate([8,28,18,7,60,30],1):
    ws4.column_dimensions[get_column_letter(i)].width = w

path = 'reports/Alpha_Sniper_2026-04-10.xlsx'
wb.save(path)
print('Saved: ' + path)
