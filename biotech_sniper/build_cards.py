#!/usr/bin/env python3
import json, sys
sys.path.insert(0,'.'); sys.path.insert(0,'intelligence'); sys.path.insert(0,'sectors')

options_data = json.load(open('state/options_chains_2026-04-10.json'))
options_by_ticker = {r['ticker']: r for r in options_data}

science_grades = json.load(open('state/science_grades.json'))
science_by_ticker = {}
for k, v in science_grades.items():
    t = k.split('_')[0]
    science_by_ticker[t] = v

plays = json.load(open('state/active_plays.json'))
active = plays['active']

play_info = {
    'IDYA': {
        'drug': 'Darovasertib + Crizotinib (DAR-UM-2)',
        'thesis': 'First-ever 1L therapy for HLA-A2-neg uveal melanoma. DB lock imminent in April 1-15 window (day 10 of 15). OI surged 61% overnight to 10,708. Announcement expected via ir.ideayabio.com pre-reg before press release.',
        'edge': 'DB lock day 10 of 15. Pre-reg on IR page fires before press release. OI=10,708 showing significant smart money positioning. Market not pricing 96% probability correctly.',
    },
    'TVTX': {
        'drug': 'Sparsentan (FILSPARI) sNDA',
        'thesis': 'PDUFA in 3 days (April 13). FILSPARI already approved for IgAN. FSGS label expansion with strong Phase 3 DUPLEX data. Stock at $31.44 vs $35 strike -- PDUFA binary event imminent.',
        'edge': 'CERTAIN date in 3 days. Already approved drug, label expansion = lower FDA risk bar. Stock drift pre-PDUFA likely. Pre-market bids wide, use limit orders at mid.',
    },
    'RGNX': {
        'drug': 'RGX-202 (AAV8 micro-dystrophin gene therapy)',
        'thesis': 'AbbVie milestone $100M expected 1H 2026. All 4 pivotal-dose DMD patients beat NSAA trajectory +7.4pts vs cTAP at JPM. BLA mid-2026. Gene therapy for incurable disease.',
        'edge': 'Science Grade F flags non-randomized design but this is standard for gene therapy (FDA approved Elevidys and Zolgensma on single-arm data). AbbVie milestone is independent validation. Stock down 8% YTD vs data quality = mispriced.',
    },
    'AXSM': {
        'drug': 'AXS-05 (dextromethorphan + bupropion) sNDA',
        'thesis': 'CERTAIN Apr 30 PDUFA. Drug already FDA-approved for MDD (Auvelity). Agitation data: time to relapse significantly extended vs placebo. BTD + Priority Review. Stock +3.3% pre-market today to $178.90.',
        'edge': 'Not a trial result bet -- this is a label extension for an ALREADY APPROVED drug. FDA has seen all the data. Pre-PDUFA drift typically accelerates in final 2 weeks. 20 days remaining.',
    },
    'ARGX': {
        'drug': 'Efgartigimod alfa-fcab (Vyvgart) sBLA -- $800/$850 spread',
        'thesis': 'CERTAIN May 10 PDUFA. Efgartigimod already approved in seropositive gMG. Seroneg expansion same mechanism, new patient subset. Stock moved from $785 to $800.50 -- now at lower leg of spread.',
        'edge': 'Stock at exactly $800 strike. 30 days to PDUFA. Spread needs 6.2% more move to max payout. Current spread value $5.75 vs $12 entry -- potential to average down. Science Grade D flagged for open-label but prior data is very strong.',
    },
    'RVMD': {
        'drug': 'RMC-6236 (RAS(ON) multi-selective inhibitor)',
        'thesis': 'First RAS(ON) multi-selective inhibitor for KRAS/NRAS/HRAS tumors. Phase 1/2 data showed unprecedented responses in pancreatic cancer. RASolute-301 Phase 3 enrollment complete. NDA submission H2 2026.',
        'edge': 'KRAS was undruggable for decades. Only company with multi-RAS selectivity. Stock +150% since entry but still $30 below $125 strike. Phase 3 readout would be first-in-class landmark. OI=55 is thin -- use limit orders.',
    },
    'AGIO': {
        'drug': 'Tebapivat (AG-946) Phase 2b -- LONG PUTS',
        'thesis': 'P(fail)=94%. Phase 2b in lower-risk MDS uses wrong endpoint (8-week TI) for the hardest patient subset. Phase 2a worked only in easiest patients. Scientific mismatch means market is mispricing the downside.',
        'edge': 'Science Grade F is CONFIRMING the put thesis -- non-randomized, surrogate endpoint, wrong patient population. Best put setup in portfolio. Stock at $32.92. $30P Aug21 at $2.73 = 6.8x if stock goes to $11.53 (35%) on failure.',
    },
    'NTLA': {
        'drug': 'NTLA-2002 (CRISPR HAE prophylaxis) BLA',
        'thesis': 'HAELO enrollment complete Sep 2025. 3-year data published April 2 in NEJM: 96% attack reduction, 31/32 patients attack-free. BLA H2 2026 forces data disclosure by June. Most durable single-shot CRISPR data ever published.',
        'edge': 'Data ALREADY PUBLISHED -- 31/32 attack-free at 3 years. Market has not fully re-rated. BLA timeline means final topline must be disclosed before June. Window is closing. Stock still only $13.83 vs $15C strike.',
    },
    'MLTX': {
        'drug': 'Sonelokimab (anti-IL-17A/F nanobody) IZAR-1 Phase 3',
        'thesis': 'IZAR-1 PsA readout June-July. Warpspeed P=100% -- extremely rare consensus. Triple-blind placebo-controlled RCT, N=960. Science Grade B = best-designed trial in portfolio. Phase 2 ARGO data was compelling.',
        'edge': 'Warpspeed at 100% means the scientific community sees no path to failure. Grade B: ACR50 endpoint is FDA gold standard for PsA, large N, blinded design. Small cap ($500M) means approval triggers large institutional re-rating. Aug21 covers full window.',
    },
    'PRAX': {
        'drug': 'Relutrigine (PRAX-562) NDA -- Nav1.2/Nav1.6 blocker',
        'thesis': 'CERTAIN Sep 27 PDUFA. First targeted sodium channel blocker for SCN2A/SCN8A epilepsy. Approximately 5,000 patients in the US with no targeted treatment. FDA accepted NDA. Randomized placebo-controlled EMBOLD data.',
        'edge': 'Jan27 expiry provides 4-month buffer past PDUFA, allowing IV to build into the event. OI=62 but spread only 6% -- highly liquid for this size. Rare disease premium historically adds 20-40% to stock on approval. Stock at $320, 2x target $640 on approval.',
    },
}

cards = []
for ticker, info in play_info.items():
    opt = options_by_ticker.get(ticker, {})
    sci = science_by_ticker.get(ticker, {})
    play = active.get(ticker, {})
    card = {
        'ticker': ticker,
        'drug': info['drug'],
        'direction': play.get('direction', opt.get('direction','')),
        'p_success': play.get('p_success', opt.get('p_success',0)),
        'science_grade': sci.get('grade','?'),
        'catalyst': opt.get('catalyst', play.get('estimated_announcement','')),
        'strike': opt.get('strike', play.get('option_strike','')),
        'opt_type': opt.get('opt_type', play.get('option_type','')),
        'expiry': opt.get('expiry', play.get('option_expiry','')),
        'mid': opt.get('mid', 0),
        'oi': opt.get('oi', 0),
        'multiple': opt.get('multiple', 0),
        'k1': opt.get('k1', 0),
        'stock_price': opt.get('stock_price', 0),
        'why': info['thesis'],
        'edge': info['edge'],
    }
    cards.append(card)

json.dump(cards, open('state/play_cards_2026-04-10.json','w'), indent=2)
print(f'Generated {len(cards)} play cards')
for c in cards:
    mult = c.get('multiple',0)
    flag = ' ***' if mult >= 2.5 else ' [below threshold]'
    print(f'  {c["ticker"]}: {mult}x | Grade {c["science_grade"]} | P={c["p_success"]}%{flag}')
