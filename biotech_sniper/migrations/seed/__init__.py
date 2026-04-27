"""Seed-data sub-package for migrations.

Modules under this package re-export the tabular seed data preserved by
f-m1-03. The canonical home of those modules is
``biotech_sniper/state/`` (the ``.py`` modules stayed put when only the
``*.json`` state files moved into ``migrations/seed/``). This package
provides import paths under ``biotech_sniper.migrations.seed.*`` so
validators and downstream consumers can rely on a single, predictable
namespace for seed data regardless of where the underlying ``.py``
file physically lives.
"""
