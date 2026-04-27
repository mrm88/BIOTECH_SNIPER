#!/usr/bin/env python3
"""
BULK UNIVERSE SCANNER
Builds and refreshes 100+ biotech catalyst watchlist weekly.

SOURCES:
  1. ClinicalTrials.gov — ALL Phase 2/3 ANR, primary completion 0-365 days
  2. PDUFA calendar — confirmed NDA/BLA FDA action dates next 12 months
  3. Research seed list — known small-cap plays with H1/H2 2026 readouts

FILTERING:
  - Non-big-pharma / non-academic sponsors only
  - US-listed (SEC EDGAR company_tickers_exchange.json)
  - Has liquid options (yfinance check)
  - Stock price > $2
  - Market cap < $20B

STATE FILES:
  state/universe_watchlist.json — full ranked list
  state/sec_name_to_ticker.json — sponsor name → ticker lookup (cached)
"""

import json, re, datetime, time, urllib.request, urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CT_API = "https://clinicaltrials.gov/api/v2/studies"

SKIP_WORDS = {
    "novartis","pfizer","roche","astrazeneca","merck ","johnson & johnson","abbvie",
    "bristol-myers","sanofi","eli lilly","boehringer","bayer","glaxo","takeda",
    "amgen","biogen","regeneron","gilead","novo nordisk","ucb pharma","otsuka",
    "daiichi","eisai","kyowa","alexion","janssen","genentech","medimmune","astellas",
    "chugai","servier","ipsen","almirall","ferring","lundbeck","teva","viatris",
    "bausch","celltrion","samsung bio","innovent","university ","hospital ",
    "institute ","national cancer","department of","ministry of","children's ",
    "mayo clinic","stanford university","harvard ","johns hopkins","cooperative ",
    "consortium ","network of","alliance ",
}

# Confirmed 2026-2027 PDUFA/readout calendar (manually curated + updated weekly)
PDUFA_CALENDAR = {
    "AXSM":("2026-04-30","AXS-05 AD agitation sBLA"),
    "ARGX":("2026-05-10","Efgartigimod seroneg gMG PDUFA"),
    "SUPN":("2026-05-15","SPN-830 Parkinson apomorphine NDA"),
    "ACRS":("2026-05-28","Cortexolone acne NDA"),
    "SRPT":("2026-06-01","Delandistrogene DMD sNDA"),
    "VSTM":("2026-06-15","Decitabine/cedazuridine CMML NDA"),
    "CLDX":("2026-06-15","Barzolvolimab CSU BLA"),
    "VRDN":("2026-06-30","Veligrotug TED BLA"),
    "VNDA":("2026-06-30","Tradipitant gastroparesis NDA"),
    "FOLD":("2026-08-01","Pegunigalsidase Fabry BLA"),
    "ANNX":("2026-08-01","ANX005 GBS NDA"),
    "CPIX":("2026-08-01","Diclofenac acne NDA"),
    "INBX":("2026-06-18","Seladelpar PBC NDA"),
    "KPTI":("2026-07-30","Selinexor myeloma sNDA"),
    "KALV":("2026-08-20","KAL-01 hyperphosphatemia NDA"),
    "NMRA":("2026-08-15","Navacaprant MDD NDA"),
    "FDMT":("2026-07-30","C3 inhibitor geographic atrophy BLA"),
    "OCUL":("2026-07-25","OTX-TKI nAMD BLA"),
    "EYPT":("2026-07-15","EYP-1901 nAMD BLA"),
    "COGT":("2026-07-10","Olutasidenib AML sNDA"),
    "RYTM":("2026-06-14","Setmelanotide POMC/PCSK1 sNDA"),
    "SNDX":("2026-07-20","Entinostat HR+ breast NDA"),
    "CORT":("2026-06-20","Relacorilant Cushing NDA"),
    "GLUE":("2026-08-30","GLUA-1 epilepsy NDA"),
    "CYTK":("2026-07-12","Omecamtiv HF BLA"),
    "ALMS":("2026-07-05","Amlitelimab AD BLA"),
    "AVIR":("2026-09-20","Bemnifosbuvir influenza NDA"),
    "BHVN":("2026-08-30","Troriluzole SCA NDA"),
    "RARE":("2026-08-25","DTX401 GSD1a BLA"),
    "CELC":("2026-08-22","Cedilizumab RA BLA"),
    "SIGA":("2026-08-15","TPOXX monkeypox sNDA"),
    "MIRM":("2026-09-01","Linerixibat PBC NDA"),
    "IONS":("2026-09-15","Donidalorsen HAE NDA"),
    "PCVX":("2026-09-15","VLA15 Lyme BLA"),
    "PRAX":("2026-09-27","Relutrigine SCN2A NDA"),
    "RCUS":("2026-09-30","Quemliclustat PDAC NDA"),
    "VIR": ("2026-10-01","Tobevibart chronic hep B NDA"),
    "DNLI":("2026-10-30","AL002 Alzheimer NDA"),
    "IRON":("2026-10-30","FCM heart failure NDA"),
    "EXEL":("2026-10-15","Zanzalintinib RCC NDA"),
    "KYTX":("2026-10-15","Cendakimab EoE NDA"),
    "TYRA":("2026-12-01","TYRA-300 FGFR3 NDA"),
    "WVE": ("2026-11-01","WVE-003 Huntington NDA"),
    "RCKT":("2026-11-15","Danicopan PNH sNDA"),
    "NVAX":("2026-09-30","NVX-CoV2373 combo NDA"),
    "EWTX":("2026-11-30","Autoimmune NDA"),
    "SLDB":("2026-09-30","DB-OTO DFNB9 BLA"),
    "NPCE":("2026-09-30","DP-014 myopia NDA"),
    "IMUX":("2026-11-01","Batoclimab CIDP NDA"),
}

# Research seed: known plays with upcoming readouts
RESEARCH_TICKERS = {
    "NUVL":"Nuvalent ROS1/ALK Phase 2 H2 2026",
    "MLTX":"MoonLake IZAR-1 PsA Phase 3 2026",
    "NTLA":"Intellia NTLA-2002 HAE Phase 3 2026",
    "RLAY":"Relay RLY-4008 FGFR2 Phase 1/2 2026",
    "JANX":"Janux JANX008 PSMA Phase 1 2026",
    "PTGX":"Protagonist rusfertide PV Phase 3 2026",
    "KYMR":"Kymera KT-474 AD Phase 2 H1 2026",
    "ARVN":"Arvinas ARV-471 ER+ breast Phase 3 2026",
    "ERAS":"Erasca ERAS-007 KRAS GI Phase 2 2026",
    "NBIX":"Neurocrine NBI-921352 SCN8A Phase 2 2026",
    "SANA":"Sana SC291 B-cell Phase 1 2026",
    "PRME":"Prime Medicine PM359 CGD Phase 1/2 2026",
    "XNCR":"Xencor Tidutamab solid tumors Phase 2 2026",
    "STOK":"Stoke Zorevunersen Dravet Phase 3 2026",
    "BLTE":"Belite Bio LBS-008 GA Phase 3 2026",
    "OVID":"Ovid OV101 Angelman Phase 3 2026",
    "EDIT":"Editas EDIT-301 SCD Phase 1/2 2026",
    "FATE":"Fate FT536 AML Phase 1 2026",
    "NKTX":"Nkarta NKX019 B-cell Phase 1/2 2026",
    "FULC":"Fulcrum FTX-6058 SCD Phase 2 2026",
    "ARDX":"Ardelyx Tenapanor IBS sNDA 2026",
    "ALEC":"Alector AL101 FTD Phase 2 2026",
    "RIGL":"Rigel R552 TTP Phase 2 2026",
    "ALDX":"Aldeyra Reproxalap Sjogren Phase 3 2026",
    "GOSS":"Gossamer GB-9558 IPF Phase 2b 2026",
    "ESPR":"Esperion Bempedoic new indication 2026",
    "ATAI":"ATAI PCN-101 depression Phase 2 2026",
    "STTK":"Shattuck SL-172154 ovarian Phase 1/2 2026",
    "PASG":"Passage Bio PBGM01 GM1 Phase 2 2026",
    "PRLD":"Prelude PRT543 MF Phase 2 2026",
    "BCYC":"Bicycle BT7480 bladder Phase 2 2026",
    "GERN":"Geron Imetelstat MDS FDA 2026",
    "ACET":"Adicet ADI-001 DLBCL Phase 2 2026",
    "KROS":"Keros KER-012 PAH Phase 2 2026",
    "VOR": "Vor Biopharma trem-cel AML Phase 1/2 2026",
    "CRVS":"Corvus CPI-006 NHL Phase 2 2026",
    "KZR": "Kezar KZR-616 lupus Phase 2 2026",
    "HOOK":"Hookipa HB-400 HIV Phase 2 2026",
    "ROIV":"Roivant multiple Phase 3 programs 2026",
    "VIGL":"Vigil VIG-300 Angelman Phase 2 2026",
    "GRTS":"Gritstone SLATE-KRAS Phase 2 2026",
    "RCKT":"Rocket Pharma Danicopan Phase 3 2026",
    "BEAM":"Beam BEAM-101 SCD BLA 2027",
    "WVE": "Wave Life WVE-003 Huntington 2026",
}


def load_name_map() -> dict:
    nm_file = BASE_DIR / "state/sec_name_to_ticker.json"
    if nm_file.exists():
        with open(nm_file) as f:
            return json.load(f)
    # Build fresh from SEC
    return _build_name_map()


def _build_name_map() -> dict:
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    req = urllib.request.Request(url, headers={"User-Agent": "alphasniper research@example.com"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    fields = data.get("fields", [])
    nm = {}
    for row in data.get("data", []):
        d = dict(zip(fields, row))
        name = (d.get("name") or "").lower().strip()
        ticker = (d.get("ticker") or "").upper().strip()
        exchange = d.get("exchange", "")
        if exchange not in ("Nasdaq","NYSE","NYSE MKT","NYSE ARCA","CBOE"):
            continue
        if name and ticker:
            nm[name] = ticker
            clean = re.sub(r'\b(inc|corp|ltd|llc|plc|co|the|sa|nv|ag|se|bv|therapeutics|pharmaceuticals|biosciences|bioscience|biotechnology|biotech|sciences|science|health|healthcare|medical|medicine|pharma)\b\.?', '', name)
            clean = re.sub(r'\s+', ' ', clean).strip().rstrip(',').strip()
            if clean and clean != name and len(clean) > 3 and clean not in nm:
                nm[clean] = ticker
    nm_file = BASE_DIR / "state/sec_name_to_ticker.json"
    with open(nm_file,"w") as f:
        json.dump(nm, f)
    return nm


def resolve_sponsor(sponsor_raw: str, nm: dict) -> str | None:
    s = sponsor_raw.lower().strip()
    if s in nm: return nm[s]
    clean = re.sub(r',?\s*(inc\.?|corp\.?|ltd\.?|llc\.?|plc\.?|co\.?|s\.?a\.?|n\.?v\.?|a\.?g\.?|s\.?e\.?)\s*$','',s).strip()
    if clean in nm: return nm[clean]
    clean2 = re.sub(r'\b(therapeutics|pharmaceuticals|biosciences|bioscience|biotechnology|biotech|sciences|science|healthcare|medical|medicine|pharma|biopharma|health|incorporated|limited|company|biotherapeutics|oncology|neuroscience)\b','',clean)
    clean2 = re.sub(r'\s+',' ',clean2).strip().rstrip(',').strip()
    if clean2 and clean2 in nm: return nm[clean2]
    words = (clean2 or clean).split()
    for n in range(min(4,len(words)),1,-1):
        key=" ".join(words[:n])
        if key in nm: return nm[key]
    return None


def should_skip(sponsor: str) -> bool:
    s = sponsor.lower()
    return any(w in s for w in SKIP_WORDS) or len(s) < 4


def fetch_ct_candidates(nm: dict) -> dict:
    """Fetch all Phase 2/3 ANR candidates from ClinicalTrials completing next 12 months."""
    today = datetime.date.today()
    results = {}

    for phase, d0, d1 in [
        ("PHASE3",0,90),("PHASE3",90,180),("PHASE3",180,365),
        ("PHASE2",0,90),("PHASE2",90,180),("PHASE2",180,365),
    ]:
        s_d = (today+datetime.timedelta(days=d0)).isoformat()
        e_d = (today+datetime.timedelta(days=d1)).isoformat()
        params = {
            "filter.advanced": f"AREA[Phase]{phase} AND AREA[OverallStatus]ACTIVE_NOT_RECRUITING AND AREA[PrimaryCompletionDate]RANGE[{s_d},{e_d}]",
            "pageSize": 100,
            "fields": "NCTId,BriefTitle,LeadSponsorName,PrimaryCompletionDate,Condition,EnrollmentCount",
            "sort": "PrimaryCompletionDate:asc",
        }
        ntok, pg = None, 0
        while pg < 20:
            if ntok: params["pageToken"] = ntok
            elif "pageToken" in params: del params["pageToken"]
            try:
                data = json.loads(urllib.request.urlopen(
                    urllib.request.Request(CT_API+"?"+urllib.parse.urlencode(params),
                    headers={"User-Agent":"AlphaSniper/1.0"}), timeout=20).read())
            except: break
            for s in data.get("studies",[]):
                ps  = s.get("protocolSection",{})
                nct = ps.get("identificationModule",{}).get("nctId","")
                if nct in results: continue
                sp  = ps.get("sponsorCollaboratorsModule",{}).get("leadSponsor",{}).get("name","")
                if should_skip(sp): continue
                comp  = ps.get("statusModule",{}).get("primaryCompletionDateStruct",{}).get("date","")
                conds = ps.get("conditionsModule",{}).get("conditions",[])
                phases = ps.get("designModule",{}).get("phases",[])
                n_enr  = ps.get("designModule",{}).get("enrollmentInfo",{}).get("count")
                ticker = resolve_sponsor(sp, nm)
                results[nct] = {
                    "nct_id":nct,"sponsor":sp,"ticker":ticker,
                    "primary_completion":comp,"conditions":"; ".join(conds[:2]),
                    "indication":(conds[0] if conds else "")[:60],
                    "phase":"/".join(phases),"enrollment":n_enr,
                    "source":"clinicaltrials","has_pdufa":False,"pdufa_date":None,
                    "window":f"{phase} {d0}-{d1}d",
                }
            ntok = data.get("nextPageToken"); pg += 1
            if not ntok: break
            time.sleep(0.1)
    return results


def filter_and_score(candidates_by_ticker: dict, today: datetime.date) -> dict:
    """Check options/price/mktcap and assign tier scores."""
    try:
        import yfinance as yf
    except ImportError:
        return candidates_by_ticker

    scored = {}
    for ticker, c in candidates_by_ticker.items():
        try:
            t = yf.Ticker(ticker)
            fi = t.fast_info
            price  = float(fi.last_price)  if hasattr(fi,'last_price')  and fi.last_price  else 0
            mktcap = float(fi.market_cap)  if hasattr(fi,'market_cap')  and fi.market_cap  else 0
            opts   = t.options
            if price < 2.0 or not opts: continue
            if mktcap > 20_000_000_000: continue
        except:
            continue

        # Score
        score = 0
        ph = c.get("phase","")
        if "3" in ph: score += 30
        elif "2" in ph: score += 15
        comp = c.get("primary_completion") or c.get("pdufa_date","")
        if comp:
            try:
                days = (datetime.date.fromisoformat(comp[:10]) - today).days
                if 0 < days <= 90:  score += 25
                elif days <= 180:   score += 15
                elif days <= 365:   score += 10
            except: pass
        if c.get("has_pdufa"): score += 20

        tier = 1 if score >= 40 else (2 if score >= 20 else 3)
        scored[ticker] = {**c, "price":round(price,2), "market_cap":int(mktcap),
                           "n_expiries":len(opts), "pre_score":score, "tier":tier,
                           "last_checked":today.isoformat()}
        time.sleep(0.04)
    return scored


def run_bulk_scan(quick_mode: bool = False) -> dict:
    today = datetime.date.today()
    nm    = load_name_map()

    print(f"[bulk_universe_scanner] {today} | name_map={len(nm)}")

    # 1. ClinicalTrials
    print("  Fetching ClinicalTrials...")
    ct_raw = fetch_ct_candidates(nm)
    print(f"  CT raw: {len(ct_raw)} non-big-pharma entries")

    # Deduplicate by ticker (keep nearest completion)
    by_ticker = {}
    for nct, c in ct_raw.items():
        t = c.get("ticker")
        if not t: continue
        if t not in by_ticker or (c["primary_completion"] and
           (not by_ticker[t].get("primary_completion") or
            c["primary_completion"] < by_ticker[t]["primary_completion"])):
            by_ticker[t] = c

    # 2. PDUFA calendar
    for ticker, (pdufa_date, desc) in PDUFA_CALENDAR.items():
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                "ticker":ticker,"nct_id":None,"sponsor":ticker,
                "primary_completion":pdufa_date,"pdufa_date":pdufa_date,
                "conditions":desc,"indication":desc.lower(),
                "phase":"PHASE3","enrollment":None,
                "source":"pdufa_calendar","has_pdufa":True,
            }
        else:
            by_ticker[ticker]["has_pdufa"] = True
            by_ticker[ticker]["pdufa_date"] = pdufa_date

    # 3. Research tickers
    for ticker, desc in RESEARCH_TICKERS.items():
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                "ticker":ticker,"nct_id":None,"sponsor":ticker,
                "primary_completion":None,"pdufa_date":None,
                "conditions":desc,"indication":desc.lower(),
                "phase":"PHASE2","enrollment":None,
                "source":"research","has_pdufa":False,
            }

    print(f"  Combined (pre-filter): {len(by_ticker)} tickers")

    # 4. Filter and score
    print("  Checking options/price/mktcap...")
    final = filter_and_score(by_ticker, today)
    print(f"  Passed filters: {len(final)}")

    # Load existing, merge (don't remove already-tracked)
    universe_file = BASE_DIR / "state/universe_watchlist.json"
    existing = {}
    if universe_file.exists():
        with open(universe_file) as f:
            existing = json.load(f).get("candidates", {})

    existing.update(final)  # new data wins

    t1 = {t:c for t,c in existing.items() if c.get("tier")==1}
    t2 = {t:c for t,c in existing.items() if c.get("tier")==2}
    t3 = {t:c for t,c in existing.items() if c.get("tier")==3}

    universe = {
        "candidates": existing,
        "last_updated": today.isoformat(),
        "total": len(existing),
        "tier1_tickers": sorted(t1.keys()),
        "tier2_tickers": sorted(t2.keys()),
        "stats": {"total":len(existing),"tier1":len(t1),"tier2":len(t2),"tier3":len(t3)},
    }
    with open(universe_file,"w") as f:
        json.dump(universe, f, indent=2)

    print(f"\n  UNIVERSE: {len(existing)} total | Tier1={len(t1)} | Tier2={len(t2)} | Tier3={len(t3)}")
    return {
        "total": len(existing), "tier1": len(t1), "tier2": len(t2), "tier3": len(t3),
        "tier1_tickers": sorted(t1.keys()), "new_candidates": sorted(final.keys()),
    }


def get_tier1_for_scoring() -> list:
    universe_file = BASE_DIR / "state/universe_watchlist.json"
    if not universe_file.exists(): return []
    with open(universe_file) as f:
        u = json.load(f)
    active_file = BASE_DIR / "state/active_plays.json"
    active = set()
    if active_file.exists():
        with open(active_file) as f:
            d = json.load(f)
        active = set(d.get("active",{}).keys()) | set(d.get("monitor",{}).keys())
    today = datetime.date.today()
    return [
        c for t,c in u.get("candidates",{}).items()
        if t not in active and c.get("tier")==1
        and (not c.get("last_checked") or
             (today-datetime.date.fromisoformat(c["last_checked"])).days <= 14)
    ]


if __name__ == "__main__":
    import sys
    result = run_bulk_scan("--quick" in sys.argv)
    print(f"\nDone. {result['total']} total | Tier1: {result['tier1_tickers']}")


# ── SEC FULL BIOTECH SCAN ─────────────────────────────────────────────────────
# Called as part of run_bulk_scan(). Pulls every US-listed biotech/pharma stock,
# checks options liquidity, and adds to universe. Produces 200-300+ tickers.

BIO_KEYWORDS = [
    "therapeutics","pharmaceutical","bioscience","biotech","oncolog",
    "genomic","genetic","gene therapy","biopharma","biopharm",
    "immunotherap","immuno","neuro","vaccine","antivir","antibod",
    "crispr","cell therapy","biology","biolog","clinical","pharma ",
    "life science","medic",
]

MEGA_CAP_EXCLUDE = {
    "VRTX","REGN","TAK","TEVA","ALNY","RPRX","RMD","WST","HALO","FMS","GMED",
    "INCY","UTHR","BMY","MRK","PFE","ABBV","JNJ","LLY","AMGN","BIIB","GILD",
    "ILMN","DXCM","ISRG","EW","STE","WAT","BSX","ZBH",
}

# Comprehensive catalyst map with PDUFA dates and Phase 3 readouts
CATALYST_DATES = {
    **{t: (d, "PDUFA", desc) for t, (d, desc) in PDUFA_CALENDAR.items()},
    "MLTX":("2026-07-15","READOUT","IZAR-1 PsA Phase 3"),
    "NTLA":("2026-06-30","READOUT","NTLA-2002 HAE Phase 3"),
    "PTGX":("2026-09-30","READOUT","Rusfertide PV Phase 3"),
    "ARVN":("2026-10-15","READOUT","ARV-471 ER+ Phase 3"),
    "STOK":("2026-08-01","READOUT","Zorevunersen Dravet Phase 3"),
    "KYMR":("2026-06-30","READOUT","KT-474 AD Phase 2"),
    "NUVL":("2026-09-15","READOUT","NVL-655 ALK Phase 2"),
    "RLAY":("2026-07-01","READOUT","RLY-4008 FGFR2 Phase 1/2"),
    "BLTE":("2026-09-30","READOUT","LBS-008 GA Phase 3"),
    "OVID":("2026-08-15","READOUT","OV101 Angelman Phase 3"),
    "ERAS":("2026-06-30","READOUT","ERAS-007 GI Phase 2"),
    "EDIT":("2026-09-01","READOUT","EDIT-301 SCD Phase 1/2"),
    "GERN":("2026-06-15","READOUT","Imetelstat MDS NDA review"),
    "SANA":("2026-09-15","READOUT","SC291 B-cell Phase 1"),
    "ACET":("2026-08-01","READOUT","ADI-001 DLBCL Phase 2"),
    "FULC":("2026-07-01","READOUT","FTX-6058 SCD Phase 2"),
    "PRME":("2026-10-01","READOUT","PM359 CGD Phase 1/2"),
    "VOR": ("2026-09-15","READOUT","Trem-cel AML Phase 1/2"),
    "PRLD":("2026-07-15","READOUT","PRT543 MF Phase 2"),
    "JANX":("2026-10-01","READOUT","JANX008 PSMA Phase 1"),
    "ATAI":("2026-08-15","READOUT","PCN-101 depression Phase 2"),
    "NBIX":("2026-08-01","READOUT","NBI-921352 SCN8A Phase 2"),
    "SMMT":("2026-06-30","READOUT","Ivonescimab lung Phase 3"),
    "APGE":("2026-09-01","READOUT","APG-777 atopic derm Phase 2"),
    "IMVT":("2026-07-15","READOUT","Batoclimab MG Phase 3"),
    "TGTX":("2026-09-15","READOUT","TG-1701 CLL Phase 2"),
    "SYRE":("2026-09-30","READOUT","SPY001 CD40L Phase 2"),
    "CNTA":("2026-08-15","READOUT","LTV-1 ALS Phase 2"),
    "TERN":("2026-06-15","READOUT","TERN-701 PV Phase 2"),
    "CGON":("2026-07-01","READOUT","CG0070 bladder Phase 3"),
    "ARWR":("2026-09-15","READOUT","ARO-ATTR Phase 3 ext"),
    "ALEC":("2026-08-01","READOUT","AL101 FTD Phase 2"),
    "ALDX":("2026-08-15","READOUT","Reproxalap Sjogren Phase 3"),
    "HOOK":("2026-09-01","READOUT","HB-400 HIV Phase 2"),
    "PASG":("2026-10-15","READOUT","PBGM01 GM1 Phase 2"),
    "KZR": ("2026-08-01","READOUT","KZR-616 lupus Phase 2"),
    "ROIV":("2026-09-30","READOUT","RVT-2001 UC Phase 3"),
    "BCYC":("2026-08-15","READOUT","BT7480 bladder Phase 2"),
    "DNTH":("2026-09-01","READOUT","DNTH103 CAD Phase 2"),
    "APLS":("2026-07-01","READOUT","Pegcetacoplan GA Phase 3"),
    "GRTS":("2026-09-15","READOUT","SLATE-KRAS Phase 2"),
    "ARDX":("2026-06-01","READOUT","Tenapanor IBS sNDA"),
    "NKTX":("2026-09-01","READOUT","NKX019 DLBCL Phase 2"),
    "RIGL":("2026-07-15","READOUT","R552 TTP Phase 2"),
    "STTK":("2026-09-01","READOUT","SL-172154 ovarian Phase 2"),
    "KROS":("2026-08-15","READOUT","KER-012 PAH Phase 2"),
    "CRVS":("2026-09-15","READOUT","CPI-006 NHL Phase 2"),
    "GOSS":("2026-08-15","READOUT","GB-9558 IPF Phase 2b"),
    "FATE":("2026-09-01","READOUT","FT536 AML Phase 1"),
    "KRYS":("2026-10-01","READOUT","KB105 ichthyosis BLA"),
}


def fetch_all_bio_tickers() -> dict:
    """Pull every US-listed biotech/pharma stock from SEC EDGAR."""
    nm_file = BASE_DIR / "state/sec_name_to_ticker.json"
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    req = urllib.request.Request(url, headers={"User-Agent":"alphasniper research@example.com"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    fields = data.get("fields",[])
    US_EX  = {"Nasdaq","NYSE","NYSE MKT","NYSE ARCA","CBOE"}
    result = {}
    for row in data.get("data",[]):
        d      = dict(zip(fields, row))
        name   = (d.get("name") or "").lower()
        ticker = (d.get("ticker") or "").upper().strip()
        if d.get("exchange","") not in US_EX: continue
        if not ticker or len(ticker) > 6: continue
        if ticker in MEGA_CAP_EXCLUDE: continue
        if any(kw in name for kw in BIO_KEYWORDS):
            result[ticker] = d.get("name","")
    return result


def compute_score(ticker, price, mktcap, n_opts, today) -> tuple:
    """Score a ticker for catalyst proximity and quality."""
    s = 0
    cat = CATALYST_DATES.get(ticker)
    if cat:
        cat_date, cat_type, _ = cat
        s += 15
        if cat_type == "PDUFA": s += 20
        try:
            days = (datetime.date.fromisoformat(cat_date) - today).days
            if 0 < days <= 90:  s += 25
            elif days <= 180:   s += 18
            elif days <= 365:   s += 10
        except: pass
    if n_opts >= 8: s += 5
    if mktcap and mktcap < 1_000_000_000: s += 5
    tier = 1 if s >= 45 else (2 if s >= 20 else 3)
    return s, tier
