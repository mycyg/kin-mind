"""One-time cleanup of the prompt store's duplicated history.

The code now keeps two previous texts per prompt instead of sixteen, and no
longer registers a prompt file for every JSON-Schema field. This script applies
that shape to an existing store without discarding the older texts: they are
written to `.history/compacted-<time>.json` first. It also retires the
`tools/undefined` document (the old nameless tool-search name) when it is not an
edited document; edited prompts and schema files are never deleted.

    python scripts/compact_prompt_history.py --root /path/to/prompts
    python scripts/compact_prompt_history.py --root /path/to/prompts --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

KEEP_PREVIOUS = 2
UNDEFINED = ("tools/undefined.json", ".defaults/tools/undefined.json", ".defaults/tools/undefined")


def _edited(document: dict) -> bool:
    return document.get("edited") is True or (
        document.get("edited") is not False and document.get("version", 1) > 1
    )


def _scan(root: Path) -> dict:
    trims, schema, retired = [], [], []
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".history":
            continue
        if "schema" in relative.parts:
            schema.append(str(relative))
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        previous = document.get("previous_texts")
        if isinstance(previous, list) and len(previous) > KEEP_PREVIOUS:
            trims.append((relative, previous[KEEP_PREVIOUS * -1:], previous[: KEEP_PREVIOUS * -1]))
    for name in UNDEFINED:
        path = root / name
        if not path.exists():
            continue
        current = root / "tools/undefined.json"
        edited = False
        if current.is_file():
            try:
                edited = _edited(json.loads(current.read_text()))
            except ValueError:
                edited = True
        if not edited:
            retired.append(name)
    return {"trims": trims, "schema": schema, "retired": retired}


def _atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(value)
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    plan = _scan(root)
    report = {
        "root": str(root),
        "trimmed": len(plan["trims"]),
        "removed_previous_texts": sum(len(removed) for _, _, removed in plan["trims"]),
        "schema_files_ignored": len(plan["schema"]),
        "retired": plan["retired"],
        "applied": False,
    }
    if args.apply and (plan["trims"] or plan["retired"]):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = root / ".history" / f"compacted-{stamp}.json"
        _atomic(backup, json.dumps({str(relative): removed for relative, _, removed in plan["trims"]}, ensure_ascii=False, indent=2) + "\n")
        for relative, kept, _ in plan["trims"]:
            path = root / relative
            document = json.loads(path.read_text())
            document["previous_texts"] = kept
            _atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        retired_root = root / ".history" / f"retired-{stamp}"
        for name in plan["retired"]:
            source = root / name
            target = retired_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        report.update(applied=True, backup=str(backup), retired_directory=str(retired_root))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
