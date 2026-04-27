#!/usr/bin/env python3
"""Alpha Sniper Run #18 — Friday April 17, 2026. AACR Day 1."""
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
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #18  --  10 qualifying (>=2.5x)  --  AACR Day 1'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIR','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

qualifying = [
    ('AXSM','CALLS','$200C','2026-05-15',10.00,13.2,13200, 65,'D',2267,'Apr 30 CERTAIN',
     '13 days. OI=2,267. BEST ENTRY. +30% on position. Pre-PDUFA drift accelerating.'),
    ('IDYA','CALLS','$35C','2026-05-15', 1.24,19.0,19000, 75,'C',11201,'NDA H2 2026',
     'PFS positive Apr 13. NDA = next catalyst. 28 days. High leverage on small cost.'),
    ('AGIO','PUTS', '$30P','2026-08-21', 2.73, 6.5, 6500,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Stock +1.4% today = sector move, NOT thesis change.'),
    ('NTLA','CALLS','$15C','2026-07-17', 2.45, 4.3, 4300, 82,'C', 1801,'May-Jun 2026',
     '-5.2% yesterday (tariff selloff, no news). Thesis intact. BLA forces June disclosure.'),
    ('MLTX','CALLS','$21C','2026-08-21', 3.40, 3.2, 3200,100,'B', 1055,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT. Stock -1.3% (sector). Thesis unchanged.'),
    ('RVMD','CALLS','$125C','2026-06-18',29.00, 5.0, 5000, 90,'C',   56,'AACR+NDA H2',
     'Phase 3 winner. AACR presentations all week. Stock -2.1% = profit-taking after +65% run.'),
    ('RGNX','CALLS','$10C', '2026-07-17', 1.50, 4.4, 4400, 68,'F', 1509,'BLA mid-2026',
     'AbbVie $100M milestone + BLA submission mid-2026. -1.8% yesterday (sector).'),
    ('VRDN','CALLS','$17C', '2026-07-17', 1.85, 5.1, 5100, 72,'C', 2049,'Jun 30 CERTAIN',
     'Jun 30 PDUFA confirmed. OI=2,049. Pre-mkt bid=$0. Use last=$1.85 at limit.'),
    ('VERA','CALLS','$55C', '2027-01-15', 8.05, 2.6, 2600, 72,'C',   19,'Jul 7 CERTAIN',
     '-2.8% yesterday (sector). Atacicept IgAN BLA. Jul 7 PDUFA. OI=19 small size.'),
    ('PRAX','CALLS','$300C','2027-01-15',103.53,2.7, 2700, 70,'C',   48,'Sep 27 CERTAIN',
     '-5.7% yesterday (sector/tariffs). $300C still ITM by $20.39. Sep 27 PDUFA intact.'),
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

# TAB 2 — All Active with P&L
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:N1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays | Position P&L included'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI',
               'CATALYST','DAYS','P&L','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',10.00,13.2,2267,'Apr 30 CERTAIN',13,
     '+30%','BEST 13.2x. OI=2,267. 13 days. Approved drug ext.'),
    ('IDYA','Darovasertib+Criz','CALLS',75,'C','$35C','2026-05-15',1.24,19.0,11201,'NDA H2 2026',28,
     '-77%','PFS positive. NDA = next catalyst. 28 days remaining.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.50,4.4,1509,'BLA mid-2026',74,
     'flat','AbbVie milestone. BLA mid-2026. Sector selloff.'),
    ('ARGX','Efgar $800/$850C','SPREAD',87,'D','$800/$850C','2026-05-15',35.70,0.4,5,'May 10 CERTAIN',23,
     '+197%','Hold existing through May 10 PDUFA. Stock $828.35, $21.65 to max payout.'),
    ('RVMD','Daraxonrasib','CALLS',90,'C','$125C','2026-06-18',29.00,5.0,56,'AACR+NDA H2',61,
     '+263%','Phase 3 winner. AACR ongoing. -2.1% = profit taking after +65% run.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.73,6.5,691,'May-Jun 2026',28,
     'flat','Grade F put. Stock +1.4% sector move. Thesis intact. May-Jun readout.'),
    ('NTLA','NTLA-2002 CRISPR','CALLS',82,'C','$15C','2026-07-17',2.45,4.3,1801,'May-Jun 2026',28,
     'flat','NEJM 3yr data. BLA forces June. -5.2% sector selloff, no news.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$17C','2026-07-17',1.85,5.1,2049,'Jun 30 CERTAIN',74,
     'flat','$17C OI=2,049. Jun 30 PDUFA. Pre-mkt bid=$0.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.40,3.2,1055,'Jun-Jul 2026',59,
     'flat','Warpspeed 100%. Grade B. Sector dip.'),
    ('VERA','Atacicept IgAN','CALLS',72,'C','$55C','2027-01-15',8.05,2.6,19,'Jul 7 CERTAIN',81,
     'flat','Jul 7 PDUFA. OI=19. Sector -2.8%.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,2.7,48,'Sep 27 CERTAIN',163,
     '+31%','Stock $320.39 (-$20 sector). $300C ITM by $20.39. Sep 27 PDUFA.'),
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
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%','PUT candidate. ~210 days out.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%','Mega cap. 2027 readout.','Remove if still 2027 in 45d'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        cell = ws3.cell(row=i,column=col,value=val)
        cs(cell,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-17.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL
lines = []
lines.append('ALPHA SNIPER -- April 17, 2026 (Friday) -- AACR Day 1')
lines.append('Run #18  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: AXSM 13.2x')
lines.append('=' * 65)
lines.append('')
lines.append('MARKET CONTEXT: Biotech selloff yesterday on macro tariff fears')
lines.append('  PRAX -5.7%, NTLA -5.2%, VERA -2.8%, RVMD -2.1% -- ALL sector moves, no news')
lines.append('  Zero 8-Ks or press releases from any active play')
lines.append('  Thesis unchanged across all 11 plays')
lines.append('')
lines.append('AACR 2026: April 17-22 -- RVMD has 9 presentations')
lines.append('  Stock -2.1% = profit-taking after +65% run April 13-15')
lines.append('  Option at $29.00 -- entry ~$8 = still ~+263% gain')
lines.append('  Watch each morning for abstract release reactions')
lines.append('')
lines.append('AGIO WATCH: Stock +1.4% (2 days in a row)')
lines.append('  This is biotech sector strength, NOT a thesis change for tebapivat')
lines.append('  Phase 2b readout still May-Jun 2026. Put OTM by $4.92 with 4 months left.')
lines.append('  Hold. The science (Grade F) has not changed.')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)
one_liners = [
    ('13d', 'AXSM', '$200C May-15 @ $10.00',  '13.2x', 'P=65%',    'OI=2,267', 'Apr 30 CERTAIN    | BEST ENTRY | +30%'),
    ('28d', 'IDYA', '$35C  May-15 @ $1.24',   '19.0x', 'P=75%',    'OI=11,201','NDA H2 2026       | PFS positive'),
    ('28d', 'AGIO', '$30P  Aug-21 @ $2.73',   '6.5x',  'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F | sector +1.4% not thesis'),
    ('28d', 'NTLA', '$15C  Jul-17 @ $2.45',   '4.3x',  'P=82%',    'OI=1,801', 'May-Jun 2026      | -5.2% sector only'),
    ('59d', 'MLTX', '$21C  Aug-21 @ $3.40',   '3.2x',  'P=100%',   'OI=1,055', 'Jun-Jul 2026      | Grade B'),
    ('61d', 'RVMD', '$125C Jun-18 @ $29.00',  '5.0x',  'P=90%',    'OI=56',    'AACR Day 1        | Phase 3 winner +263%'),
    ('74d', 'RGNX', '$10C  Jul-17 @ $1.50',   '4.4x',  'P=68%',    'OI=1,509', 'BLA mid-2026      | AbbVie milestone'),
    ('74d', 'VRDN', '$17C  Jul-17 @ $1.85',   '5.1x',  'P=72%',    'OI=2,049', 'Jun 30 CERTAIN    | OI=2,049 | use limit orders'),
    ('81d', 'VERA', '$55C  Jan-27 @ $8.05',   '2.6x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | small size'),
    ('163d','PRAX', '$300C Jan-27 @ $103.53', '2.7x',  'P=70%',    'OI=48',    'Sep 27 CERTAIN    | ITM by $20.39 | -5.7% sector'),
]
for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.4x fresh (worthless to add).')
lines.append('  Existing position: entry ~$12, spread value ~$35.70 = +197% theoretical.')
lines.append('  Stock $828.35, $21.65 from $850. 23 days to May 10 PDUFA. HOLD.')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES (unchanged)')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50.')
lines.append('Grade C -- IDYA, RVMD, NTLA, VRDN, VERA, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (confirms put thesis).')
lines.append('')
lines.append('=' * 65)
lines.append('POSITION SUMMARY (unrealized)')
lines.append('-' * 65)
lines.append('  RVMD:  entry ~$8   -> $29.00 = ~+263% | AACR presentations all week')
lines.append('  ARGX:  entry ~$12  -> spread ~$35.70  = ~+197% | 23d to PDUFA, $21.65 to max')
lines.append('  AXSM:  entry ~$7.70-> $10.00  = +30%  | 13d to Apr 30 PDUFA')
lines.append('  PRAX:  entry $79   -> $103.53 = +31%  | ITM by $20.39, sector dip')
lines.append('  AGIO:  entry $2.73 -> $2.73   = flat  | thesis intact, readout May-Jun')
lines.append('  IDYA:  entry $5.35 -> $1.24   = -77%  | NDA = next catalyst, 28 days')
lines.append('')
lines.append('WEEKEND NOTE: AACR abstracts may continue releasing Saturday/Sunday.')
lines.append('Intraday scanner is off weekends. Monitor RVMD Sunday evening for Monday gap.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-17.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
