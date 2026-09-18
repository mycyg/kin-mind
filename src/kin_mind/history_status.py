"""What an operator asks the stored history, before and after the write format changes.

Two read-only commands. `history-status` says what the rows are — how many of each shape, how
deep the chains have grown, how much of the store they take, and where the first patch row is,
which is the one number a rollback plan turns on: everything before it the previous release can
read, and from that row on it cannot. `history-verify` rebuilds the lot and says whether the
bytes are what each row claims.

The count that matters most in the verification is the one for rows it could **not** check. The
rows this store already holds were written before there was a hash to write, and nothing can be
said about their contents beyond that they parse. They are not rewritten — this package writes
no row twice and deletes nothing — so they stay uncheckable for good, and a report that quietly
added them to the verified total would be a clean bill of health for eight hundred rows nobody
looked at. They are counted on their own, the state says `partial` while any of them remain, and
`verified` means every row in the history was checked and every one of them held.
"""

from __future__ import annotations

import json

from .history import PATCH_FLAG, compacting, shape_of, sweep, writes_patches
from .history_admin import command
from .history_compaction import compacted_through

# How many revisions a report names before it stops listing. The counts are always complete, and
# a history that has more than twenty broken revisions has one cause, not twenty.
SAMPLE = 20
# Why a row cannot be verified, in the report itself, because a count with no explanation beside
# it is read as a count of something wrong.
UNVERIFIABLE = "written before this release, with no recorded hash to check the bytes against"
# What a verified verdict does not cover once rows have been rewritten in place. Compaction gave
# those rows the hash they are now checked against, so the two agreeing says the row has not moved
# since — not that it still holds what it held before. Only `history-compact-verify` says that, and
# only because it compares against bytes copied out before the first row was touched.
COMPACTED = ("rebuilt and hashed by the compaction that wrote them; run history-compact-verify to"
             " compare them with the archived originals")


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


@command("history-status")
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


@command("history-verify")
def verify(mind, *, limit=SAMPLE):
    """Rebuild every revision and check its bytes against what its row claims.

    Read only, and safe to run against a live store: it takes a read connection and writes
    nothing. One forward pass, so the cost is one rebuild per revision rather than one chain per
    revision.

    `state` is `verified` only when every row was checked and every one held. It is `partial`
    when nothing failed but some rows could not be checked at all, which is every store that
    still holds rows from before this release. It is `incomplete` when something failed, and
    `failures` names the first few revisions — the count is always complete.

    `shim_missing` is the other thing worth refusing a release over. A patch row keeps two keys
    where a whole state used to be, and that stub is what the previous release's dedupe guard
    reads. A patch row without it is a row that would go unseen by a rolled-back host.

    `compacted_through` is what this verdict does **not** cover. A row rewritten in place was given
    its hash by the pass that wrote its bytes, so the two agreeing here is one pass agreeing with
    itself. What says such a row still holds what it held is the comparison against the archived
    original, and that is `history-compact-verify`, not this."""
    scope, limit = mind.scope.key(), max(1, min(200, int(limit)))
    counts = {"verified": 0, "unverifiable": 0, "failed": 0}
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
    state = ("incomplete" if counts["failed"] or without_shim
             else "partial" if counts["unverifiable"] else "verified")
    return {"state": state, "scope": scope, "rows": rows, **counts,
            "unverifiable_reason": UNVERIFIABLE,
            "compacted_through": through,
            "compacted_reason": COMPACTED if through else None,
            "failures": failures[:limit], "failed_count": len(failures),
            "shim_missing": without_shim[:limit], "shim_missing_count": len(without_shim),
            "deepest": deepest, "compacting": compaction,
            **{"rows_" + shape: count for shape, count in shapes.items()}}
