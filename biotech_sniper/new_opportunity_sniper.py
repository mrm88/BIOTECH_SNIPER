#!/usr/bin/env python3
"""
NEW OPPORTUNITY SNIPER
Real-time discovery → scoring → options → alert pipeline.

Runs inside the hourly intraday scan. When a new candidate clears all filters,
fires an immediate email — not waiting for 6AM.

Pipeline per new candidate:
  1. Source: ClinicalTrials IMMINENT (60d) | SEC 8-K topline | BiopharmCatalyst new PDUFA | News RSS
  2. Resolve ticker via SEC lookup + Alpaca validation
  3. Check options exist + pull chain for correct expiry
  4. Dual-model score (Claude Opus + Gemini) using science-enriched prompt
  5. Calculate multiple at correct expiry (realistic mid-fill)
  6. Filter: P>=60% LONG or P<=40% PUT, multiple>=2.5x
  7. Fire immediate email with full play card

Dedup: alert key = "new_opp:{ticker}:{nct_or_pdufa_date}" — fires ONCE only.
"""

import json
import datetime
import requests
from pathlib import Path
from typing import Optional

from biotech_sniper.paths import BASE_DIR
ACTIVE_FILE    = BASE_DIR / "state/active_plays.json"
LOG_FILE       = BASE_DIR / "state/intraday_log.json"
SNIPER_FILE    = BASE_DIR / "state/new_opp_sniper.json"  # dedup + history

CT_HEADERS = {"User-Agent": "BioCatalystBot research@mantisvc.com", "Accept": "application/json"}
MIN_MULTIPLE   = 2.5
MIN_P_LONG     = 60
MAX_P_PUT      = 40


# ── STATE ─────────────────────────────────────────────────────────────────────

def load_sniper_state() -> dict:
    if SNIPER_FILE.exists():
        with open(SNIPER_FILE) as f:
            return json.load(f)
    return {"alerted_keys": [], "found_opportunities": [], "last_scan": None}


def save_sniper_state(state: dict):
    with open(SNIPER_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_active() -> dict:
    if ACTIVE_FILE.exists():
        with open(ACTIVE_FILE) as f:
            return json.load(f)
    return {"active": {}, "monitor": {}}


# ── TICKER RESOLUTION ─────────────────────────────────────────────────────────

_SEC_LUT: dict = {}

def load_sec_lut() -> dict:
    global _SEC_LUT
    if _SEC_LUT:
        return _SEC_LUT
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=CT_HEADERS, timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            for entry in data.values():
                name  = entry.get("title", "").lower().strip()
                ticker = entry.get("ticker", "").upper()
                cik    = str(entry.get("cik_str", ""))
                if name and ticker:
                    _SEC_LUT[name] = {"ticker": ticker, "cik": cik}
                    # First word shortcut
                    words = [w for w in name.split()
                             if len(w) > 3 and w not in ("inc", "corp", "ltd", "llc", "the", "pharma",
                                                          "therapeutics", "biosciences", "pharmaceuticals")]
                    if words and words[0] not in _SEC_LUT:
                        _SEC_LUT[words[0]] = {"ticker": ticker, "cik": cik}
    except Exception as e:
        print(f"  [sniper] SEC LUT error: {e}")
    return _SEC_LUT


def resolve_ticker(sponsor_name: str) -> str:
    """Try to find US-listed ticker from sponsor name."""
    lut = load_sec_lut()
    name_lower = sponsor_name.strip().lower()

    # Exact match
    if name_lower in lut:
        return lut[name_lower]["ticker"]

    # Remove common suffixes and try again
    for suffix in [", inc.", " inc", " corp", " ltd", " llc", ", llc", " se", " plc", " nv", " ag"]:
        stripped = name_lower.replace(suffix, "").strip()
        if stripped in lut:
            return lut[stripped]["ticker"]

    # First significant word
    words = [w for w in name_lower.split()
             if len(w) > 3 and w not in ("inc", "corp", "ltd", "llc", "the", "pharma",
                                          "therapeutics", "biosciences", "pharmaceuticals",
                                          "development", "commercialization", "research")]
    for word in words[:3]:
        if word in lut:
            return lut[word]["ticker"]

    return ""


def validate_ticker(ticker: str) -> dict:
    """Check ticker has tradeable options on Alpaca and collect expirations.

    M3 update (f-m3-02): the legacy vendor-backed price/marketcap
    filter is dropped — Alpaca's free tier does not expose
    market-cap directly. Stock-price + market-cap filtering moves to
    the M3 selection layer (which queries the SQLite ``universe``
    table populated by the bulk universe scanner). This helper now
    only validates that *some* options chain exists and surfaces the
    distinct expirations we saw — which is exactly what the
    new-opportunity sniper actually consumes.
    """
    if not ticker:
        return {"valid": False}
    try:
        # Local import keeps module import cheap and avoids forcing the
        # Alpaca SDK to load when the new-opportunity sniper is not in
        # use.
        from biotech_sniper.options_chains.pull_options import pull_chain

        chain = pull_chain(ticker)
        expiries = sorted(
            {row.get("expiry") for row in chain if row.get("expiry")}
        )
        if not chain:
            return {"valid": False, "reason": "no_options_chain"}

        return {
            "valid":       True,
            "price":       None,
            "has_options": True,
            "expirations": list(expiries[:8]),
            "mktcap":      None,
        }
    except Exception:
        return {"valid": False}


# ── PRICE RESOLUTION ──────────────────────────────────────────────────────────


def _resolve_underlying_price(ticker: str) -> Optional[float]:
    """Best-effort underlying-price lookup using the Alpaca SDK.

    Added by f-m3-13 as the explicit fallback when
    :func:`validate_ticker` returns ``price=None`` (the new default
    after the f-m3-02 yfinance removal). The lookup is best-effort:
    any failure (no SDK, no creds, network error) returns ``None`` so
    the caller can decide whether to skip-with-WARN or proceed with
    a different sizing path. Never raises.
    """
    if not ticker:
        return None
    try:
        # Local import: keeps the new_opportunity_sniper import-cheap
        # on hosts without alpaca-py creds (e.g. CI smoke gate).
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        from biotech_sniper import config

        api_key = config.get_alpaca_key_id()
        secret_key = config.get_alpaca_secret_key()
        if not api_key or not secret_key:
            return None
        client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        resp = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ticker)
        )
        trade = resp.get(ticker) if isinstance(resp, dict) else None
        if trade is None:
            return None
        raw_price = getattr(trade, "price", None)
        if raw_price is None:
            return None
        price = float(raw_price)
        if price <= 0:
            return None
        return price
    except Exception:
        return None


# ── CORRECT EXPIRY ─────────────────────────────────────────────────────────────

def get_correct_expiry(catalyst_date_str: str, available_exps: list) -> Optional[str]:
    """
    Find the first available expiry that covers catalyst_date + 3 trading days.
    TIMING RULE: expiry must be AFTER catalyst + 3 trading days.
    """
    if not catalyst_date_str or not available_exps:
        return None
    try:
        # Parse catalyst date — handle YYYY-MM or YYYY-MM-DD
        if len(catalyst_date_str) == 7:
            # Month only e.g. "2026-05" → use last day of month as worst case
            y, m = int(catalyst_date_str[:4]), int(catalyst_date_str[5:7])
            import calendar
            last_day = calendar.monthrange(y, m)[1]
            cat_date = datetime.date(y, m, last_day)
        else:
            cat_date = datetime.date.fromisoformat(catalyst_date_str[:10])

        # Add 3 trading days buffer (approximate: +5 calendar days)
        min_expiry = cat_date + datetime.timedelta(days=5)

        for exp in sorted(available_exps):
            exp_date = datetime.date.fromisoformat(exp)
            if exp_date >= min_expiry:
                return exp
    except Exception:
        pass
    return None


# ── OPTIONS CHAIN ─────────────────────────────────────────────────────────────

def get_best_option(ticker: str, expiry: str, direction: str, price: float) -> Optional[dict]:
    """
    Pull the options chain for the given expiry and find the best strike.
    Best = highest multiple at realistic mid fill, with OI > 10.

    M3 update (f-m3-02): chain pull now goes through
    :func:`biotech_sniper.options_chains.pull_options.pull_chain`,
    which is backed by the Alpaca options API instead of the legacy
    market-data vendor.

    M3 update (f-m3-13): ``price`` MUST be a positive numeric. Callers
    are responsible for resolving the underlying price BEFORE invoking
    this helper — passing ``None`` (which the legacy yfinance-backed
    ``validate_ticker`` used to populate) used to raise ``TypeError``
    on ``price * 1.8`` / ``price * 0.35`` and was silently swallowed
    by the outer broad-except, rejecting otherwise-valid candidates.
    The guard is now explicit and the broad-except narrowed so future
    regressions surface instead of being masked.
    """
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        print(f"  [sniper] get_best_option {ticker}: invalid price={price!r} — skipping")
        return None
    try:
        from biotech_sniper.options_chains.pull_options import pull_chain

        target_type = "call" if direction == "LONG_CALLS" else "put"
        chain = pull_chain(ticker, expiry)
        contracts = [
            r for r in chain
            if (r.get("type") or "").lower() == target_type
        ]

        best = None
        best_mult = 0.0

        for row in contracts:
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            oi  = int(row.get("oi", 0) or 0)
            try:
                strike = float(row.get("strike") or 0)
            except (TypeError, ValueError):
                continue

            if oi < 10 or ask <= 0:
                continue

            mid = (bid + ask) / 2
            if mid <= 0:
                continue

            spread_pct = (ask - bid) / mid * 100
            if spread_pct > 120:  # spreads >120% = unworkable
                continue

            # Target price on correct outcome
            if direction == "LONG_CALLS":
                # Stock doubles or 70% move on positive catalyst
                target_price = price * 1.8
                intrinsic = max(0.0, target_price - strike)
            else:
                # Stock drops 50-70% on failure
                target_price = price * 0.35
                intrinsic = max(0.0, strike - target_price)

            multiple = intrinsic / mid if mid > 0 else 0

            if multiple > best_mult:
                best_mult = multiple
                best = {
                    "strike": strike,
                    "expiry": expiry,
                    "direction": direction,
                    "bid": bid,
                    "ask": ask,
                    "mid": round(mid, 2),
                    "spread_pct": round(spread_pct, 0),
                    "oi": oi,
                    "multiple": round(multiple, 1),
                    "target_price": round(target_price, 2),
                    "fill_price": round(mid, 2),
                    "k1_return": round(multiple * 1000, 0),  # $1k investment return
                }

        return best
    except (ImportError, ValueError, KeyError) as e:
        # f-m3-13: narrowed from a bare ``Exception`` so that
        # ``TypeError`` (the one yfinance-removal regression that used
        # to silently reject otherwise-valid candidates via
        # ``price * 1.8`` on a ``None``) propagates loudly and cannot
        # mask future regressions of the same shape. Network / chain
        # parse failures still degrade gracefully.
        print(f"  [sniper] Options error {ticker} {expiry}: {e}")
        return None


# ── QUICK SCORER ──────────────────────────────────────────────────────────────

def quick_score_candidate(candidate: dict, science_prompt: Optional[str] = None) -> dict:
    """
    Score a new candidate for options trading.

    score_candidate_with_models() is a stub that returns (None, None, None, False) and
    expects the daily cron agent to run the actual model calls. For real-time intraday
    discovery we instead use the science grade + indication base rate as a fast heuristic,
    then return a structured score dict. Candidates that clear the threshold still get
    the full science-enriched prompt saved so the 6AM cron can re-score properly.
    """
    import sys
    sys.path.insert(0, str(BASE_DIR))
    sys.path.insert(0, str(BASE_DIR / "sectors"))

    ticker     = candidate.get("ticker", "UNKNOWN")
    drug       = candidate.get("drug", "")
    indication = candidate.get("indication", "")
    company    = candidate.get("company_hint", candidate.get("company", ""))
    completion = candidate.get("primary_completion", "")
    nct_id     = candidate.get("nct_id", "")

    # ── Fast heuristic score from science grade ───────────────────────────────
    # Use the pre-computed science flags to estimate P(success).
    # This avoids the stub model call and gives a real signal.
    p_success   = 50  # default: uncertain
    confidence  = "LOW"
    direction   = "DROP"
    one_line    = f"New Phase 3 trial completing {completion} — {indication}"
    edge_line   = f"Discovered via ClinicalTrials ACTIVE_NOT_RECRUITING 60-day window"

    # Pull science grade if available from the science_prompt context
    design_score = candidate.get("science_grade_data", {}).get("design_score", 0)
    endpoint_type = candidate.get("science_grade_data", {}).get("endpoint_type", "")
    bearish_flags = candidate.get("science_grade_data", {}).get("bearish_flags", [])
    bullish_flags = candidate.get("science_grade_data", {}).get("bullish_flags", [])

    # Indication base rate from science_scorer
    try:
        sys.path.insert(0, str(BASE_DIR / "intelligence"))
        from intelligence.science_scorer import get_indication_base_rate
        base_rate, matched = get_indication_base_rate(indication, [indication])
        p_success = round(base_rate * 100)
    except Exception:
        p_success = 45  # general Phase 3 base rate

    # Adjust for design quality
    if design_score >= 4:
        p_success = min(85, round(p_success * 1.15))
        confidence = "MEDIUM"
    elif design_score >= 0:
        confidence = "MEDIUM"
    elif design_score < -3:
        p_success = max(5, round(p_success * 0.75))
        confidence = "LOW"

    # Classify direction
    if p_success >= MIN_P_LONG:
        direction = "LONG_CALLS"
    elif p_success <= MAX_P_PUT:
        direction = "LONG_PUTS"
    else:
        direction = "DROP"

    # Build one-liner
    if bullish_flags:
        one_line = f"{ticker} Phase 3 {indication[:40]}: {bullish_flags[0][:80]}"
    elif bearish_flags:
        one_line = f"{ticker} Phase 3 {indication[:40]}: {bearish_flags[0][:80]}"
    else:
        one_line = f"{ticker} Phase 3 {indication[:40]} — base rate {p_success}%"

    edge_line = (
        f"Auto-discovered: primary completion {completion}. "
        f"Endpoint: {endpoint_type or 'unknown'}. "
        f"Full science scoring in 6AM daily report."
    )

    return {
        "p_success":  p_success,
        "direction":  direction,
        "confidence": confidence,
        "one_line":   one_line,
        "edge_line":  edge_line,
    }


# ── ClinicalTrials IMMINENT SCANNER ───────────────────────────────────────────

def scan_clinicaltrials_imminent(alerted_keys: set) -> list:
    """
    Pull Phase 3 trials completing in next 60 days.
    Only returns ones we haven't alerted on yet.
    Resolves ticker, validates options, returns scored candidates.
    """
    today     = datetime.date.today()
    cutoff_60 = (today + datetime.timedelta(days=60)).isoformat()
    active    = load_active()
    active_tickers = set(active.get("active", {}).keys()) | set(active.get("monitor", {}).keys())

    # Load already-seen NCTs from discovery state
    try:
        disc_state = json.load(open(BASE_DIR / "state/discovery_state.json"))
        seen_ncts = set(disc_state.get("seen_nct_ids", []))
    except Exception:
        seen_ncts = set()

    new_candidates = []

    try:
        r = requests.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params={
                "filter.advanced": f"AREA[Phase]PHASE3 AND AREA[OverallStatus]ACTIVE_NOT_RECRUITING AND AREA[PrimaryCompletionDate]RANGE[{today.isoformat()},{cutoff_60}]",
                "pageSize": 50,
                "sort": "PrimaryCompletionDate:asc",
                "countTotal": "true",
            },
            headers=CT_HEADERS, timeout=25
        )
        if r.status_code != 200:
            return []

        studies = r.json().get("studies", [])
        print(f"  [new_opp] CT imminent: {len(studies)} Phase 3 trials in 60d window")

        for study in studies:
            ps       = study.get("protocolSection", {})
            nct_id   = ps.get("identificationModule", {}).get("nctId", "")
            title    = ps.get("identificationModule", {}).get("briefTitle", "")[:80]
            sponsor  = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")
            pcomp    = ps.get("statusModule", {}).get("primaryCompletionDateStruct", {}).get("date", "")
            conds    = ps.get("conditionsModule", {}).get("conditions", [])
            n        = ps.get("designModule", {}).get("enrollmentInfo", {}).get("count", "?")
            interv   = ps.get("armsInterventionsModule", {}).get("interventions", [])
            drugs    = [iv.get("name","") for iv in interv
                        if iv.get("type","").upper() in ("DRUG","BIOLOGICAL","GENETIC","COMBINATION_PRODUCT")]

            # Skip if already in seen NCTs or active plays
            if nct_id in seen_ncts:
                continue

            # Resolve ticker
            ticker = resolve_ticker(sponsor)
            if not ticker:
                continue  # Can't trade without a ticker

            # Skip if already tracking
            if ticker in active_tickers:
                seen_ncts.add(nct_id)
                continue

            # Dedup key
            alert_key = f"new_opp:{ticker}:{nct_id}"
            if alert_key in alerted_keys:
                continue

            new_candidates.append({
                "nct_id":      nct_id,
                "trial_name":  title,
                "company":     sponsor,
                "ticker":      ticker,
                "drug":        "; ".join(drugs[:2]),
                "indication":  "; ".join(conds[:2]),
                "primary_completion": pcomp,
                "n":           n,
                "alert_key":   alert_key,
                "source":      "ct_imminent",
            })
            print(f"  [new_opp] NEW CT: {ticker} | {sponsor[:35]} | {pcomp} | {conds[:1]}")

    except Exception as e:
        print(f"  [new_opp] CT imminent error: {e}")

    return new_candidates


# ── SEC 8-K NEW TICKER SCANNER ────────────────────────────────────────────────

def scan_sec_new_topline(alerted_keys: set, seen_urls: set) -> list:
    """
    SEC EDGAR full-text search for 8-Ks with 'topline' + 'phase 3' filed in last 48h.
    Only returns companies NOT in our active plays.
    """
    active = load_active()
    active_tickers = set(active.get("active", {}).keys()) | set(active.get("monitor", {}).keys())

    today    = datetime.date.today()
    start_dt = (today - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    new_candidates = []

    queries = ['"topline" "phase 3"', '"primary endpoint" "phase 3" "results"']

    for query in queries:
        try:
            r = requests.get(
                "https://efts.sec.gov/LATEST/search-index?q={}&dateRange=custom&startdt={}&forms=8-K".format(
                    requests.utils.quote(query), start_dt
                ),
                headers={"User-Agent": "BioCatalystBot research@mantisvc.com"},
                timeout=20
            )
            if r.status_code != 200:
                continue

            hits = r.json().get("hits", {}).get("hits", [])
            for hit in hits[:15]:
                src = hit.get("_source", {})
                url = src.get("file_date", "")
                accession = src.get("accession_no", "")
                entity    = src.get("entity_name", "")
                filed     = src.get("file_date", "")
                form_type = src.get("form_type", "")

                if accession in seen_urls:
                    continue

                seen_urls.add(accession)
                ticker = resolve_ticker(entity)
                if not ticker or ticker in active_tickers:
                    continue

                alert_key = f"new_opp_8k:{ticker}:{accession}"
                if alert_key in alerted_keys:
                    continue

                new_candidates.append({
                    "ticker":    ticker,
                    "company":   entity,
                    "source":    "sec_8k_topline",
                    "alert_key": alert_key,
                    "filing_url": f"https://www.sec.gov/Archives/edgar/data/{src.get('entity_id','')}/{accession.replace('-','')}/{accession}-index.htm",
                    "filed":     filed,
                    "drug":      "",
                    "indication": "",
                    "primary_completion": today.isoformat(),  # It's happening NOW
                })
                print(f"  [new_opp] NEW 8K: {ticker} | {entity} | {filed}")

        except Exception as e:
            print(f"  [new_opp] SEC 8K error: {e}")

    return new_candidates


# ── SCORE + OPTIONS + FILTER ──────────────────────────────────────────────────

def score_and_price_candidate(candidate: dict) -> Optional[dict]:
    """
    Full pipeline for a single new candidate:
    1. Validate ticker + options
    2. Science profile (if NCT ID)
    3. Score with models
    4. Find correct expiry + best strike
    5. Calculate multiple
    6. Return if >= 2.5x and P qualifies
    """
    # Defensive: only process dicts — tuples or other types crash .get() calls
    if not isinstance(candidate, dict):
        print(f"  [sniper] score_and_price_candidate: expected dict, got {type(candidate).__name__} — skipping")
        return None

    ticker = candidate.get("ticker", "")
    nct_id = candidate.get("nct_id", "")
    catalyst_date = candidate.get("primary_completion", "")

    print(f"  [new_opp] Pricing {ticker}...")

    # Step 1: Validate ticker
    info = validate_ticker(ticker)
    if not info.get("valid"):
        print(f"    {ticker}: invalid ({info.get('reason','no options/price')})")
        return None

    price = info.get("price")
    exps  = info.get("expirations", [])

    if not info.get("has_options") or not exps:
        print(f"    {ticker}: no options")
        return None

    # f-m3-13: validate_ticker now returns price=None after the
    # f-m3-02 yfinance removal. Resolve a positive underlying price
    # before scoring options — fall back to AlpacaClient.get_latest_trade
    # when available, otherwise WARN-and-skip so a TypeError in
    # get_best_option (``price * 1.8`` / ``price * 0.35``) cannot be
    # silently swallowed and reject the candidate.
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        price = _resolve_underlying_price(ticker)
        if price is None:
            print(
                f"    [sniper] WARN {ticker}: underlying price unavailable "
                f"(validate_ticker returned None and Alpaca latest-trade "
                f"fallback failed) — skipping candidate"
            )
            return None

    # Step 2: Science profile if NCT available
    science_prompt = None
    science_grade  = None
    if nct_id and nct_id.startswith("NCT"):
        try:
            import sys
            sys.path.insert(0, str(BASE_DIR))
            sys.path.insert(0, str(BASE_DIR / "intelligence"))
            from intelligence.trial_science_reader import get_science_profile
            from intelligence.science_scorer import compute_science_grade
            sp = get_science_profile(nct_id, candidate)
            science_prompt = sp.get("science_prompt")
            grade_result   = compute_science_grade(sp)
            science_grade  = grade_result.get("grade", "?")
            design_score   = grade_result.get("design_score", 0)
            print(f"    {ticker}: science grade {science_grade} ({design_score:+d})")
        except Exception as e:
            print(f"    {ticker}: science error: {e}")

    # Step 3: Score
    scored = quick_score_candidate(candidate, science_prompt)
    p      = scored.get("p_success", 50)
    direction = scored.get("direction", "DROP")
    confidence = scored.get("confidence", "LOW")

    # Apply filters
    if direction == "DROP":
        print(f"    {ticker}: P={p}% → DROP (41-59 range)")
        return None
    if direction == "LONG_CALLS" and p < MIN_P_LONG:
        print(f"    {ticker}: P={p}% below LONG threshold ({MIN_P_LONG}%)")
        return None
    if direction == "LONG_PUTS" and p > MAX_P_PUT:
        print(f"    {ticker}: P={p}% above PUT threshold ({MAX_P_PUT}%)")
        return None

    # Step 4: Find correct expiry
    expiry = get_correct_expiry(catalyst_date, exps)
    if not expiry:
        # Fall back to 3rd+ expiry available (covers most near-term catalysts)
        expiry = exps[2] if len(exps) > 2 else exps[-1] if exps else None
    if not expiry:
        print(f"    {ticker}: no valid expiry")
        return None

    # Verify timing
    try:
        cat_dt = datetime.date.fromisoformat(catalyst_date[:10]) if len(catalyst_date) >= 10 else None
        exp_dt = datetime.date.fromisoformat(expiry)
        if cat_dt and exp_dt < cat_dt:
            print(f"    {ticker}: TIMING FAIL — expiry {expiry} before catalyst {catalyst_date}")
            # Try next expiry
            for e in exps:
                if datetime.date.fromisoformat(e) > cat_dt:
                    expiry = e
                    break
    except Exception:
        pass

    # Step 5: Best option
    best_option = get_best_option(ticker, expiry, direction, price)
    if not best_option:
        print(f"    {ticker}: no liquid options at {expiry}")
        return None

    multiple = best_option.get("multiple", 0)
    if multiple < MIN_MULTIPLE:
        print(f"    {ticker}: multiple {multiple}x below threshold ({MIN_MULTIPLE}x)")
        return None

    # Step 6: Days to catalyst
    try:
        cat_dt = datetime.date.fromisoformat(catalyst_date[:7] + "-01") if len(catalyst_date) == 7 else datetime.date.fromisoformat(catalyst_date[:10])
        days_to_cat = (cat_dt - datetime.date.today()).days
    except Exception:
        days_to_cat = "?"

    print(f"    {ticker}: QUALIFIES P={p}% {direction} {multiple}x | {days_to_cat}d | {expiry}")

    return {
        "ticker":       ticker,
        "company":      candidate.get("company", ""),
        "drug":         candidate.get("drug", ""),
        "indication":   candidate.get("indication", ""),
        "nct_id":       nct_id,
        "source":       candidate.get("source", ""),
        "alert_key":    candidate.get("alert_key", ""),
        "p_success":    p,
        "direction":    direction,
        "confidence":   confidence,
        "science_grade": science_grade,
        "catalyst_date": catalyst_date,
        "days_to_cat":  days_to_cat,
        "strike":       best_option["strike"],
        "expiry":       expiry,
        "option_type":  "C" if direction == "LONG_CALLS" else "P",
        "mid":          best_option["mid"],
        "bid":          best_option["bid"],
        "ask":          best_option["ask"],
        "spread_pct":   best_option["spread_pct"],
        "oi":           best_option["oi"],
        "multiple":     multiple,
        "k1_return":    best_option["k1_return"],
        "price":        price,
        "one_line":     scored.get("one_line", ""),
        "edge_line":    scored.get("edge_line", ""),
        "found_at":     datetime.datetime.now().isoformat(),
    }


# ── EMAIL FORMATTER ───────────────────────────────────────────────────────────

def format_new_opp_email(opportunities: list, scan_time: str) -> str:
    """
    Format an instant alert email for newly found opportunities.
    Same one-liner format as the daily email but fired immediately.
    """
    lines = []
    lines.append("ALPHA SNIPER — NEW OPPORTUNITY ALERT")
    lines.append(f"Found: {scan_time}")
    lines.append("=" * 55)
    lines.append("")

    for opp in sorted(opportunities, key=lambda x: x.get("days_to_cat", 999) if isinstance(x.get("days_to_cat"), int) else 999):
        ticker    = opp["ticker"]
        drug      = opp.get("drug", "")[:30]
        ind       = opp.get("indication", "")[:35]
        p         = opp.get("p_success", "?")
        direction = opp.get("direction", "?")
        strike    = opp.get("strike", "?")
        opt_type  = opp.get("option_type", "?")
        expiry    = opp.get("expiry", "?")
        mid       = opp.get("mid", "?")
        oi        = opp.get("oi", "?")
        multiple  = opp.get("multiple", "?")
        k1        = opp.get("k1_return", "?")
        days      = opp.get("days_to_cat", "?")
        cat_date  = opp.get("catalyst_date", "?")
        company   = opp.get("company", "")[:40]
        science   = opp.get("science_grade", "")
        one_line  = opp.get("one_line", "")
        edge      = opp.get("edge_line", "")
        conf      = opp.get("confidence", "")
        source    = opp.get("source", "")

        science_tag = f" | Science:{science}" if science else ""
        conf_tag    = f" | {conf}" if conf else ""

        # One-liner header
        lines.append(f"{days}d | {ticker} ${strike}{opt_type} {expiry} @ ${mid:.2f} — {multiple}x | P={p}% | OI={oi} | catalyst: {cat_date}{science_tag}{conf_tag}")

        # Detail block
        lines.append(f"  {company} | {drug}")
        lines.append(f"  Indication: {ind}")
        lines.append(f"  $1k investment → ~${k1:,.0f} if right")
        if one_line:
            lines.append(f"  WHY: {one_line}")
        if edge:
            lines.append(f"  EDGE: {edge}")

        # Option ladder context
        lines.append(f"  OPTION: ${strike}{opt_type} exp {expiry} | bid=${opp.get('bid','?'):.2f} ask=${opp.get('ask','?'):.2f} mid=${mid:.2f} | spread={opp.get('spread_pct','?'):.0f}% | OI={oi}")
        lines.append(f"  SOURCE: {source}")
        lines.append("")

    lines.append("─" * 55)
    lines.append("This was auto-discovered. Full card in next 6AM report.")
    lines.append("Verify spread and liquidity before entering.")

    return "\n".join(lines)


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────

def scan_for_new_opportunities(seen_urls: set) -> list:
    """
    Called from run_intraday_scan() every hour.
    Returns list of qualified new opportunities (each ready to email immediately).
    """
    state        = load_sniper_state()
    alerted_keys = set(state.get("alerted_keys", []))
    found_opps   = state.get("found_opportunities", [])

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M PT")
    today_str = datetime.date.today().isoformat()
    print(f"\n[NEW OPP SNIPER] Scanning for new opportunities...")

    all_raw = []

    # Source 1: ClinicalTrials IMMINENT (60-day window)
    # Only run CT scan ONCE per calendar day to avoid re-evaluating the same
    # slow-moving trial list every hour. SEC 8-K runs every hour (breaking events).
    ct_last_run = state.get("ct_last_run_date", "")
    if ct_last_run != today_str:
        ct_candidates = scan_clinicaltrials_imminent(alerted_keys)
        all_raw.extend(ct_candidates)
        state["ct_last_run_date"] = today_str
        print(f"  [new_opp] CT scan ran (first run today). Found {len(ct_candidates)} new candidates.")
    else:
        ct_candidates = []
        print(f"  [new_opp] CT scan skipped (already ran today). Only checking SEC 8-K.")

    # Source 2: SEC 8-K topline (breaking data) — runs every hour
    sec_candidates = scan_sec_new_topline(alerted_keys, seen_urls)
    all_raw.extend(sec_candidates)

    # Deduplicate on ticker — only score each ticker once per run
    seen_tickers_this_run = set()
    deduped_raw = []
    for c in all_raw:
        t = c.get("ticker", "") if isinstance(c, dict) else ""
        if t and t not in seen_tickers_this_run:
            seen_tickers_this_run.add(t)
            deduped_raw.append(c)
    all_raw = deduped_raw

    print(f"[NEW OPP SNIPER] Raw candidates: {len(all_raw)} (CT: {len(ct_candidates)}, SEC: {len(sec_candidates)}, after dedup)")

    # Score + price each candidate
    # Also accumulate ALL evaluated NCT IDs to persist to discovery state
    # so we don’t re-evaluate the same CT candidates every hour
    evaluated_ncts_this_run = set()
    qualified = []
    for candidate in all_raw:
        nct = candidate.get("nct_id", "") if isinstance(candidate, dict) else ""
        if nct:
            evaluated_ncts_this_run.add(nct)

        result = score_and_price_candidate(candidate)
        if result:
            qualified.append(result)
            alerted_keys.add(result["alert_key"])
            found_opps.append(result)

    # Persist ALL evaluated NCTs (qualifying or not) to discovery state
    # This prevents re-evaluation every hour for the same 60-day window trials
    if evaluated_ncts_this_run:
        try:
            disc_path = BASE_DIR / "state/discovery_state.json"
            disc_state = json.load(open(disc_path))
            seen_set = set(disc_state.get("seen_nct_ids", []))
            seen_set.update(evaluated_ncts_this_run)
            disc_state["seen_nct_ids"] = list(seen_set)[-3000:]
            json.dump(disc_state, open(disc_path, "w"), indent=2)
            print(f"[NEW OPP SNIPER] Persisted {len(evaluated_ncts_this_run)} evaluated NCTs to discovery state")
        except Exception as e:
            print(f"[NEW OPP SNIPER] Failed to persist NCTs: {e}")

    # Save state
    state["alerted_keys"]        = list(alerted_keys)[-2000:]
    state["found_opportunities"] = found_opps[-200:]
    state["last_scan"]           = now
    save_sniper_state(state)

    print(f"[NEW OPP SNIPER] Qualified: {len(qualified)}")
    return qualified
