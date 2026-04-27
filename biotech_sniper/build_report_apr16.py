#!/usr/bin/env python3
"""Alpha Sniper Run #17 — Thursday April 16, 2026."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'; PRP='BC8CFF'

def cs(c, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    c.fill = PatternFill(fill_type='solid', fgColor=bg)
    c.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    c.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for col, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=col, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True)

wb = openpyxl.Workbook()

# TAB 1 — 2.5x+ Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #17  --  10 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',10.00,13.2,13200, 65,'D',2270,'Apr 30 CERTAIN',
     '14 days. OI=2,270. BEST ENTRY. Approved drug label ext. +30% on entry.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.40,16.4,16400, 75,'C',11214,'NDA H2 2026',
     'PFS positive Apr 13. High math on low cost. NDA announcement = next re-rating.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.73, 6.6, 6600,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Stock +3.5% yesterday (sector, not thesis change).'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.95, 4.0, 4000, 82,'C', 1796,'May-Jun 2026',
     'NEJM 3yr data published. BLA forces June disclosure. +1.1% yesterday.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.80, 3.0, 3000,100,'B', 1056,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock -4% yesterday (no news).'),
    ('RVMD','CALLS','$125C','2026-06-18',28.58, 5.2, 5200, 90,'C',   46,'AACR+NDA H2',
     'Phase 3 winner. Stock $152.54 (+$5). Entry ~$8 -> $28.58 = ~+260%. AACR TOMORROW.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 1.50, 4.6, 4600, 68,'F', 1509,'BLA mid-2026',
     'AbbVie $100M milestone. Gene therapy DMD. BLA mid-2026.'),
    ('VRDN','CALLS','$17C', '2026-07-17', 1.90, 5.1, 5100, 72,'C', 1936,'Jun 30 CERTAIN',
     'UPDATED to $17C (OI=1,936 >> $20C OI=176). Pre-mkt bid=$0. Jun30 PDUFA. 5.1x.'),
    ('VERA','CALLS','$55C', '2027-01-15', 8.05, 2.9, 2900, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Jul 7 PDUFA. OI=19, use limit orders.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53,3.0, 3000, 70,'C',   48,'Sep 27 CERTAIN',
     'Stock $339.93. $300C ITM by $39.93. OI=48 (verified). Sep 27 PDUFA.'),
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

for i, w in enumerate([8,8,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2 — All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays | Position P&L included'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','ENTRY P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',10.00,13.2,2270,'Apr 30 CERTAIN',14,
     '~+30%','BEST 13.2x. OI=2,270. Label ext approved drug. 14d.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.40,16.4,11214,'NDA H2 2026',29,
     '-74%','PFS positive. NDA = next catalyst. 29 days.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.50,4.6,1509,'BLA mid-2026',75,
     'flat','AbbVie $100M milestone. BLA mid-2026.'),
    ('ARGX','Efgar \$800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',33.60,0.5,22,'May 10 CERTAIN',24,
     '+180%','Hold existing. Entry ~$12 -> value $33.60. Stock $840.87, $9.13 to max payout.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',28.58,5.2,46,'AACR+NDA H2',62,
     '~+260%','Phase 3 winner. AACR TOMORROW. +$5 yesterday to $152.54.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.73,6.6,691,'May-Jun 2026',29,
     'flat','Grade F put. Stock +3.5% (sector only). Thesis intact. May-Jun readout.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',2.95,4.0,1796,'May-Jun 2026',29,
     'flat','NEJM 3yr data pub. BLA forces June disclosure.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.90,5.1,1936,'Jun 30 CERTAIN',75,
     '-2%','Updated to $17C (OI=1,936). Pre-mkt bid=$0. Use limit orders.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.80,3.0,1056,'Jun-Jul 2026',60,
     'flat','Warpspeed 100%. Grade B. Stock -4% no news.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.9,19,'Jul 7 CERTAIN',82,
     'flat','Jul 7 PDUFA. OI=19, limit orders.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,3.0,48,'Sep 27 CERTAIN',164,
     '+31%','Stock $339.93. ITM by $39.93. Sep 27 PDUFA.'),
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

for i,w in enumerate([8,20,8,7,8,12,12,9,9,7,14,6,8,55],1):
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
    ('NAMS','Obicetrapib PREVAIL','Nov 2026 (~213d)','37%','PUT candidate. Too far. N=9,541.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. 2027 readout.','Remove if still 2027 in 45d'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        cell = ws3.cell(row=i,column=col,value=val)
        cs(cell,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-16.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 16, 2026')
lines.append('Run #17  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: IDYA 16.4x (math) / AXSM 13.2x (entry)')
lines.append('=' * 65)
lines.append('')
lines.append('AACR 2026 STARTS TOMORROW (April 17-22)')
lines.append('  RVMD has 9 presentations. Daraxonrasib data could drive further upside.')
lines.append('  $125C Jun18 at $28.58 -- entry ~$8 = ~+260% gain already.')
lines.append('  Stock $152.54. Watch abstracts/presentations as they release.')
lines.append('')
lines.append('VRDN UPDATED: $17C Jul17 (OI=1,936) replaces $20C (OI=176)')
lines.append('  Better OI by 11x. Last=$1.90. Stock $14.88. Jun 30 PDUFA confirmed.')
lines.append('  5.1x if stock 2x to $26.78 on approval. $1k -> ~$5,100.')
lines.append('')
lines.append('ARGX UPDATE: Stock $840.87 -- only $9.13 from the $850 upper leg')
lines.append('  Entry ~$12. Current spread value ~$33.60 = +180% gain already.')
lines.append('  May 10 PDUFA = 24 days. P=87% approval. If stock breaks $850 -> max $50 payout.')
lines.append('  Decision: exit now (+$21.60 profit) or hold 24 days for +$38 max.')
lines.append('  Recommendation: HOLD. 87% approval, 6% more move for max, 24 days.')
lines.append('')
lines.append('AGIO: Stock +3.5% yesterday to $34.45 -- this is sector rotation, not thesis change.')
lines.append('  $30P still OTM by $4.45 with 4 months left. Readout May-Jun 2026. Hold.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('14d', 'AXSM', '$200C May-15 @ $10.00',  '13.2x', 'P=65%',    'OI=2,270','Apr 30 CERTAIN    | BEST ENTRY | +30% gain'),
    ('29d', 'IDYA', '$35C  May-15 @ $1.40',   '16.4x', 'P=75%',    'OI=11,214','NDA H2 2026       | PFS positive'),
    ('29d', 'AGIO', '$30P  Aug-21 @ $2.73',   '6.6x',  'P(f)=94%', 'OI=691',  'May-Jun 2026      | Grade F puts | sector move not thesis'),
    ('29d', 'NTLA', '$15C  Jul-17 @ $2.95',   '4.0x',  'P=82%',    'OI=1,796','May-Jun 2026      | NEJM data published'),
    ('60d', 'MLTX', '$21C  Aug-21 @ $3.80',   '3.0x',  'P=100%',   'OI=1,056','Jun-Jul 2026      | Grade B Warpspeed 100%'),
    ('62d', 'RVMD', '$125C Jun-18 @ $28.58',  '5.2x',  'P=90%',    'OI=46',   'AACR TOMORROW     | Phase 3 winner +260%'),
    ('75d', 'RGNX', '$10C  Jul-17 @ $1.50',   '4.6x',  'P=68%',    'OI=1,509','BLA mid-2026      | AbbVie milestone'),
    ('75d', 'VRDN', '$17C  Jul-17 @ $1.90',   '5.1x',  'P=72%',    'OI=1,936','Jun 30 CERTAIN    | UPDATED strike | pre-mkt bid=$0'),
    ('82d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.9x',  'P=72%',    'OI=19',   'Jul 7 CERTAIN     | OI=19 limit orders'),
    ('164d','PRAX', '$300C Jan-27 @ $103.53', '3.0x',  'P=70%',    'OI=48',   'Sep 27 CERTAIN    | ITM by $39.93'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.5x fresh (worthless to add). Hold existing (+180%).')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50.')
lines.append('Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong, +180% gain).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (confirms put thesis).')
lines.append('')
lines.append('=' * 65)
lines.append('POSITION GUIDANCE')
lines.append('-' * 65)
lines.append('')
lines.append('RVMD ($125C Jun18) -- ~+260% gain:')
lines.append('  Stock $152.54. AACR Apr 17-22 starts tomorrow (9 RVMD presentations).')
lines.append('  Entry ~$8, current $28.58 = ~+260% unrealized gain.')
lines.append('  Option has $27.54 intrinsic + $1.04 time value with 62 days left.')
lines.append('  Consider: partial exit to lock gains. AACR data could push further OR cause consolidation.')
lines.append('')
lines.append('ARGX ($800/$850C May15) -- +180% on spread:')
lines.append('  Stock $840.87. Only $9.13 from $850 upper leg. Entry ~$12, value ~$33.60.')
lines.append('  24 days to May 10 PDUFA. P=87% approval.')
lines.append('  HOLD recommendation: 87% approval, 6% stock move for max payout of $50.')
lines.append('')
lines.append('AXSM ($200C May15) -- +30% gain, 14 days:')
lines.append('  OI=2,270 (nearly 4x from 573 at entry). Smart money accumulating.')
lines.append('  Pre-PDUFA drift typically strongest in final 10 days.')
lines.append('')
lines.append('AGIO ($30P Aug21) -- flat, thesis intact:')
lines.append('  Stock +3.5% yesterday on sector strength, NOT company news.')
lines.append('  Put is OTM by $4.45 with 4 months left. Readout May-Jun 2026.')
lines.append('  Grade F science confirms the thesis. Hold.')
lines.append('')
lines.append('IDYA ($35C May15) -- -74%, NDA thesis:')
lines.append('  Stock $32.18. Option at $1.40 (OTM by $2.82). 29 days.')
lines.append('  NDA filing announcement is the next re-rating event.')
lines.append('  Consider rolling to Sep18 or Jan27 for more time.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-16.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
