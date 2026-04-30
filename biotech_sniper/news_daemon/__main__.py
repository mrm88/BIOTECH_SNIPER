"""``python -m biotech_sniper.news_daemon`` entrypoint.

This module is intentionally tiny — it forwards ``sys.argv`` to
:func:`biotech_sniper.news_daemon.poll_loop.main` and exits with the
returned status code.  All argument parsing, logging configuration,
and lifecycle management lives in :mod:`poll_loop` so that the
function is unit-testable without subprocess overhead.
"""

from __future__ import annotations

import sys

from biotech_sniper.news_daemon.poll_loop import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
