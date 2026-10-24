"""Read-only deployment checks for current persona documents and computer-reader roots.
The optional scratch directory contains only the reader symlink probes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from eventmem.core.models import Scope
from eventmem.core.persona import load_persona

from .codex_executor import protected_roots, reader_settings
from .computer import ComputerReader

# What a probe inside each protected root stands for; nothing at these paths is ever read.
PROBES = {"host": ("mind-config.json", "state/mind-status.json"), "memory": ("memory.sqlite3", "persona-policy.json"),
          "codex-home": ("auth.json", "config.toml")}


def _read_only(path):
    return sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=5)


def persona_approval(config):
    """Read the effective editable persona. The legacy result key is retained for release receipts."""
    if not config.get('root') or not isinstance(config.get('scope'), dict):
        return {'ok': False, 'problems': ['memory-root-or-scope-unconfigured'], 'version': None}
    root = Path(config['root']).expanduser().resolve()
    try:
        policy = load_persona(SimpleNamespace(db=SimpleNamespace(root=root)), Scope.model_validate(config['scope']))
        if not policy:
            return {'ok': False, 'problems': ['persona-seed-missing-or-other-scope'], 'version': None}
        return {'ok': True, 'problems': [], 'version': policy.get('version'),
                'prompt_version': policy.get('prompt_version'), 'source': 'prompts/persona'}
    except (ValueError, TypeError, OSError):
        return {'ok': False, 'problems': ['persona-document-unreadable'], 'version': None}


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
