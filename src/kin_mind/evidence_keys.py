"""What evidence has already been scored, kept where running does not need the snapshots.

A single underlying event may be appraised once. Until now the only record of that was the state
snapshot inside each `affect` history row: the guard asked whether any row's
`snapshot.last_evidence_key` matched, which is an unindexed scan of every snapshot ever written —
0.2 s inside the write transaction of every new appraisal, and growing with history. That is
tolerable only while the snapshots are still there. History is about to be stored as patches and
then compacted, so the one thing the running system reads out of those snapshots has to live in a
table of its own first. This module is that table, and it is step two of an order that must not be
rearranged: the index moves out before the format changes under it.

`mind_evidence_keys` is keyed by `(scope, evidence_key)` and says which event first introduced that
key and at which revision. First, not last: an `affect` row that never went through the scoring path
— an evolution or a reversion submitted through the appraisal lane takes that kind — carries the key
of the row before it, so the same key can appear on several rows. Ascending revision order plus
`INSERT OR IGNORE` means the row that really introduced the key is the one recorded, and that is the
same answer the scan gives, which is what lets the two be compared at all.

Three things keep the table honest:

- every history row records its own key as it is written, so the table cannot fall behind a commit;
- a **catch-up scan** runs before the table is read, because a period spent on the previous release,
  or an import, writes rows this code never saw. The cursor in `mind_memory_migrations` says how far
  the rows have been read, so the catch-up is normally an indexed no-op;
- while the stage is open the guard refuses if **either** the table or the old scan says it has seen
  the key, and a disagreement is a metric. The scan is the authority until compaction removes its
  evidence; the table only has to prove it agrees first.

`evidence-keys-backfill` fills the table for a store written before it existed, in batches, resumable
from the cursor, idempotent, and writing nothing at all without `--apply`. `evidence-keys-verify`
compares the two directions row by row and names what does not line up.
"""

from __future__ import annotations

from eventmem.core.db import dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_evidence_keys(
 scope TEXT NOT NULL,evidence_key TEXT NOT NULL,event_id TEXT NOT NULL,
 revision INTEGER NOT NULL,PRIMARY KEY(scope,evidence_key));
"""

# The row of `mind_memory_migrations` whose `cursor` is the revision through which the history rows
# have been read into the table. Everything at or below it is accounted for.
MIGRATION = "evidence-keys-v1"
# The only kind that introduces a key. The scan reads exactly this kind, so the table must read the
# same one or the two guards could never be compared.
KIND = "affect"
# Where the key sits in a history row. A patch row keeps this path in its old-reader shim, so this
# one string goes on reading both formats.
KEY_PATH = "$.snapshot.last_evidence_key"
# The scan as the previous release wrote it, kept verbatim — both of the above spelled out inside it
# on purpose: it is the authority until compaction, and a rewrite of it would be a change of meaning
# rather than of style. Change either constant above and this has to be read again beside it.
LEGACY_QUERY = (
    "SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' AND"
    " json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1"
)
# Off takes the table out of the guard and leaves the scan alone, which is the previous behaviour.
INDEX_FLAG = "evidence_key_index"
# Off stops the guard paying for the scan. Only for a store whose snapshots are gone, and only once
# the two have agreed for a while.
LEGACY_FLAG = "history_legacy_guard"
# The two disagreed about one key. Either is enough to refuse, so this costs nobody an appraisal; it
# is the signal that the table is not yet trustworthy on its own.
MISMATCH_METRIC = "evidence_key_guard_mismatch"
# Rows per write transaction while backfilling. Small enough that an ordinary host write waits
# milliseconds, because production has other writers.
BATCH = 200
# How many identifiers a verification names before it stops listing. Counts are always complete.
SAMPLE = 20


def installed(conn):
    """The table and the cursor it is read behind. Both, because a table nobody can say how far has
    been read into is not one a guard may trust: without the cursor there is no catch-up, so a store
    that has the one and not the other answers from the scan alone, as the previous release did.
    `mind_memory_migrations` belongs to the memory layer, which every path that scores anything
    installs; a bare `Mind` may not have it."""
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('mind_evidence_keys','mind_memory_migrations')"
    ).fetchall()} == {"mind_evidence_keys", "mind_memory_migrations"}


# --- the cursor ----------------------------------------------------------------------------------

def cursor(conn, scope):
    row = conn.execute("SELECT cursor FROM mind_memory_migrations WHERE scope=? AND name=?",
                       (scope, MIGRATION)).fetchone()
    return int(row[0]) if row else 0


def _advance(conn, scope, through, *, at):
    """The cursor only moves forward, so a resumed run never re-reads what it already read, and a
    dry run that wrote nothing leaves the next run exactly the work it had."""
    current = cursor(conn, scope)
    if through <= current:
        return current
    conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                 (scope, MIGRATION, through, dumps({"through": through, "updated_at": at})))
    return through


def head(conn, scope):
    """The highest revision this scope has written. Read from the index, not from the rows."""
    return conn.execute("SELECT MAX(revision) FROM mind_events WHERE scope=?", (scope,)).fetchone()[0] or 0


# --- reading the rows ----------------------------------------------------------------------------

def scan(conn, scope, after, *, limit=None):
    """The rows that introduce a key, oldest first, as `(revision, event_id, key)`.

    `data` itself never comes back into Python: a row is half a megabyte of snapshot and there are
    hundreds of them. SQLite still parses each one, which is the same 0.2 s the guard's own scan
    costs, and the cursor means that is paid once rather than per appraisal."""
    return [
        (row["revision"], row["id"], row["evidence_key"])
        for row in conn.execute(
            "SELECT revision,id,json_extract(data,?) AS evidence_key FROM mind_events"
            " WHERE scope=? AND kind=? AND revision>? ORDER BY revision" + (" LIMIT ?" if limit else ""),
            (KEY_PATH, scope, KIND, after, *([limit] if limit else [])),
        ).fetchall()
    ]


def stored(conn, scope):
    return {row["evidence_key"]: (row["event_id"], row["revision"]) for row in conn.execute(
        "SELECT evidence_key,event_id,revision FROM mind_evidence_keys WHERE scope=?", (scope,)).fetchall()}


def _insert(conn, scope, key, event_id, revision):
    """`OR IGNORE`, so the row that first introduced the key keeps it. A later row carrying the same
    key inherited it and did not score anything."""
    return conn.execute("INSERT OR IGNORE INTO mind_evidence_keys VALUES(?,?,?,?)",
                        (scope, key, event_id, revision)).rowcount


# --- keeping up ----------------------------------------------------------------------------------

def record(conn, scope, state, kind, event_id):
    """The key this history row carries, written with the row itself.

    Called from the one place that writes history, so no commit can leave the table behind. The same
    filter the backfill uses, on purpose: a row that inherited its key inserts nothing."""
    if kind != KIND or not installed(conn):
        return 0
    key = state.get("last_evidence_key")
    if not key:
        return 0
    return _insert(conn, scope, key, event_id, state["revision"])


def catch_up(conn, scope, at):
    """Read the rows written since the cursor into the table, before anyone trusts it.

    A store that spent a while on the previous release, or that had history imported into it, holds
    `affect` rows this code never saw. Without this the table would answer for them by saying it has
    never seen their evidence, which is the one wrong answer a dedupe guard can give."""
    if not installed(conn):
        return 0
    through, reached = cursor(conn, scope), head(conn, scope)
    if reached <= through:
        return 0
    inserted = sum(_insert(conn, scope, key, event_id, revision)
                   for revision, event_id, key in scan(conn, scope, through) if key)
    _advance(conn, scope, reached, at=at)
    return inserted


# --- the guard -----------------------------------------------------------------------------------

def already_scored(conn, scope, key, *, at, deferred=None):
    """Whether this evidence has been appraised before, asked of the table and of the old scan.

    Either one is enough to refuse. The scan is still the authority — it reads the snapshots, which
    are still there — and the table is on trial beside it until they are not.

    A disagreement is always a refusal, and a refusal rolls this transaction back, so it cannot be
    written down here: it is handed to `deferred` and recorded once the failed write is out of the
    way. A metric written inside the refusal would be a metric nobody ever sees."""
    from .autonomy_schema import optimized
    index = installed(conn) and optimized(conn, scope, INDEX_FLAG)
    # A guard both switches can turn off would be no guard at all, so without the table the scan
    # runs whatever the legacy flag says.
    legacy = not index or optimized(conn, scope, LEGACY_FLAG)
    if index:
        catch_up(conn, scope, at)
    table_says = bool(index and conn.execute(
        "SELECT 1 FROM mind_evidence_keys WHERE scope=? AND evidence_key=?", (scope, key)).fetchone())
    legacy_says = bool(legacy and conn.execute(LEGACY_QUERY, (scope, key)).fetchone())
    if index and legacy and table_says != legacy_says and deferred is not None:
        deferred.append({"evidence_key": key, "table": table_says, "legacy": legacy_says})
    return table_says or legacy_says


def flush_mismatches(engine, found):
    """Write down what the guard saw, now that the write it saw it in has been rolled back.

    A digest, and which of the two said it had seen it. No evidence text and no source id."""
    while found:
        engine.db.metric(MISMATCH_METRIC, 1, found.pop(0))


# --- the operator's own routes -------------------------------------------------------------------

def _require(conn):
    """An operator asking about the index in a store that has none gets told so, rather than a
    report of an empty table that would read as a clean bill of health."""
    if not installed(conn):
        raise RuntimeError("This store has no evidence key index to read")


def backfill(mind, *, apply=False, batch=BATCH):
    """Fill the table from the history of a store written before it existed.

    Resumable from the cursor, idempotent, and a dry run writes nothing at all — not the rows, not
    the cursor — so the report can be read before anything is decided. Each batch is its own write
    transaction, because production has other writers."""
    scope, at = mind.scope.key(), mind.clock()
    batch = max(1, min(2000, int(batch)))
    with mind.engine.db.connect() as conn:
        _require(conn)
        if not apply:
            known, pending, without_key, carried = stored(conn, scope), {}, 0, 0
            through = cursor(conn, scope)
            for revision, event_id, key in scan(conn, scope, through):
                if not key:
                    without_key += 1
                elif key in known or key in pending:
                    carried += 1
                else:
                    pending[key] = (event_id, revision)
            return {"state": "dry-run", "scope": scope, "scanned": len(pending) + carried + without_key,
                    "would_insert": len(pending), "would_ignore": carried, "without_key": without_key,
                    "keys": len(known), "cursor": through, "head": head(conn, scope)}

    scanned = inserted = ignored = without_key = 0
    while True:
        with mind.engine.db.connect(write=True) as conn:
            through = cursor(conn, scope)
            rows = scan(conn, scope, through, limit=batch)
            if not rows:
                _advance(conn, scope, head(conn, scope), at=at)
                break
            for revision, event_id, key in rows:
                scanned += 1
                if not key:
                    without_key += 1
                    continue
                written = _insert(conn, scope, key, event_id, revision)
                inserted, ignored = inserted + written, ignored + (1 - written)
            _advance(conn, scope, rows[-1][0], at=at)
    # A fill nobody checked is a fill nobody can release, so the run proves itself before it says it
    # is done: the state an operator reads is the verification's, not the insert count's.
    checked = verify(mind)
    with mind.engine.db.connect() as conn:
        return {"state": "complete" if checked["state"] == "verified" else "incomplete",
                "scope": scope, "scanned": scanned, "inserted": inserted,
                "ignored": ignored, "without_key": without_key,
                "keys": conn.execute("SELECT COUNT(*) FROM mind_evidence_keys WHERE scope=?", (scope,)).fetchone()[0],
                "cursor": cursor(conn, scope), "head": head(conn, scope), "verification": checked}


def verify(mind, *, limit=SAMPLE):
    """Compare the table and the history rows in both directions, one row at a time.

    Read only. Forward: every key a history row introduced is in the table, against the event and
    revision that introduced it. Backward: every row of the table was introduced by a history row
    that is still there. `missing` would let evidence be scored twice; `extra` would refuse evidence
    that never was; `mismatched` means the table credits the wrong row, which is what a backfill in
    the wrong order produces."""
    scope, limit = mind.scope.key(), max(1, min(200, int(limit)))
    first, rows, carried, without_key = {}, 0, 0, 0
    with mind.engine.db.connect() as conn:
        _require(conn)
        for revision, event_id, key in scan(conn, scope, 0):
            rows += 1
            if not key:
                without_key += 1
            elif key in first:
                carried += 1
            else:
                first[key] = (event_id, revision)
        table, through, reached = stored(conn, scope), cursor(conn, scope), head(conn, scope)
    missing = sorted(key for key in first if key not in table)
    extra = sorted(key for key in table if key not in first)
    mismatched = sorted(key for key in first if key in table and table[key] != first[key])
    return {"state": "verified" if not (missing or extra or mismatched) else "incomplete",
            "scope": scope, "rows": rows, "keys": len(first), "stored": len(table),
            "matched": len(first) - len(missing) - len(mismatched),
            "carried_over": carried, "without_key": without_key,
            "missing": missing[:limit], "extra": extra[:limit], "mismatched": mismatched[:limit],
            "missing_count": len(missing), "extra_count": len(extra), "mismatched_count": len(mismatched),
            "cursor": through, "head": reached, "caught_up": through >= reached}
