"""Register one release's configuration declaration in the memory store, as a versioned source.

    python -m kin_mind.register_configuration --root <MemoryPalace> \\
        --scope '{"project":"personal","persona":"Kin","collection":"default","world":"real"}' \\
        --version <agent_version> --declaration <declaration.json>

kin-deploy calls it once a release has succeeded as a whole, and writes the source id it answers
into the configuration registration (`kin-self-knowledge.json`), so the two name each other.

The declaration is kept as a source of namespace `kin-configuration-declaration`, which the
source-origin table reads as host configuration (class `role_configuration`): shown in the
self-knowledge view and to audit, never recalled as a shared experience, never extracted
(`extract=False`), written by an operation and not by the owner. Nothing else is touched — not
the mind's state, not its persona and not the memory settings: a configuration is not growth,
and `MemoryContinuity.configure` is called only when a memory setting itself changes, which is
not this command's business.

Idempotent by version and content. The source's version is `<agent_version>+<first 16 hex of the
sha256>` of the declaration's canonical JSON (keys sorted, no insignificant space), so the same
declaration again answers `unchanged` with the same source id and writes nothing, and a changed
one under the same agent version is a new version of the source, which supersedes the one before
as any newer source version does.

It prints one JSON object on standard output:

    {"state": "registered" | "unchanged" | "failed", "sourceId": "src_…" | null,
     "version": "<agent_version>", "sha256": "<hex>" | null}

with an `error` naming what failed, never a word of the declaration. Exit status 0 for
`registered` and `unchanged`, 1 for `failed`; a usage error exits 2, as argparse does.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

NAMESPACE = "kin-configuration-declaration"
KEY = "release-configuration"
# A version the source's own version field still holds with the content hash after it.
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")


class Failed(Exception):
    """What the command reports as `failed`: a short reason, no content."""


def canonical(declaration):
    """The declaration as the text that is stored and hashed: one JSON object, keys sorted."""
    if not isinstance(declaration, dict):
        raise Failed("the declaration is not one JSON object")
    return json.dumps(declaration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def register(root, scope, version, declaration):
    """Register `declaration` (a dict) for `version` in `scope`. Returns the report."""
    from eventmem.core import Engine
    from eventmem.core.db import Deleted, digest
    from eventmem.core.models import Scope, SourceInput

    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise Failed("the version is not a short plain name")
    try:
        scope = scope if isinstance(scope, Scope) else Scope.model_validate(scope)
    except ValueError:
        raise Failed("the scope is not a valid scope") from None
    text = canonical(declaration)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store = Path(root).expanduser() / "memory.sqlite3"
    if not store.is_file():
        # A mistyped root would otherwise become a new, empty store holding only this.
        raise Failed(f"no memory store at {store}")
    source = SourceInput(namespace=NAMESPACE, key=KEY, version=f"{version}+{sha[:16]}", scope=scope, text=text,
                         media_type="application/json", title=f"Configuration declaration {version}",
                         kind="knowledge", authority="operation", extract=False,
                         metadata={"agent_version": version, "sha256": sha, "configuration_declaration": True})
    engine = Engine(store.parent)
    sid = "src_" + digest([source.namespace, source.key, source.version, source.scope.key()])[:32]
    with engine.db.connect() as conn:
        existed = bool(conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone())
    try:
        received = engine.receive(source)
    except Deleted:
        raise Failed("this version of the declaration was deleted from the store") from None
    return {"state": "unchanged" if existed else "registered", "sourceId": received["id"], "version": version,
            "sha256": sha}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m kin_mind.register_configuration",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, help="the memory root (the directory holding memory.sqlite3)")
    parser.add_argument("--scope", required=True, help="the scope, as a JSON object")
    parser.add_argument("--version", required=True, help="the release's agent version")
    parser.add_argument("--declaration", required=True, help="a JSON file holding one object")
    args = parser.parse_args(argv)
    report = {"state": "failed", "sourceId": None, "version": args.version, "sha256": None}
    try:
        try:
            scope = json.loads(args.scope)
        except ValueError:
            raise Failed("the scope is not JSON") from None
        try:
            declaration = json.loads(Path(args.declaration).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise Failed("the declaration file cannot be read as JSON") from None
        report = register(args.root, scope, args.version, declaration)
    except Failed as failure:
        report["error"] = str(failure)
    except Exception as error:  # noqa: BLE001 - the report names the kind of failure, never content
        report["error"] = type(error).__name__
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["state"] in ("registered", "unchanged") else 1


if __name__ == "__main__":
    sys.exit(main())
