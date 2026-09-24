"""Is this interpreter the environment the core's uv.lock resolves to for the extras a deployment
installs? (T1-02, CR2-OPS-08, CR2-MEM-03)

    python -I scripts/python_env.py [--extras graph,local-embedding,vector] [path/to/uv.lock]

uv.lock is one universal resolution. A package may be locked at several versions, each for some
Pythons, some platforms or one side of a declared conflict between extras, and every dependency
edge says by its marker when it holds. What one deployment installs is fixed by two things: the
interpreter's marker environment (Python version, platform ..., as packaging reports it) and the
project's extras the deployment chose. --extras names them, comma-separated; the default is
production's choice, PRODUCTION_EXTRAS, and an empty value means none.

The check walks the lock from the project through its dependencies and those extras, each edge's
marker evaluated for that environment with those extras switched on, and takes the one version
each edge selects. Every package in that closure must be installed at exactly that version. Every
other installed distribution the lock names must be at a version the lock selects for this
interpreter when the chosen extras are installed together with others that do not conflict with
them. The project itself is left out: the tests and the services run its source tree.

Exit 0 when the interpreter is that environment. Exit 1 with every difference when it is not.
Exit 2 when it cannot be decided, and nothing is skipped to make it decidable: an extra the lock
does not have, extras the lock declares in conflict, a marker that does not parse or cannot be
evaluated, an edge that selects no single version, a version whose resolution markers exclude
this interpreter, or a lock this check cannot read. Nothing is installed or downloaded; the
environment is prepared before a release."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import sys
import tomllib
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The extras production installs. Host mind-config.json runs the core with the kin-s8
# interpreter, which carries graph (igraph), vector (lancedb, pyarrow, numpy) and local-embedding
# (sentence-transformers, transformers, typer), and none of media, dev and all. The host's
# scripts/ci-local.sh passes the same list.
PRODUCTION_EXTRAS = ("graph", "local-embedding", "vector")


class Undecided(Exception):
    """The lock cannot be read for this interpreter. Never a reason to skip a requirement."""


def key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---- PEP 508 markers, with uv's conflict extras as a set ------------------------------------

VARIABLES = {"python_version", "python_full_version", "os_name", "sys_platform", "platform_release",
             "platform_system", "platform_version", "platform_machine", "platform_python_implementation",
             "implementation_name", "implementation_version", "extra"}
ALIASES = {"os.name": "os_name", "sys.platform": "sys_platform", "platform.version": "platform_version",
           "platform.machine": "platform_machine", "platform.python_implementation": "platform_python_implementation",
           "python_implementation": "platform_python_implementation"}
TOKEN = re.compile(r"""\s*(?:(?P<paren>[()])|(?P<op>===|==|!=|<=|>=|~=|<|>|not\s+in\b|in\b)"""
                   r"""|(?P<string>'[^']*'|"[^"]*")|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))""")
# In a lock's edges uv names only conflict items, extra-<length>-<package>-<extra> or group-...;
# the extras themselves are the optional-dependencies tables.
CONFLICT_ITEM = re.compile(r"(extra|group)-\d+-.+")
_parsed: dict[str, tuple] = {}


def parse(marker: str) -> tuple:
    """A marker as a tree: ("or", [...]), ("and", [...]) or ("cmp", left, op, right)."""
    if marker in _parsed:
        return _parsed[marker]
    tokens, position = [], 0
    while position < len(marker):
        if not marker[position:].strip():
            break
        found = TOKEN.match(marker, position)
        if not found or found.end() == position:
            raise Undecided(f"marker does not parse at {position}: {marker!r}")
        kind = found.lastgroup
        text = found.group(kind)
        if kind == "op":
            text = re.sub(r"\s+", " ", text)
        tokens.append((kind, text))
        position = found.end()
    index = 0

    def peek():
        return tokens[index] if index < len(tokens) else (None, None)

    def take():
        nonlocal index
        if index >= len(tokens):
            raise Undecided(f"marker ends too early: {marker!r}")
        index += 1
        return tokens[index - 1]

    def value():
        kind, text = take()
        if kind == "string":
            return ("str", text[1:-1])
        if kind == "word" and (text in VARIABLES or text in ALIASES):
            return ("var", ALIASES.get(text, text))
        raise Undecided(f"marker has {text!r} where a variable or a string belongs: {marker!r}")

    def atom():
        if peek() == ("paren", "("):
            take()
            node = either()
            if take() != ("paren", ")"):
                raise Undecided(f"marker has an unclosed parenthesis: {marker!r}")
            return node
        left = value()
        kind, op = take()
        if kind != "op":
            raise Undecided(f"marker has no comparison after {left[1]!r}: {marker!r}")
        return ("cmp", left, op, value())

    def both():
        nodes = [atom()]
        while peek() == ("word", "and"):
            take()
            nodes.append(atom())
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def either():
        nodes = [both()]
        while peek() == ("word", "or"):
            take()
            nodes.append(both())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    if not tokens:
        raise Undecided(f"marker is empty: {marker!r}")
    tree = either()
    if index != len(tokens):
        raise Undecided(f"marker has more after a complete expression: {marker!r}")
    _parsed[marker] = tree
    return tree


def compare(left: str, op: str, right: str) -> bool:
    """PEP 508's comparison as packaging evaluates it: a version comparison whenever the operator
    and the right side form a version specifier, otherwise the string operator."""
    if op == "in":
        return left in right
    if op == "not in":
        return left not in right
    try:
        from packaging.specifiers import InvalidSpecifier, Specifier
    except ImportError as error:
        raise Undecided("packaging is not importable; version markers cannot be evaluated") from error
    try:
        spec = Specifier(op + right)
    except InvalidSpecifier:
        spec = None
    if spec is not None:
        try:
            return spec.contains(left, prereleases=True)
        except Exception as error:
            raise Undecided(f"cannot compare {left!r} {op} {right!r}") from error
    operators = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
                 "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
    if op not in operators:
        raise Undecided(f"cannot compare {left!r} {op} {right!r}")
    return operators[op](left, right)


def evaluate(node: tuple, env: dict, extras: frozenset) -> bool:
    """Every part is evaluated, so a part that cannot be is never hidden by a short circuit."""
    if node[0] in ("and", "or"):
        results = [evaluate(part, env, extras) for part in node[1]]
        return all(results) if node[0] == "and" else any(results)
    _, left, op, right = node
    if (left[0] == "var") == (right[0] == "var"):
        raise Undecided(f"marker compares {left[1]!r} with {right[1]!r}; one side must be a variable")
    variable, literal = (left, right) if left[0] == "var" else (right, left)
    if variable[1] == "extra":
        if op not in ("==", "!=") or not CONFLICT_ITEM.fullmatch(key(literal[1])):
            raise Undecided(f"marker tests extra {op} {literal[1]!r}, which is not a conflict item of the lock")
        member = key(literal[1]) in extras
        return member if op == "==" else not member
    if variable[1] not in env:
        raise Undecided(f"the environment has no {variable[1]}")
    actual = env[variable[1]]
    return compare(actual, op, literal[1]) if left[0] == "var" else compare(literal[1], op, actual)


def holds(marker: str | None, env: dict, extras: frozenset) -> bool:
    return True if not marker else evaluate(parse(marker), env, extras)


def environment() -> dict:
    try:
        from packaging.markers import default_environment
    except ImportError as error:
        raise Undecided("packaging is not importable; the marker environment cannot be read") from error
    return dict(default_environment())


def within(requires: str, python: str) -> bool:
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError as error:
        raise Undecided("packaging is not importable; requires-python cannot be evaluated") from error
    try:
        return SpecifierSet(requires).contains(python, prereleases=True)
    except Exception as error:
        raise Undecided(f"cannot read requires-python {requires!r}") from error


def same(have: str, pinned: str) -> bool:
    try:
        from packaging.version import Version

        return Version(have) == Version(pinned)
    except Exception:
        return have == pinned


# ---- the lock -------------------------------------------------------------------------------

class Lock:
    def __init__(self, data: dict):
        if not isinstance(data.get("package"), list):
            raise Undecided("the lock has no [[package]] entries")
        self.entries: dict[str, list[dict]] = {}
        for package in data["package"]:
            self.entries.setdefault(key(package["name"]), []).append(package)
        roots = [p for p in data["package"] if p.get("source", {}).get("editable") == "."
                 or p.get("source", {}).get("virtual") == "."]
        if len(roots) != 1:
            raise Undecided(f"the lock has {len(roots)} projects at '.'; exactly one is expected")
        self.root = roots[0]
        self.name = key(self.root["name"])
        # The project's extras and dependency groups, and uv's name for each inside markers.
        self.sections: dict[str, tuple[str, list[dict]]] = {}
        for kind, table in (("extra", "optional-dependencies"), ("group", "dev-dependencies")):
            for section, edges in (self.root.get(table) or {}).items():
                self.sections[f"{kind}:{key(section)}"] = (self.encode(kind, self.name, section), edges)
        self.conflicts: list[set[str]] = []
        for group in data.get("conflicts") or []:
            items = set()
            for item in group:
                kind = "extra" if "extra" in item else "group" if "group" in item else None
                if kind is None:
                    raise Undecided(f"a conflict names neither an extra nor a group: {item}")
                items.add(self.encode(kind, key(item.get("package", self.name)), item[kind]))
            self.conflicts.append(items)
        # Parse every marker once, before anything is decided: one that does not parse stops here.
        for package in data["package"]:
            for marker in package.get("resolution-markers") or []:
                parse(marker)
            for edge in self.edges_of(package):
                if edge.get("marker"):
                    parse(edge["marker"])
        for table in ("resolution-markers", "supported-markers"):
            for marker in data.get(table) or []:
                parse(marker)

    @staticmethod
    def encode(kind: str, package: str, name: str) -> str:
        package = key(package)
        return f"{kind}-{len(package)}-{package}-{key(name)}"

    @staticmethod
    def edges_of(package: dict):
        yield from package.get("dependencies") or []
        for table in ("optional-dependencies", "dev-dependencies"):
            for edges in (package.get(table) or {}).values():
                yield from edges

    def extra(self, name: str) -> str:
        section = f"extra:{key(name)}"
        if section not in self.sections:
            have = ", ".join(s.split(":", 1)[1] for s in self.sections if s.startswith("extra:")) or "none"
            raise Undecided(f"uv.lock has no extra {name!r} (it has {have})")
        return section

    def choose(self, extras) -> set[str]:
        return {self.extra(name) for name in extras}

    def expand(self, env: dict, sections: set[str]) -> set[str]:
        """The sections with the project's own extras they name (kin-mind[vector]), followed."""
        sections = set(sections)
        while True:
            active = frozenset(self.sections[s][0] for s in sections)
            named = set()
            for edge in [*(self.root.get("dependencies") or []), *(e for s in sections for e in self.sections[s][1])]:
                if key(edge["name"]) == self.name and holds(edge.get("marker"), env, active):
                    named |= {self.extra(x) for x in edge.get("extra") or []}
            if named <= sections:
                return sections
            sections |= named

    def clashes(self, sections: set[str]) -> list[str]:
        """Each declared conflict these sections break, as 'a and b'."""
        names = {self.sections[s][0]: s.split(":", 1)[1] for s in sections}
        return [" and ".join(sorted(names[item] for item in conflict & names.keys()))
                for conflict in self.conflicts if len(conflict & names.keys()) > 1]

    def activate(self, env: dict, sections: set[str]) -> tuple[set[str], frozenset]:
        sections = self.expand(env, sections)
        clashes = self.clashes(sections)
        if clashes:
            raise Undecided(f"extras {'; '.join(clashes)} are declared in conflict in uv.lock: "
                            "no locked resolution installs them together")
        return sections, frozenset(self.sections[s][0] for s in sections)

    def resolve(self, env: dict, sections: set[str]) -> dict[str, dict]:
        """The one entry each package in the closure resolves to, for this environment and these
        sections."""
        sections, active = self.activate(env, sections)
        todo = list(self.root.get("dependencies") or [])
        for section in sections:
            todo += self.sections[section][1]
        chosen: dict[str, dict] = {}
        opened: set[tuple[str, str | None]] = set()
        while todo:
            edge = todo.pop()
            if not holds(edge.get("marker"), env, active):
                continue
            name = key(edge["name"])
            if name == self.name:
                outside = [x for x in edge.get("extra") or [] if self.extra(x) not in sections]
                if outside:
                    raise Undecided(f"a dependency asks for the project's extras {outside}, which the chosen extras do not include")
                continue
            candidates = self.entries.get(name)
            if not candidates:
                raise Undecided(f"an edge names {name}, which the lock does not have")
            if "version" in edge:
                candidates = [c for c in candidates if c.get("version") == edge["version"]]
                if len(candidates) > 1 and "source" in edge:
                    candidates = [c for c in candidates if c.get("source") == edge["source"]]
            if len(candidates) != 1:
                raise Undecided(f"an edge to {name} selects {len(candidates)} locked versions, not one")
            entry = candidates[0]
            markers = entry.get("resolution-markers")
            if markers and not any(holds(marker, env, active) for marker in markers):
                raise Undecided(f"{name} {entry['version']} is selected, but its resolution markers exclude this interpreter")
            prior = chosen.get(name)
            if prior is not None and prior.get("version") != entry.get("version"):
                raise Undecided(f"{name} resolves to both {prior.get('version')} and {entry.get('version')}")
            chosen[name] = entry
            for part in [None, *(key(x) for x in edge.get("extra") or [])]:
                if (name, part) in opened:
                    continue
                opened.add((name, part))
                if part is None:
                    todo += entry.get("dependencies") or []
                else:
                    todo += (entry.get("optional-dependencies") or {}).get(part, [])
        return chosen

    def selectable(self, env: dict, chosen: set[str]) -> dict[str, set[str]]:
        """Every version the lock installs on this interpreter when the chosen sections are
        installed with any others the conflicts allow beside them. Only sections that are, or
        bring in, one in a conflict change which version an edge selects, so all the others are
        taken, with each allowed combination of those."""
        conflicting = {encoded for conflict in self.conflicts for encoded in conflict}
        touching = {s for s in self.sections if any(self.sections[t][0] in conflicting for t in self.expand(env, {s}))}
        fixed = (set(self.sections) - touching) | set(chosen)
        free = sorted(touching - set(chosen))
        versions: dict[str, set[str]] = {}
        for size in range(len(free) + 1):
            for subset in combinations(free, size):
                sections = self.expand(env, fixed | set(subset))
                if self.clashes(sections):
                    continue
                for name, entry in self.resolve(env, sections).items():
                    versions.setdefault(name, set()).add(entry["version"])
        return versions


def installed() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(key(name), dist.version)
    return found


def check(data: dict, have: dict[str, str], env: dict, *, extras) -> tuple[list[str], dict]:
    """(differences, what was checked) for the interpreter with these installed distributions and
    this marker environment, deployed with these extras. Raises Undecided."""
    lock = Lock(data)
    chosen = lock.choose(extras)
    sections, active = lock.activate(env, chosen)
    taken = {"extras": sorted(s.split(":", 1)[1] for s in sections if s.startswith("extra:")), "packages": 0}
    python = env.get("python_full_version")
    if not python:
        raise Undecided("the environment has no python_full_version")
    requires = data.get("requires-python")
    if requires and not within(requires, python):
        return [f"Python {python} is outside the lock's requires-python {requires}"], taken
    for table in ("supported-markers", "resolution-markers"):
        markers = data.get(table) or []
        if markers and not any(holds(marker, env, active) for marker in markers):
            return [f"uv.lock resolves nothing for Python {python} on {env.get('sys_platform')}: none of its {table} holds"], taken
    reached = lock.resolve(env, chosen)
    taken["packages"] = len(reached)
    found = []
    for name, entry in sorted(reached.items()):
        if name not in have:
            found.append(f"{name} missing (lock {entry['version']})")
        elif not same(have[name], entry["version"]):
            found.append(f"{name} {have[name]} (lock {entry['version']})")
    selectable = lock.selectable(env, chosen)
    for name, version in sorted(have.items()):
        if name == lock.name or name in reached or name not in lock.entries:
            continue
        allowed = selectable.get(name, set())
        if not any(same(version, pin) for pin in allowed):
            found.append(f"{name} {version} (lock {', '.join(sorted(allowed))})" if allowed
                         else f"{name} {version} (not locked for this interpreter beside these extras)")
    return found, taken


def differences(data: dict, have: dict[str, str], env: dict | None = None, *, extras) -> list[str]:
    return check(data, have, environment() if env is None else env, extras=extras)[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python_env.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--extras", default=",".join(PRODUCTION_EXTRAS),
                        help="the project's extras the deployment installs, comma-separated; "
                             f"default {','.join(PRODUCTION_EXTRAS)} (production), empty for none")
    parser.add_argument("lock", nargs="?", default=str(ROOT / "uv.lock"), help="the uv.lock (default: the core's)")
    args = parser.parse_args(argv)
    extras = [name.strip() for name in args.extras.split(",") if name.strip()]
    lock_path = Path(args.lock)
    try:
        env = environment()
        found, taken = check(tomllib.loads(lock_path.read_text()), installed(), env, extras=extras)
    except Exception as error:  # noqa: BLE001 - whatever stops the check decides nothing, never "differs"
        reason = str(error) if isinstance(error, Undecided) else f"{type(error).__name__}: {error}"
        print(f"{sys.executable}: cannot decide whether this is the environment {lock_path} pins for extras "
              f"{', '.join(extras) or 'none'}: {reason}", file=sys.stderr)
        return 2
    where = (f"Python {env.get('python_full_version')} on {env.get('sys_platform')}, "
             f"extras {', '.join(taken['extras']) or 'none'}")
    if found:
        print(f"{sys.executable} is not the environment {lock_path} pins ({where}; {len(found)} difference(s)):",
              *(f"  {line}" for line in found), sep="\n", file=sys.stderr)
        return 1
    print(f"{sys.executable} matches {lock_path.name} ({where}: {taken['packages']} locked packages at their versions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
