"""Is this interpreter the environment the core's uv.lock describes? (T1-02)

    python -I scripts/python_env.py [path/to/uv.lock]

The composed test runs with the interpreter the host runs, and a release ships only on the
environment the lock pins. Two rules, checked against this interpreter's installed
distributions: every distribution the lock also names is at a version the lock pins for
it, and every runtime dependency of the package (its dependencies, followed through the
lock, with markers evaluated for this interpreter) is installed. The package itself is left
out: the tests run its source tree, whichever copy is installed. Exit 0 with one line when
both hold, 1 with every difference otherwise. Nothing is installed or downloaded; the
environment is prepared before a release."""

from __future__ import annotations

import importlib.metadata as metadata
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def applies(marker: str | None) -> bool:
    """Whether a dependency edge holds for this interpreter. Without `packaging` a guarded
    edge is not required (a distribution installed anyway is still held to its pin)."""
    if not marker:
        return True
    try:
        from packaging.markers import Marker

        return Marker(marker).evaluate()
    except Exception:
        return False


def same(have: str, pinned: str) -> bool:
    try:
        from packaging.version import Version

        return Version(have) == Version(pinned)
    except Exception:
        return have == pinned


def root_name(lock: dict) -> str:
    """The locked project itself: the one package whose source is this directory."""
    for package in lock.get("package", []):
        source = package.get("source", {})
        if source.get("editable") == "." or source.get("virtual") == ".":
            return package["name"]
    return "kin-mind"


def closure(lock: dict, root: str) -> set[str]:
    """The root's runtime dependencies, transitively; optional and dev groups are not followed."""
    entries: dict[str, list[dict]] = {}
    for package in lock.get("package", []):
        entries.setdefault(key(package["name"]), []).append(package)
    needed: set[str] = set()
    todo = [(key(root), None)]
    while todo:
        name, version = todo.pop()
        for package in entries.get(name, []):
            if version is not None and package.get("version") != version:
                continue
            for edge in package.get("dependencies", []):
                if not applies(edge.get("marker")):
                    continue
                target = key(edge["name"])
                if target not in needed:
                    needed.add(target)
                    todo.append((target, edge.get("version")))
    needed.discard(key(root))
    return needed


def installed() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(key(name), dist.version)
    return found


def differences(lock: dict, have: dict[str, str]) -> list[str]:
    root = key(root_name(lock))
    pinned: dict[str, set[str]] = {}
    for package in lock.get("package", []):
        if "version" in package:
            pinned.setdefault(key(package["name"]), set()).add(package["version"])
    found = []
    for name, version in sorted(have.items()):
        if name != root and name in pinned and not any(same(version, pin) for pin in pinned[name]):
            found.append(f"{name} {version} (lock {', '.join(sorted(pinned[name]))})")
    for name in sorted(closure(lock, root) - set(have)):
        found.append(f"{name} missing (lock {', '.join(sorted(pinned.get(name, {'?'})))})")
    return found


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    lock_path = Path(argv[0]) if argv else ROOT / "uv.lock"
    found = differences(tomllib.loads(lock_path.read_text()), installed())
    if found:
        print(f"{sys.executable} is not the environment {lock_path} pins ({len(found)} difference(s)):",
              *(f"  {line}" for line in found), sep="\n", file=sys.stderr)
        return 1
    print(f"{sys.executable} matches {lock_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
