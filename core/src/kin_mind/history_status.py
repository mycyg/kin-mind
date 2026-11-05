"""Read-only history shape and reconstruction reports.

Reports count recorded revisions, patch structure and readable states. Legacy content hashes are
ignored; readability says that database parents and patch operations can reconstruct the state.
It does not certify the consistency of Kin's subjective thoughts or make them depend on evidence.
"""

from __future__ import annotations

import json

from .history import PATCH_FLAG, compacting, shape_of, sweep, writes_patches
from .history_compaction import compacted_through

# How many revisions a report names before it stops listing. The counts are always complete, and
# a history that has more than twenty broken revisions has one cause, not twenty.
SAMPLE = 20
# Compaction coverage identifies the revisions copied into the existing archive.
COMPACTED = "reconstructed from compacted rows; history-compact-verify compares archived originals"


def _shapes(conn, scope):
    """One pass over the rows, parsing each one once and hashing none of them."""
    found = {"rows": 0, "bytes": 0, "patch_bytes": 0, "deepest": 0, "healed": 0,
             "first_revision": None, "last_revision": None, "first_patch_revision": None,
             "rows_patch": 0, "rows_checkpoint": 0, "rows_legacy": 0, "rows_unreadable": 0}
    for row in conn.execute(
        "SELECT revision,data,LENGTH(data) AS bytes FROM mind_events WHERE scope=? ORDER BY revision",
        (scope,),
    ):
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = None
        shape = shape_of(data)
        found["rows"] += 1
        found["bytes"] += row["bytes"]
        found["rows_" + shape] += 1
        if found["first_revision"] is None:
            found["first_revision"] = row["revision"]
        found["last_revision"] = row["revision"]
        if shape == "patch":
            found["patch_bytes"] += row["bytes"]
            found["deepest"] = max(found["deepest"], data.get("depth", 0))
            if found["first_patch_revision"] is None:
                found["first_patch_revision"] = row["revision"]
        if isinstance(data, dict) and data.get("healed"):
            found["healed"] += 1
    return found


def status(mind):
    """What this store's history is made of, and what the previous release could still read.

    Read only, and it reads every row: the shape of a row is inside its own document, so there is
    no index that could answer this. `first_patch_revision` is the rollback boundary — with it
    still empty, the release before this one reads every row here as it always did."""
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        found = _shapes(conn, scope)
        return {"scope": scope, "writing_patches": writes_patches(conn, scope),
                "flag": PATCH_FLAG, "compacting": compacting(conn),
                "compacted_through": compacted_through(conn, scope), **found}


def verify(mind, *, limit=SAMPLE):
    """Reconstruct every stored revision and report missing parents or malformed patches.

    Read only, in one forward pass. Old checksum metadata is ignored. A patch shim must name
    its database revision, so legacy readers can still identify the recorded change.
    """
    scope, limit = mind.scope.key(), max(1, min(200, int(limit)))
    counts = {"readable": 0, "failed": 0}
    shapes = {"patch": 0, "checkpoint": 0, "legacy": 0, "unreadable": 0}
    failures, without_shim, deepest, rows = [], [], 0, 0
    with mind.engine.db.connect() as conn:
        for found in sweep(conn, scope):
            rows += 1
            counts[found["verdict"]] += 1
            shapes[found["shape"]] += 1
            if found["verdict"] == "failed":
                failures.append(found["revision"])
            if found["shape"] == "patch":
                deepest = max(deepest, found["depth"])
                if not found["shim"]:
                    without_shim.append(found["revision"])
        compaction, through = compacting(conn), compacted_through(conn, scope)
    state = "incomplete" if counts["failed"] or without_shim else "readable"
    return {"state": state, "scope": scope, "rows": rows, **counts,
            "compacted_through": through,
            "compacted_reason": COMPACTED if through else None,
            "failures": failures[:limit], "failed_count": len(failures),
            "shim_missing": without_shim[:limit], "shim_missing_count": len(without_shim),
            "deepest": deepest, "compacting": compaction,
            **{"rows_" + shape: count for shape, count in shapes.items()}}
