"""Deployment assertions for a rehearsal, read-only unless given a scratch directory (mind review
2026-09-24, deployment conditions; mind2 review).

Two facts the code alone cannot hold and a deployment must:

- the persona canon has its external approval record: mind-config `persona_contract` names the
  version and all three hashes, the canon the Python consumers read (the memory root's
  `persona-policy.json`) matches it, and the owner sources its `approved_source` names exist in
  its scope as her own explicit sources;
- exploration never reads the host, the memory store or Kin's Codex home, whatever roots the owner
  authorizes and however a path reaches them: the effective deny list is computed the way an
  exploration run builds it, and paths into each protected root, directly and through a symlink
  when a scratch directory is given, are refused by the reader itself.

By default nothing here writes: the memory store is opened read-only, no credentials file is
read, and no persona text, source text or file content is returned; each answer names problems by
code only. `--scratch DIR` is the one exception: the symlink probe then creates one symlink per
protected root inside DIR and leaves it there, so DIR must be a rehearsal's own temporary directory,
never an owner root.

    python -m kin_mind.deploy_checks /path/to/mind-config.json [--scratch DIR]

prints both answers as JSON and exits 0 when both hold, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from eventmem.core.models import Scope
from eventmem.core.persona import load_persona
from eventmem.core.read_policy import _IDENTIFIER

from .codex_executor import protected_roots, reader_settings
from .computer import ComputerReader

APPROVAL_FIELDS = ("version", "core_sha256", "voice_sha256", "maintenance_sha256")
APPROVED_SOURCE_FIELDS = ("approved_source", "approved_sources", "approved_source_id", "approved_source_ids")
HEX64 = re.compile(r"[0-9a-f]{64}")
# What a probe inside each protected root stands for; nothing at these paths is ever read.
PROBES = {"host": ("mind-config.json", "state/mind-status.json"), "memory": ("memory.sqlite3", "persona-policy.json"),
          "codex-home": ("auth.json", "config.toml")}


def _read_only(path):
    return sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=5)


def _owner_source(conn, identifier, scope_key):
    row = conn.execute("SELECT scope,data,deleted FROM sources WHERE id=?", (identifier,)).fetchone()
    if not row:
        return "approved-source-unresolved:" + identifier
    if row[2]:
        return "approved-source-deleted:" + identifier
    if row[0] != scope_key:
        return "approved-source-other-scope:" + identifier
    if json.loads(row[1]).get("authority") != "explicit":
        return "approved-source-not-owner:" + identifier
    return None


def persona_approval(config):
    """Whether the persona canon's external approval record is complete and holds. Read-only."""
    if not config.get("root") or not isinstance(config.get("scope"), dict):
        return {"ok": False, "problems": ["memory-root-or-scope-unconfigured"], "version": None, "approved_ids": 0}
    problems = []
    approved = config.get("persona_contract")
    if not isinstance(approved, dict):
        problems.append("approval-record-missing")
        approved = {}
    for field in APPROVAL_FIELDS:
        value = approved.get(field)
        complete = isinstance(value, str) and (bool(value.strip()) if field == "version" else bool(HEX64.fullmatch(value)))
        if not complete:
            problems.append("approval-record-incomplete:" + field)
    root = Path(config["root"]).expanduser().resolve()
    canon = root / "persona-policy.json"
    # The host reads `persona_contract.path` when it names one; the mind reads the memory root's.
    if approved.get("path") and Path(approved["path"]).expanduser().resolve() != canon:
        problems.append("persona-contract-path-differs")
    scope = Scope.model_validate(config["scope"])
    policy = None
    if not canon.is_file():
        problems.append("persona-contract-missing")
    else:
        try:
            policy = load_persona(SimpleNamespace(db=SimpleNamespace(root=root)), scope)
            if policy is None:
                problems.append("persona-contract-other-scope")
        except ValueError:
            problems.append("persona-contract-invalid")
    named = []
    if policy:
        for field in APPROVAL_FIELDS:
            if field in approved and approved.get(field) != policy.get(field):
                problems.append("approval-record-differs:" + field)
        named = sorted({i for field in APPROVED_SOURCE_FIELDS for i in _IDENTIFIER.findall(json.dumps(policy.get(field, "")))})
        if not named:
            problems.append("approved-source-missing")
        database = root / "memory.sqlite3"
        try:
            with _read_only(database) as conn:
                for identifier in named:
                    if identifier.startswith("src_"):
                        problem = _owner_source(conn, identifier, scope.key())
                        if problem:
                            problems.append(problem)
                        continue
                    row = conn.execute("SELECT scope,data,deleted FROM records WHERE id=?", (identifier,)).fetchone()
                    if not row or row[2] or row[0] != scope.key():
                        problems.append("approved-source-unresolved:" + identifier)
                        continue
                    sources = json.loads(row[1]).get("source_ids") or []
                    found = [_owner_source(conn, source, scope.key()) for source in sources]
                    if not sources or all(found):
                        problems.append("approved-source-not-owner:" + identifier)
        except sqlite3.Error:
            problems.append("memory-store-unreadable")
    return {"ok": not problems, "problems": problems, "version": policy.get("version") if policy else None,
            "approved_ids": len(named)}


def _within(path, root):
    return path == root or root in path.parents


def exploration_exclusions(config, *, scratch=None):
    """Whether the exploration reader refuses the host, the memory store and Kin's Codex home,
    directly and through symlinks. Read-only without `scratch`; with it, one symlink per protected
    root is made inside `scratch` and left there."""
    computer = config.get("computer_exploration") or {}
    if not (computer.get("enabled") and computer.get("file_reader_enabled", True)):
        return {"ok": True, "problems": [], "reader": "disabled", "refused": 0}
    protected = {"host": config.get("host_root"), "memory": config.get("root"),
                 "codex-home": config.get("native_codex_home") or (Path(config["host_root"]) / "state" / "codex-home"
                                                                   if config.get("host_root") else None)}
    problems = [("protected-root-unknown:" + name) for name, root in protected.items() if not root and name != "codex-home"]
    protected = {name: Path(root).expanduser().resolve() for name, root in protected.items() if root}
    # The run's own directory stands in for the one a real run gets; it is never created.
    run = Path(config.get("exploration_directory") or "/nonexistent") / "deploy-check-run"
    settings = reader_settings({**computer, "protected_roots": protected_roots(config)}, directory=run, attempt=1,
                               ledger=run / "computer-observations.json",
                               codex_home=protected.get("codex-home"))
    reader = ComputerReader(settings)
    denied = [Path(root).expanduser().resolve() for root in settings["internal_deny_roots"]]
    for name, root in protected.items():
        if not any(_within(root, deny) for deny in denied):
            problems.append("protected-root-not-denied:" + name)
    for authorized in reader.roots:
        for name, root in protected.items():
            if _within(authorized, root):
                problems.append("authorized-root-inside-protected:" + name)
    refused = 0

    def probe(path, label):
        nonlocal refused
        try:
            reader.checked_path(path)
        except ValueError:
            refused += 1
            return
        problems.append("readable:" + label)

    for name, root in protected.items():
        for relative in ("",) + PROBES[name]:
            probe(root / relative if relative else root, name + (":" + relative if relative else ""))
    if scratch is not None:
        # A symlink inside a rehearsal directory, authorized for this probe alone, that points into
        # each protected root: the reader resolves it and must refuse it all the same.
        scratch = Path(scratch).expanduser().resolve()
        linked = ComputerReader({**settings, "roots": [str(scratch)]})
        for name, root in protected.items():
            link = scratch / ("deploy-check-link-" + name)
            if not link.is_symlink():
                link.symlink_to(root, target_is_directory=True)
            for relative in PROBES[name]:
                try:
                    linked.checked_path(link / relative)
                except ValueError:
                    refused += 1
                    continue
                problems.append("readable-through-symlink:" + name + ":" + relative)
    return {"ok": not problems, "problems": problems, "reader": "enabled", "refused": refused}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config")
    parser.add_argument("--scratch")
    args = parser.parse_args(argv)
    path = Path(args.config).resolve()
    # The configuration only: its credentials file is never opened here.
    config = json.loads(path.read_text())
    config.setdefault("host_root", str(path.parent))
    answer = {"persona": persona_approval(config), "exploration": exploration_exclusions(config, scratch=args.scratch)}
    print(json.dumps(answer, ensure_ascii=False, sort_keys=True))
    return 0 if answer["persona"]["ok"] and answer["exploration"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
