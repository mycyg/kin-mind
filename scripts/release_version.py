"""The package version moves with every release (T1-14).

    python scripts/release_version.py                   print the version in pyproject.toml
    python scripts/release_version.py --bump [part]     write the next version (patch by default)
    python scripts/release_version.py --check REV       exit 1 when the package changed since REV
                                                        but its version did not

A release bumps the version in the commit it ships. The deploy gate runs --check against the
commit it last deployed, so two different builds can never carry the same version."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# What goes into the wheel and the sdist, and what pins what they run with.
PACKAGE = ("src/eventmem", "src/kin_mind", "pyproject.toml", "uv.lock")
LINE = re.compile(r'^version = "(\d+)\.(\d+)\.(\d+)"$', re.M)
# The project's own entry in uv.lock carries the same version; a lock left behind would no
# longer match its pyproject.toml.
LOCKED_ROOT = re.compile(r'(\[\[package\]\]\nname = "[^"]+"\nversion = ")[^"]+("\nsource = \{ (?:editable|virtual) = "\." \})')


def version(text: str) -> tuple[int, int, int]:
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    found = LINE.search(project)
    if not found:
        raise SystemExit('pyproject.toml has no [project] version = "X.Y.Z"')
    return tuple(int(part) for part in found.groups())


def bumped(current: tuple[int, int, int], part: str) -> tuple[int, int, int]:
    major, minor, patch = current
    return {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}[part]


def write(root: Path, new: tuple[int, int, int]):
    path = root / "pyproject.toml"
    text = path.read_text()
    head, rest = text.split("[project]", 1)
    project, sep, tail = rest.partition("\n[")
    path.write_text(head + "[project]" + LINE.sub('version = "%d.%d.%d"' % new, project, count=1) + sep + tail)
    lock = root / "uv.lock"
    if lock.exists():
        lock.write_text(LOCKED_ROOT.sub(lambda m: m.group(1) + "%d.%d.%d" % new + m.group(2), lock.read_text(), count=1))


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def check(root: Path, since: str) -> str | None:
    """Why the version must move before this tree ships, or None when it need not."""
    before = git(root, "show", f"{since}:pyproject.toml")
    if before.returncode:
        raise SystemExit(f"cannot read pyproject.toml at {since}: {before.stderr.strip()}")
    changed = git(root, "diff", "--quiet", since, "--", *PACKAGE).returncode
    if changed not in (0, 1):
        raise SystemExit(f"cannot compare the package with {since}")
    now = version((root / "pyproject.toml").read_text())
    if changed and version(before.stdout) >= now:
        return ("the package changed since %s but its version is still %s; "
                "run python scripts/release_version.py --bump" % (since, ".".join(map(str, now))))
    return None


def main(argv=None, root: Path = ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--bump", nargs="?", const="patch", choices=["patch", "minor", "major"])
    action.add_argument("--check", metavar="REV")
    args = parser.parse_args(argv)
    current = version((root / "pyproject.toml").read_text())
    if args.bump:
        new = bumped(current, args.bump)
        write(root, new)
        print(".".join(map(str, new)))
    elif args.check:
        reason = check(root, args.check)
        if reason:
            print(reason, file=sys.stderr)
            return 1
        print(".".join(map(str, current)))
    else:
        print(".".join(map(str, current)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
