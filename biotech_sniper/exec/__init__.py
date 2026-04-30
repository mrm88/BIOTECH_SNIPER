"""Stage-2 execution-side helpers (Reading-B M3).

This subpackage hosts the dispatcher that turns a post-fanout, post-gate
:class:`biotech_sniper.llm.ensemble.EnsembleEventResult` plus its source
``candidate_events`` row into a single-leg ``news_event_entry``
play_card destined for :class:`biotech_sniper.paper_executor.PaperExecutor`.

Public surface (Reading-B M3):

* :mod:`biotech_sniper.exec.stage2_dispatcher`
"""
