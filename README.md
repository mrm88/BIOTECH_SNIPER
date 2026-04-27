# BIOTECH_SNIPER

Biotech catalyst-driven options paper-trading system.

This repository is the canonical source of truth for the Biotech Sniper project.
It is being bootstrapped from a 22k-LOC unzipped reference project under
`biotech_sniper/` (staged locally, not yet committed) and will be incrementally
refactored across Milestones M1–M5 of the mission build.

## Status

Bootstrap commit. The full project tree (paths refactor, requirements,
config, SQLite migrations, multi-LLM scoring, Alpaca paper executor,
systemd units, backtest harness) lands in subsequent milestone features.

## Layout (target end state)

- `biotech_sniper/` — main Python package (refactored from the unzipped reference)
- `tests/` — pytest suites with VCR cassettes
- `data/` — SQLite database (gitignored)
- `state/` — runtime JSON state (gitignored)
- `reports/` — generated XLSX/PDF/HTML reports (gitignored)
- `logs/` — local log output (gitignored)
- `requirements.txt` — pinned dependencies
- `.env.example` — required env-var names (no secrets)
- `paths.py` — single source of truth for filesystem paths
- `config.py` — secrets + feature flags + risk defaults

## Environment

- `BIOTECH_SNIPER_HOME` — repo root (defaults to the directory containing `paths.py`)
- See `.env.example` (added in M1) for the full env-var list

## Deploy target

The VPS at `root@199.247.25.111` clones this repo into
`/root/alpha_sniper/repo/` and runs cron-driven daily / intraday / watchdog
oneshots via systemd timers (added in M4). Paper-trading only.

## License

Private / unreleased.
