#!/usr/bin/env python3
import sys, json, datetime, traceback, requests

# Load .env before paths.py reads BIOTECH_SNIPER_HOME, so audit.py invoked
# directly via `python -m biotech_sniper.audit` (without sourcing .env in
# the shell) writes its JSON to <project>/state/, not <package>/state/.
try:  # pragma: no cover - best-effort; dotenv is a hard dep but imports must not crash
    from pathlib import Path as _PathForEnv
    from dotenv import load_dotenv as _load_dotenv  # type: ignore[import-not-found]
    _here = _PathForEnv(__file__).resolve().parent
    for _cand in (_here.parent / '.env', _here.parent.parent / '.env'):
        if _cand.is_file():
            _load_dotenv(dotenv_path=str(_cand), override=False)
            break
except Exception:
    pass

from biotech_sniper.paths import BASE_DIR

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'intelligence'))
sys.path.insert(0, str(BASE_DIR / 'sectors/contracts'))
sys.path.insert(0, str(BASE_DIR / 'sectors/adcom'))
sys.path.insert(0, str(BASE_DIR / 'sectors'))

failures = []
warnings = []
sources: dict = {}
today = datetime.date.today().isoformat()
print(f'FULL RUNTIME AUDIT — {today}')
print('='*60)

# ── 1. ClinicalTrials.gov ───────────────────────────────────
print('\n[1] ClinicalTrials.gov API')
try:
    r = requests.get(
        'https://clinicaltrials.gov/api/v2/studies?filter.advanced=AREA[Phase]PHASE3+AND+AREA[OverallStatus]ACTIVE_NOT_RECRUITING&pageSize=5&sort=LastUpdatePostDate',
        timeout=10)
    studies = r.json().get('studies', [])
    print(f'  Status: {r.status_code} | Studies: {len(studies)}')
    if studies:
        print(f'  Sample: {studies[0].get("protocolSection",{}).get("identificationModule",{}).get("briefTitle","")[:60]}')
    print('  OK')
    sources['clinicaltrials_gov'] = {'ok': r.status_code == 200, 'status': r.status_code, 'studies': len(studies)}
except Exception as e:
    failures.append(f'ClinicalTrials: {e}'); print(f'  FAIL: {e}')
    sources['clinicaltrials_gov'] = {'ok': False, 'error': str(e)}

# ── 2. Warpspeed.sh ─────────────────────────────────────────
print('\n[2] Warpspeed.sh')
try:
    r = requests.get('https://warpspeed.sh/', timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
    has_data = any(kw in r.text.lower() for kw in ['experiment', 'ideaya', 'phase', 'trial'])
    print(f'  Status: {r.status_code} | Length: {len(r.text):,} | Experiment data: {has_data}')
    if not has_data:
        warnings.append('Warpspeed: no experiment data in raw HTML — JS-rendered, needs browser_task in cron')
        print('  WARNING: JS-rendered, browser_task needed in cron (expected)')
    else:
        print('  OK')
except Exception as e:
    failures.append(f'Warpspeed: {e}'); print(f'  FAIL: {e}')

# ── 3. SEC EDGAR ────────────────────────────────────────────
print('\n[3] SEC EDGAR RSS + CIK file')
try:
    import feedparser
    r = requests.get(
        'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=10&search_text=&output=atom',
        headers={'User-Agent': 'BioCatalystBot research@mantisvc.com'}, timeout=12)
    feed = feedparser.parse(r.text)
    print(f'  RSS: {r.status_code} | Entries: {len(feed.entries)}')
    r2 = requests.get('https://www.sec.gov/files/company_tickers.json',
                      headers={'User-Agent': 'BioCatalystBot research@mantisvc.com'}, timeout=12)
    tickers_data = r2.json()
    print(f'  CIK file: {r2.status_code} | Companies: {len(tickers_data):,}')
    print('  OK')
    sources['sec_edgar'] = {'ok': r.status_code == 200 and r2.status_code == 200,
                            'rss_status': r.status_code, 'cik_status': r2.status_code,
                            'rss_entries': len(feed.entries), 'companies': len(tickers_data)}
except Exception as e:
    failures.append(f'SEC: {e}'); print(f'  FAIL: {e}')
    sources['sec_edgar'] = {'ok': False, 'error': str(e)}

# ── 4. USASpending.gov ──────────────────────────────────────
print('\n[4] USASpending.gov awards')
try:
    payload = {
        'filters': {
            'time_period': [{'start_date': '2026-03-01', 'end_date': today}],
            'award_type_codes': ['A', 'B', 'C', 'D'],
            'keywords': ['Rocket Lab']
        },
        'fields': ['Award ID', 'Recipient Name', 'Award Amount', 'Awarding Agency', 'Description'],
        'limit': 5, 'page': 1, 'sort': 'Award Amount', 'order': 'desc'
    }
    r = requests.post('https://api.usaspending.gov/api/v2/search/spending_by_award/',
                      json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
    awards = r.json().get('results', [])
    print(f'  Status: {r.status_code} | Awards: {len(awards)}')
    for a in awards[:2]:
        print(f'    {a.get("Recipient Name","?")} | ${float(a.get("Award Amount",0)):,.0f} | {a.get("Awarding Agency","?")[:40]}')
    print('  OK')
except Exception as e:
    failures.append(f'USASpending: {e}'); print(f'  FAIL: {e}')

# ── 5. Defense.gov RSS ──────────────────────────────────────
print('\n[5] Defense.gov contract RSS')
try:
    r = requests.get('https://www.defense.gov/News/Contracts/rss/',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
    print(f'  Status: {r.status_code} | Length: {len(r.text):,}')
    if r.status_code == 200:
        feed = feedparser.parse(r.text)
        print(f'  Entries: {len(feed.entries)}')
        if feed.entries:
            print(f'  Sample: {feed.entries[0].get("title","")[:80]}')
            print('  OK')
        else:
            warnings.append('Defense.gov: 0 entries parsed from RSS'); print('  WARNING: 0 entries')
    else:
        failures.append(f'Defense.gov: HTTP {r.status_code}'); print(f'  FAIL: {r.status_code}')
except Exception as e:
    failures.append(f'Defense.gov: {e}'); print(f'  FAIL: {e}')

# ── 6. FDA news RSS (press releases) ────────────────────────
# The legacy advisory-committee-meetings-coming-soon.rss endpoint now 404s;
# use the FDA press-releases RSS as the canonical "news_rss" health source.
print('\n[6] FDA press-releases RSS')
try:
    r = requests.get('https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
    feed = feedparser.parse(r.text)
    print(f'  Status: {r.status_code} | Entries: {len(feed.entries)}')
    if feed.entries:
        print(f'  Sample: {feed.entries[0].get("title","")[:80]}')
    print('  OK')
    sources['news_rss'] = {'ok': r.status_code == 200, 'status': r.status_code,
                           'entries': len(feed.entries), 'feed': 'fda_press_releases_rss'}
except Exception as e:
    failures.append(f'FDA RSS: {e}'); print(f'  FAIL: {e}')
    sources['news_rss'] = {'ok': False, 'error': str(e)}

# ── 7. BiopharmCatalyst ─────────────────────────────────────
print('\n[7] BiopharmCatalyst AdCom')
try:
    r = requests.get('https://www.biopharmcatalyst.com/calendars/adcom-calendar',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
    has_adcom = 'advisory' in r.text.lower() or 'adcom' in r.text.lower()
    print(f'  Status: {r.status_code} | Length: {len(r.text):,} | AdCom data: {has_adcom}')
    if not has_adcom:
        warnings.append('BiopharmCatalyst: JS-rendered — no table data in raw HTML, browser_task needed')
        print('  WARNING: JS-rendered, browser_task needed in cron (expected)')
    else:
        print('  OK')
except Exception as e:
    failures.append(f'BiopharmCatalyst: {e}'); print(f'  FAIL: {e}')

# ── 8. IDYA IR page live signal check ───────────────────────
print('\n[8] IDEAYA IR live check')
try:
    r = requests.get('https://ir.ideayabio.com/events',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
    has_reg     = 'register' in r.text.lower()
    has_topline = any(kw in r.text.lower() for kw in ['topline', 'top-line', 'phase 3', 'optimum'])
    print(f'  Status: {r.status_code} | Pre-reg: {has_reg} | Topline content: {has_topline}')
    if has_reg and has_topline:
        print('  *** SIGNAL: webcast pre-reg detected on IDYA IR page ***')
    print('  OK')
except Exception as e:
    failures.append(f'IDYA IR: {e}'); print(f'  FAIL: {e}')

# ── 9. Twitter via DDG ──────────────────────────────────────
print('\n[9] Twitter via DuckDuckGo')
try:
    r = requests.post('https://html.duckduckgo.com/html/',
                      data={'q': 'site:twitter.com BioPharmCatalyst PDUFA 2026'},
                      headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
    has_results = 'result__a' in r.text and len(r.text) > 3000
    print(f'  Status: {r.status_code} | Length: {len(r.text):,} | Results: {has_results}')
    if not has_results:
        warnings.append('Twitter/DDG: unreliable HTML scraping — cron must use search_web tool instead')
        print('  WARNING: DDG not returning results — cron uses search_web tool (expected)')
    else:
        print('  OK')
except Exception as e:
    warnings.append(f'Twitter/DDG: {e}')
    print(f'  WARNING: {e}')

# ── 10. master_discovery full run ───────────────────────────
print('\n[10] master_discovery.run_discovery()')
try:
    from intelligence.master_discovery import run_discovery
    result = run_discovery()
    biotech   = len(result.get('new_biotech', []))
    contracts = len(result.get('new_contracts', []))
    adcom     = len(result.get('new_adcom', []))
    defense   = len(result.get('defense_rss', []))
    signals   = len(result.get('signals', []))
    print(f'  biotech={biotech} contracts={contracts} adcom={adcom} defense={defense} signals={signals}')
    print('  OK')
except Exception as e:
    failures.append(f'master_discovery: {e}'); print(f'  FAIL: {e}'); traceback.print_exc()

# ── 11. twitter build_queries ───────────────────────────────
print('\n[11] twitter build_twitter_search_queries()')
try:
    from intelligence.twitter_biotech_monitor import build_twitter_search_queries, parse_twitter_results
    queries = build_twitter_search_queries()
    print(f'  Queries generated: {len(queries)}')
    print(f'  Sample: {queries[0] if queries else "NONE"}')
    # parse_twitter_results with dummy data
    dummy = [{'title': 'IDYA topline positive phase 3 optimum', 'snippet': 'IDEAYA hits primary endpoint', 'url': 'https://x.com/test'}]
    sigs = parse_twitter_results(dummy)
    print(f'  parse_twitter_results (dummy): {len(sigs)} signals')
    print('  OK')
except Exception as e:
    failures.append(f'twitter_monitor: {e}'); print(f'  FAIL: {e}'); traceback.print_exc()

# ── 12. adcom drug_ticker_map ───────────────────────────────
print('\n[12] adcom build_drug_ticker_map()')
try:
    from sectors.adcom.adcom_scanner import build_drug_ticker_map, run_adcom_scan
    drug_map = build_drug_ticker_map()
    print(f'  Drug entries: {len(drug_map)}')
    # Check a known drug
    axs05 = drug_map.get('AXS-05', drug_map.get('axs-05', {}))
    print(f'  AXS-05 lookup: {axs05}')
    print('  OK')
except Exception as e:
    failures.append(f'adcom drug_map: {e}'); print(f'  FAIL: {e}'); traceback.print_exc()

# ── 13. contracts defense.gov fetch ─────────────────────────
print('\n[13] sam_sniper.fetch_defense_gov_contracts()')
try:
    from sectors.contracts.sam_sniper import fetch_defense_gov_contracts, run_contract_scan
    contracts = fetch_defense_gov_contracts(days_back=7)
    print(f'  Contracts: {len(contracts)}')
    if contracts:
        print(f'  Sample: {contracts[0].get("title","")[:60]}')
    print('  OK')
except Exception as e:
    failures.append(f'defense_contracts: {e}'); print(f'  FAIL: {e}'); traceback.print_exc()

# ── 14. company_ticker_map aliases ──────────────────────────
print('\n[14] company_ticker_map aliases check')
try:
    import json
    cmap = json.load(open(BASE_DIR / 'sectors/contracts/company_ticker_map.json'))
    companies = cmap.get('companies', {})
    has_aliases = sum(1 for v in companies.values() if v.get('aliases'))
    has_options = sum(1 for v in companies.values() if 'options' in v)
    has_cik     = sum(1 for v in companies.values() if v.get('sec_cik'))
    print(f'  Companies: {len(companies)} | With aliases: {has_aliases} | With options flag: {has_options} | With CIK: {has_cik}')
    if has_aliases < 10:
        failures.append(f'company_ticker_map: only {has_aliases} companies have aliases (need full expansion)')
    else:
        print('  OK')
except Exception as e:
    failures.append(f'ticker_map: {e}'); print(f'  FAIL: {e}')

# ── 15. active_plays seed check ─────────────────────────────
# f-m1-03 moved the runtime state JSONs to migrations/seed/. The runtime
# active_plays SQLite path is introduced in M2; until then, validate the
# seed JSON shape so M2 has a clean source to backfill from.
print('\n[15] active_plays.json health (seed)')
try:
    seed_candidates = [
        BASE_DIR / 'migrations/seed/active_plays.json',          # VPS layout (BASE_DIR=repo root)
        BASE_DIR.parent / 'migrations/seed/active_plays.json',    # local layout (BASE_DIR=package dir)
    ]
    seed_path = next((p for p in seed_candidates if p.is_file()), None)
    if seed_path is None:
        # Not a hard failure: the seed file is only needed by the M2 backfill;
        # M1 audit should not flag it as broken.
        warnings.append('active_plays: migrations/seed/active_plays.json not found (expected for fresh checkouts)')
        print('  WARNING: migrations/seed/active_plays.json not found (expected for fresh checkouts)')
    else:
        plays = json.load(open(seed_path))
        active = plays.get('active', {})
        required_fields = ['ticker', 'direction', 'p_success', 'estimated_announcement', 'option_expiry']
        for ticker, play in active.items():
            missing = [f for f in required_fields if not play.get(f) and play.get('direction') != 'EQUITY_ONLY']
            if missing:
                print(f'  WARNING {ticker}: missing {missing}')
        print(f'  Source: {seed_path}')
        print(f'  Active: {len(active)} | Monitor: {len(plays.get("monitor",{}))}')
        print('  OK')
except Exception as e:
    failures.append(f'active_plays: {e}'); print(f'  FAIL: {e}')

print()
print('='*60)
print(f'FAILURES ({len(failures)}):')
for f in failures:
    print(f'  FAIL: {f}')
print(f'WARNINGS ({len(warnings)}):')
for w in warnings:
    print(f'  WARN: {w}')
if not failures:
    print('\nALL CRITICAL TESTS PASS')

# ── Write structured audit JSON to state/audit_latest.json ──
try:
    state_dir = BASE_DIR / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    audit_path = state_dir / 'audit_latest.json'
    audit_data = {
        'as_of_date': today,
        'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'sources': sources,
        'failures': failures,
        'warnings': warnings,
    }
    with open(audit_path, 'w') as f:
        json.dump(audit_data, f, indent=2, sort_keys=True)
    print(f'\nAudit JSON written to {audit_path}')
except Exception as e:
    print(f'\nWARNING: failed to write audit JSON: {e}')
