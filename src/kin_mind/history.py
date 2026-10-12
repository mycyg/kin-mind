"""State history as existing snapshots or bounded patches linked by database revision.

History records what was stored and when. It does not hash subjective content or treat agreement
with an old checksum as permission to read, change, or remember it. Legacy checksum fields are
ignored. Missing database parents and malformed patch operations remain structural read errors.
"""

from __future__ import annotations

import json
from copy import deepcopy

from eventmem.core.db import Conflict, dumps

# A row written as a patch says so in its own data. Everything without this marker is a full
# snapshot: the rows production already holds, and the checkpoints the patch writer keeps
# among them.
PATCH_FORMAT = 2
# The only keys a patch row's `snapshot` may carry. That stub is not a state — it exists so the
# legacy readers can identify its revision. Old evidence-key shims remain readable.
SHIM_KEYS = frozenset({"last_evidence_key", "revision"})
# How deep a keyed path may go. Two, measured; see above.
MAX_DEPTH = 2
# A chain longer than this is not a chain any more, it is a loop or a corrupted `base`. The
# checkpoint policy keeps real chains two orders of magnitude below it.
MAX_CHAIN = 1000
# What every refusal in here carries. One code for every cause on purpose: whether the parent was
# missing or its patch malformed is an operator's question, and the caller's answer is the same.
REBUILD_FAILED = "history-rebuild-failed"

# --- what the writer is told -------------------------------------------------------------------
# The flag that chooses the format, read with `enabled()` and therefore **off** unless a store has
# explicitly asked for patches. See the module docstring for why this one is not default-on.
PATCH_FLAG = "history_patches"
# Kinds that always carry a whole state. `initialize` has nothing to stand on, and an `evolution`
# is the row a reversion reads the state *before* — the one rebuild that must not depend on a
# chain of other people's patches holding together.
CHECKPOINT_KINDS = frozenset({"initialize", "evolution"})
# No row stands on more than this many patches. Fifty is where the measurement put the balance
# between the checkpoints' bytes and the work of a rebuild.
CHECKPOINT_DEPTH = 50
# A patch worth more than this fraction of the state is not worth storing as a patch: the next
# reader would pay for both. Expressed as a divisor so the comparison stays in integers.
CHECKPOINT_SHARE = 2
# While this is set in `meta`, compaction owns every row and is rewriting them in place. A write
# landing in the middle of that would be a row the archive does not hold, so there is no write.
COMPACTION_MARKER = "history_compaction_active"
COMPACTING = "history-compaction-active"


def _unreadable(at=None):
    """Every way the history can fail to rebuild, under one static message and one code.

    The revision travels as a structured fact, never inside the message, so a host that logs only
    the message still cannot print anything it was not already holding."""
    raise Conflict(
        "This revision cannot be rebuilt from the recorded history",
        kind="runtime", code=REBUILD_FAILED, actual=at,
    )





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
    """A request and the state it stored, without a content proof."""
    return {"request": payload, "snapshot": state}


def diff(base, target, *, depth=MAX_DEPTH):
    """What has to happen to `base` for it to become `target`, as keyed operations.

    Ordering is by key at every level, so the same pair of documents always produces the same
    patch and a stored patch can be compared with a recomputed one."""
    ops = []
    _diff(ops, base, target, [], depth)
    return ops


def alike(left, right) -> bool:
    """Whether two values Python calls equal are also the same document.

    They usually are, and `is` says so in one comparison: a revision mutates the document it was
    loaded from, so every section it did not touch is the very same object on both sides.

    Where they are not the same object, equality is not enough to skip a key. Python reads `1`,
    `1.0` and `True` as one value; JSON writes three different things. A diff that took `==` for
    an answer would leave the older of the two in the rebuilt document and silently lose the
    changed JSON type. The patch therefore records type changes as well as value changes."""
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return all(alike(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return all(alike(value, other) for value, other in zip(left, right))
    return True


def _diff(ops, base, target, path, depth):
    for key in sorted(set(base) | set(target)):
        here = path + [key]
        if key not in target:
            ops.append(["del", here])
        elif key not in base:
            ops.append(["set", here, target[key]])
        elif base[key] == target[key] and alike(base[key], target[key]):
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
    applies would leave a document that looks like a state but misses part of its recorded change."""
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

def verify(state, expected=None, *, at=None):
    """Legacy caller compatibility: old checksum arguments do not gate historical state."""
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


def materialize(conn, scope, revision, *, data=None, listing=False):
    """Follow existing database revisions and apply the recorded changes."""
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
    for at, step in reversed(chain):
        state = _apply(state, step["patch"], at=at)
    if state.get("revision") not in (None, revision):
        _unreadable(revision)
    return state


def preceding(conn, scope, revision):
    """The row before `revision` for this scope, as its revision and its own parsed contents.

    The row before, not `revision - 1`. A host operation may move the mind's revision without
    writing a row here, and the reversion path used to read the arithmetic answer, find nothing,
    and fail on the nothing rather than on the missing history.

    Unmaterialized, because the writer reads two things out of the row itself — how deep the chain
    under it already is, and how many patch bytes it has carried since the last whole state — and
    then often does not have to rebuild it at all."""
    row = conn.execute(
        "SELECT revision FROM mind_events WHERE scope=? AND revision<? ORDER BY revision DESC LIMIT 1",
        (scope, revision),
    ).fetchone()
    if not row:
        _unreadable(revision)
    return row[0], _data(conn, scope, row[0])


def parent(conn, scope, revision):
    """The same row, as its revision and its reconstructed state.

    Both halves, because both callers need both: a reader wants the document, and a writer wants
    the number to record as the patch's `base` as well as the document to compute it against. One
    query and one verification serve them, so they cannot disagree about which row it was."""
    at, data = preceding(conn, scope, revision)
    return at, materialize(conn, scope, at, data=data)


def before(conn, scope, revision):
    """The reconstructed state as the row before `revision` left it."""
    return parent(conn, scope, revision)[1]


def chain_depth(data) -> int:
    """How many patches this row stands on before something carries a whole state.

    A row with its own snapshot stands on none. The brief uses the word "depth" for two different
    numbers — this one, which the checkpoint rule `depth >= 50` counts, and the two keys a patch
    path may address — so the one the writer records is named here rather than guessed at twice."""
    return data.get("depth", 0) if is_patch(data) else 0


def chain_bytes(data) -> int:
    """How many patch bytes have been written since this row's chain last carried a whole state.

    The other half of the checkpoint arithmetic, kept in the row for the same reason `depth` is:
    the alternative is re-reading every row back to the last checkpoint on every single write, to
    learn a number the row before already knew."""
    return data.get("since", 0) if is_patch(data) else 0


def entries(conn, scope, limit):
    """The last `limit` revisions, in the shape `read(history=N)` has always returned.

    The keys are the six a caller already knows. Nothing about how a row was stored appears here:
    `format`, `base` and `patch` are this module's business, and a projection that
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
            entry["snapshot"] = materialize(conn, scope, row["revision"], data=data, listing=True)
        except Conflict as error:
            if error.code != REBUILD_FAILED:
                raise
            entry["history_error"] = REBUILD_FAILED
        found.append(entry)
    # A delete is in force from its own transaction. The stored rows are rewritten by a job, in
    # batches and after compaction, so until the last batch a row may still hold the words: what
    # is handed back is scrubbed against the store's deletion facts here, the same scrub the
    # rewrite applies, whatever the job has reached (CR-MEM-03).
    from .erasure import erased_ids, scrub
    ids = erased_ids(conn)
    if ids:
        for entry in found:
            entry["request"] = scrub(entry["request"], ids)
            entry["snapshot"] = scrub(entry["snapshot"], ids)
    return found


def canonical(state) -> str:
    """The serialization used to store or export a state document."""
    return dumps(state)


def shape_of(data) -> str:
    if not isinstance(data, dict):
        return "unreadable"
    if is_patch(data):
        return "patch"
    return "checkpoint" if data.get("format") == PATCH_FORMAT else "legacy"


def sweep(conn, scope):
    """Read every recorded revision; report readability, never content consistency proof."""
    state = previous = None
    for row in conn.execute(
        "SELECT revision,kind,data,LENGTH(data) AS bytes FROM mind_events WHERE scope=? ORDER BY revision", (scope,)
    ):
        revision = row["revision"]
        found = {"revision": revision, "kind": row["kind"], "bytes": row["bytes"]}
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = None
        found["shape"] = shape_of(data)
        if not isinstance(data, dict):
            state, previous = None, revision
            yield {**found, "verdict": "failed"}
            continue
        if is_patch(data):
            carried = isinstance(data.get("snapshot"), dict) and SHIM_KEYS.issuperset(data["snapshot"])
            found.update(depth=chain_depth(data), shim=carried)
        try:
            state = (_apply(state, data["patch"], at=revision)
                     if is_patch(data) and state is not None and data.get("base") == previous
                     else materialize(conn, scope, revision, data=data))
            if state.get("revision") not in (None, revision):
                _unreadable(revision)
            verdict = "readable"
        except Conflict as error:
            if error.code != REBUILD_FAILED:
                raise
            state, verdict = None, "failed"
        previous = revision
        yield {**found, "verdict": verdict}


def shim(state):
    """The database revision carried by a patch instead of a whole snapshot."""
    return {"revision": state["revision"]}


def checkpoint_row(payload, state, *, healed=None, state_hash=None):
    """A whole state. Legacy checksum input is accepted and ignored."""
    row = {"request": payload, "format": PATCH_FORMAT, "snapshot": state}
    if healed:
        row["healed"] = healed
    return row


def patch_row(payload, state, *, base, depth, since, patch, base_hash=None, state_hash=None):
    """Recorded changes linked to an existing database parent revision."""
    return {"request": payload, "format": PATCH_FORMAT, "base": base,
            "depth": depth, "since": since, "patch": patch, "snapshot": shim(state)}


def keeps_whole(depth, since, weight, size) -> bool:
    """Whether this revision keeps the whole state rather than only what changed.

    Three ways of saying the same thing — that nobody should have to read very much to learn what
    the state was at one revision. The chain never reaches `CHECKPOINT_DEPTH` rows. The patches
    standing on one whole state never cost more than that state did, so a rebuild reads at most
    twice the document. And a single patch that would cost more than half the document is not a
    saving at all; it is the document, written twice."""
    return depth >= CHECKPOINT_DEPTH or since >= size or weight * CHECKPOINT_SHARE > size


def compacting(conn) -> bool:
    """Whether compaction currently owns every row of this store."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (COMPACTION_MARKER,)).fetchone()
    return bool(row and row[0])


def writes_patches(conn, scope) -> bool:
    """Whether this store has asked for the new format. `enabled`, so silence means no."""
    from .autonomy_schema import enabled
    return enabled(conn, scope, PATCH_FLAG)


def row_for(conn, scope, state, kind, payload, *, loaded=None):
    """Store a snapshot or bounded changes, without calculating or comparing content hashes."""
    if compacting(conn):
        raise Conflict("History is being compacted; no revision can be written until it finishes",
                       kind="runtime", code=COMPACTING)
    if not writes_patches(conn, scope):
        return snapshot_row(payload, state)
    if kind in CHECKPOINT_KINDS:
        return checkpoint_row(payload, state)
    try:
        at, data = preceding(conn, scope, state["revision"])
        prior = json.loads(loaded) if loaded is not None else None
        before = (prior if isinstance(prior, dict) and prior.get("revision") == at
                  else materialize(conn, scope, at, data=data))
    except (ValueError, Conflict) as error:
        if isinstance(error, Conflict) and error.code != REBUILD_FAILED:
            raise
        return checkpoint_row(payload, state, healed="parent")
    size = len(canonical(state).encode())
    patch = diff(before, state)
    weight = len(dumps(patch).encode())
    since, depth = chain_bytes(data) + weight, chain_depth(data) + 1
    if keeps_whole(depth, since, weight, size):
        return checkpoint_row(payload, state)
    return patch_row(payload, state, base=at, depth=depth, since=since, patch=patch)
