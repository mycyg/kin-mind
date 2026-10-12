"""Legacy evidence-key index retained for existing stores and operator maintenance tools.

Current appraisals neither create content keys nor consult this index as a psychological gate.
Historical keys may still be read or copied by the compatibility maintenance commands below.
Their presence, absence or disagreement cannot refuse a new subjective judgment.
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
    """Compatibility entry point: new subjective history produces no content keys."""
    return 0


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
    """Compatibility entry point: prior content never refuses a new appraisal."""
    return False


def flush_mismatches(engine, found):
    """Discard legacy deferred gate reports; the current workflow has no such gate."""
    found.clear()


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
