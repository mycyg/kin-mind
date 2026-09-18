"""One reader for the state history, whatever shape its rows were written in.

`mind_events` has always kept a whole state document per revision. That is what makes the
history inspectable, and it is also why it grew to a third of the database: a document whose
wishes are never pruned is copied again on every change, however little of it moved. Stage 5
moves the writer to keyed patches, and the two shapes then stand side by side for as long as
the old rows do — which is for ever, because nothing here ever deletes one.

So this module is where every reader goes, and it answers one question: what did the state look
like at revision r. A row that carries its own snapshot answers by itself. A row that carries a
patch answers by asking the row before it and applying what changed. Either way the answer is
checked against the hash the writer recorded, before anyone is allowed to use it — a snapshot
that cannot be verified is not evidence of anything, and the one caller that restores a
personality from it would otherwise put back a profile nobody can vouch for.

What this module will not do is guess. A parent that is missing, a patch that does not fit, a
`base` that points forward, a rebuild whose bytes do not hash to what the row claims: all of them
are the one refusal, `history-rebuild-failed`. The two callers then differ, deliberately. On the
commit path `Mind._revert` lets it fail the transaction, because a half-known profile is worse
than no reversion. On the read path `Mind.read(history=N)` puts `snapshot: null` and a static
`history_error` on that one entry and returns the rest, because one unreadable revision is not a
reason to withhold the ninety-nine beside it that are fine.

The patch format is a keyed diff of depth two — `["set", [k] or [k1, k2], value]` and
`["del", path]`, with lists replaced whole. It is deliberately not general. Depth two is where
the measured saving is (about 95 % against the full snapshots; a top-level diff gives 73 % and a
fully recursive one only 2 MB more), and anything deeper buys little and costs a parser no
reviewer can hold in their head.

The parent of a row is the row **before** it, not `revision - 1`. The mind's revision has always
been allowed to move without a row here, so a chain built on arithmetic would break on the first
hole; one built on "the previous row for this scope" does not.
"""

from __future__ import annotations

import json
from copy import deepcopy

from eventmem.core.db import Conflict, digest, dumps

# A row written as a patch says so in its own data. Everything without this marker is a full
# snapshot: the rows production already holds, and the checkpoints the patch writer keeps
# among them.
PATCH_FORMAT = 2
# The only keys a patch row's `snapshot` may carry. That stub is not a state — it exists so the
# previous release's dedupe query and the evidence-key table still find what they look for in a
# row they have no code to rebuild.
SHIM_KEYS = frozenset({"last_evidence_key", "revision"})
# How deep a keyed path may go. Two, measured; see above.
MAX_DEPTH = 2
# A chain longer than this is not a chain any more, it is a loop or a corrupted `base`. The
# checkpoint policy keeps real chains two orders of magnitude below it.
MAX_CHAIN = 1000
# What every refusal in here carries. One code for every cause on purpose: whether the parent was
# missing or its hash was wrong is an operator's question, and the caller's answer is the same.
REBUILD_FAILED = "history-rebuild-failed"


def _unreadable(at=None):
    """Every way the history can fail to rebuild, under one static message and one code.

    The revision travels as a structured fact, never inside the message, so a host that logs only
    the message still cannot print anything it was not already holding."""
    raise Conflict(
        "This revision cannot be rebuilt from the recorded history",
        kind="runtime", code=REBUILD_FAILED, actual=at,
    )


def row_hash(state) -> str:
    """The digest a row records as `state_hash`: sha256 over the canonical serialization.

    Canonical because `dumps` sorts keys and fixes the separators, so a rebuilt document hashes to
    the stored value regardless of the order the patches happened to put its keys back in."""
    return digest(state)


def is_patch(data) -> bool:
    """Whether this row says what changed rather than what the state became."""
    return data.get("format") == PATCH_FORMAT and isinstance(data.get("patch"), list)


def snapshot_of(data):
    """The whole state a row carries, or None when all it carries is the old-reader shim."""
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or SHIM_KEYS.issuperset(snapshot):
        return None
    return snapshot


def snapshot_row(payload, state):
    """What a full-snapshot row stores.

    The request and the snapshot are exactly what they have always been, so the previous release
    reads this row without noticing anything. `state_hash` is the one addition, and it is what
    makes every later rebuild checkable at all: a row written before stage 5 carries no hash, so
    nothing can be said about it beyond that it parsed."""
    return {"request": payload, "snapshot": state, "state_hash": row_hash(state)}


# --- the diff ------------------------------------------------------------------------------

def diff(base, target, *, depth=MAX_DEPTH):
    """What has to happen to `base` for it to become `target`, as keyed operations.

    Ordering is by key at every level, so the same pair of documents always produces the same
    patch and a stored patch can be compared with a recomputed one."""
    ops = []
    _diff(ops, base, target, [], depth)
    return ops


def _diff(ops, base, target, path, depth):
    for key in sorted(set(base) | set(target)):
        here = path + [key]
        if key not in target:
            ops.append(["del", here])
        elif key not in base:
            ops.append(["set", here, target[key]])
        elif base[key] == target[key]:
            continue
        elif depth > 1 and isinstance(base[key], dict) and isinstance(target[key], dict):
            # One level further in, which is where the saving is: a wish that gained a receipt
            # rewrites that wish, not the other hundred and nineteen beside it.
            _diff(ops, base[key], target[key], here, depth - 1)
        else:
            # A list, a scalar, or a change of type. Replaced whole: an index-addressed edit is
            # fragile against every insertion, and the measurement says it is not worth it.
            ops.append(["set", here, target[key]])
    return ops


def apply(base, patch):
    """`base` with `patch` applied, leaving `base` as it was."""
    return _apply(deepcopy(base), patch)


def _apply(state, patch, *, at=None):
    """The same, in place. The materializer owns its copy and walks a whole chain with it, so it
    does not pay for a second copy of a nine-hundred-kilobyte document at every step.

    Every shape this does not recognise is a refusal rather than a best effort. A patch that half
    applies leaves a document that looks like a state and is not one, and the hash check after it
    would only tell us so once the damage was already in someone's hands."""
    if not isinstance(patch, list):
        _unreadable(at)
    for op in patch:
        if not isinstance(op, (list, tuple)) or len(op) < 2 or op[0] not in ("set", "del"):
            _unreadable(at)
        name, path = op[0], op[1]
        if len(op) != (2 if name == "del" else 3):
            _unreadable(at)
        if (not isinstance(path, list) or not 1 <= len(path) <= MAX_DEPTH
                or not all(isinstance(key, str) for key in path)):
            _unreadable(at)
        holder = state
        for key in path[:-1]:
            holder = holder.get(key)
            if not isinstance(holder, dict):
                _unreadable(at)
        if name == "del":
            if path[-1] not in holder:
                _unreadable(at)
            holder.pop(path[-1])
        else:
            holder[path[-1]] = deepcopy(op[2])
    return state


# --- reading it back -----------------------------------------------------------------------

def verify(state, expected, *, at=None):
    """Hand back `state` only when its canonical bytes hash to what the row recorded.

    `expected` is None for every row written before stage 5. There is nothing to check against
    those and nothing is claimed about them: they are trusted exactly as far as they were before
    this module existed, and no further."""
    if expected is not None and row_hash(state) != expected:
        _unreadable(at)
    return state


def _data(conn, scope, revision):
    row = conn.execute(
        "SELECT data FROM mind_events WHERE scope=? AND revision=?", (scope, revision)
    ).fetchone()
    if not row:
        _unreadable(revision)
    try:
        data = json.loads(row[0])
    except ValueError:
        _unreadable(revision)
    if not isinstance(data, dict):
        _unreadable(revision)
    return data


def materialize(conn, scope, revision, *, data=None):
    """The verified state as of `revision`.

    Walk back to the nearest row that carries a whole document, then forward again applying what
    each row in between says changed, checking the hash at every step. `data` is the row's already
    parsed contents where the caller has them, which saves reading the largest column twice."""
    chain, at = [], revision
    data = _data(conn, scope, at) if data is None else data
    while is_patch(data):
        base = data.get("base")
        if type(base) is not int or base >= at or len(chain) >= MAX_CHAIN:
            _unreadable(at)
        chain.append((at, data))
        at, data = base, _data(conn, scope, base)
    state = snapshot_of(data)
    if state is None:
        _unreadable(at)
    known = data.get("state_hash")
    verify(state, known, at=at)
    for at, step in reversed(chain):
        wanted = step.get("base_hash")
        if wanted is not None:
            if known is None:
                known = row_hash(state)
            if known != wanted:
                # The parent is readable and is not the one this patch was computed against. That
                # is the case the checkpoint policy exists to end, not one to paper over.
                _unreadable(at)
        state = _apply(state, step["patch"], at=at)
        known = step.get("state_hash")
        verify(state, known, at=at)
    if state.get("revision") not in (None, revision):
        # A row written before stage 5 carries no hash, so the only thing that can be checked
        # about it is that the document it holds says it belongs to the revision it was filed
        # under. Every row in production does. A document with no revision of its own — another
        # table's history, later — is not asked for one.
        _unreadable(revision)
    return state


def parent(conn, scope, revision):
    """The row before `revision` for this scope, as its revision and its verified state.

    The row before, not `revision - 1`. A host operation may move the mind's revision without
    writing a row here, and the reversion path used to read the arithmetic answer, find nothing,
    and fail on the nothing rather than on the missing history.

    Both halves, because both callers need both: a reader wants the document, and a writer wants
    the number to record as the patch's `base` as well as the document to compute it against. One
    query and one verification serve them, so they cannot disagree about which row it was."""
    row = conn.execute(
        "SELECT revision FROM mind_events WHERE scope=? AND revision<? ORDER BY revision DESC LIMIT 1",
        (scope, revision),
    ).fetchone()
    if not row:
        _unreadable(revision)
    return row[0], materialize(conn, scope, row[0])


def before(conn, scope, revision):
    """The verified state as the row before `revision` left it."""
    return parent(conn, scope, revision)[1]


def chain_depth(data) -> int:
    """How many patches this row stands on before something carries a whole state.

    A row with its own snapshot stands on none. The brief uses the word "depth" for two different
    numbers — this one, which the checkpoint rule `depth >= 50` counts, and the two keys a patch
    path may address — so the one the writer records is named here rather than guessed at twice."""
    return data.get("depth", 0) if is_patch(data) else 0


def entries(conn, scope, limit):
    """The last `limit` revisions, in the shape `read(history=N)` has always returned.

    The keys are the six a caller already knows. Nothing about how a row was stored appears here:
    `format`, `base`, `patch` and the hashes are this module's business, and a projection that
    leaked them would make the storage format part of the contract. A revision that cannot be
    rebuilt is the single exception — it says so, in a static code, and the others still arrive."""
    found = []
    for row in conn.execute(
        "SELECT id,kind,revision,occurred_at,data FROM mind_events WHERE scope=? ORDER BY revision DESC LIMIT ?",
        (scope, limit),
    ).fetchall():
        entry = {
            "id": row["id"], "kind": row["kind"], "revision": row["revision"],
            "occurred_at": row["occurred_at"], "request": None, "snapshot": None,
        }
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # The row itself will not parse. Its columns are still true and still say that
            # something happened at this revision, which is more than dropping it would.
            entry["history_error"] = REBUILD_FAILED
            found.append(entry)
            continue
        # The request is the row's own text and needs no rebuilding, so a revision whose state
        # cannot be put back together still says what was asked of it.
        entry["request"] = data.get("request")
        try:
            entry["snapshot"] = materialize(conn, scope, row["revision"], data=data)
        except Conflict as error:
            if error.code != REBUILD_FAILED:
                raise
            entry["history_error"] = REBUILD_FAILED
        found.append(entry)
    return found


def canonical(state) -> str:
    """The bytes a snapshot is stored and hashed as. The archive and the compaction check compare
    against this, so there is one answer to "what should this row say" and it lives here."""
    return dumps(state)
