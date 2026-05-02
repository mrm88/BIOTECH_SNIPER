"""``python -m biotech_sniper.cli`` package-level entry point.

The CLI subpackage hosts multiple operator-triggered surfaces. By
convention the first positional argument selects the surface; the
remaining arguments are forwarded to the surface's own ``main()``.

Today only ``force_scan`` is wired through this dispatcher. New
surfaces register themselves in :data:`SUBCOMMANDS` below.
"""

from __future__ import annotations

import sys
from typing import Callable, Sequence

from biotech_sniper.cli import force_scan as _force_scan_module


SUBCOMMANDS: dict[str, Callable[[Sequence[str]], int]] = {
    "force_scan": lambda argv: _force_scan_module.main(argv=list(argv)),
    "force-scan": lambda argv: _force_scan_module.main(argv=list(argv)),
}


def _print_usage() -> None:
    sys.stderr.write(
        "usage: python -m biotech_sniper.cli <subcommand> [args...]\n"
        "available subcommands: "
        + ", ".join(sorted(set(SUBCOMMANDS))) + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        _print_usage()
        return 0 if args else 2
    name, rest = args[0], args[1:]
    handler = SUBCOMMANDS.get(name)
    if handler is None:
        _print_usage()
        sys.stderr.write(f"error: unknown subcommand {name!r}\n")
        return 2
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
