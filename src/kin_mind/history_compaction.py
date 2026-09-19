"""Rewriting the history already written, which is the one thing in this stage that touches data
the store already holds.

Everything before this added a table or changed what the next write looks like. Nothing before this
could lose anything: a new table can be dropped, a new format only applies to rows that did not
exist yet, and a flag turned back off restores the previous behaviour exactly. This module rewrites
hundreds of megabytes of documents that were written once and are the only record of what the mind
held at each revision. So it is built the other way round from the rest: the question it asks at
every step is not "may this proceed" but "is there anything at all to stop for", and every answer
that is not a plain no stops it.

**The archive comes first — all of it.** A run is two phases, in the only order they may happen in.
Phase one copies every row the run will touch — every column, verbatim — into a separate database
file, fsyncs it, then reads each row back on a fresh connection and compares it byte for byte with
what is still in the store. Only when the whole target set is archived and verified does the rewrite
loop run, and the loop refuses any row that phase one did not cover, however that happened. Each
batch is then rewritten inside a transaction that also moves the cursor and rebuilds what it just
wrote, so there is no committed state in which a row was rewritten and either nobody wrote down that
it had been or nobody checked that it came out right. The archive is never deleted by any code here,
at any point, for any reason; `history-restore` reads from it and leaves it exactly where it is.

What a rewrite does, precisely: `data` becomes what changed since the row before instead of the
whole document, and **nothing else moves**. Every row stays, every column stays, and `id`, `scope`,
`revision`, `kind` and `occurred_at` are not written at all. Four of the seven readers of this table
want the columns and never look inside `data`; they cannot tell this ran.

Some rows keep their whole document, and which ones is not a matter of taste:

- the first row, which has nothing to stand on;
- every fiftieth row, so no rebuild ever reads more than forty-nine patches;
- every `evolution` row **and the row before it**, because a reversion restores the row before the
  evolution, and that one rebuild must not depend on a chain of other rows holding together;
- **the row under any row that was already a patch when this started.** The brief names one case of
  it — the last old-format row before the first new-format one — and the general rule is what that
  case is an instance of. A patch written by the live writer recorded how deep the chain under it
  was at the time, and under a whole document that is nothing. Rewrite that document into a patch
  and the row above goes on claiming a depth of one over a chain of fifty, and the writer's own
  bound is computed from the wrong number for as long as the store lives.

Two further rules are this module's own and can only ever keep more rows whole, never fewer: a patch
that is not smaller than the document it would replace is not worth storing, because it would make
the row bigger, which is the opposite of the point; and the chain is never allowed to reach the live
writer's own checkpoint bound in a store where the fiftieth-row rule and rows that are already
patches interleave. Both are counted separately in the report, so what fired is visible rather than
inferred.

Preconditions, checked on every run and none of them skippable: the store is quiet by the proof in
`liveness.quiescence` — this is that proof's first real caller — there is a backup of the database
that can be shown to hold *this* history, there is enough free disk for the backup, the archive and
the working room, and `meta.history_compaction_active` is set so that the live writer refuses to
write a revision while this owns the rows. A failed precondition is a refusal that has written
nothing.

"Shown to hold this history" is literal, and it is not counted. At the start of a run the store is
surveyed once: one content digest folded over every row's key columns and bytes, and a per-row
manifest — id, kind, occurred_at and the same sha256 the archive records — of every row the run may
touch. The manifest is frozen into the run's own progress record; the backup is opened read-only,
`quick_check`ed and compared against the manifest row by row; and what was trusted — the file's
hash, size, permissions and name — is written down with it. A file that holds the same number of
rows between the same endpoints but different bytes is not a backup of this store, however it came
to be named. And a resumed run checks the backup against the frozen manifest, never against rows
the run itself has already rewritten: those are the archive's answer, and the two proofs — that the
fallback is of this store, and that the originals are kept — do not stand in for each other.

The marker is deliberately not self-clearing. A run that stops in the middle leaves it set, and the
store stays closed to new revisions until someone decides what to do: resume, which is the ordinary
answer, or restore. That is the right way round for a package that can destroy the history. The cost
of the store being closed is a stopped host; the cost of it being open is a revision written into a
half-rewritten history that the archive does not hold.

What can be claimed afterwards is narrower than it looks. A rewritten row carries a `state_hash`, so
`history-verify` calls it verified where it used to call it unverifiable. That says the row is
self-consistent — its bytes still hash to what it claims — and it says nothing about whether it still
holds what it held before the rewrite, because both halves were written by the same pass. The only
thing that says that is `history-compact-verify`, which rebuilds each revision from the store and
compares it with the **archived original bytes**, copied before the first row moved. Those are two
different claims, and only the second one is the reason this is allowed to run at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from eventmem.core.db import Conflict, digest, dumps

from . import history
from .history_admin import command

# The archive lives beside the database it was taken from, in a directory of its own, one file per
# compaction. Not inside the store: a safety net sharing a file with the thing it is the net for is
# not a net.
ARCHIVE_DIR = "archive"
ARCHIVE_STEM = "mind-events-v1"
# Where a pre-compaction backup is taken when the operator does not name one. Beside the database
# for the same reason, and 0600 like everything else here.
BACKUP_STEM = "memory-precompaction"
# Progress, in the table every other resumable migration in this package uses. `cursor` is the
# revision through which the history is rewritten — and therefore archived, because no row is
# rewritten that the archive does not already hold verified. Cursor and rewrite are one commit.
MIGRATION = "history-compaction-v1"
# Rows per batch. Small on purpose: in the rewrite loop the batch is the unit an interruption can
# cost, and twenty-five rows of half a megabyte is a write transaction measured in hundreds of
# milliseconds. The archive phase reads in chunks of the same size, so the copy of the store it
# holds in memory is never bigger than one batch either.
BATCH = 25
# Every n-th row keeps its whole document. Counted in rows, not revisions: the mind's revision has
# always been allowed to move without a row here, so counting revisions would leave a store with
# holes in it standing on longer chains than the number says.
CHECKPOINT_EVERY = 50
# What a dry run spends to say what the saving would be: the patches of this many rows are really
# computed against the real documents, and the rest is that ratio.
SAMPLE_ROWS = 25
# How many revisions a report names before it stops listing. Counts are always complete.
SAMPLE = 20
# Free bytes wanted beyond the backup and the archive: room for the rewrite's own write-ahead log,
# and slack, because running a disk to zero in the middle of this is the one failure with no clean
# recovery.
HEADROOM = 64 * 1024 * 1024
# The archive is a plain copy of the same text, so it costs about what the rows cost plus the page
# overhead of a second database. A tenth is generous, and generous is the only safe direction.
ARCHIVE_OVERHEAD = 1.1

REFUSED = "history-compaction-refused"
# The keys an old-format row may carry. Anything else in there is something nobody described, and
# dropping it silently is the kind of loss this package exists to make impossible.
KNOWN_KEYS = frozenset({"request", "snapshot", "state_hash"})

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_events_v1(
 scope TEXT NOT NULL, revision INTEGER NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
 occurred_at TEXT NOT NULL, data TEXT NOT NULL, data_sha256 TEXT NOT NULL,
 archived_at TEXT NOT NULL, PRIMARY KEY(scope, revision));
CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# The columns a row is copied and compared by.
COLUMNS = ("id", "scope", "revision", "kind", "occurred_at", "data")


def _refuse(reason, **facts):
    """Every stop in this module, under one code and one static message.

    The reason is a fixed word rather than a sentence, and everything that varies travels as
    structured facts, so a host that logs only the message prints nothing it was not already
    holding."""
    raise Conflict(
        "History compaction refused",
        kind="runtime", code=REFUSED, target=reason,
        actual={key: value for key, value in facts.items() if value is not None} or None,
    )


# --- the cursor -----------------------------------------------------------------------------------

def _has_migrations(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_memory_migrations'").fetchone())


def progress(conn, scope):
    """How far compaction has got, and what the first run of it settled.

    `through` is the revision at or below which every row is archived and rewritten. The rest is
    what every later invocation has to go on using: which file the archive is, and which backup it
    was allowed to start against."""
    if not _has_migrations(conn):
        return {"through": 0, "found": False}
    row = conn.execute("SELECT cursor,data FROM mind_memory_migrations WHERE scope=? AND name=?",
                       (scope, MIGRATION)).fetchone()
    if not row:
        return {"through": 0, "found": False}
    try:
        data = json.loads(row["data"])
    except ValueError:
        data = {}
    return {**data, "through": int(row["cursor"]), "found": True}


def compacted_through(conn, scope) -> int:
    """The revision this store has been compacted through, for a reader that wants only the number.

    `history-verify` reads it to say what its own verdict does not cover: at or below this line a
    row's hash was written by the same pass that wrote its bytes, so the two agreeing is not
    independent evidence of anything. The archive is, and `history-compact-verify` is what reads
    it."""
    return progress(conn, scope)["through"]


def _record(conn, scope, through, data):
    conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                 (scope, MIGRATION, through, dumps(data)))


# --- the marker -----------------------------------------------------------------------------------

def _set_marker(db, active):
    """Close the store to new revisions, or open it again.

    Its own transaction, always. This fact has to be true before the first batch and must not ride
    on the first batch's commit, because a batch that fails still leaves behind every batch before
    it."""
    with db.connect(write=True) as conn:
        if active:
            conn.execute("INSERT INTO meta VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=1",
                         (history.COMPACTION_MARKER,))
        else:
            conn.execute("DELETE FROM meta WHERE key=?", (history.COMPACTION_MARKER,))


# --- the archive ----------------------------------------------------------------------------------

def archive_dir(db) -> Path:
    return Path(db.root) / ARCHIVE_DIR


def _fsync(path: Path):
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _archive_connection(path: Path, *, create=False):
    """A connection to the archive, which is a different database and never shares one with the
    store. Created 0600 before anything goes into it, because it is about to hold a verbatim copy of
    every state the mind has ever been in."""
    if not create:
        if not path.exists():
            _refuse("archive-missing", archive=path.name)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not path.exists()
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    os.chmod(path, 0o600)
    # Outside any transaction: `executescript` commits, and a commit nobody asked for is a
    # transaction that ended without anyone saying so.
    conn.executescript(ARCHIVE_SCHEMA)
    conn.execute("PRAGMA synchronous=FULL")
    if fresh:
        _fsync(path.parent)
    return conn


def archive_counts(path: Path, scope):
    """What the archive holds for this scope."""
    conn = _archive_connection(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS held,MIN(revision) AS first,MAX(revision) AS last,"
            "COALESCE(SUM(LENGTH(data)),0) AS bytes FROM mind_events_v1 WHERE scope=?",
            (scope,)).fetchone()
        return {"rows": row["held"], "first": row["first"], "last": row["last"], "bytes": row["bytes"]}
    finally:
        conn.close()


def _archive_batch(path, scope, rows, *, at, started_at, source, verified=None):
    """Copy originals into the archive verbatim, fsync, and read them back to compare.

    `OR IGNORE`, so a row the archive already holds keeps the copy it already has: the first copy
    was taken from the original, and a second pass over the same revision must never replace it with
    whatever is there now.

    The comparison is on a **fresh connection**, so nothing that only ever reached this process's
    page cache can answer for the file, and it is every column, byte for byte. A row that differs is
    either an archive that did not receive what it was handed, or a store whose row has moved since
    the copy was taken — which would mean something other than this has been rewriting history — and
    neither is a thing to write past.

    `verified`, when given, collects the proof per row: its revision to the hash of the bytes the
    archive was just shown to hold. The rewrite loop accepts no row without one."""
    written = 0
    conn = _archive_connection(path, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key, value in (("scope", scope), ("source", source), ("started_at", started_at)):
            conn.execute("INSERT OR REPLACE INTO archive_meta VALUES(?,?)", (key, str(value)))
        for row in rows:
            written += conn.execute(
                "INSERT OR IGNORE INTO mind_events_v1 VALUES(?,?,?,?,?,?,?,?)",
                (row["scope"], row["revision"], row["id"], row["kind"], row["occurred_at"],
                 row["data"], digest(row["data"].encode()), at)).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    _fsync(path)
    check = _archive_connection(path)
    try:
        for row in rows:
            kept = check.execute(
                "SELECT id,scope,revision,kind,occurred_at,data,data_sha256 FROM mind_events_v1"
                " WHERE scope=? AND revision=?", (scope, row["revision"])).fetchone()
            if kept is None:
                _refuse("archive-row-missing", revision=row["revision"])
            for column in COLUMNS:
                if kept[column] != row[column]:
                    _refuse("archive-row-differs", revision=row["revision"], column=column)
            if kept["data_sha256"] != digest(kept["data"].encode()):
                _refuse("archive-digest-differs", revision=row["revision"])
            if verified is not None:
                verified[row["revision"]] = kept["data_sha256"]
    finally:
        check.close()
    return written


def _archive_target_set(db, path, scope, through, settled, *, at, batch, limit):
    """Phase one of the two: the whole target set of this run, archived and read-back-verified
    before the first row is rewritten.

    The target set is every row above the cursor, bounded by `limit` where the operator gave one —
    exactly the rows this invocation may touch. It is taken in chunks so the copy held in memory is
    never bigger than one batch; every chunk is committed, fsynced and read back before the next is
    fetched, and the phase does not return until every target row has been compared.

    Every row is first checked against the manifest the run froze at its start: its columns and its
    digest must be what was frozen, so a store that moved since — between invocations, past the
    marker, however — is a stop here, before the archive is asked to hold a row nobody froze.

    What comes back is the coverage proof the rewrite loop checks every row against: revision to
    the hash of the bytes the archive was verified to hold. A resumed run walks the same phase
    again: `OR IGNORE` keeps the copies already there and the read-back compares them with the
    store a second time, so an interruption here costs nothing but the walk.

    `archived` counts rows this invocation copied in; `confirmed` counts the rows it read back out
    and compared, which on a resumed or repeated run includes the ones that were already there. A
    run that archived nothing and confirmed everything is a run over a history the archive already
    holds, and saying so takes both numbers."""
    entries = settled["manifest"]["rows"]
    covered, archived, confirmed = {}, 0, 0
    last = through
    remaining = None if limit is None else int(limit)
    while remaining is None or remaining > 0:
        take = batch if remaining is None else min(batch, remaining)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id,scope,revision,kind,occurred_at,data FROM mind_events"
                " WHERE scope=? AND revision>? ORDER BY revision LIMIT ?",
                (scope, last, take)).fetchall()
        if not rows:
            break
        for row in rows:
            _against_manifest(entries, row)
        archived += _archive_batch(path, scope, rows, at=at, started_at=settled["started_at"],
                                   source=Path(db.path).name, verified=covered)
        confirmed += len(rows)
        last = rows[-1]["revision"]
        if remaining is not None:
            remaining -= len(rows)
    return covered, archived, confirmed


def _against_manifest(entries, row):
    """The row the run froze is the row the archive may hold. A target row missing from the
    manifest, or one whose columns no longer match it, means the store moved since the freeze
    without this run doing the moving — a stop found before anything is copied or rewritten."""
    entry = entries.get(str(row["revision"]))
    if entry is None:
        _refuse("row-not-in-the-run-manifest", revision=row["revision"])
    for column in ("id", "kind", "occurred_at"):
        if row[column] != entry[column]:
            _refuse("row-differs-from-the-run-manifest", revision=row["revision"], column=column)
    if digest(row["data"].encode()) != entry["data_sha256"]:
        _refuse("row-differs-from-the-run-manifest", revision=row["revision"], column="data")


def _covered(covered, row):
    """The proof the rewrite loop requires of phase one: this row is in the archive, verified, and
    its bytes have not moved since.

    A row with no verified archive copy is never rewritten, whatever the reason it is missing —
    holding the whole set back until every row is covered is the point of running the archive as a
    phase of its own. A row whose bytes moved since its copy was verified is the same refusal the
    write guard below would give, found one read earlier."""
    kept = covered.get(row["revision"])
    if kept is None:
        _refuse("row-not-in-verified-archive", revision=row["revision"])
    if kept != digest(row["data"].encode()):
        _refuse("row-changed-under-the-rewrite", revision=row["revision"])


# --- the backup -----------------------------------------------------------------------------------

def _fold(h, row):
    """One row into a running digest: its place in the history, its key columns, and the digest of
    its bytes — the same sha256 the archive records for it. Fixed-width or terminated pieces, so
    two different rows can never fold to the same bytes."""
    h.update(row["revision"].to_bytes(8, "big"))
    for column in ("id", "kind", "occurred_at"):
        h.update(str(row[column]).encode())
        h.update(b"\0")
    h.update(digest(row["data"].encode()).encode())


def _store_identity(conn, scope, *, content=True):
    """Which history, at which point, a copy of it is a copy of.

    The history and not the database: how many rows this scope has, where they start and end, and —
    with `content` — one digest folded over every row's key columns and bytes, so a copy that holds
    the same number of rows between the same endpoints but holds different rows is not a copy of
    this store. Count and endpoints alone were never enough to say that; for a while they were all
    that was asked. The database's generation would be stricter still and would say nothing more
    about the history: it moves every time anything at all is written, including a source arriving,
    which changes no revision.

    Without `content` this is the cheap structural answer. Rows here are only ever appended and
    nothing deletes one, so count and endpoints still say which set of revisions a copy covers."""
    if not content:
        row = conn.execute(
            "SELECT COUNT(*) AS held,COALESCE(MIN(revision),0) AS first,COALESCE(MAX(revision),0) AS head"
            " FROM mind_events WHERE scope=?", (scope,)).fetchone()
        return {"rows": row["held"], "first": row["first"], "head": row["head"]}
    everything = hashlib.sha256()
    rows, first, head = 0, 0, 0
    for row in conn.execute(
            "SELECT id,revision,kind,occurred_at,data FROM mind_events WHERE scope=?"
            " ORDER BY revision", (scope,)):
        rows += 1
        first = first or row["revision"]
        head = row["revision"]
        _fold(everything, row)
    return {"rows": rows, "first": first, "head": head,
            "history_sha256": everything.hexdigest()}


def _survey(conn, scope, through):
    """One pass over the history that freezes both halves of a run's record: the store's content
    identity, and the per-row manifest of the target set — every row above `through`, with each
    column the rewrite could move and the digest of the bytes it would move them for.

    The manifest is what the backup is verified against, and what the archive phase checks each row
    against before it copies it: a store that moved since the freeze, however it moved, is a stop
    found before the first rewrite rather than a surprise found after it."""
    everything, target = hashlib.sha256(), hashlib.sha256()
    rows, first, head = 0, 0, 0
    entries = {}
    for row in conn.execute(
            "SELECT id,revision,kind,occurred_at,data FROM mind_events WHERE scope=?"
            " ORDER BY revision", (scope,)):
        rows += 1
        first = first or row["revision"]
        head = row["revision"]
        _fold(everything, row)
        if row["revision"] > through:
            _fold(target, row)
            entries[row["revision"]] = {
                "id": row["id"], "kind": row["kind"], "occurred_at": row["occurred_at"],
                "data_sha256": digest(row["data"].encode())}
    return {"identity": {"rows": rows, "first": first, "head": head,
                         "history_sha256": everything.hexdigest()},
            "target_sha256": target.hexdigest(), "entries": entries}


def _freeze_manifest(survey, scope, through, *, at):
    """The maintenance object a run is, frozen before the backup is trusted and persisted before
    the first rewrite. Every later invocation of the same run answers to it.

    `run_id` names the run; `source` is the content identity of the store it started against;
    `target` says which rows it will touch and `rows` is what each of them held, by column and by
    digest. A resume never re-derives any of this from the store as it finds it — the store it
    finds is half the run's own work, and comparing that against the original rows would be
    unrecoverable by construction."""
    entries = survey["entries"]
    revisions = sorted(entries)
    return {"run_id": digest(
                f"history-compaction|{scope}|{through}|{at}|{survey['target_sha256']}"
                .encode())[:16],
            "scope": scope, "frozen_at": at, "through": through, "source": survey["identity"],
            "target": {"rows": len(entries), "first": revisions[0] if revisions else None,
                       "head": revisions[-1] if revisions else None,
                       "sha256": survey["target_sha256"]},
            "rows": {str(revision): entries[revision] for revision in revisions}}


def _structure(identity):
    """The shape of a history without its content: which set of revisions a store holds."""
    return {key: identity[key] for key in ("rows", "first", "head")}


def _backup_identity(path: Path, scope):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('mind_events','meta')")}
        if tables != {"mind_events", "meta"}:
            _refuse("backup-is-not-a-store", backup=path.name)
        return _store_identity(conn, scope)
    except sqlite3.DatabaseError:
        _refuse("backup-is-not-a-store", backup=path.name)
    finally:
        conn.close()


def _take_backup(db, path: Path):
    """A copy of the whole database through SQLite's own backup, beside it and fsynced.

    Through a temporary name and a rename, so an interrupted copy is never left under the name a
    later run would take for a finished one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".partial")
    if temp.exists():
        temp.unlink()
    source = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(temp, timeout=30)
    try:
        source.backup(target)
        # The copy inherits the store's write-ahead journal, and a file nobody will ever write to
        # again does not need one: left in that mode, every later read of it creates a pair of
        # scratch files beside it. One copy, one file.
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()
    os.chmod(temp, 0o600)
    _fsync(temp)
    temp.rename(path)
    _fsync(path.parent)
    return path


def _file_sha256(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_verify(scope, path: Path, manifest, *, at):
    """The apply-time proof that the fallback is a copy of this store as the run froze it.

    The file is opened read-only, checked by SQLite's own `quick_check`, and its history compared
    with the frozen manifest: the content identity first, then every target row's columns against
    the manifest's own entries, so a disagreement names its revision and column rather than leaving
    an operator to diff two hashes. What was then trusted is written down — the file's own hash,
    its size, its permissions — so the basis the run stood on is reviewable afterwards.

    This runs the same whether the file was just taken, was already there, or was named by the
    operator: a copy nobody verified is a copy nobody has, whoever suggested it. It is compared
    with the manifest and never with the store as it stands — on a resume the store is half the
    run's own work, and the backup's job is to be what the run started against."""
    held = _backup_identity(path, scope)
    if held != manifest["source"]:
        _refuse("backup-is-not-of-this-store", backup=held, store=manifest["source"])
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        try:
            problems = [row[0] for row in conn.execute("PRAGMA quick_check")]
        except sqlite3.DatabaseError as error:
            _refuse("backup-fails-integrity-check", backup=path.name, detail=str(error))
        if problems != ["ok"]:
            _refuse("backup-fails-integrity-check", backup=path.name, detail=problems[:3])
        entries, found = manifest["rows"], set()
        for row in conn.execute(
                "SELECT id,revision,kind,occurred_at,data FROM mind_events WHERE scope=?"
                " AND revision>? ORDER BY revision", (scope, manifest["through"])):
            entry = entries.get(str(row["revision"]))
            if entry is None:
                _refuse("backup-row-not-in-the-manifest", revision=row["revision"])
            for column in ("id", "kind", "occurred_at"):
                if row[column] != entry[column]:
                    _refuse("backup-row-differs", revision=row["revision"], column=column)
            if digest(row["data"].encode()) != entry["data_sha256"]:
                _refuse("backup-row-differs", revision=row["revision"], column="data")
            found.add(row["revision"])
        missing = sorted(int(revision) for revision in entries if int(revision) not in found)
        if missing:
            _refuse("backup-row-missing", revision=missing[0])
    finally:
        conn.close()
    stat = path.stat()
    return {"name": path.name, "sha256": _file_sha256(path), "bytes": stat.st_size,
            "mode": oct(stat.st_mode & 0o777), "verified_at": at}


# --- the preconditions ------------------------------------------------------------------------------

def _disk_check(db, *, backup_bytes, archive_bytes):
    free = shutil.disk_usage(db.root).free
    needed = int(backup_bytes + archive_bytes * ARCHIVE_OVERHEAD) + HEADROOM
    return {"check": "disk", "ready": free >= needed, "free": free, "needed": needed,
            "for_backup": backup_bytes, "for_archive": archive_bytes,
            "reason": "enough-free-space" if free >= needed else "not-enough-free-space"}


def _backup_check(db, scope, path: Path, identity, *, take):
    """Whether there is a copy of this history, of what it holds now, to fall back to.

    Freshness is not an age. A backup is fresh when it holds the same history rows, between the same
    first and last revision, *with the same bytes row for row* — the identity compared here carries
    the content digest, so same shape with different rows is not a match. A quiet store's history
    does not move, so one taken at the start of a run is still fresh for every batch of it,
    including the batches a later invocation runs — and a resumed run passes the identity its
    manifest froze, not the store as it stands half-rewritten.

    `take` is false when the operator named the file. Then a copy that is of something else is a
    refusal and never quietly replaced: they meant that file. Left to itself this names the file
    after what it is a copy of, so a history that has moved on simply gets a new one and the old
    one stays where it is."""
    found = {"check": "backup", "name": path.name}
    if not path.exists():
        if not take:
            return {**found, "ready": False, "reason": "backup-absent", "would_take": True}
        return {**found, "ready": True, "reason": "backup-would-be-taken", "would_take": True,
                "bytes": Path(db.path).stat().st_size}
    held = _backup_identity(path, scope)
    if held != identity:
        return {**found, "ready": False, "reason": "backup-is-not-of-this-store",
                "would_take": False, "backup": held, "store": identity}
    return {**found, "ready": True, "reason": "backup-matches-the-store", "would_take": False,
            "bytes": path.stat().st_size, "store": identity}


def _quiet_check(mind, config):
    from .liveness import quiescence
    verdict = quiescence(mind, config)
    return {"check": "quiescence", "ready": verdict["quiet"],
            "reason": "store-is-quiet" if verdict["quiet"] else "store-is-not-quiet",
            "blocking": [entry["reason"] for entry in verdict["blocking"]]}


def preconditions(mind, config, *, scope, backup: Path, take_backup, archive_bytes, identity):
    """Everything that has to be true, every run, in the order that costs least to ask.

    Quiescence first, because a running host is the common answer and the cheapest to get. The
    backup's identity next, because it reads the store. Disk last, because how much is needed
    depends on whether a backup has to be taken.

    `identity` is the caller's choice of what the backup must answer to: the store as it stands for
    a fresh run, the frozen manifest's source identity for a resumed one — so the dry run and the
    apply of the same invocation report and enforce the same verdict."""
    db = mind.engine.db
    checks = [_quiet_check(mind, config)]
    with db.connect() as conn:
        present = _has_migrations(conn)
    checks.append({"check": "cursor", "ready": present,
                   "reason": "migration-table-present" if present else "no-migration-table"})
    if not present:
        return checks
    held = _backup_check(db, scope, backup, identity, take=take_backup)
    checks.append(held)
    checks.append(_disk_check(db, backup_bytes=held.get("bytes", 0) if held.get("would_take") else 0,
                              archive_bytes=archive_bytes))
    return checks


def _require(checks):
    for check in checks:
        if not check["ready"]:
            _refuse(check["reason"], check=check["check"], blocking=check.get("blocking"),
                    free=check.get("free"), needed=check.get("needed"),
                    backup=check.get("backup"), store=check.get("store"))


# --- what shape each row keeps ------------------------------------------------------------------------

def _new_format(data) -> bool:
    """Whether this row was already written in the format compaction produces. Those rows are left
    exactly as they are: a checkpoint the live writer wrote is already a checkpoint, and a patch it
    wrote is already a patch."""
    return isinstance(data, dict) and data.get("format") == history.PATCH_FORMAT


def _parse(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def plan(conn, scope):
    """One pass over the rows, deciding before anything is touched what each of them will be.

    `data` never comes back into Python here — only whether it says `format` and whether it carries a
    patch, which SQLite answers from the column without handing half a megabyte per row across. What
    comes out is, for every revision, its place in the history and every reason it has to keep its
    whole document."""
    rows = conn.execute(
        "SELECT revision,kind,"
        " CASE WHEN json_valid(data) THEN json_extract(data,'$.format') END AS format,"
        " CASE WHEN json_valid(data) THEN json_type(data,'$.patch') END AS patch_type"
        " FROM mind_events WHERE scope=? ORDER BY revision", (scope,)).fetchall()
    formatted = [(row["revision"], row["kind"], row["format"] == history.PATCH_FORMAT,
                  row["format"] == history.PATCH_FORMAT and row["patch_type"] == "array")
                 for row in rows]
    whole, already = {}, 0
    for index, (revision, kind, new_format, _carries_patch) in enumerate(formatted):
        already += 1 if new_format else 0
        reasons = []
        if index == 0 or revision == 1:
            reasons.append("first-row")
        if (index + 1) % CHECKPOINT_EVERY == 0:
            reasons.append("every-fiftieth-row")
        if kind in history.CHECKPOINT_KINDS:
            reasons.append("kind-" + kind)
        following = formatted[index + 1] if index + 1 < len(formatted) else None
        if following and following[1] in history.CHECKPOINT_KINDS:
            reasons.append("before-kind-" + following[1])
        if following and following[3]:
            # The row a patch already standing there was computed against, which is also the brief's
            # "last old-format row before the first new-format one". That patch recorded the depth
            # and the bytes of the chain under it as they were when it was written — nothing, because
            # what it stands on is a whole document. Turn that document into a patch and the number
            # in the row above is wrong for good.
            reasons.append("under-existing-patch")
        if reasons:
            whole[revision] = reasons
    return {"whole": whole, "rows": len(formatted), "already_new_format": already}


# --- rewriting one row -------------------------------------------------------------------------------

def _original(row):
    """The whole document a row about to be rewritten holds, with every check that it is one.

    A row whose `data` will not parse, that carries a key nobody described, or whose document is not
    the state of the revision it is filed under, is not rewritten. It stays exactly as it is and the
    run stops, because the alternative is a rewrite computed from something that was never read
    correctly in the first place."""
    data = _parse(row["data"])
    if data is None:
        _refuse("row-will-not-parse", revision=row["revision"])
    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        _refuse("row-carries-keys-nobody-described", revision=row["revision"], keys=unknown)
    state = history.snapshot_of(data)
    if state is None:
        _refuse("row-carries-no-state", revision=row["revision"])
    if state.get("revision") not in (None, row["revision"]):
        _refuse("row-state-belongs-to-another-revision", revision=row["revision"])
    text = history.canonical(state)
    known = data.get("state_hash")
    if known is not None and digest(text.encode()) != known:
        _refuse("row-does-not-match-its-own-hash", revision=row["revision"])
    return data, state, text


def _rewrite(row, carried, planned):
    """What one row becomes, and the proof that it becomes nothing else.

    A patch is only returned once it has been applied to the very document it was computed against
    and the result compared with the original text character for character — compared, not hashed.
    A patch that does not reproduce its row is not a patch this writes; the row keeps its whole
    document instead, which is always available and always correct."""
    data, state, text = _original(row)
    state_hash, size = digest(text.encode()), len(text.encode())
    reasons = list(planned)
    if carried is None:
        reasons.append("no-parent")
    patch = weight = None
    if not reasons:
        patch = history.diff(carried["state"], state)
        weight = len(dumps(patch).encode())
        if weight >= size:
            reasons.append("patch-is-not-smaller")
        elif carried["depth"] + 1 >= history.CHECKPOINT_DEPTH:
            # Only reachable where the fiftieth-row rule and rows that were already patches
            # interleave. The bound that has to hold afterwards is the live writer's.
            reasons.append("chain-would-reach-the-writers-bound")
    if reasons:
        return (history.checkpoint_row(data.get("request"), state, state_hash=state_hash),
                text, {"state": state, "hash": state_hash, "depth": 0, "since": 0,
                       "revision": row["revision"]}, "checkpoint", reasons)
    if history.canonical(history.apply(carried["state"], patch)) != text:
        _refuse("patch-does-not-rebuild-the-row", revision=row["revision"])
    depth, since = carried["depth"] + 1, carried["since"] + weight
    return (history.patch_row(data.get("request"), state, base=carried["revision"],
                              base_hash=carried["hash"], depth=depth, since=since,
                              patch=patch, state_hash=state_hash),
            text, {"state": state, "hash": state_hash, "depth": depth, "since": since,
                   "revision": row["revision"]}, "patch", [])


def _carry(conn, scope, revision):
    """What the row after this one is computed against, read back out of the store and verified.

    Verified because everything after it stands on it: a break here is a break in all of them, and
    finding it before the rewrite costs one rebuild while finding it afterwards costs the batch."""
    row = conn.execute("SELECT revision,data FROM mind_events WHERE scope=? AND revision=?",
                       (scope, revision)).fetchone()
    if row is None:
        _refuse("parent-row-is-gone", revision=revision)
    data = _parse(row["data"])
    if data is None:
        _refuse("parent-row-will-not-parse", revision=revision)
    state = history.materialize(conn, scope, revision, data=data)
    return {"state": state, "hash": data.get("state_hash") or history.row_hash(state),
            "depth": history.chain_depth(data), "since": history.chain_bytes(data),
            "revision": revision}


def _carry_past(conn, scope, row, data, carried):
    """The same, for a row that is passed over, taking the one step rather than the whole chain.

    A store that ran on the new format for a while before this is a store where every row would
    otherwise be rebuilt from its own checkpoint on the way past, which is the chain read once per
    row instead of once. Where the row standing directly on what is already in hand, one step is the
    whole of it — and the step is still checked against the hash the row recorded, so the shortcut
    proves as much as the long way round."""
    if carried is None or not history.is_patch(data) or data.get("base") != carried["revision"]:
        return _carry(conn, scope, row["revision"])
    state = history.apply(carried["state"], data["patch"])
    known = data.get("state_hash")
    if known is not None and history.row_hash(state) != known:
        _refuse("passed-over-row-does-not-match-its-own-hash", revision=row["revision"])
    return {"state": state, "hash": known or history.row_hash(state),
            "depth": history.chain_depth(data), "since": history.chain_bytes(data),
            "revision": row["revision"]}


def _verify_landed(conn, scope, landed, base, at):
    """Rebuild what was just written, out of the store, and compare it with the text the rows held
    before they were touched.

    Called inside the same transaction as the writes, so a revision that does not come back out the
    way it went in takes the whole batch down with it and nothing is committed. Forward, carrying
    the state, which is one rebuild per row rather than one chain per row and is the same walk
    `history-verify` makes.

    Every row of the batch is walked, including the ones that were passed over: their state is what
    the row after them was computed against, so a walk that skipped them would arrive at the next
    patch holding the wrong document. What a passed-over row is not is compared, because it was not
    written here and its original was never a document to begin with."""
    state, previous = base, at
    for revision, text, shape in landed:
        data = _parse(conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                                   (scope, revision)).fetchone()["data"])
        if data is None:
            _refuse("rebuilt-row-will-not-parse", revision=revision)
        if history.is_patch(data):
            if state is None or data.get("base") != previous:
                _refuse("rebuilt-row-lost-its-parent", revision=revision)
            state = history.apply(state, data["patch"])
        else:
            state = history.snapshot_of(data)
            if state is None:
                _refuse("rebuilt-row-carries-no-state", revision=revision)
        previous = revision
        if text is None:
            continue
        if history.canonical(state) != text:
            _refuse("rebuilt-row-differs-from-the-original", revision=revision, shape=shape)
        if data.get("state_hash") != digest(text.encode()):
            _refuse("rebuilt-row-hash-differs-from-the-original", revision=revision)


# --- the command ----------------------------------------------------------------------------------------

def _day(at):
    return at[:10].replace("-", "")


def _named(db, name) -> Path:
    """One archive file of this store's own archive directory, by its bare name.

    A name and not a path: an operator naming which archive to read is choosing between the files
    this wrote, and a separator or a parent reference in there would be choosing something else
    entirely."""
    if not name or Path(name).name != name or name.startswith("."):
        _refuse("archive-name-is-not-a-file-name")
    return archive_dir(db) / name


def _archive_file(db, stored, at) -> Path:
    """The archive this run writes to: the one the first run of this compaction chose, or a new one
    named for today. Once chosen it stays in the cursor and does not move, because a second file
    would be a second partial copy of the history with no way to tell which is which."""
    return archive_dir(db) / (stored.get("archive") or f"{ARCHIVE_STEM}-{_day(at)}.sqlite3")


def _backup_file(db, identity, at, given) -> Path:
    """Where the fallback copy lives. Named after the history it is a copy of — its endpoint, its
    row count and the first bytes of its content digest — so a run that finds the history has moved
    on, or that finds rows changed in place under the same shape, takes a new copy under a new name
    rather than writing over the one that is there or, worse, taking the one that is there as its
    own. Nothing here replaces a backup and nothing removes one."""
    if given:
        return Path(given).expanduser()
    digest12 = (identity.get("history_sha256") or "")[:12]
    suffix = f"-{digest12}" if digest12 else ""
    return archive_dir(db) / (
        f"{BACKUP_STEM}-{_day(at)}-r{identity['head']}n{identity['rows']}{suffix}.sqlite3")


@command("history-compact", config=True)
def compact(mind, config=None, *, apply=False, batch=BATCH, backup=None, limit=None):
    """Rewrite the stored history into patches, in place, keeping every row and every column.

    Writes nothing at all without `apply`. A dry run checks every precondition and reports each
    one's verdict rather than stopping at the first, plans what every row would become, and measures
    the saving on one batch's worth of real patches — so what an operator reads before deciding is
    the decision the run will make and not a description of it.

    With `apply`, a failed precondition is a refusal that has written nothing. Past that the run
    freezes its manifest — the store's content identity, and for every row it may touch the columns
    and the digest of the bytes it starts from — proves the backup against it (opened read-only,
    `quick_check`ed, compared row by row, its own hash and permissions recorded), and only then runs
    two phases: first the whole target set is archived, fsynced and read back byte for byte, and
    once every one of those rows is verified in the archive the rewrite loop runs, one batch at a
    time: one transaction that rewrites the rows, rebuilds what it wrote, compares each rebuild with
    the original text and moves the cursor. `limit` stops after that many rows, for an operator who
    wants to watch the first batch before committing to the rest; the cursor makes the next
    invocation carry on from there.

    Resumable and idempotent. The manifest is frozen once and answered to thereafter — a resume
    verifies the backup against what the run started against, never against rows the run itself has
    already rewritten — the archive phase repeats without harm, the cursor and the rewrite commit
    together, so an interrupted run has either done a batch or not done it, and a row already in
    the new format is passed over."""
    scope, at = mind.scope.key(), mind.clock()
    db, batch = mind.engine.db, max(1, min(200, int(batch)))
    with db.connect() as conn:
        stored, shapes = progress(conn, scope), plan(conn, scope)
        remaining = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)),0) FROM mind_events WHERE scope=? AND revision>?",
            (scope, stored["through"])).fetchone()[0]
        present = _has_migrations(conn)
        # A run that froze its manifest and has not finished is the same run, resumed: the backup
        # and the rows answer to the manifest, never to the store as it stands half-rewritten. One
        # that froze and died before the marker went on and before anything was rewritten, in a
        # store that has legitimately moved since, never began — what is true now is what freezes.
        manifest = stored.get("manifest") if present else None
        resuming = manifest is not None and not stored.get("finished_at")
        moved = False
        if resuming:
            current = _store_identity(conn, scope, content=False)
            moved = _structure(current) != _structure(manifest["source"])
            if moved and "counts" not in stored:
                manifest, resuming, moved = None, False, False
        survey = _survey(conn, scope, stored["through"]) if present and not resuming else None
        if resuming:
            identity, head = manifest["source"], current["head"]
        elif survey:
            identity, head = survey["identity"], survey["identity"]["head"]
        else:
            identity, head = {"rows": 0, "first": 0, "head": 0, "history_sha256": ""}, 0
    path = _archive_file(db, stored, at)
    if resuming and not backup:
        backup_path = archive_dir(db) / stored["backup"]
    else:
        backup_path = _backup_file(db, identity, at, backup)
    checks = preconditions(mind, config, scope=scope, backup=backup_path,
                           take_backup=backup is None, archive_bytes=remaining, identity=identity)
    if resuming:
        checks.append({"check": "history", "ready": not moved,
                       "reason": "history-moved-since-the-run-started" if moved
                       else "history-matches-the-run-manifest"})
    if not apply:
        return {"state": "dry-run", "scope": scope,
                "ready": all(check["ready"] for check in checks), "preconditions": checks,
                "cursor": stored["through"], "head": head, "archive": path.name,
                "backup": backup_path.name, "bytes_remaining": remaining,
                **_estimate(db, scope, stored, shapes, remaining)}

    _require(checks)
    if not resuming:
        manifest = _freeze_manifest(survey, scope, stored["through"], at=at)
    if not backup_path.exists():
        _take_backup(db, backup_path)
    # Whether it was just taken, was already there, or was named by the operator, the backup is
    # opened and proven against the frozen manifest before the run starts — and what was trusted
    # is written into the run's own record, so the basis it stood on is reviewable afterwards.
    verification = _backup_verify(scope, backup_path, manifest, at=at)
    settled = {"archive": path.name, "backup": backup_path.name,
               "started_at": stored.get("started_at") or at, "updated_at": at,
               "manifest": manifest, "backup_verified": verification}
    with db.connect(write=True) as conn:
        _record(conn, scope, stored["through"], settled)
    _set_marker(db, True)
    return _run(mind, scope, at, path, shapes, settled, batch=batch, limit=limit, head=head)


def _estimate(db, scope, stored, shapes, remaining):
    """What a dry run can say about the saving without doing the run.

    One batch of patches is really computed, against the real documents, and that ratio is applied
    to the rows that will become patches. The rows that keep their whole document are not among
    them and are counted at what they cost now, because that is what they will still cost: an
    estimate that applied the patch ratio to a checkpoint would promise a saving on the one kind of
    row this deliberately does not save on. It is an estimate and says so; what it is not is a
    guess."""
    planned = {"rows": shapes["rows"], "already_new_format": shapes["already_new_format"],
               "planned_whole": len(shapes["whole"]), "whole_reasons": _counted(shapes["whole"])}
    sampled = measured = before = 0
    carried, unreadable = None, []
    with db.connect() as conn:
        kept = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)),0) FROM mind_events WHERE scope=? AND revision>?"
            " AND revision IN (%s)" % ",".join("?" * len(shapes["whole"])),
            (scope, stored["through"], *shapes["whole"])).fetchone()[0] if shapes["whole"] else 0
        for row in conn.execute(
                "SELECT id,scope,revision,kind,occurred_at,data FROM mind_events"
                " WHERE scope=? AND revision>? ORDER BY revision LIMIT ?",
                (scope, stored["through"], SAMPLE_ROWS + 1)).fetchall():
            data = _parse(row["data"])
            if data is None:
                unreadable.append(row["revision"])
                carried = None
                continue
            if _new_format(data):
                carried = None
                continue
            state = history.snapshot_of(data)
            if state is None:
                unreadable.append(row["revision"])
                carried = None
                continue
            text = history.canonical(state)
            if carried is not None and row["revision"] not in shapes["whole"]:
                sampled += 1
                before += len(text.encode())
                measured += len(dumps(history.diff(carried, state)).encode())
            carried = state
    share = (measured / before) if before else None
    return {**planned, "sampled_rows": sampled, "sampled_bytes": before,
            "sampled_patch_bytes": measured, "estimated_patch_share": share,
            "whole_bytes": kept,
            "estimated_bytes_after": int(max(remaining - kept, 0) * share) + kept
            if share is not None else None,
            "unreadable": unreadable}


def _counted(whole):
    counts = {}
    for reasons in whole.values():
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _run(mind, scope, at, path, shapes, settled, *, batch, limit, head):
    """The two phases, in the only order they may happen in.

    Phase one archives and read-back-verifies the whole target set before anything moves, checking
    each row against the frozen manifest as it goes. Phase two is the batch rewrite loop, which
    accepts no row phase one did not cover: every row it reads is checked against the coverage
    proof before it is prepared, so there is no committed state in which a row was rewritten
    without a verified archive copy of what it held."""
    db = mind.engine.db
    done = {"rewritten": 0, "checkpoints": 0, "patched": 0, "passed_over": 0, "archived": 0,
            "archive_confirmed": 0, "bytes_before": 0, "bytes_after": 0}
    reasons, stopped = {}, None
    with db.connect() as conn:
        through = progress(conn, scope)["through"]
    covered, done["archived"], done["archive_confirmed"] = _archive_target_set(
        db, path, scope, through, settled, at=at, batch=batch, limit=limit)
    while True:
        # `limit` bounds the rows an invocation touches, not only the batches it runs, so an
        # operator who asked to see three rows before committing to the rest sees three.
        take = batch if limit is None else min(batch, int(limit) - done["rewritten"] - done["passed_over"])
        if take <= 0:
            stopped = "limit"
            break
        with db.connect() as conn:
            through = progress(conn, scope)["through"]
            rows = conn.execute(
                "SELECT id,scope,revision,kind,occurred_at,data FROM mind_events"
                " WHERE scope=? AND revision>? ORDER BY revision LIMIT ?",
                (scope, through, take)).fetchall()
            if not rows:
                break
            carried = _carry(conn, scope, through) if through else None
            prepared, landed = [], []
            for row in rows:
                _covered(covered, row)
                data = _parse(row["data"])
                if data is not None and _new_format(data):
                    # Already the shape this produces. It is archived like every other row and then
                    # passed over, and the state it rebuilds to is what the next row stands on.
                    prepared.append((row, None, "passed-over"))
                    landed.append((row["revision"], None, "passed-over"))
                    carried = _carry_past(conn, scope, row, data, carried)
                    continue
                new, text, carried, shape, fired = _rewrite(
                    row, carried, shapes["whole"].get(row["revision"], ()))
                prepared.append((row, dumps(new), shape))
                landed.append((row["revision"], text, shape))
                for reason in fired:
                    reasons[reason] = reasons.get(reason, 0) + 1

        # No per-batch archive here: phase one already holds every row this loop may touch, and
        # `_covered` has just proved it of each row in this batch.
        with db.connect(write=True) as conn:
            for row, text, shape in prepared:
                if text is None:
                    done["passed_over"] += 1
                    continue
                changed = conn.execute(
                    "UPDATE mind_events SET data=? WHERE scope=? AND revision=? AND data=?",
                    (text, scope, row["revision"], row["data"])).rowcount
                if changed != 1:
                    _refuse("row-changed-under-the-rewrite", revision=row["revision"])
                done["rewritten"] += 1
                done["checkpoints" if shape == "checkpoint" else "patched"] += 1
                done["bytes_before"] += len(row["data"].encode())
                done["bytes_after"] += len(text.encode())
            if landed:
                base = conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM mind_events WHERE scope=? AND revision<?",
                    (scope, landed[0][0])).fetchone()[0]
                _verify_landed(conn, scope, landed,
                               _carry(conn, scope, base)["state"] if base else None, base or None)
            _record(conn, scope, rows[-1]["revision"],
                    {**settled, "updated_at": at, "counts": dict(done)})

    with db.connect() as conn:
        through = progress(conn, scope)["through"]
    # Finished means the cursor has reached the last row, whatever stopped the loop. A `limit` that
    # happened to land on the end finished the history, and the store is opened again for it.
    complete = through >= head
    if complete:
        # The run's record is closed with what it did and when it finished: `finished_at` is what
        # tells the next invocation that this manifest belongs to a completed run, and that a new
        # run freezes its own.
        with db.connect(write=True) as conn:
            _record(conn, scope, through,
                    {**settled, "updated_at": at, "counts": dict(done), "finished_at": at})
        _set_marker(db, False)
    return {"state": "complete" if complete else "stopped", "scope": scope,
            "cursor": through, "head": head, "archive": path.name, "backup": settled["backup"],
            "run_id": settled["manifest"]["run_id"],
            "backup_verified": settled["backup_verified"],
            "stopped_because": None if complete else stopped, "whole_reasons": reasons,
            "compaction_active": not complete,
            "rows": shapes["rows"], "already_new_format": shapes["already_new_format"],
            "saved_bytes": done["bytes_before"] - done["bytes_after"], **done}


# --- checking it afterwards ------------------------------------------------------------------------------

@command("history-compact-verify")
def compact_verify(mind, *, limit=SAMPLE, deep=False, archive=None):
    """Rebuild every revision the archive holds and compare it with the archived original bytes.

    This is the check the rewrite is allowed to have happened for. `history-verify` asks whether a
    row still hashes to what it claims, which after a compaction is two halves of one pass agreeing
    with each other. This asks whether what the store can now rebuild is the document that was there
    before anything was touched, and what it compares against was copied out before the first row
    moved.

    Read only, against the store and the archive both. One forward pass by default, which is one
    rebuild per revision. `deep` rebuilds each revision from its own nearest whole document instead:
    the same answer reached without carrying anything between rows, much slower, and worth running
    once on a copy before a release."""
    scope, limit = mind.scope.key(), max(1, min(200, int(limit)))
    db = mind.engine.db
    with db.connect() as conn:
        stored = progress(conn, scope)
    path = _named(db, archive) if archive else _archive_file(db, stored, mind.clock())
    counts = {"checked": 0, "rebuilt": 0, "differs": 0, "unrebuildable": 0, "archive_corrupt": 0,
              "patch_originals": 0}
    differs, unrebuildable = [], []
    source = _archive_connection(path)
    try:
        held = source.execute(
            "SELECT revision,data,data_sha256 FROM mind_events_v1 WHERE scope=? ORDER BY revision",
            (scope,)).fetchall()
    finally:
        source.close()
    with db.connect() as conn:
        state, previous = None, None
        for row in held:
            revision = row["revision"]
            counts["checked"] += 1
            if digest(row["data"].encode()) != row["data_sha256"]:
                counts["archive_corrupt"] += 1
                differs.append(revision)
                state, previous = None, revision
                continue
            archived = _parse(row["data"])
            want = history.snapshot_of(archived) if archived else None
            state, previous = (_materialize(conn, scope, revision), revision) if deep \
                else _step(conn, scope, revision, state, previous)
            if want is None:
                # The archived original was already a patch when it was copied, so there is no
                # document here to compare a document with. The walk still has to pass through it.
                counts["patch_originals"] += 1
                continue
            if state is None:
                counts["unrebuildable"] += 1
                unrebuildable.append(revision)
            elif history.canonical(state) == history.canonical(want):
                counts["rebuilt"] += 1
            else:
                counts["differs"] += 1
                differs.append(revision)
    ok = counts["checked"] and not (counts["differs"] or counts["unrebuildable"]
                                    or counts["archive_corrupt"])
    return {"state": "verified" if ok else "empty" if not counts["checked"] else "incomplete",
            "scope": scope, "archive": path.name, "cursor": stored["through"], "deep": bool(deep),
            **counts, "differs": differs[:limit], "differs_count": len(differs),
            "unrebuildable": unrebuildable[:limit], "unrebuildable_count": len(unrebuildable)}


def _materialize(conn, scope, revision):
    try:
        return history.materialize(conn, scope, revision)
    except Conflict as error:
        if error.code != history.REBUILD_FAILED:
            raise
        return None


def _step(conn, scope, revision, state, previous):
    """One row of the forward walk: apply what it says changed, or take the document it carries."""
    row = conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                       (scope, revision)).fetchone()
    data = _parse(row["data"]) if row else None
    if data is None:
        return None, revision
    if not history.is_patch(data):
        return history.snapshot_of(data), revision
    if state is None or data.get("base") != previous:
        return _materialize(conn, scope, revision), revision
    try:
        return history.apply(state, data["patch"]), revision
    except Conflict as error:
        if error.code != history.REBUILD_FAILED:
            raise
        return None, revision


# --- putting it back -------------------------------------------------------------------------------------

@command("history-restore", config=True)
def restore(mind, config=None, *, apply=False, batch=BATCH, archive=None):
    """Write the archived originals back into the history, row by row.

    The way out of a compaction that should not have happened, or one that stopped somewhere nobody
    wants to resume from. Every row the archive holds for this scope is compared with the row in the
    store, and where they differ the archived bytes go back exactly as they were. The columns beside
    `data` are compared and never written: if one of them has moved, something other than compaction
    has been here, and that is a thing to stop for rather than to paper over.

    Quiet is required here too. The marker stops the mind writing revisions, but this writes the
    same rows compaction does and must not race anything that might still be reading them.

    The archive is not touched, on success or on failure. It is the only copy of what those rows
    said, and nothing in this package deletes it."""
    scope, at = mind.scope.key(), mind.clock()
    db, batch = mind.engine.db, max(1, min(200, int(batch)))
    with db.connect() as conn:
        stored = progress(conn, scope)
    path = _named(db, archive) if archive else _archive_file(db, stored, at)
    if not path.exists():
        _refuse("archive-missing", archive=path.name)
    source = _archive_connection(path)
    try:
        held = source.execute(
            "SELECT revision,id,kind,occurred_at,data FROM mind_events_v1 WHERE scope=?"
            " ORDER BY revision", (scope,)).fetchall()
    finally:
        source.close()
    checks = [_quiet_check(mind, config)]
    if not apply:
        with db.connect() as conn:
            differing = [row["revision"] for row in held if _differs(conn, scope, row)]
        return {"state": "dry-run", "scope": scope, "archive": path.name,
                "ready": all(check["ready"] for check in checks), "preconditions": checks,
                "archived": len(held), "would_restore": len(differing),
                "revisions": differing[:SAMPLE], "cursor": stored["through"]}
    _require(checks)
    restored = unchanged = 0
    for start in range(0, len(held), batch):
        with db.connect(write=True) as conn:
            for row in held[start:start + batch]:
                current = conn.execute(
                    "SELECT id,kind,occurred_at,data FROM mind_events WHERE scope=? AND revision=?",
                    (scope, row["revision"])).fetchone()
                if current is None:
                    _refuse("row-is-gone", revision=row["revision"])
                for column in ("id", "kind", "occurred_at"):
                    if current[column] != row[column]:
                        _refuse("row-column-has-moved", revision=row["revision"], column=column)
                if current["data"] == row["data"]:
                    unchanged += 1
                    continue
                conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                             (row["data"], scope, row["revision"]))
                restored += 1
    with db.connect(write=True) as conn:
        _record(conn, scope, 0, {**{key: stored[key] for key in ("archive", "backup", "started_at")
                                    if key in stored},
                                 "archive": path.name, "restored_at": at, "restored": restored})
    # The history is what it was, so the store is open again. The archive stays where it is.
    _set_marker(db, False)
    return {"state": "restored", "scope": scope, "archive": path.name, "restored": restored,
            "unchanged": unchanged, "archived": len(held), "cursor": 0}


def _differs(conn, scope, row):
    current = conn.execute("SELECT data FROM mind_events WHERE scope=? AND revision=?",
                           (scope, row["revision"])).fetchone()
    return current is None or current["data"] != row["data"]
