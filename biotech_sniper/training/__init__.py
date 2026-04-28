"""Training-data assembly for the biotech_sniper supplementary ranker.

This subpackage owns the pipelines that emit parquet feature stores
under ``data/training/`` for downstream model training (LightGBM
ranker in M5). The flagship module is
:mod:`biotech_sniper.training.build_feature_store` which joins
resolved plays + performance-ledger IV snapshots + ScienceProfile
records + calibration parameters + company-pipeline metadata into a
single ``data/training/catalysts.parquet`` row per (catalyst → outcome)
pair.

The companion module
:mod:`biotech_sniper.training.train_ranker` consumes that parquet and
produces a versioned LightGBM booster (``models/ranker_v{N}.lgb``)
plus a JSON manifest sidecar — the supplementary signal layer behind
the ``LIGHTGBM_RANKER_ENABLED`` feature flag.
"""
