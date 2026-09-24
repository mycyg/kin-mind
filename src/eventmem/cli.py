"""The `eventmem` command: the MemoryPalace service and its tools (eventmem.core.cli)."""

from __future__ import annotations

import sys

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from eventmem.core.cli import main as core_main
    from eventmem.core.integrity import enforce_source_root

    # A command line is pointed at whatever checkout its operator meant to use, so a
    # foreign source is said out loud here and never acted on; the services that must not
    # start on the wrong code refuse for themselves.
    enforce_source_root(warn_only=True)
    return core_main(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
