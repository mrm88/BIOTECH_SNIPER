"""LLM provider clients for the biotech_sniper package.

This sub-package houses the thin SDK / HTTP wrappers around the LLM
providers used by the scoring stack:

* :mod:`biotech_sniper.llm.xai_client` — Grok-4 fast-tier ranking
  (M2, this milestone).
* :mod:`biotech_sniper.llm.claude_client` — Claude Opus deep-tier
  science reasoning (M2, this milestone).
* :mod:`biotech_sniper.llm.gemini_client` — Gemini 2.5 Pro deep-tier
  ensemble partner (M2, this milestone).

Every client in this package MUST:

* Read its API key via :mod:`biotech_sniper.config` only — never raw
  ``os.environ`` lookups.
* Log a per-call row into the SQLite ``llm_cost_ledger`` table after
  each successful response (``provider``, ``model_id``, ``purpose``,
  token counts, latency, USD cost).
* Surface typed errors (e.g. :class:`XAIAuthError`) so callers can
  short-circuit on auth failures instead of retrying.
"""
