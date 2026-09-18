"""One reader and one writer for the state history, whatever shape its rows were written in.

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

The writer is here too, rather than in the one place that inserts a row, so that the two shapes
cannot drift apart: what `row_for` decides to store is read back by `materialize` a few lines
down, and a change to either is a change made while looking at the other. It is off unless a
store asks for it. Every other optimization in this stage defaults on, because turning one on
changes how fast something runs; turning this one on changes what gets written, and the release
that can no longer be rolled back to is not an optimization.

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
    makes every later rebuild checkable at all: a row written before the release that introduced the hash carries none, so
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


def alike(left, right) -> bool:
    """Whether two values Python calls equal are also the same document.

    They usually are, and `is` says so in one comparison: a revision mutates the document it was
    loaded from, so every section it did not touch is the very same object on both sides.

    Where they are not the same object, equality is not enough to skip a key. Python reads `1`,
    `1.0` and `True` as one value; JSON writes three different things. A diff that took `==` for
    an answer would leave the older of the two in the rebuilt document, which is a state that
    hashes to something its row never claimed — the whole history would fail verification one
    revision after such a value first appeared, and the cause would be invisible in the patch,
    because the patch would be the one place it is not mentioned."""
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

    `expected` is None for every row written before that release. There is nothing to check against
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


def materialize(conn, scope, revision, *, data=None, listing=False):
    """The verified state as of `revision`.

    Walk back to the nearest row that carries a whole document, then forward again applying what
    each row in between says changed, checking the hash at every step. `data` is the row's already
    parsed contents where the caller has them, which saves reading the largest column twice.

    `listing` narrows one check, and only one. A row that carries its own whole state and is being
    read for itself — the hundred entries of a history view — is taken at the word of its columns:
    the document says which revision it belongs to, and that is checked, but its bytes are not
    hashed. The moment anything is rebuilt *through* that document it is hashed again, because
    then it is not an answer but the ground under one. Sweeping every row belongs to
    `history-verify`, which is an operator's command and says what it could not check; it does not
    belong on the path a reply is waiting behind."""
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
    if chain or not listing:
        verify(state, known, at=at)
    for index, (at, step) in enumerate(reversed(chain)):
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
        # Each step's recorded hash is compared with the next step's `base_hash` above, which
        # costs nothing and is what says the chain is the chain it claims to be. Hashing the
        # document itself is what costs, and under `listing` it is paid once, on the answer: a
        # step whose bytes are wrong gives a final document whose bytes are wrong, and that is
        # the one this row is being asked for.
        if not listing or index == len(chain) - 1:
            verify(state, known, at=at)
    if state.get("revision") not in (None, revision):
        # A row written before the release that introduced the hash carries none, so the only thing that can be checked
        # about it is that the document it holds says it belongs to the revision it was filed
        # under. Every row in production does. A document with no revision of its own — another
        # table's history, later — is not asked for one.
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
    """The same row, as its revision and its verified state.

    Both halves, because both callers need both: a reader wants the document, and a writer wants
    the number to record as the patch's `base` as well as the document to compute it against. One
    query and one verification serve them, so they cannot disagree about which row it was."""
    at, data = preceding(conn, scope, revision)
    return at, materialize(conn, scope, at, data=data)


def before(conn, scope, revision):
    """The verified state as the row before `revision` left it."""
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
            entry["snapshot"] = materialize(conn, scope, row["revision"], data=data, listing=True)
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


def shape_of(data) -> str:
    """What a parsed row is, by the only three things that decide it.

    `patch` says what changed. `checkpoint` carries a whole state and the hash to check it by —
    which a row this release wrote with the flag off does too, and it serves the same purpose for
    the reader, so it is not given a name of its own. `legacy` carries a whole state and no hash:
    every row production already holds."""
    if not isinstance(data, dict):
        return "unreadable"
    if is_patch(data):
        return "patch"
    return "checkpoint" if data.get("state_hash") else "legacy"


def sweep(conn, scope):
    """Every row of this scope's history, oldest first, and what can be said about each one.

    One pass, carrying the state forward. Rebuilding each revision from its own checkpoint would
    read the same patch fifty times over; here each row is one step from the row before it, which
    is the order they were written in and the order they verify in.

    Three verdicts, and the middle one is the point of having three. **verified** means the bytes
    hash to what the row claims. **failed** means they do not, or the row will not parse, or the
    patch does not fit what it says it was computed against. **unverifiable** means the row was
    written before this release and carries no hash at all: there is nothing to check it against,
    and saying so is the only honest thing that can be said about eight hundred and seventy rows
    that are otherwise perfectly readable. A caller that folds those into the verified count is
    reporting a clean history it never looked at."""
    state = known = previous = None
    for row in conn.execute(
        "SELECT revision,kind,data,LENGTH(data) AS bytes FROM mind_events WHERE scope=? ORDER BY revision",
        (scope,),
    ):
        revision = row["revision"]
        found = {"revision": revision, "kind": row["kind"], "bytes": row["bytes"]}
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = None
        if not isinstance(data, dict):
            state = known = None
            previous = revision
            yield {**found, "shape": shape_of(data), "verdict": "failed"}
            continue
        found["shape"] = shape_of(data)
        if is_patch(data):
            carried = isinstance(data.get("snapshot"), dict) and SHIM_KEYS.issuperset(data["snapshot"])
            found.update(depth=chain_depth(data), shim=carried)
            wanted = data.get("base_hash")
            usable = state is not None and data.get("base") == previous
            if usable and wanted is not None:
                # Hashing the row below is paid for here and nowhere else: a store with no patch
                # rows in it yet — which is every store until the flag goes on — never hashes a
                # document it is only carrying forward.
                known = row_hash(state) if known is None else known
                usable = known == wanted
            try:
                # The cheap step where the chain is intact, and a full rebuild where it is not —
                # a row after a break is answered on its own terms rather than blamed for the
                # break, and a row whose `base` points somewhere unexpected is followed there.
                state = _apply(state, data["patch"]) if usable else materialize(conn, scope, revision, data=data)
                known = row_hash(state)
                verdict = "verified" if known == data.get("state_hash") else "failed"
            except Conflict as error:
                if error.code != REBUILD_FAILED:
                    raise
                verdict = "failed"
        else:
            whole, claimed = snapshot_of(data), data.get("state_hash")
            if whole is None or whole.get("revision") not in (None, revision):
                state, verdict = None, "failed"
            elif claimed is None:
                # Every row production already holds. It is trusted exactly as far as it was
                # before this module existed, which is far enough to build on and not far enough
                # to call verified.
                state, verdict = whole, "unverifiable"
            else:
                state = whole
                verdict = "verified" if row_hash(whole) == claimed else "failed"
            known = claimed if state is not None else None
        if verdict == "failed":
            state = known = None
        previous = revision
        yield {**found, "verdict": verdict}


# --- writing it down -------------------------------------------------------------------------

def shim(state):
    """The two keys a patch row puts where a whole state used to be.

    Not a state and not a summary of one: the previous release's dedupe guard looks for the
    evidence key at `$.snapshot.last_evidence_key`, and the evidence key table can be rebuilt from
    the same place. Rolled back, that release finds both of them here and goes on working against
    rows it has no code to rebuild. `last_evidence_key` is written even when the state has none —
    a mind that has not yet appraised anything — because absent and null read the same through
    `json_extract`, and writing it always keeps the two keys of the shim visible as two."""
    return {"last_evidence_key": state.get("last_evidence_key"), "revision": state["revision"]}


def checkpoint_row(payload, state, *, healed=None, state_hash=None):
    """A whole state again, and the foot every patch after it stands on.

    `healed` says the writer chose this rather than reached it: the row before could not be read
    or did not hash to what it claims, or the patch computed against it did not rebuild this
    state. Either way the chain from here on is sound again, and the reason stays in the row,
    because the only place a write transaction could put a metric is a second connection it is
    itself holding the lock against."""
    row = {"request": payload, "format": PATCH_FORMAT, "snapshot": state,
           "state_hash": state_hash or row_hash(state)}
    if healed:
        row["healed"] = healed
    return row


def patch_row(payload, state, *, base, base_hash, depth, since, patch, state_hash):
    """What changed, what it changed from, and how to tell whether the answer came out right."""
    return {"request": payload, "format": PATCH_FORMAT, "base": base, "base_hash": base_hash,
            "depth": depth, "since": since, "patch": patch, "state_hash": state_hash,
            "snapshot": shim(state)}


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
    """The row this revision stores, in whichever shape this store is writing.

    With the flag off this is the row the previous release wrote, key for key, so a store that
    never turns it on cannot tell this module is here. With it on, most rows become a keyed diff
    against the row before them and the rest are checkpoints — because a chain that only ever
    grows is a chain whose oldest end nobody can afford to read.

    A checkpoint is written when the kind is one that must answer for itself, when the chain under
    this row would reach `CHECKPOINT_DEPTH`, when the patches since the last whole state have cost
    as much as one, when this single patch would cost more than half of one, and when anything at
    all is wrong with the row before. That last case is the one that matters: the writer never
    tries to repair a broken chain, and it never builds on one either. It writes a whole state,
    says so in the row, and everything after that stands on the new ground.

    `loaded` is the text the command read the state from. Almost always that is the parent row's
    own state, and one hash says so, which is the difference between a write that walks forty
    rows back to a checkpoint and one that reads a single row. What that shortcut cannot see is a
    break further down the chain: it proves this row's ground, not the ground under it. So the
    self-heal answers for a parent that is gone, unreadable, or not the state this revision was
    computed from — and `history-verify` is what finds a break deeper than that. The repair for
    one is an operator's: with the flag off for a single revision the next row is a whole state
    again, which is a checkpoint by another name, and the chain starts over from there."""
    if compacting(conn):
        raise Conflict(
            "History is being compacted; no revision can be written until it finishes",
            kind="runtime", code=COMPACTING,
        )
    if not writes_patches(conn, scope):
        return snapshot_row(payload, state)
    if kind in CHECKPOINT_KINDS:
        return checkpoint_row(payload, state)
    try:
        at, data = preceding(conn, scope, state["revision"])
        known = data.get("state_hash")
        if loaded is not None and known is not None and digest(loaded.encode()) == known:
            before = json.loads(loaded)
        else:
            before = materialize(conn, scope, at, data=data)
            known = known if known is not None else row_hash(before)
    except Conflict as error:
        if error.code != REBUILD_FAILED:
            raise
        return checkpoint_row(payload, state, healed="parent")
    blob = canonical(state).encode()
    state_hash, size = digest(blob), len(blob)
    patch = diff(before, state)
    weight = len(dumps(patch).encode())
    since = chain_bytes(data) + weight
    depth = chain_depth(data) + 1
    if keeps_whole(depth, since, weight, size):
        return checkpoint_row(payload, state, state_hash=state_hash)
    # The last thing checked is the only thing that matters: does this patch, against this parent,
    # give back exactly the document being stored. `before` is this function's own copy and is
    # spent here. A patch that does not reproduce the state is not stored as one, ever — and a
    # writer that could not write at all would be worse than one that writes a larger row, so the
    # refusal `_apply` raises is a reason to keep the whole state rather than to fail the commit.
    try:
        rebuilt = row_hash(_apply(before, patch)) == state_hash
    except Conflict as error:
        if error.code != REBUILD_FAILED:
            raise
        rebuilt = False
    if not rebuilt:
        return checkpoint_row(payload, state, healed="patch", state_hash=state_hash)
    return patch_row(payload, state, base=at, base_hash=known, depth=depth, since=since,
                     patch=patch, state_hash=state_hash)
