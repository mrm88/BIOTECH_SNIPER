#!/usr/bin/env python3
"""Alpha Sniper Run #22 — Tuesday April 21, 2026. AGIO put ITM. AXSM 9 days."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'

def cs(c, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    c.fill = PatternFill(fill_type='solid', fgColor=bg)
    c.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    c.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for col, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=col, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True)

wb = openpyxl.Workbook()

# TAB 1 — Qualifying Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #22  --  10 qualifying  --  AGIO PUT ITM'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',10.82,12.9,12900, 65,'D',2280,'Apr 30 CERTAIN',
     '9 DAYS TO PDUFA. +40% on position. Stock $188.69. Final stretch.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.25,20.4,20400, 75,'C',11832,'NDA H2 2026',
     'Stock $33.60, OTM by $1.40. OI=11,832. NDA filing announcement = catalyst.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.35, 8.7, 8700,  4,'F',  692,'Tebapivat MDS H1 2026',
     'PUT IN THE MONEY. Stock $27.07 (-23% yesterday). ITM by $2.93. THESIS STRENGTHENED.'),
    ('NTLA','CALLS','$15C','2026-07-17', 3.00, 4.1, 4100, 82,'C', 1863,'May-Jun 2026',
     'Stock $15.21, ATM. BLA forces June disclosure. +1.7% yesterday.'),
    ('VRDN','CALLS','$17C','2026-07-17', 1.55, 6.1, 6100, 72,'C', 2253,'Jun 30 CERTAIN',
     'OI=2,253. Multiple jumped to 6.1x. Jun 30 PDUFA. Pre-mkt bid=$0.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.40, 3.3, 3300,100,'B', 1068,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Best designed trial.'),
    ('RVMD','CALLS','$125C','2026-06-18',29.00, 4.8, 4800, 90,'C',   56,'AACR+NDA H2',
     'Phase 3 winner. AACR Day 5 (last day). -1.6% yesterday = profit taking.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 2.87, 2.5, 2500, 68,'F', 1517,'BLA mid-2026',
     'AbbVie milestone + BLA mid-2026.'),
    ('VERA','CALLS','$55C', '2027-01-15', 8.05, 2.5, 2500, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. OI=19, use limit orders.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53, 3.1, 3100, 70,'C',  48,'Sep 27 CERTAIN',
     'Stock $342.57. $300C ITM by $42.57. Sep 27 PDUFA confirmed.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    rbg = RBG if 'PUT' in direc else GBG
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        cell = ws1.cell(row=row, column=col, value=val)
        fg = (YLW if col==1 else (RED if 'PUT' in direc else GRN) if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        cs(cell, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 40
    row += 1

for i, w in enumerate([8,8,10,12,10,10,12,9,10,7,22,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2 — All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',10.82,12.9,2280,'Apr 30 CERTAIN',9,
     '+40%','9 DAYS. Approved drug label ext. OI=2,280.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.25,20.4,11832,'NDA H2 2026',24,
     '-77%','Stock $33.60. OI=11,832. NDA = catalyst.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',2.87,2.5,1517,'BLA mid-2026',70,
     'flat','AbbVie milestone. BLA mid-2026.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',24.29,1.1,5,'May 10 2026',19,
     '+102%','Stock pulled back to $834.45. $15.55 from upper leg. 19d to PDUFA. Hold.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',29.00,4.8,56,'AACR+NDA H2',57,
     '+263%','Phase 3 winner. AACR ends today. NDA filing timing play.'),
    ('AGIO','Tebapivat MDS','PUTS',4,'F','$30P','2026-08-21',2.35,8.7,692,'Tebapivat MDS H1 2026',24,
     '-14%','PUT NOW ITM BY $2.93. Stock -23% on Novo SCD data. Thesis strengthened.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',3.00,4.1,1863,'May-Jun 2026',24,
     'flat','ATM at $15.21. BLA forces June.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.55,6.1,2253,'Jun 30 CERTAIN',70,
     'flat','OI=2,253. 6.1x. Jun 30 PDUFA.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.40,3.3,1068,'Jun-Jul 2026',55,
     'flat','Warpspeed 100%. Grade B.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.5,19,'Jul 7 CERTAIN',77,
     'flat','Jul 7 PDUFA. OI=19.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,3.1,48,'Sep 27 CERTAIN',159,
     '+31%','Stock $342.57. ITM by $42.57. Sep 27 PDUFA.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,pnl,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    pnl_color = GRN if '+' in pnl else RED if '-' in pnl else GRY
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',pnl,notes]
    for col,val in enumerate(row_vals,1):
        cell = ws2.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9
              else pnl_color if col==13 else WHT)
        cs(cell,bg=rbg,fg=fg,bold=(col==1),wrap=(col==14))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,20,8,7,8,12,12,9,9,7,22,6,8,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3 — Monitor
ws3 = wb.create_sheet('Monitor')
ws3.sheet_properties.tabColor = 'D29922'
ws3.merge_cells('A1:F1')
t3 = ws3['A1']
t3.value = f'MONITOR -- {today_str}'
cs(t3, bg=DARK, fg=ORN, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])
mon_rows = [
    ('RZLT','Ersodetug upLIFT','H2 2026','72%','No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~200 days.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. REMOVE in 20 days if still 2027.','20 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-21.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 21, 2026 (Tuesday) -- AACR Final Day')
lines.append('Run #22  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: IDYA 20.4x')
lines.append('=' * 65)
lines.append('')
lines.append('AGIO PUT IS NOW IN THE MONEY (+8.7x multiple today)')
lines.append('')
lines.append('  Stock dropped -23% Monday to $27.07.')
lines.append('  Cause: Novo Nordisk released positive etavopivat SCD data (27% VOC reduction).')
lines.append('  This is competitive pressure on mitapivat SCD -- NOT the tebapivat MDS readout.')
lines.append('')
lines.append('  IMPORTANT: Our PUT thesis was on tebapivat Phase 2b LR-MDS (wrong endpoint,')
lines.append('  wrong patient population, non-randomized). That readout is STILL PENDING (H1 2026).')
lines.append('')
lines.append('  Current situation:')
lines.append('  $30P Aug21: stock $27.07 = ITM by $2.93 (intrinsic $2.93)')
lines.append('  Pre-market mid: $2.35 (stale, bid=$0). Should reprice above intrinsic at open.')
lines.append('  Entry $2.73 -> intrinsic now $2.93 = effectively at breakeven on intrinsic.')
lines.append('  HOLD: tebapivat MDS readout still pending. If fails (P=94%), stock -> $15-20.')
lines.append('  At $15 stock: $30P intrinsic = $15 on $2.73 entry = 5.5x gain.')
lines.append('')
lines.append('9 TRADING DAYS TO AXSM PDUFA (April 30)')
lines.append('  $200C May-15 at $10.82 -- option +40% from entry.')
lines.append('  OI=2,280. Stock $188.69. Approved drug label extension for AD agitation.')
lines.append('')
lines.append('ARGX PULLBACK: Stock $834.45 ($15.55 from upper leg, was $0.96 Friday)')
lines.append('  Spread value $24.29 = +102% on $12 entry.')
lines.append('  19 days to May 10 PDUFA. P=87% approval. HOLD.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('9d',  'AXSM', '$200C May-15 @ $10.82',  '12.9x', 'P=65%',   'OI=2,280',  'Apr 30 CERTAIN    | +40% gain | Grade D'),
    ('24d', 'IDYA', '$35C  May-15 @ $1.25',   '20.4x', 'P=75%',   'OI=11,832', 'NDA H2 2026       | OTM $1.40 | -77% entry'),
    ('24d', 'AGIO', '$30P  Aug-21 @ $2.35',   '8.7x',  'P(f)=96%','OI=692',    'MDS PENDING       | PUT ITM +$2.93 | THESIS INTACT'),
    ('24d', 'NTLA', '$15C  Jul-17 @ $3.00',   '4.1x',  'P=82%',   'OI=1,863',  'May-Jun 2026      | ATM at $15.21'),
    ('55d', 'MLTX', '$21C  Aug-21 @ $3.40',   '3.3x',  'P=100%',  'OI=1,068',  'Jun-Jul 2026      | Grade B'),
    ('57d', 'RVMD', '$125C Jun-18 @ $29.00',  '4.8x',  'P=90%',   'OI=56',     'AACR ends today   | Phase 3 winner +263%'),
    ('70d', 'RGNX', '$10C  Jul-17 @ $2.87',   '2.5x',  'P=68%',   'OI=1,517',  'BLA mid-2026'),
    ('70d', 'VRDN', '$17C  Jul-17 @ $1.55',   '6.1x',  'P=72%',   'OI=2,253',  'Jun 30 CERTAIN    | multiple jumped to 6.1x'),
    ('77d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.5x',  'P=72%',   'OI=19',     'Jul 7 CERTAIN     | small size'),
    ('159d','PRAX', '$300C Jan-27 @ $103.53', '3.1x',  'P=70%',   'OI=48',     'Sep 27 CERTAIN    | ITM by $42.57'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 1.1x fresh. Existing: +102% at $24.29.')
lines.append('  Stock $834.45, $15.55 from max payout. 19 days to May 10 PDUFA. HOLD.')
lines.append('')
lines.append('POSITION SUMMARY:')
lines.append('  RVMD:  entry ~$8    -> $29.00 = ~+263%')
lines.append('  ARGX:  entry ~$12   -> $24.29 = +102%  | $15.55 from max (+317%)')
lines.append('  AXSM:  entry ~$7.70 -> $10.82 = +40%   | 9 days to PDUFA')
lines.append('  PRAX:  entry $79    -> $103.53 = +31%  | ITM by $42.57')
lines.append('  AGIO:  entry $2.73  -> ITM by $2.93    | MDS catalyst still pending')
lines.append('  IDYA:  entry $5.35  -> $1.25   = -77%  | $1.40 OTM, NDA = catalyst')
lines.append('')
lines.append('SCIENCE GRADES:')
lines.append('Grade B -- MLTX. Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (PUT thesis confirmed).')
lines.append('')
lines.append('AACR: Last day today (Apr 21). RVMD final presentations this week.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-21.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
