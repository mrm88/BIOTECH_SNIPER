#!/usr/bin/env python3
"""Alpha Sniper Run #16 — Wednesday April 15, 2026."""
import json, datetime, openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from pathlib import Path

from biotech_sniper.paths import BASE_DIR as BASE
today_str = datetime.date.today().strftime('%B %d, %Y')

DARK='0D1117'; GBG='0D2818'; RBG='2D0A0A'; OBG='2D1A00'; HDR='161B22'
WHT='FFFFFF'; GRN='3FB950'; RED='F85149'; ORN='D29922'; YLW='E3B341'
GRY='8B949E'; BLU='58A6FF'; PRP='BC8CFF'

def cs(cell, bg=DARK, fg=WHT, bold=False, sz=10, al='left', wrap=False):
    cell.fill = PatternFill(fill_type='solid', fgColor=bg)
    cell.font = Font(color=fg, bold=bold, size=sz, name='Consolas')
    cell.alignment = Alignment(horizontal=al, vertical='center', wrap_text=wrap)

def hrow(ws, row, vals, bg=HDR):
    for c, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cs(cell, bg=bg, fg=YLW, bold=True)

wb = openpyxl.Workbook()

# TAB 1: 2.5x+ Plays
ws1 = wb.active
ws1.title = '2.5x+ Plays'
ws1.sheet_properties.tabColor = '3FB950'
ws1.merge_cells('A1:L1')
t = ws1['A1']
t.value = f'ALPHA SNIPER  --  {today_str}  --  Run #16  --  10 qualifying (>=2.5x)'
cs(t, bg=DARK, fg=GRN, bold=True, sz=12, al='center')
ws1.row_dimensions[1].height = 28
hrow(ws1, 2, ['TICKER','DIRECTION','STRIKE','EXPIRY','MID FILL','MULTIPLE','$1k RETURN',
               'P(WIN)%','SCIENCE','OI','CATALYST DATE','NOTE'])
ws1.row_dimensions[2].height = 20

# sorted by days, then by p_success for ties
qualifying = [
    ('AXSM', 'CALLS','$200C','2026-05-15',10.00,13.2,13200, 65,'D', 2257,'Apr 30 CERTAIN',
     'BEST ENTRY 13.2x. AXSM +3.3% yesterday. OI=2,257. Approved drug label extension. 15 days.'),
    ('RGNX', 'CALLS','$10C', '2026-07-17', 1.50, 4.5, 4500, 68,'F', 1509,'BLA mid-2026',
     'AbbVie $100M milestone + BLA submission. Gene therapy DMD. Catalyst window updated to mid-2026.'),
    ('IDYA', 'CALLS','$35C', '2026-05-15', 1.62,14.3,14300, 75,'C',10912,'NDA H2 2026',
     'PFS positive Apr 13. High math multiple at low cost. NDA filing announcement = re-rating catalyst.'),
    ('AGIO', 'PUTS', '$30P', '2026-08-21', 2.73, 6.7, 6700,  6,'F',  691,'May-Jun 2026',
     'P(fail)=94%. Grade F confirms put. Best risk/reward put in portfolio.'),
    ('NTLA', 'CALLS','$15C', '2026-07-17', 2.60, 4.5, 4500, 82,'C', 1774,'May-Jun 2026',
     'NEJM 3yr data published. BLA forces June disclosure. NTLA +3.6% yesterday.'),
    ('MLTX', 'CALLS','$21C', '2026-08-21', 3.80, 3.4, 3400,100,'B', 1056,'Jun-Jul 2026',
     'Warpspeed 100%. Grade B RCT triple-blind N=960. Best designed trial.'),
    ('RVMD', 'CALLS','$125C','2026-06-18',19.60, 7.1, 7100, 90,'C',   46,'AACR + NDA H2 2026',
     'Phase 3 winner. Stock $147.01 (+8% yesterday). AACR ongoing. Option ~+150% from entry.'),
    ('VRDN', 'CALLS','$20C', '2026-07-17', 1.95, 3.8, 3800, 72,'C',  173,'Jun 30 CERTAIN',
     'NEW: Promoted from monitor today. OI=173 built. Jun 30 PDUFA confirmed. Pre-mkt bid=$0, last=$1.95.'),
    ('VERA', 'CALLS','$55C', '2027-01-15', 8.05, 3.1, 3100, 72,'C',   19,'Jul 7 CERTAIN',
     'Atacicept IgAN BLA. Stock $44.36. OI=19, use limit orders.'),
    ('PRAX', 'CALLS','$300C','2027-01-15',103.53,3.1, 3100, 70,'C',    0,'Sep 27 CERTAIN',
     'Stock $343.60 (+8.4%). $300C NOW ITM by $43.60. OI=0 pre-mkt but OI was 62 -- check at market open.'),
]

row = 3
for ticker, direc, strike, expiry, mid, mult, k1, p, grade, oi, cat, note in qualifying:
    rbg = RBG if 'PUT' in direc else GBG
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    vals = [ticker, direc, strike, expiry, f'${mid:.2f}', f'{mult}x',
            f'${k1:,}', f'{p}%', f'Grade {grade}', oi, cat, note[:120]]
    for col, val in enumerate(vals, 1):
        c = ws1.cell(row=row, column=col, value=val)
        fg = (YLW if col==1 else (RED if 'PUT' in direc else GRN) if col==2
              else (ORN if mult>=10 else BLU) if col==6
              else (GRN if p>=60 else RED) if col==8
              else gcol if col==9 else WHT)
        cs(c, bg=rbg, fg=fg, bold=(col==1), wrap=(col==12))
    ws1.row_dimensions[row].height = 40
    row += 1

for i, w in enumerate([8,10,10,12,10,10,12,9,10,7,20,68], 1):
    ws1.column_dimensions[get_column_letter(i)].width = w

# TAB 2: All Active
ws2 = wb.create_sheet('All Active')
ws2.sheet_properties.tabColor = '58A6FF'
ws2.merge_cells('A1:M1')
t2 = ws2['A1']
t2.value = f'ALL ACTIVE PLAYS -- {today_str} -- 11 plays'
cs(t2, bg=DARK, fg=BLU, bold=True, sz=11, al='center')
ws2.row_dimensions[1].height = 24
hrow(ws2, 2, ['TICKER','DRUG','DIR','P','SCI','STRIKE','EXPIRY','MID','MULT','OI','CATALYST','DAYS','NOTES'])

all_rows = [
    ('AXSM','AXS-05 sNDA','CALLS',65,'D','$200C','2026-05-15',10.00,13.2,2257,'Apr 30 CERTAIN',15,
     'BEST ENTRY 13.2x. OI=2,257 (tripled). Pre-PDUFA drift. +3.3% yesterday.'),
    ('RGNX','RGX-202 gene therapy','CALLS',68,'F','$10C','2026-07-17',1.50,4.5,1509,'BLA mid-2026',75,
     'AbbVie $100M milestone. Gene therapy DMD. BLA submission mid-2026.'),
    ('IDYA','Darovasertib+Crizotinib','CALLS',75,'C','$35C','2026-05-15',1.62,14.3,10912,'NDA H2 2026',30,
     'PFS positive Apr 13. Option OTM, 30 days. NDA = next catalyst. High math at low cost.'),
    ('ARGX','Efgartigimod spread','SPREAD',87,'D','$800/$850C','2026-05-15',33.60,0.5,5,'May 10 CERTAIN',25,
     'BELOW THRESHOLD. Spread ~0.5x fresh (dead). Hold existing through May 10 PDUFA. Stock $828.35.'),
    ('RVMD','RMC-6236 daraxonrasib','CALLS',90,'C','$125C','2026-06-18',19.60,7.1,46,'AACR + NDA H2',63,
     'Phase 3 winner. +8% yesterday to $147.01. Option ~+150% entry. AACR presentations ongoing.'),
    ('AGIO','Tebapivat AG-946','PUTS',6,'F','$30P','2026-08-21',2.73,6.7,691,'May-Jun 2026',30,
     'P(fail)=94%. Grade F confirms put. +13% entry gain.'),
    ('NTLA','NTLA-2002 CRISPR HAE','CALLS',82,'C','$15C','2026-07-17',2.60,4.5,1774,'May-Jun 2026',30,
     'NEJM 3yr data. BLA forces June disclosure. +3.6% yesterday.'),
    ('MLTX','Sonelokimab IZAR-1','CALLS',100,'B','$21C','2026-08-21',3.80,3.4,1056,'Jun-Jul 2026',61,
     'Warpspeed 100%. Grade B. +1.9% yesterday.'),
    ('VRDN','Veligrotug TED BLA','CALLS',72,'C','$20C','2026-07-17',1.95,3.8,173,'Jun 30 CERTAIN',76,
     'NEW TODAY. PDUFA Jun 30. OI=173 built. Last=$1.95 (bid=$0 pre-mkt). Use limit orders.'),
    ('VERA','Atacicept IgAN BLA','CALLS',72,'C','$55C','2027-01-15',8.05,3.1,19,'Jul 7 CERTAIN',83,
     'Jul 7 PDUFA. OI=19, small size. Stock $44.36.'),
    ('PRAX','Relutrigine NDA','CALLS',70,'C','$300C','2027-01-15',103.53,3.1,0,'Sep 27 CERTAIN',165,
     'Stock $343.60 (+8.4%). $300C ITM by $43.60. OI=0 pre-mkt (was 62). Verify at open.'),
]

for i, rd in enumerate(all_rows, 3):
    ticker,drug,direc,p,grade,strike,expiry,mid,mult,oi,cat,days,notes = rd
    rbg = RBG if 'PUT' in direc else (GBG if mult>=2.5 else OBG)
    gcol = {'A':GRN,'B':GRN,'C':YLW,'D':ORN,'F':RED}.get(grade, WHT)
    row_vals = [ticker,drug,direc,f'{p}%',f'Grade {grade}',strike,expiry,
                f'${mid:.2f}',f'{mult}x',oi,cat,f'{days}d',notes]
    for col,val in enumerate(row_vals,1):
        c = ws2.cell(row=i,column=col,value=val)
        fg = (YLW if col==1 else (GRN if p>=60 else RED) if col==4
              else gcol if col==5
              else (ORN if mult>=10 else BLU if mult>=2.5 else GRY) if col==9 else WHT)
        cs(c,bg=rbg,fg=fg,bold=(col==1),wrap=(col==13))
    ws2.row_dimensions[i].height = 36

for i,w in enumerate([8,24,8,7,8,12,12,9,9,7,14,7,55],1):
    ws2.column_dimensions[get_column_letter(i)].width = w

# TAB 3: Monitor
ws3 = wb.create_sheet('Monitor')
ws3.sheet_properties.tabColor = 'D29922'
ws3.merge_cells('A1:F1')
t3 = ws3['A1']
t3.value = f'MONITOR -- {today_str}'
cs(t3, bg=DARK, fg=ORN, bold=True, sz=12, al='center')
ws3.row_dimensions[1].height = 24
hrow(ws3, 2, ['TICKER','DRUG','CATALYST','P%','NOTES','GRADUATE WHEN'])
mon_rows = [
    ('RZLT','Ersodetug upLIFT','H2 2026','72%',
     '16-patient trial. No liquid options.','If liquidity builds'),
    ('NAMS','Obicetrapib PREVAIL','Nov 2026','37%',
     'PUT candidate P=37%. N=9,541. Too far.','When within 90 days'),
    ('AMGN','Olpasiran OCEAN-a','2027','71%',
     'Mega cap. 2027 readout.','Remove if still 2027 in 60 days'),
]
for i,rd in enumerate(mon_rows,3):
    for col,val in enumerate(rd,1):
        c = ws3.cell(row=i,column=col,value=val)
        cs(c,bg=DARK,fg=(YLW if col==1 else WHT),wrap=(col in [5,6]))
    ws3.row_dimensions[i].height = 36
for i,w in enumerate([8,28,18,7,55,26],1):
    ws3.column_dimensions[get_column_letter(i)].width = w

path = BASE / 'reports/Alpha_Sniper_2026-04-15.xlsx'
wb.save(path)
print(f'Saved: {path}')

# EMAIL BODY
lines = []
lines.append(f'ALPHA SNIPER -- April 15, 2026')
lines.append(f'Run #16  |  11 active plays  |  10 qualifying (>=2.5x)  |  Best: IDYA 14.3x (math) / AXSM 13.2x (entry)')
lines.append('=' * 65)
lines.append('')
lines.append('NEW PLAY: VRDN $20C Jul17 @ $1.95 -- 3.8x')
lines.append('  Veligrotug TED BLA. Jun 30 PDUFA confirmed. OI=173 (built from zero last week).')
lines.append('  Pre-market bid=$0 (normal). Use last price $1.95 as fill. Jul17 covers PDUFA + 12d buffer.')
lines.append('')
lines.append('PORTFOLIO GAINS (yesterday):')
lines.append('  RVMD:  +7.9% to $147.01 | $125C at $19.60 | entry ~$7-8 = ~+150% gain | AACR ongoing')
lines.append('  PRAX:  +8.4% to $343.60 | $300C NOW ITM by $43.60 | option at $103.53')
lines.append('  AXSM:  +3.3% to $184.18 | OI=2,257 (tripled) | 15 days to Apr 30 PDUFA')
lines.append('  NTLA:  +3.6% to $14.78  | BLA forces June disclosure')
lines.append('  ARGX:  +2.4% to $828.35 | spread still below threshold (0.5x fresh)')
lines.append('')
lines.append('ONE-LINERS (sorted nearest catalyst, >=2.5x):')
lines.append('-' * 65)

one_liners = [
    ('15d', 'AXSM', '$200C May-15 @ $10.00',  '13.2x', 'P=65%',    'OI=2,257', 'Apr 30 CERTAIN    | BEST ENTRY | Grade D'),
    ('30d', 'RGNX', '$10C  Jul-17 @ $1.50',   '4.5x',  'P=68%',    'OI=1,509', 'BLA mid-2026      | Gene therapy'),
    ('30d', 'IDYA', '$35C  May-15 @ $1.62',   '14.3x', 'P=75%',    'OI=10,912','NDA H2 2026       | PFS positive Apr 13'),
    ('30d', 'AGIO', '$30P  Aug-21 @ $2.73',   '6.7x',  'P(f)=94%', 'OI=691',   'May-Jun 2026      | Grade F confirms put'),
    ('30d', 'NTLA', '$15C  Jul-17 @ $2.60',   '4.5x',  'P=82%',    'OI=1,774', 'May-Jun 2026      | NEJM data pub'),
    ('61d', 'MLTX', '$21C  Aug-21 @ $3.80',   '3.4x',  'P=100%',   'OI=1,056', 'Jun-Jul 2026      | Grade B'),
    ('63d', 'RVMD', '$125C Jun-18 @ $19.60',  '7.1x',  'P=90%',    'OI=46',    'AACR + NDA H2     | Phase 3 winner +150%'),
    ('76d', 'VRDN', '$20C  Jul-17 @ $1.95',   '3.8x',  'P=72%',    'OI=173',   'Jun 30 CERTAIN    | NEW TODAY'),
    ('83d', 'VERA', '$55C  Jan-27 @ $8.05',   '3.1x',  'P=72%',    'OI=19',    'Jul 7 CERTAIN     | small size'),
    ('165d','PRAX', '$300C Jan-27 @ $103.53', '3.1x',  'P=70%',    'OI=0*',    'Sep 27 CERTAIN    | ITM by $43.60 | verify OI at open'),
]

for days, ticker, strike_str, mult, prob, oi_str, note in one_liners:
    lines.append(f'{days} | {ticker}  {strike_str} -- {mult} | {prob} | {oi_str} | {note}')

lines.append('')
lines.append('BELOW THRESHOLD:')
lines.append('  ARGX $800/$850C May15 -- 0.5x fresh (dead). Hold existing through May 10 PDUFA (25 days).')
lines.append('')
lines.append('=' * 65)
lines.append('SCIENCE GRADES')
lines.append('-' * 65)
lines.append('Grade B -- MLTX: RCT triple-blind N=960 ACR50.')
lines.append('Grade C -- IDYA, RVMD, NTLA, VERA, VRDN, PRAX: Adequate design.')
lines.append('Grade D -- AXSM (PDUFA bet). ARGX (prior data strong, hold).')
lines.append('Grade F -- RGNX (gene therapy standard). AGIO (confirms put thesis).')
lines.append('')
lines.append('=' * 65)
lines.append('POSITION UPDATES')
lines.append('-' * 65)
lines.append('')
lines.append('RVMD ($125C Jun18):')
lines.append('  Entry: ~$7-8 | Current: $19.60 | Gain: ~+150%')
lines.append('  Stock: $147.01 (+$3.21 yesterday, +$18 since Apr 13 announcement)')
lines.append('  AACR Apr 17-22: 9 presentations. Each could catalyze further upside.')
lines.append('  Action: consider partial profit-taking or hold through NDA filing.')
lines.append('')
lines.append('PRAX ($300C Jan27):')
lines.append('  Entry: $79 | Current: $103.53 | Gain: +31%')
lines.append('  Stock: $343.60. $300C is now ITM by $43.60.')
lines.append('  166 days to expiry. Sep 27 PDUFA confirmed. Let it run.')
lines.append('  Note: OI=0 pre-market -- verify real OI at market open (was 62 yesterday).')
lines.append('')
lines.append('AXSM ($200C May15):')
lines.append('  Entry: ~$7.70 | Current: $10.00 | Gain: +30%')
lines.append('  OI tripled to 2,257 = institutional accumulation.')
lines.append('  15 days to Apr 30 PDUFA. Pre-PDUFA drift typically accelerates in final 2 weeks.')
lines.append('')
lines.append('ARGX ($800/$850C May15):')
lines.append('  Entry: ~$12 | Current spread value: ~$28.35 | Theoretical gain: +136%')
lines.append('  Stock $828.35. Lower leg ($800C) deep ITM, upper leg ($850C) $21.65 OTM.')
lines.append('  May 10 PDUFA = 25 days. Existing position worth holding.')
lines.append('  Option: exit now for +$16 profit OR hold for max payout if stock breaks $850.')
lines.append('')
lines.append('VRDN ($20C Jul17) -- NEW:')
lines.append('  Fresh entry: $1.95 (last trade). OI=173. Bid=$0 pre-mkt only.')
lines.append('  Stock $15.19. $20C is $4.81 OTM. Jun 30 PDUFA = 76 days.')
lines.append('  If approved: stock could 2x to $27+. Intrinsic $7 / $1.95 = 3.6x.')
lines.append('  Use limit orders at $1.95 or below at market open.')
lines.append('')
lines.append('IDYA ($35C May15):')
lines.append('  Entry: $5.35 | Current: $1.62 | Loss: -70%')
lines.append('  NDA filing announcement = next catalyst. 30 days left.')
lines.append('  High math multiple (14.3x) on small remaining premium = high risk/reward.')
lines.append('  Consider rolling to Sep18 or Jan27 strike for more time if conviction holds.')

body = '\n'.join(lines)
with open(BASE/'reports/email_body_2026-04-15.txt','w') as f:
    f.write(body)
print(f'Email: {len(body)} chars')
print('Done.')
