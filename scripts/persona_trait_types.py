"""Record the owner's word that every type of trait may change (the persona contract's `mutable_trait_keys`).

小光, 2026-09-27: "人格契约所有类型都应该能改变哦". The contract's texts -- core, voice and maintenance -- their
hashes and the owner's approval record outside the file stay exactly as approved: only the list of trait
types that conversation and reflection may grow into a trait is widened, by ALL_TRAIT_TYPES, with the
owner's words and the time beside it. Idempotent. The file is replaced atomically and keeps its mode.

    python scripts/persona_trait_types.py --root <memory root> --quote <the owner's words> --at <ISO time> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eventmem.core.persona import ALL_TRAIT_TYPES, APPROVAL_FIELDS, _checked  # noqa: E402

# Everything the owner's approval of the canon covers, besides the list this widens.
KEPT = ("schema", "scope", "requires_owner_confirmation", "approved_source", "core", "voice", "maintenance",
        "core_sha256", "voice_sha256", "maintenance_sha256", *APPROVAL_FIELDS)


def widen(policy, quote, at):
    """The contract with every trait type mutable, or None when it already is."""
    keys = list(policy["mutable_trait_keys"])
    if ALL_TRAIT_TYPES in keys:
        return None
    return {**policy, "mutable_trait_keys": [*keys, ALL_TRAIT_TYPES],
            "mutable_trait_keys_approval": {"all": True, "by": "owner", "quote": quote, "at": at}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the memory root that holds persona-policy.json")
    parser.add_argument("--quote", required=True, help="the owner's own words approving it")
    parser.add_argument("--at", required=True, help="when the owner said so (ISO time)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.quote.strip() or not args.at.strip():
        parser.error("--quote and --at name the owner's approval")
    path = Path(args.root) / "persona-policy.json"
    policy = _checked(json.loads(path.read_text(encoding="utf-8")))
    widened = widen(policy, args.quote, args.at)
    if widened is None:
        print(json.dumps({"state": "unchanged", "mutable_trait_keys": policy["mutable_trait_keys"]}, ensure_ascii=False))
        return 0
    _checked(widened)
    changed = [key for key in KEPT if widened.get(key) != policy.get(key)]
    if changed:
        raise SystemExit(f"refused: the approved canon would change ({', '.join(changed)})")
    if args.dry_run:
        print(json.dumps({"state": "planned", "mutable_trait_keys": widened["mutable_trait_keys"]}, ensure_ascii=False))
        return 0
    mode = path.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".persona-policy.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(widened, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    print(json.dumps({"state": "widened", "mutable_trait_keys": widened["mutable_trait_keys"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
