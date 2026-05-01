# Biotech Sniper — top-level project Makefile.
#
# Targets here are CI gates and developer entry points. Reading-B's
# f-cross-06 secrets-and-network audit feature adds the
# ``audit-secrets`` and ``audit-network`` targets used by both the
# pytest suite (VAL-CROSS-043 / VAL-CROSS-044) and any future CI
# wiring (.github/workflows/*.yml or pre-commit hook).

PY := .venv/bin/python
PYTEST := .venv/bin/pytest


.PHONY: help test audit-secrets audit-network audit lint

help:
	@echo "Available targets:"
	@echo "  test            Run full test suite under -n 2"
	@echo "  audit-secrets   Run source-tree secret-pattern audit (VAL-CROSS-044)"
	@echo "  audit-network   Run network whitelist completeness audit (VAL-CROSS-043)"
	@echo "  audit           Run both audits (audit-secrets + audit-network)"
	@echo "  lint            py_compile every biotech_sniper module"

test:
	$(PYTEST) -q -n 2

# VAL-CROSS-044 — source-tree secret-pattern audit gate.
# Runs the canonical pytest module that scans every text-shaped file
# under the repo for secret-shaped tokens (sk-, pplx-, ALPACA_KEY_ID=,
# ghp_, xai-, AKIA...). Returns non-zero on any finding.
audit-secrets:
	$(PYTEST) -q tests/test_no_secret_leakage.py

# VAL-CROSS-043 — code-level network whitelist completeness audit.
# Runs the static URL-extraction regression that asserts every
# hostname constructed under biotech_sniper/ resolves to an entry in
# ALLOWED_NETWORK_HOSTS (or a tightly documented exception).
audit-network:
	$(PYTEST) -q tests/test_network_whitelist_completeness.py tests/test_network_whitelist.py

audit: audit-secrets audit-network

lint:
	$(PY) -m py_compile $$(find biotech_sniper -name '*.py')
