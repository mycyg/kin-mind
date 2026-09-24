"""Replays, fixtures and benchmarks run on copies, never on a live memory root (T1-07, T1-10, E3-10).

A root a service holds keeps its credential (local-token) and its pid claim (service.pid).
KIN_MEMORY_ROOT names the host's live root, and the hermetic test runner's KIN_PROTECTED_ROOTS
names every root a test must not reach. A root matching any of these is refused before the store
is opened, because opening it already migrates it."""

from __future__ import annotations

import os
from pathlib import Path

MARKERS = ("local-token", "service.pid")


def live_roots() -> list[Path]:
    named = [os.environ.get("KIN_MEMORY_ROOT", ""), *os.environ.get("KIN_PROTECTED_ROOTS", "").split(":")]
    return [Path(value).expanduser().resolve() for value in named if value]


def refuse_live(root) -> Path:
    root = Path(root).expanduser().resolve()
    for live in live_roots():
        if root == live or live in root.parents:
            raise SystemExit(f"refusing {root}: it is the live memory root {live}; run on a copy")
    held = [name for name in MARKERS if (root / name).exists()]
    if held:
        raise SystemExit(f"refusing {root}: {' and '.join(held)} say a service holds it; "
                         "run on a copy made without them")
    return root


def refuse_existing_store(root) -> Path:
    """A fixture writes into the scope it is given, the real one included, so it only ever
    builds a store of its own: a root that already holds one is refused before any write."""
    root = refuse_live(root)
    if (root / "memory.sqlite3").exists():
        raise SystemExit(f"refusing {root}: it already holds a store; a fixture only builds a new one")
    return root
