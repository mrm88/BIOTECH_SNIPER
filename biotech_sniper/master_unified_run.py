#!/usr/bin/env python3
"""
MASTER UNIFIED ALPHA SCANNER
Runs all three sectors + intelligence suite in one daily run.

SECTORS:
  1. BIOTECH — Warpspeed + ClinicalTrials.gov + multi-source (existing)
  2. CONTRACTS — SAM.gov J&A + pre-solicitation notices (new)
  3. ADCOM — FDA Advisory Committee meetings + briefing doc NLP (new)

OUTPUT: Single unified report → email → Excel
Same scoring, same lifecycle, same email format for all sectors.

Sector tags in report:
  [BIOTECH] — clinical trial binary event
  [CONTRACT] — government contract award
  [ADCOM] — FDA advisory committee vote

STEP ORDER (6AM daily):
  Step 0a: company_resolver.auto_resolve_missing_ir_urls()
  Step 0b: master_discovery.run_discovery() → populates needs_scoring queue
  Step 1a: amendment_tracker.run_amendment_check()
  Step 1b: ir_events_watcher.run_ir_events_check()
  Step 1c: sec_8k_monitor.run_8k_monitor()
  Step 1d: twitter_biotech_monitor.build_twitter_search_queries() → cron runs → parse
  Step 2:  Dual-model scoring for needs_scoring queue
  Step 3:  run_contract_scan()
  Step 4:  run_adcom_scan()
  Step 5:  watchlist_lifecycle + report generation
"""

import json
import datetime
import sys
from pathlib import Path

from biotech_sniper.paths import BASE_DIR
OUTPUT_FILE = BASE_DIR / "intelligence/unified_master_signals.json"

sys.path.insert(0, str(BASE_DIR / "intelligence"))
sys.path.insert(0, str(BASE_DIR / "sectors/contracts"))
sys.path.insert(0, str(BASE_DIR / "sectors/adcom"))
sys.path.insert(0, str(BASE_DIR / "sectors"))


def run_unified_scan():
    today = datetime.date.today().isoformat()
    all_signals  = []
    sector_results = {}
    needs_scoring  = []  # global scoring queue — populated by discovery + sectors

    print(f"\n{'#'*70}")
    print(f"# UNIFIED ALPHA SCANNER — {today}")
    print(f"# Sectors: BIOTECH | CONTRACTS | ADCOM")
    print(f"{'#'*70}")

    # ── STEP 0a: AUTO-RESOLVE MISSING IR URLS ────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"STEP 0a: AUTO-RESOLVE MISSING IR URLS")
    print(f"{'─'*70}")
    try:
        from company_resolver import auto_resolve_missing_ir_urls
        auto_resolve_missing_ir_urls()
    except Exception as e:
        print(f"  Auto-resolver skipped: {e}")

    # ── STEP 0b: MASTER DISCOVERY ENGINE ─────────────────────────────────────
    # Runs BEFORE scoring — finds new candidates in all 3 sectors
    print(f"\n{'─'*70}")
    print(f"STEP 0b: MASTER DISCOVERY ENGINE (All 3 Sectors)")
    print(f"{'─'*70}")
    discovery_result = {}
    try:
        from master_discovery import run_discovery
        discovery_result = run_discovery()

        # All newly discovered candidates with known tickers → scoring queue
        # For BIOTECH candidates with NCT IDs, enrich with science data first
        for candidate in discovery_result.get("all_new", []):
            ticker = candidate.get("ticker", "")
            sector = candidate.get("sector", "BIOTECH")
            if ticker:
                nct_id = candidate.get("nct_id", "")
                base_scoring_prompt = candidate.get("scoring_prompt", "")

                # Enrich biotech candidates with science profile (Shkreli-style)
                science_scoring_prompt = base_scoring_prompt
                if sector == "BIOTECH" and nct_id and nct_id.startswith("NCT"):
                    try:
                        from intelligence.trial_science_reader import get_science_profile
                        science_profile = get_science_profile(nct_id, candidate)
                        if science_profile.get("science_prompt"):
                            science_scoring_prompt = science_profile["science_prompt"]
                            candidate["science_grade_data"] = {
                                "design_score": science_profile.get("design_score", 0),
                                "bearish_flags": science_profile.get("bearish_flags", []),
                                "bullish_flags": science_profile.get("bullish_flags", []),
                                "endpoint_type": science_profile.get("endpoint_type", ""),
                            }
                            print(f"    [science] {ticker} enriched with protocol data (score={science_profile.get('design_score',0):+d})")
                    except Exception as se:
                        print(f"    [science] {ticker} enrichment failed: {se}")

                needs_scoring.append({
                    "ticker":       ticker,
                    "sector":       sector,
                    "sector_tag":   sector,
                    "source":       candidate.get("source", "discovery"),
                    "company":      candidate.get("company_hint", "") or candidate.get("company", ""),
                    "drug":         candidate.get("drug", ""),
                    "nct_id":       nct_id,
                    "detected_date": candidate.get("detected_date", today),
                    "scoring_prompt": science_scoring_prompt,
                    "science_grade_data": candidate.get("science_grade_data", {}),
                })

        # Discovery signals (briefing docs, etc.) → all_signals
        for sig in discovery_result.get("signals", []):
            sig["sector_tag"] = sig.get("sector", "DISCOVERY")
            all_signals.append(sig)

        sector_results["discovery"] = {
            "new_biotech":          len(discovery_result.get("new_biotech", [])),
            "new_contracts":        len(discovery_result.get("new_contracts", [])),
            "new_adcom":            len(discovery_result.get("new_adcom", [])),
            "defense_rss_total":    len(discovery_result.get("defense_rss", [])),
            "defense_rss_matched":  len([r for r in discovery_result.get("defense_rss", []) if r.get("ticker")]),
            "scoring_queue_added":  len([c for c in discovery_result.get("all_new", []) if c.get("ticker")]),
            "signals":              len(discovery_result.get("signals", [])),
            "sam_gov_urls":         discovery_result.get("sam_gov_urls", {}),
            "nasa_queries":         discovery_result.get("nasa_queries", []),
            "conference_queries":   discovery_result.get("conference_queries", []),
        }
        print(f"  → Discovery complete: {sector_results['discovery']['new_biotech']} biotech | "
              f"{sector_results['discovery']['new_contracts']} contracts | "
              f"{sector_results['discovery']['new_adcom']} adcom | "
              f"{sector_results['discovery']['defense_rss_matched']} defense RSS matches")
        print(f"  → Scoring queue: {len(needs_scoring)} new candidates")

    except Exception as e:
        print(f"  → DISCOVERY ERROR: {e}")
        sector_results["discovery"] = {"error": str(e)}

    # ── SECTOR 1: BIOTECH INTELLIGENCE SUITE ────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"SECTOR 1: BIOTECH (Steps 1a-1d)")
    print(f"{'─'*70}")
    try:
        # Step 1a: Amendment tracker
        from amendment_tracker import run_amendment_check
        # Step 1b: IR events watcher (checks all IR pages + SEC EDGAR)
        from ir_events_watcher import run_ir_events_check
        # Step 1c: SEC 8-K monitor
        from sec_8k_monitor import run_8k_monitor
        # Step 1d: Twitter/X queries (queries only — cron agent runs them)
        from twitter_biotech_monitor import build_twitter_search_queries
        # Lifecycle manager
        from watchlist_lifecycle import run_lifecycle_check

        # Step 1a
        print(f"\n  Step 1a: Amendment tracker...")
        amendment_result = run_amendment_check()

        # Step 1b
        print(f"\n  Step 1b: IR events watcher...")
        ir_result = run_ir_events_check()

        # Step 1c
        print(f"\n  Step 1c: SEC 8-K monitor...")
        sec_result = run_8k_monitor(mode="daily")

        # Step 1d: Build Twitter queries for cron agent
        print(f"\n  Step 1d: Building Twitter/X search queries...")
        twitter_queries = build_twitter_search_queries()
        print(f"    Built {len(twitter_queries)} queries for cron agent")

        # Lifecycle
        lifecycle_result = run_lifecycle_check(
            sec_8k_report=sec_result,
            ir_events_report=ir_result
        )

        biotech_signals = (
            amendment_result.get("signals", []) +
            ir_result.get("signals", []) +
            sec_result.get("signals", [])
        )
        for s in biotech_signals:
            s["sector_tag"] = "BIOTECH"

        # Biotech signals that need scoring → add to queue
        for s in sec_result.get("signals", []):
            if s.get("severity") == "CRITICAL" and s.get("ticker"):
                # New topline 8-K = potentially needs probability re-score
                pass

        all_signals.extend(biotech_signals)

        sector_results["biotech"] = {
            "signals":              len(biotech_signals),
            "active_plays":         lifecycle_result.get("active_count", 0),
            "removed":              lifecycle_result.get("removed", []),
            "graduated":            lifecycle_result.get("graduated", []),
            "active_plays_ordered": lifecycle_result.get("active_plays_ordered", []),
            "twitter_queries":      twitter_queries[:10],  # first 10 in report
            "twitter_query_count":  len(twitter_queries),
        }
        print(f"  → {len(biotech_signals)} signals | {lifecycle_result.get('active_count',0)} active plays")

    except Exception as e:
        print(f"  → BIOTECH ERROR: {e}")
        sector_results["biotech"] = {"error": str(e)}

    # ── SECTOR 2: GOVERNMENT CONTRACTS ──────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"SECTOR 2: GOVERNMENT CONTRACTS (USASpending + Defense.gov RSS + SAM.gov)")
    print(f"{'─'*70}")
    try:
        from sam_sniper import run_contract_scan
        contracts_result = run_contract_scan(days_back=3)

        contract_signals = contracts_result.get("signals", [])
        for s in contract_signals:
            s["sector_tag"] = "CONTRACT"
        all_signals.extend(contract_signals)

        # New contract candidates → scoring queue
        for s in contracts_result.get("needs_scoring", []):
            s["sector_tag"] = "CONTRACT"
            needs_scoring.append(s)

        sector_results["contracts"] = {
            "signals":      len(contract_signals),
            "critical":     len(contracts_result.get("critical", [])),
            "high":         len(contracts_result.get("high", [])),
            "needs_scoring": contracts_result.get("needs_scoring", []),
        }
        print(f"  → {len(contract_signals)} signals | {len(contracts_result.get('critical',[]))} critical")

    except Exception as e:
        print(f"  → CONTRACTS ERROR: {e}")
        sector_results["contracts"] = {"error": str(e)}

    # ── SECTOR 3: FDA ADCOM ──────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"SECTOR 3: FDA ADVISORY COMMITTEES (multi-source)")
    print(f"{'─'*70}")
    try:
        from adcom_scanner import run_adcom_scan
        adcom_result = run_adcom_scan()

        adcom_signals = adcom_result.get("signals", [])
        for s in adcom_signals:
            s["sector_tag"] = "ADCOM"
        all_signals.extend(adcom_signals)

        # New AdCom candidates → scoring queue
        for s in adcom_result.get("needs_scoring", []):
            s["sector_tag"] = "ADCOM"
            needs_scoring.append(s)

        sector_results["adcom"] = {
            "signals":        len(adcom_signals),
            "upcoming_count": adcom_result.get("upcoming_count", 0),
            "briefing_alerts": adcom_result.get("briefing_alerts", []),
            "needs_scoring":   adcom_result.get("needs_scoring", []),
            "browser_tasks":   adcom_result.get("browser_tasks", []),
        }
        print(f"  → {len(adcom_signals)} new AdCom | {adcom_result.get('upcoming_count',0)} total upcoming")
        if adcom_result.get("briefing_alerts"):
            for ba in adcom_result["briefing_alerts"]:
                score = ba.get("sentiment_score", "?")
                rec   = ba.get("recommendation", "")
                print(f"    Briefing: {ba.get('ticker','?')} | {ba.get('days_out','?')}d | score={score} ({rec})")

    except Exception as e:
        print(f"  → ADCOM ERROR: {e}")
        sector_results["adcom"] = {"error": str(e)}

    # ── SCIENCE ENRICHMENT (Shkreli-style protocol analysis) ────────────────
    print(f"\n{'─'*70}")
    print(f"SCIENCE ENRICHMENT: Reading protocols (Shkreli-style)")
    print(f"{'─'*70}")
    science_enrichment_result = {}
    science_section_text = ""
    try:
        from intelligence.science_enrichment_pipeline import run_science_enrichment, format_science_section_for_email
        science_enrichment_result = run_science_enrichment()
        science_section_text = format_science_section_for_email(
            science_enrichment_result.get("enriched_plays", {})
        )
        grades = science_enrichment_result.get("grades_summary", {})
        print(f"  → {len(grades)} plays graded | {len(science_enrichment_result.get('science_alerts',[]))} science alerts")
        for t, g in grades.items():
            print(f"    {t}: Grade {g.get('grade','?')} | adj P={g.get('adjusted_p','?')}%")
    except Exception as e:
        print(f"  → SCIENCE ENRICHMENT ERROR: {e}")
        import traceback; traceback.print_exc()

    # ── STEP: PERFORMANCE TRACKER + AUTO RESOLVER + LEARNING ENGINE ────────────
    # Runs every morning: snapshot prices → detect resolutions → recalibrate
    print(f"\n{'─'*70}")
    print(f"PERFORMANCE TRACKING + SELF-LEARNING")
    print(f"{'─'*70}")
    tracker_results = {}
    resolver_results = {}
    learning_results = {}
    try:
        from performance_tracker import run_tracker
        tracker_results = run_tracker()
        print(f"  Tracker: {len(tracker_results.get('updated', []))} plays updated | {len(tracker_results.get('catalyst_signals', []))} catalyst signals")
    except Exception as e:
        print(f"  Tracker error: {e}")

    try:
        from auto_resolver import run_auto_resolver
        resolver_results = run_auto_resolver()
        print(f"  Resolver: {resolver_results.get('new_resolutions', 0)} new resolutions | {resolver_results.get('total_resolved', 0)} total resolved")
    except Exception as e:
        print(f"  Resolver error: {e}")

    try:
        from learning_engine import run_learning_cycle
        learning_results = run_learning_cycle()
        print(f"  Learning: {learning_results.get('summary', 'no data yet')}")
    except Exception as e:
        print(f"  Learning error: {e}")

    # ── CONSOLIDATE ALL SIGNALS ──────────────────────────────────────────────
    critical = [s for s in all_signals if s.get("severity") == "CRITICAL"]
    high     = [s for s in all_signals if s.get("severity") == "HIGH"]

    # Deduplicate needs_scoring by ticker
    seen_tickers_scoring = set()
    deduped_scoring = []
    for item in needs_scoring:
        key = f"{item.get('ticker','?')}_{item.get('sector','?')}"
        if key not in seen_tickers_scoring:
            seen_tickers_scoring.add(key)
            deduped_scoring.append(item)

    # Active plays summary
    active_plays = sector_results.get("biotech", {}).get("active_plays_ordered", [])

    # SAM.gov patterns for cron agent
    sam_patterns = discovery_result.get("sam_gov_urls", {}) if discovery_result else {}

    master = {
        "run_date":                 today,
        "sectors_run":              ["BIOTECH", "CONTRACTS", "ADCOM"],
        "all_signals":              all_signals,
        "critical":                 critical,
        "high":                     high,
        "total_signals":            len(all_signals),
        "needs_dual_model_scoring": deduped_scoring,
        "active_plays":             active_plays,
        "sector_results":           sector_results,
        "discovery_summary":        discovery_result.get("summary", {}) if discovery_result else {},
        "cron_agent_tasks": {
            "twitter_queries":      sector_results.get("biotech", {}).get("twitter_queries", []),
            "twitter_query_count":  sector_results.get("biotech", {}).get("twitter_query_count", 0),
            "conference_queries":   sector_results.get("discovery", {}).get("conference_queries", []),
            "nasa_queries":         sector_results.get("discovery", {}).get("nasa_queries", []),
            "sam_gov_urls":         sam_patterns,
            "browser_tasks":        sector_results.get("adcom", {}).get("browser_tasks", []),
        },
        "summary_for_email": format_unified_email_summary(
            all_signals, critical, high, active_plays, deduped_scoring,
            sector_results, discovery_result
        ),
        "tracker_results":  tracker_results,
        "resolver_results": resolver_results,
        "learning_results": learning_results,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(master, f, indent=2)

    print(f"\n{'#'*70}")
    print(f"UNIFIED SCAN COMPLETE — {today}")
    print(f"  Total signals: {len(all_signals)} ({len(critical)} critical, {len(high)} high)")
    print(f"  Needs scoring: {len(deduped_scoring)} new candidates")
    print(f"  Active plays: {len(active_plays)}")
    if discovery_result:
        dr = discovery_result.get("summary", {})
        print(f"  Discovery: {dr.get('total_new_biotech',0)} biotech | "
              f"{dr.get('total_new_contracts',0)} contracts | "
              f"{dr.get('total_new_adcom',0)} adcom | "
              f"{dr.get('defense_rss_matched',0)} defense RSS matched")
    print(f"{'#'*70}\n")

    return master


def format_unified_email_summary(all_signals, critical, high, active_plays,
                                   needs_scoring, sector_results, discovery_result=None):
    """Format the intelligence signals block for the top of the daily email."""
    lines = []
    lines.append("=" * 65)
    lines.append("INTELLIGENCE SIGNALS — ALL SECTORS")
    lines.append("=" * 65)

    if not all_signals and not needs_scoring:
        lines.append("✓ All clear. No new signals across Biotech, Contracts, or AdCom.")
        return "\n".join(lines)

    # Critical first
    if critical:
        lines.append(f"\n🚨 CRITICAL ({len(critical)}) — REVIEW BEFORE TRADING:")
        for s in critical:
            tag = s.get("sector_tag", "")
            lines.append(f"  [{tag}] {s.get('icon','')} {s.get('ticker','?')}: {s.get('type','')}")
            lines.append(f"    {s.get('detail', s.get('title',''))[:120]}")
            if s.get("implication"):
                lines.append(f"    → {s['implication'][:120]}")

    if high:
        lines.append(f"\n🟠 HIGH ({len(high)}):")
        for s in high:
            tag = s.get("sector_tag", "")
            lines.append(f"  [{tag}] {s.get('ticker','?')}: {s.get('detail', s.get('title',''))[:100]}")

    # Discovery summary
    if discovery_result:
        dr = discovery_result.get("summary", {})
        defense_matched = dr.get("defense_rss_matched", 0)
        lines.append(f"\n🔍 DISCOVERY ENGINE:")
        lines.append(f"  New biotech candidates:  {dr.get('total_new_biotech',0)}")
        lines.append(f"  New contract signals:    {dr.get('total_new_contracts',0)}")
        lines.append(f"  New AdCom meetings:      {dr.get('total_new_adcom',0)}")
        if defense_matched:
            lines.append(f"  Defense.gov RSS matches: {defense_matched}")
        needs_resolve = dr.get("needs_ticker_resolution", 0)
        if needs_resolve:
            lines.append(f"  Need ticker resolution:  {needs_resolve} (cron agent resolves)")

    # New candidates needing scoring
    if needs_scoring:
        lines.append(f"\n🔬 NEW CANDIDATES — NEEDS DUAL-MODEL SCORING ({len(needs_scoring)}):")
        for s in needs_scoring[:5]:  # Top 5
            tag = s.get("sector_tag", "?")
            lines.append(f"  [{tag}] {s.get('ticker','?')}: {s.get('company','')[:40]} | {s.get('drug','')[:30]}")

    # AdCom briefing alerts
    adcom_result = sector_results.get("adcom", {})
    briefing_alerts = adcom_result.get("briefing_alerts", [])
    if briefing_alerts:
        lines.append(f"\n⚡ ADCOM BRIEFING DOCS ({len(briefing_alerts)}):")
        for ba in briefing_alerts:
            score = ba.get("sentiment_score", "?")
            rec   = ba.get("recommendation", "")
            lines.append(f"  {ba.get('ticker','?')}: AdCom in {ba.get('days_out','?')}d | briefing={score} ({rec})")

    # Cron agent tasks
    cron_twitter = sector_results.get("biotech", {}).get("twitter_query_count", 0)
    cron_conference = len(sector_results.get("discovery", {}).get("conference_queries", []))
    if cron_twitter or cron_conference:
        lines.append(f"\n🤖 CRON AGENT TASKS:")
        if cron_twitter:
            lines.append(f"  Run {cron_twitter} Twitter/X search queries (see cron_agent_tasks)")
        if cron_conference:
            lines.append(f"  Run {cron_conference} conference abstract queries")
        browser_tasks = adcom_result.get("browser_tasks", [])
        if browser_tasks:
            lines.append(f"  browser_task: {len(browser_tasks)} pages need JS rendering")

    # Sector status
    lines.append(f"\nSECTOR STATUS:")
    for sector_name, result in sector_results.items():
        if sector_name == "discovery":
            continue
        if "error" not in result:
            sig_count = result.get("signals", 0)
            lines.append(f"  {sector_name.upper()}: {sig_count} signals")
        else:
            lines.append(f"  {sector_name.upper()}: ERROR — {result['error'][:60]}")

    return "\n".join(lines)


if __name__ == "__main__":
    run_unified_scan()
