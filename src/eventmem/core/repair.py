"""The one-time data repair that goes with the 2026-09-24 memory-kernel release.

    python -m eventmem.core.repair --root <MemoryPalace>            # dry run: writes nothing
    python -m eventmem.core.repair --root <MemoryPalace> --snapshot # dry run of a still copy
    python -m eventmem.core.repair --root <MemoryPalace> --apply    # the default steps
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps reerase
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps quarantine

Run it with the memory service stopped, after the backup the deploy takes. Every step is
idempotent: it asks what is already done rather than counting what it did, so a second run
reports nothing left and writes nothing. The dry run opens the store read-only and builds
nothing: no table, index or metadata of this release, no cache under the root (CR-MEM-05).
SQLite itself still keeps a reader's `-wal` and `-shm` beside a store in WAL mode, and a reader
cannot take them away when it is the last to leave. `--snapshot` leaves not even those: the store
is cloned from a moment when nothing holds it or its side files open and nothing about them changes
(lsof, and inode, size and times before and after), into a private directory outside the root, and
the dry run reads the clone, which goes when it is done. A store never still for `--wait` seconds
is refused. It is the dry run for a store in use -- one taken before the services stop.
A root without a `memory.sqlite3` is refused in both modes rather than becoming a new store.

Exit status: 0 with the JSON report on stdout (also in `--output`, 0600); 2 when refused — an
unknown step or no store at the root — with one line on stderr and nothing on stdout; anything
else (1) is a failure -- one line on stderr when `reerase` stopped itself because its deletes would
take, or took, more than its own dry run said (below), a traceback otherwise -- and an `--apply`
that failed may have finished the steps before the failing one, each of which a rerun finds done. Only `reerase` deletes, and only what a
delete made before this release should have taken (below); nothing else here deletes a record, a
source, a revision or a history row: records leave active use by a new revision (`archive`,
`superseded`), queue rows change state, and derived indexes are rebuilt. The report carries
identifiers, namespaces and counts only — never a line of content.

Steps, in order (the default is all but `reerase` and `quarantine`):

- `origins`   Relabel what is already stored by the source-origin table
              (`source-origins.json`): a classification row for every source whose namespace or
              metadata the table names and that has none yet. By source identity, never by
              words in the text (K4-12, K4-16, E3-06).
- `maintenance` The host's own bookkeeping leaves the text index and the organise queue, and
              the session-maintenance notes that nothing current cites are archived (DB1-01).
- `versions`  Root records of an older version of a source that are still active beside the
              newer one are superseded by the engine's own rule (DB1-13).
- `queues`    Dirty rows of records that left active use are dropped (DB1-08); failed digest
              jobs whose digest is ready again are marked canceled, and sources whose
              extraction can no longer finish are marked failed rather than pending (DB1-09).
              Embed jobs that failed only because the embedding service was down, for a record
              still current at that revision, are queued once more (K4-04).
- `indexes`   The graph and memory node indexes are rebuilt under their nodes' rowids, once
              (K4-10, DB1-07).
- `spool`     Host receipts that can never be replayed move to host-spool/rejected/ (E1-02,
              E3-17, DB1-06); the rest are replayed as the service would.
- `evidence`  The evidence-key table is filled up to the head of history and compared with the
              snapshot scan row by row; where the two agree completely, the old scan stops
              running inside every appraisal's write lock (`history_legacy_guard` off). Where
              they do not, nothing is switched and the report names the counts (K4-15).
- `lineage`   Sources written from others before this release carry no record of it, so a
              delete of what they were written from never reached them. Each is given what the
              store recorded it was written from as `derived_from`, and its root record those
              records as dependencies -- the rule this release applies when it stores one: a
              reflection's `evidence_ids` and what its appraisal was about (the request of its
              event's history row), an exploration report's run's `evidence_ids` (only a report
              with a result), a creation's artifact event's `input_source_ids`, and a creation's
              result event the step's evidence as its run (`task_id`) recorded it, or as the
              run's artifact events named it (CL6-MM-06). A source that has a lineage already is
              given only what it lacks, so a later run -- or a later release that knows another
              basis -- still completes it. A result the host observed (a native task's reply)
              names nothing it was written from: it stays the conversation record, counted as
              `results_without_basis`. Settled runs and the mind's command receipts name what
              they rest on as well (`result.evidence_ids`, `rests_on`), so a delete finds the
              words they hold. Only a delete follows a dependency; an archive, a correction, a
              replacement or a newer version of an input leaves the derived source as it is. An
              input already deleted is named, and `reerase` erases the derived source; an input
              the store never held, or holds in another scope, is left out and counted. Nothing
              is erased here (CR5-MM-02). What it reads of the history is read once per run.
- `reerase`   Opt-in. Deletes made before this release left their words in the mind's derived
              layers and state history; this runs the same erase for every tombstone and
              queues the history rewrite (K4-01, K4-20, K4-21). A derived source whose lineage
              names a deletion fact is deleted as a delete of that input would have taken it,
              and stored receipts, sessions and metrics that name a deletion fact go, as a
              delete removes them (CR5-MM-02). The plan says how much those deletes take --
              `records`, `sources`, and `derived_in_closure`, the other derived sources among
              them -- counted as they will be after `lineage` when both run, and the apply says
              what they took; the release's `expect` holds the plan (CL6-MM-02). It says them by
              kind too: the derived sources as reflections, reports, a creation's artifact and
              result events (`derived_by_kind`, `closure_by_kind`), the reflections taken only for
              what their appraisal was about (`reflections_by_target`), the sources' own records
              beside the notes that cite them (`records_by_kind`). The apply holds itself to the
              plan of the same run: deletes that would take more are not made, and deletes that
              took more stop the run (CL6D-MM-02). Beside them, every derived source the store
              holds, by the same kinds (`store_by_kind`), so the share each deletion takes can be
              read. The rows of a model's or an executor's own process that do not name what
              they were shown -- every one a release before this one wrote -- lose their words to
              any delete, and are counted by table among the layers as `unnamed:<table>`; a queue
              row still to be judged also loses the proposal it kept, and is judged afresh
              (CL6D-MM-04). What a release before this one rendered from the conversation habits
              -- a delivery, a window receipt, a compression -- names no message they were set
              from: in a scope where a deletion fact set a habit it goes, counted with the rest
              (`context_receipts`, `mind_context_cache`, CL8-MM-01). It rewrites history rows,
              so it is named explicitly; run it after `lineage`, in the same run or a later one.
              What an earlier run already erased is left alone, and the history is queued only
              for identifiers it has neither finished nor still owes a pass for, so a second run
              changes nothing (CR-MEM-06).
- `quarantine` Opt-in. Quarantined appraisals whose evidence is gone are retired (state
              `superseded`, reason kept, no model call); the others are listed by reason for a
              decision, never resumed here, because a resume pays for a model call (K4-06, DB1-05).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .db import NAMED, digest, named_in_json
from .models import Scope
from .read_policy import (
    RULE_ORIGIN,
    RULES_VERSION,
    UNINDEXED_ORIGINS,
    origin_found,
    origin_of,
    origin_of_rule,
)

STEPS = ("origins", "maintenance", "versions", "queues", "indexes", "spool", "evidence", "lineage", "reerase", "quarantine")
DEFAULT_STEPS = STEPS[:-2]
COMMAND = "repair-20260924"
# The maintenance notes this release archives: session-maintenance requests. Internal mind
# events stay active — appraisals and actions cite them as their stimulus — and only leave the
# index.
ARCHIVED_NAMESPACES = ("kin-session-maintenance",)
CHUNK = 200


class Refused(ValueError):
    """The command was asked for something it will not do; nothing was read or written."""


class ReadOnlyStore:
    """The store as the dry run sees it (CR-MEM-05): a read-only connection per read, and nothing
    created or initialised on the way in. `Engine()` would run this release's DDL, fill in
    metadata and pin the tokenizer cache under the root before the report said a word. The only
    files that may appear are the `-wal` and `-shm` SQLite coordinates its readers with."""

    def __init__(self, path, root=None):
        self.path = Path(path).resolve()
        # A snapshot's store is read from its clone, and the rest of the root where it lies.
        self.root = Path(root).resolve() if root is not None else self.path.parent
        self.blobs = self.root / "blobs"

    @contextmanager
    def connect(self, write=False):
        if write:
            raise PermissionError("The dry run writes nothing")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=1")
            conn.execute("BEGIN")
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def metric(self, name, value, data=None):
        """A dry run records nothing, not even that it ran."""


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# --- a still copy, for a dry run of a store in use -------------------------------------------

SIDE_FILES = ("-wal", "-shm", "-journal")
# How long a snapshot waits for a moment when the store is still, and how often it looks.
SNAPSHOT_WAIT = 120.0
SNAPSHOT_PAUSE = 1.0


def _side_files(store):
    return [store.with_name(store.name + suffix) for suffix in SIDE_FILES if store.with_name(store.name + suffix).exists()]


def _fingerprint(files):
    """The files as the file system has them: inode, size and the times of change, to the nanosecond."""
    out = []
    for path in files:
        try:
            found = os.stat(path)
            out.append((path.name, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns))
        except FileNotFoundError:
            out.append((path.name, None))
    return out


def holders(files):
    """Whether any process holds any of `files` open, as lsof sees every process of this user:
    `none`, `some`, or `unknown` when it cannot say."""
    try:
        answer = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-w", "-F", "p", "--", *map(str, files)],
                                capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if answer.returncode == 0 and re.search(r"^p\d+$", answer.stdout, re.M):
        return "some"
    if answer.returncode == 1 and not answer.stdout.strip() and not answer.stderr.strip():
        return "none"
    return "unknown"


def _clone(source, target):
    if subprocess.run(["/bin/cp", "-c", "-p", str(source), str(target)], capture_output=True).returncode != 0:
        shutil.copy2(source, target)


def snapshot(store, into, *, wait=SNAPSHOT_WAIT, pause=SNAPSHOT_PAUSE, held=holders, clock=time.monotonic, sleep=time.sleep):
    """`store` and its side files cloned into `into` from one moment of them, without SQLite, or
    anything else of ours, ever opening them: taken only when no process holds any of them open,
    before and after, and they are the same file for file (inode, size, times) and the same set
    after the clone as before. A write meanwhile changes the store or its side files, and the clone
    is taken again. A WAL or a journal it came with is recovered in the clone, never beside the
    store. Refused when the store is not still for `wait` seconds."""
    deadline, tries, last = clock() + wait, 0, None
    while True:
        tries += 1
        files = [store, *_side_files(store)]
        before, first = _fingerprint(files), held(files)
        if first == "none":
            copies = [into / path.name for path in files]
            for path, copy in zip(files, copies):
                _clone(path, copy)
            now = [store, *_side_files(store)]
            second = held(now)
            if second == "none" and _fingerprint(now) == before:
                if len(copies) > 1:
                    with sqlite3.connect(copies[0]) as conn:
                        if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return {"how": "still-clone", "tries": tries, "side_files": [path.name[len(store.name):] for path in files[1:]]}
            for copy in copies:
                copy.unlink(missing_ok=True)
            last = "held" if second == "some" else "unknown" if second == "unknown" else "changed"
        else:
            last = "held" if first == "some" else "unknown"
        if clock() >= deadline:
            raise Refused(f"The store was not still for a moment in {wait:g} seconds (last: {last}); nothing was read")
        sleep(pause)


def _root(sid):
    return "mem_" + digest([sid, "root"])[:32]


# --- origins ---------------------------------------------------------------------------------

def plan_origins(conn):
    """Sources the origin table classifies and that carry no classification row yet."""
    rows = []
    for row in conn.execute(
            "SELECT s.id,s.namespace,s.scope,s.data FROM sources s LEFT JOIN source_evidence_class c"
            " ON c.source_id=s.id AND c.scope=s.scope WHERE c.source_id IS NULL ORDER BY s.id"):
        try:
            metadata = json.loads(row["data"]).get("metadata")
        except (ValueError, AttributeError):
            metadata = None
        kind = origin_of(row["namespace"], metadata)
        if kind is None:
            continue
        if (metadata or {}).get("role") == "user" and json.loads(row["data"]).get("authority") == "explicit":
            # The owner's own words stay experience whatever namespace carried them.
            continue
        found = origin_found(kind)
        rows.append({"source_id": row["id"], "scope": row["scope"], "namespace": row["namespace"],
                     "origin": kind, "class": found.kind, "rule": found.rule})
    return rows


def apply_origins(engine, rows):
    written = 0
    for start in range(0, len(rows), CHUNK):
        with engine.db.connect(write=True) as conn:
            for row in rows[start:start + CHUNK]:
                written += conn.execute("INSERT OR IGNORE INTO source_evidence_class VALUES(?,?,?,?,?)",
                                        (row["scope"], row["source_id"], row["class"], row["rule"],
                                         RULES_VERSION)).rowcount
            engine.db.bump(conn)
    return written


# --- maintenance -----------------------------------------------------------------------------

def _unindexed_sources(conn):
    marks = ",".join("?" for _ in UNINDEXED_ORIGINS)
    return [row[0] for row in conn.execute(
        f"SELECT source_id FROM source_evidence_class WHERE rule IN ({marks})",
        [RULE_ORIGIN + kind for kind in sorted(UNINDEXED_ORIGINS)])]


def plan_maintenance(conn, pending_origins=()):
    """Root records of the host's own bookkeeping that are still indexed or queued, and the
    session-maintenance notes that can be archived: active, and cited neither by the current
    state of any mind nor by an assessment still to run."""
    sources = set(_unindexed_sources(conn)) | {row["source_id"] for row in pending_origins
                                              if origin_of_rule(row["rule"]) in UNINDEXED_ORIGINS}
    indexed, queued, archive, cited = [], [], [], 0
    # Everything the current states and the assessments still to run name, read once.
    named = set()
    if _table(conn, "mind_state"):
        for (text,) in conn.execute("SELECT data FROM mind_state"):
            named.update(named_in_json(text))
    if _table(conn, "mind_appraisals"):
        for (text,) in conn.execute("SELECT data FROM mind_appraisals WHERE state IN ('pending','running','batched')"):
            named.update(named_in_json(text))
    for sid in sorted(sources):
        record = conn.execute("SELECT rowid,id,status FROM records WHERE id=?", (_root(sid),)).fetchone()
        if record is None:
            continue
        if conn.execute("SELECT 1 FROM search WHERE rowid=?", (record["rowid"],)).fetchone():
            indexed.append(record["id"])
        if conn.execute("SELECT 1 FROM dirty WHERE record_id=?", (record["id"],)).fetchone():
            queued.append(record["id"])
        namespace = conn.execute("SELECT namespace FROM sources WHERE id=?", (sid,)).fetchone()
        if record["status"] != "active" or not namespace or namespace[0] not in ARCHIVED_NAMESPACES:
            continue
        if sid in named or record["id"] in named:
            cited += 1
            continue
        archive.append(record["id"])
    return {"unindex": indexed, "unqueue": queued, "archive": archive, "kept_because_cited": cited}


def apply_maintenance(engine, plan):
    from .models import RevisionInput

    with engine.db.connect(write=True) as conn:
        for rid in plan["unindex"]:
            row = conn.execute("SELECT rowid FROM records WHERE id=?", (rid,)).fetchone()
            if row:
                conn.execute("DELETE FROM search WHERE rowid=?", (row[0],))
        for rid in plan["unqueue"]:
            conn.execute("DELETE FROM dirty WHERE record_id=?", (rid,))
    archived = 0
    for rid in plan["archive"]:
        record = engine.get(rid)
        if record["status"] != "active":
            continue
        engine.revise(rid, RevisionInput(expected_revision=record["revision"], command_id=f"{COMMAND}:archive:{rid}",
                                         action="archive", reason="Host maintenance note, not a shared experience"))
        archived += 1
    return {"unindexed": len(plan["unindex"]), "unqueued": len(plan["unqueue"]), "archived": archived}


# --- versions --------------------------------------------------------------------------------

def plan_versions(conn):
    """Groups of one source identity (namespace, key, scope) whose root records of more than one
    version are active side by side."""
    groups, candidates = [], {}
    for row in conn.execute("SELECT id,namespace,source_key,scope,version,data FROM sources ORDER BY id"):
        try:
            kind = json.loads(row["data"]).get("kind")
        except (ValueError, AttributeError):
            kind = None
        if kind == "checkpoint":
            continue  # checkpoints replace by session, and that path already ran on receipt
        candidates.setdefault((row["namespace"], row["source_key"], row["scope"]), []).append(row)
    for (namespace, key, scope), rows in sorted(candidates.items()):
        if len({row["version"] for row in rows}) < 2:
            continue
        active = [row["id"] for row in rows if conn.execute(
            "SELECT 1 FROM records WHERE id=? AND status='active'", (_root(row["id"]),)).fetchone()]
        if len(active) > 1:
            groups.append({"namespace": namespace, "source_ids": sorted(active)})
    return groups


def apply_versions(engine, groups):
    changed = 0
    for group in groups:
        with engine.db.connect(write=True) as conn:
            before = conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            engine.supersede_source_versions(conn, group["source_ids"][0])
            changed += conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] - before
            engine.db.bump(conn)
    return changed


# --- queues ----------------------------------------------------------------------------------

def plan_queues(conn):
    dirty = [row[0] for row in conn.execute(
        "SELECT d.record_id FROM dirty d LEFT JOIN records r ON r.id=d.record_id"
        " WHERE r.id IS NULL OR r.status!='active' OR r.deleted!=0 ORDER BY d.record_id")]
    digests = []
    if _table(conn, "mind_event_digests"):
        for row in conn.execute("SELECT id,payload FROM jobs WHERE kind='event_digest' AND state='failed' ORDER BY id"):
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                continue
            scope = payload.get("scope")
            try:
                scope = Scope.model_validate(scope).key() if isinstance(scope, dict) else scope
            except ValueError:
                continue
            ready = conn.execute("SELECT 1 FROM mind_event_digests WHERE scope=? AND event_id=? AND state='ready'",
                                 (scope, payload.get("event_id"))).fetchone()
            if ready:
                digests.append(row["id"])
    stuck = [row[0] for row in conn.execute(
        "SELECT s.id FROM sources s WHERE s.model='pending' AND NOT EXISTS(SELECT 1 FROM jobs j WHERE"
        " j.kind IN ('extract','extract_part','extract_complete') AND json_extract(j.payload,'$.source_id')=s.id"
        " AND j.state NOT IN ('failed','canceled')) AND EXISTS(SELECT 1 FROM jobs j WHERE"
        " j.kind IN ('extract','extract_part','extract_complete') AND json_extract(j.payload,'$.source_id')=s.id)"
        " ORDER BY s.id")]
    from .jobs import ENVIRONMENTAL_ERRORS

    embeds = []
    for row in conn.execute("SELECT j.id,j.error,j.payload FROM jobs j WHERE j.kind IN ('embed','visual_embed')"
                            " AND j.state='failed' ORDER BY j.id"):
        if (row["error"] or "") not in ENVIRONMENTAL_ERRORS:
            continue
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        current = conn.execute("SELECT 1 FROM records WHERE id=? AND revision=? AND deleted=0"
                               " AND status IN ('active','unverified')",
                               (payload.get("record_id"), payload.get("revision"))).fetchone()
        if current:
            embeds.append(row["id"])
    return {"dirty": dirty, "digest_jobs": digests, "extraction_sources": stuck, "embed_jobs": embeds}


def apply_queues(engine, plan):
    with engine.db.connect(write=True) as conn:
        for start in range(0, len(plan["dirty"]), CHUNK):
            page = plan["dirty"][start:start + CHUNK]
            conn.execute(f"DELETE FROM dirty WHERE record_id IN ({','.join('?' for _ in page)})", page)
        for jid in plan["digest_jobs"]:
            conn.execute("UPDATE jobs SET state='canceled',updated_at=datetime('now') WHERE id=? AND state='failed'", (jid,))
        for sid in plan["extraction_sources"]:
            conn.execute("UPDATE sources SET model='failed' WHERE id=? AND model='pending'", (sid,))
    from .jobs import Worker

    recovered = 0
    for start in range(0, len(plan["embed_jobs"]), 50):
        page = plan["embed_jobs"][start:start + 50]
        recovered += len(Worker(engine).recover(page, command_id=f"{COMMAND}:embeds:{digest(page)[:16]}")["recovered"])
    return {**{key: len(value) for key, value in plan.items()}, "embed_jobs": recovered}


# --- indexes ---------------------------------------------------------------------------------

def plan_indexes(conn):
    from kin_mind import graph, memory

    found = {}
    for name, table, flag in (("graph", "mind_graph_search", graph.SEARCH_ALIGNED),
                              ("memory", "mind_memory_search", memory.SEARCH_ALIGNED)):
        if _table(conn, table) and not conn.execute("SELECT 1 FROM meta WHERE key=?", (flag,)).fetchone():
            found[name] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return found


def apply_indexes(engine, plan):
    from kin_mind import graph, memory

    done = {}
    with engine.db.connect(write=True) as conn:
        if "graph" in plan:
            done["graph"] = graph.align_search(conn)
        if "memory" in plan:
            done["memory"] = memory.align_search(conn)
    return done


# --- spool -----------------------------------------------------------------------------------

def plan_spool(engine):
    spool = engine.db.root / "host-spool"
    files = sorted(spool.glob("*.json")) if spool.exists() else []
    unreadable = 0
    for path in files:
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or "event" not in data or "payload" not in data:
                unreadable += 1
        except (OSError, ValueError):
            unreadable += 1
    return {"receipts": len(files), "unreadable": unreadable}


def apply_spool(engine):
    from .jobs import SPOOL_REJECTED, Worker

    worker = Worker(engine)
    worker.replay_hosts()
    spool = engine.db.root / "host-spool"
    return {"left": len(list(spool.glob("*.json"))) if spool.exists() else 0,
            "rejected": len(list((spool / SPOOL_REJECTED).glob("*.json"))) if (spool / SPOOL_REJECTED).exists() else 0}


# --- lineage ---------------------------------------------------------------------------------

# The runtime events a creation writes from the step's evidence (`input_source_ids`).
CREATION_EVENTS = ("artifact-created", "task-result")


def _json(text):
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def _named(values):
    return [value for value in values or () if isinstance(value, str) and NAMED.fullmatch(value)]


def _tombstoned(conn, keys):
    keys = sorted(set(keys))
    for start in range(0, len(keys), 500):
        page = keys[start:start + 500]
        if conn.execute(f"SELECT 1 FROM tombstones WHERE key IN ({','.join('?' for _ in page)}) LIMIT 1", page).fetchone():
            return True
    return False


def _resolve(conn, identifier, scope):
    """One input of a derived source as this release registers it: `deleted` when the store holds
    its deletion fact; `present` with the reference to keep and the records its root depends on
    (a source's root record, or what was parsed from an attachment); `unresolved` when the store
    never held it, or holds it in another scope."""
    root = _root(identifier) if identifier.startswith("src_") else None
    if _tombstoned(conn, [identifier, *([root] if root else [])]):
        return "deleted", {"source_id" if root else "record_id": identifier}, []
    table = "sources" if root else "records"
    row = conn.execute(f"SELECT scope FROM {table} WHERE id=? AND deleted=0", (identifier,)).fetchone()
    if not row or row["scope"] != scope:
        return "unresolved", None, []
    if not root:
        return "present", {"record_id": identifier}, [identifier]
    if conn.execute("SELECT 1 FROM records WHERE id=? AND deleted=0", (root,)).fetchone():
        return "present", {"source_id": identifier}, [root]
    return "present", {"source_id": identifier}, [r[0] for r in conn.execute(
        "SELECT e.record_id FROM evidence e JOIN records r ON r.id=e.record_id WHERE e.source_id=? AND r.deleted=0"
        " ORDER BY e.record_id", (identifier,))]


def _content(conn, rid):
    row = conn.execute("SELECT CASE WHEN json_valid(data) THEN json_extract(data,'$.content') END FROM records WHERE id=?",
                       (rid,)).fetchone()
    return row[0] if row else None


def _history(conn, event_ids, cache):
    """What each mind event rested on as its history row keeps it -- `request.evidence_ids`, in the
    whole-document and the patch shapes alike: a list, empty when the request named nothing, or
    None when no history row holds the event any more. History rows do not change while the repair
    runs, so each is read once per run, in batches, whichever step asks (CL6-MM-09)."""
    missing = [event for event in dict.fromkeys(event_ids) if isinstance(event, str) and event not in cache]
    for name in ("mind_events", "mind_events_v1"):
        if not missing or not _table(conn, name):
            continue
        for start in range(0, len(missing), 500):
            page = missing[start:start + 500]
            for event, evidence in conn.execute(
                    f"SELECT id,CASE WHEN json_valid(data) THEN json_extract(data,'$.request.evidence_ids') END FROM {name}"
                    f" WHERE id IN ({','.join('?' for _ in page)})", page):
                cache.setdefault(event, _named(_json(evidence)) if evidence else [])
        missing = [event for event in missing if event not in cache]
    for event in missing:
        cache[event] = None
    return {event: cache.get(event) for event in event_ids if isinstance(event, str)}


def _candidates(conn, cache):
    """Every source written from others, with what the store recorded it was written from --
    whether or not an earlier run, or this release's own writer, gave it a lineage already
    (CL6-MM-06). Yields (kind, source id, scope, stored data, identifiers, notes): `notes` says
    where a basis could not be found."""
    reflections = conn.execute("SELECT id,scope,source_key,data FROM sources WHERE namespace='kin-reflection' ORDER BY id").fetchall()
    events = {}
    for row in reflections:
        metadata = (_json(row["data"]) or {}).get("metadata") or {}
        events[row["id"]] = metadata.get("appraisal_event_id") or row["source_key"]
    targets = _history(conn, events.values(), cache)
    for row in reflections:
        data = _json(row["data"]) or {}
        # What the understanding cited, and what the appraisal was about (`request.evidence_ids` of
        # its event): the lineage this release's writer gives a reflection (memory.py).
        about = targets.get(events[row["id"]])
        yield ("reflections", row["id"], row["scope"], data,
               [*_named((data.get("metadata") or {}).get("evidence_ids")), *(about or ())],
               {"targets": set(about or ()), "targets_unfound": about is None})
    runs = {}
    if _table(conn, "mind_explorations"):
        for (text,) in conn.execute("SELECT data FROM mind_explorations"):
            run = _json(text) or {}
            if isinstance(run.get("source_id"), str):
                runs[run["source_id"]] = run
    for row in conn.execute("SELECT id,scope,data FROM sources WHERE namespace='kin-exploration' ORDER BY id"):
        run = runs.get(row["id"])
        written = run if run is not None else _json(_content(conn, _root(row["id"]))) or {}
        if written.get("result") is None:
            continue  # a run that reported nothing wrote no words: no derived source
        yield "reports", row["id"], row["scope"], _json(row["data"]) or {}, _named((run or {}).get("evidence_ids")), {}
    created, by_task, decided = {}, {}, {}
    if _table(conn, "mind_runtime_events"):
        for scope, text in conn.execute("SELECT scope,data FROM mind_runtime_events WHERE kind IN (%s)"
                                        % ",".join("?" for _ in CREATION_EVENTS), CREATION_EVENTS):
            event = _json(text) or {}
            if isinstance(event.get("source_id"), str):
                created[event["source_id"]] = event
            if event.get("kind") == "artifact-created" and isinstance(event.get("task_id"), str):
                by_task.setdefault((scope, event["task_id"]), []).extend(_named(event.get("input_source_ids")))
    if _table(conn, "mind_plan_runs"):
        for row in conn.execute("SELECT id,scope,data FROM mind_plan_runs"):
            decision = (_json(row["data"]) or {}).get("decision") or {}
            decided[(row["scope"], row["id"])] = [ref["source_id"] for ref in decision.get("evidence") or []
                                                  if isinstance(ref, dict) and isinstance(ref.get("source_id"), str)]
    for row in conn.execute("SELECT id,scope,data FROM sources WHERE namespace='kin-runtime'"
                            " AND json_extract(data,'$.metadata.host_event') IN (%s) ORDER BY id"
                            % ",".join("?" for _ in CREATION_EVENTS), CREATION_EVENTS):
        data = _json(row["data"]) or {}
        event = created.get(row["id"]) or _json(_content(conn, _root(row["id"]))) or {}
        if "inputs_withheld" in event:
            continue  # stored without the words its inputs had: a fact, not a derived source
        kind = (data.get("metadata") or {}).get("host_event")
        inputs = _named(event.get("input_source_ids"))
        if kind == "task-result" and "input_source_ids" not in event:
            # The last release wrote a creation's result without its inputs: they are the step's
            # evidence, as the run that made it recorded them, or as its artifacts named them.
            task = (row["scope"], event.get("task_id"))
            inputs = decided.get(task) or list(dict.fromkeys(by_task.get(task) or []))
        if inputs:
            yield ("creations" if kind == "artifact-created" else "results"), row["id"], row["scope"], data, inputs, {}
        elif kind == "task-result" and "derived_from" not in data:
            # A result the host observed -- a native task's reply -- names nothing it was written
            # from: kept as the conversation record, like Kin's other replies, and counted.
            yield "results", row["id"], row["scope"], data, None, {}


def _identity(ref):
    return ref.get("source_id"), ref.get("record_id")


def _lineage_sources(conn, cache, totals=None):
    """The derived sources `lineage` would write to: each with the references to add to its
    `derived_from` (all of them, where it has none) and the dependencies its root record lacks.
    `totals`, when given, counts every derived source in the store by kind, registered or not."""
    sources = []
    for kind, sid, scope, data, inputs, notes in _candidates(conn, cache):
        if inputs is None:
            sources.append({"kind": kind, "source_id": sid, "without_basis": True})
            continue
        if totals is not None:
            totals[kind] += 1
        marked = data.get("derived_from")
        have = {_identity(ref) for ref in marked or () if isinstance(ref, dict)}
        refs, records, deleted, unresolved, added = [], [], [], 0, set()
        for identifier in dict.fromkeys(inputs):
            state, ref, rests = _resolve(conn, identifier, scope)
            if state == "unresolved":
                unresolved += 1
                continue
            records.extend(rests)
            if _identity(ref) not in have:
                refs.append(ref)
                added.add(identifier)
                if state == "deleted":
                    deleted.append(identifier)
        root = _root(sid)
        if not conn.execute("SELECT 1 FROM records WHERE id=? AND deleted=0", (root,)).fetchone():
            root, records = None, []
        new = [rid for rid in dict.fromkeys(records) if rid != root and not conn.execute(
            "SELECT 1 FROM dependencies WHERE record_id=? AND evidence_id=?", (root, rid)).fetchone()]
        if marked is not None and not refs and not new:
            continue  # nothing left to register
        sources.append({"kind": kind, "source_id": sid, "root": root, "marked": marked is not None, "refs": refs,
                        "records": new, "deleted": deleted, "unresolved": unresolved,
                        # A reflection that now names what its appraisal was about, or whose appraisal
                        # no history row holds any more.
                        "targets": bool(added & notes.get("targets", set())),
                        "targets_unfound": bool(notes.get("targets_unfound"))})
    return sources


def _lineage(conn, cache=None):
    """What `lineage` would write, by what it is: the derived sources with their references and
    dependencies, the runs and the receipts with the identifiers they would name. `cache` carries
    what was read of the history from one step to the next within a run."""
    cache = {} if cache is None else cache
    totals = dict.fromkeys(KINDS, 0)
    sources = _lineage_sources(conn, cache, totals)
    runs = []
    if _table(conn, "mind_plan_runs"):
        for row in conn.execute("SELECT id,data FROM mind_plan_runs WHERE CASE WHEN json_valid(data) THEN"
                                " json_type(data,'$.result')='object' AND json_type(data,'$.result.evidence_ids') IS NULL END"
                                " ORDER BY id"):
            evidence = ((_json(row["data"]) or {}).get("decision") or {}).get("evidence") or []
            runs.append({"id": row["id"], "evidence_ids": sorted({ref[key] for ref in evidence if isinstance(ref, dict)
                                                                   for key in ("source_id", "record_id")
                                                                   if isinstance(ref.get(key), str)})})
    rows = conn.execute("SELECT id,json_extract(result,'$.event_id') AS event FROM commands"
                        " WHERE id LIKE 'mind:%' AND CASE WHEN json_valid(result) THEN"
                        " json_type(result,'$.event_id')='text' AND json_type(result,'$.rests_on') IS NULL END"
                        " ORDER BY id").fetchall()
    rested = _history(conn, [row["event"] for row in rows], cache)
    receipts = [{"id": row["id"], "rests_on": sorted(set(rested.get(row["event"]) or ()))} for row in rows]
    return {"sources": sources, "runs": runs, "receipts": receipts, "totals": totals}


def plan_lineage(conn, found=None):
    found = found or _lineage(conn)
    sources = [s for s in found["sources"] if not s.get("without_basis")]
    return {"reflections": sum(1 for s in sources if s["kind"] == "reflections"),
            "targets": sum(1 for s in sources if s.get("targets")),
            "targets_unfound": sum(1 for s in sources if s.get("targets_unfound")),
            "reports": sum(1 for s in sources if s["kind"] == "reports"),
            "creations": sum(1 for s in sources if s["kind"] == "creations"),
            "results": sum(1 for s in sources if s["kind"] == "results"),
            "dependencies": sum(len(s["records"]) for s in sources),
            "inputs_deleted": sum(1 for s in sources if s["deleted"]),
            "inputs_unresolved": sum(s["unresolved"] for s in sources),
            "without_inputs": sum(1 for s in sources if not s["marked"] and not s["refs"]),
            "runs": len(found["runs"]), "receipts": len(found["receipts"]),
            # Described, never registered: results the host observed, with nothing stored they
            # were written from (CL6-MM-06).
            "results_without_basis": sum(1 for s in found["sources"] if s.get("without_basis"))}


def apply_lineage(engine, cache=None):
    """In one write: every derived source given the lineage it lacks -- all of it, or what an
    earlier run could not name -- its dependencies, and the runs and receipts not yet naming what
    they rest on. A rerun finds nothing left."""
    from .db import dumps

    written = {"sources": 0, "dependencies": 0, "runs": 0, "receipts": 0}
    with engine.db.connect(write=True) as conn:
        found = _lineage(conn, cache)
        for entry in found["sources"]:
            if entry.get("without_basis"):
                continue
            row = conn.execute("SELECT data FROM sources WHERE id=?", (entry["source_id"],)).fetchone()
            data = _json(row["data"]) if row else None
            if data is None:
                continue
            if "derived_from" not in data or entry["refs"]:
                data["derived_from"] = [*(data.get("derived_from") or []), *entry["refs"]]
                conn.execute("UPDATE sources SET data=? WHERE id=?", (dumps(data), entry["source_id"]))
                written["sources"] += 1
            for rid in entry["records"]:
                written["dependencies"] += conn.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?)",
                                                        (entry["root"], rid)).rowcount
        for entry in found["runs"]:
            data = _json(conn.execute("SELECT data FROM mind_plan_runs WHERE id=?", (entry["id"],)).fetchone()[0])
            data["result"]["evidence_ids"] = entry["evidence_ids"]
            conn.execute("UPDATE mind_plan_runs SET data=? WHERE id=?", (dumps(data), entry["id"]))
            written["runs"] += 1
        for entry in found["receipts"]:
            result = _json(conn.execute("SELECT result FROM commands WHERE id=?", (entry["id"],)).fetchone()[0])
            result["rests_on"] = entry["rests_on"]
            conn.execute("UPDATE commands SET result=? WHERE id=?", (dumps(result), entry["id"]))
            written["receipts"] += 1
    return written


# --- reerase ---------------------------------------------------------------------------------

def _split(ids):
    return frozenset(i for i in ids if i.startswith("mem_")), frozenset(i for i in ids if i.startswith("src_"))


# What a delete removes besides the records, sources and the mind's layers: stored receipts, session
# sets and metrics that name what went (Engine.delete).
RECEIPT_TABLES = (("commands", "result"), ("sessions", "data"), ("metrics", "data"))


def _resting_on_deletions(conn, lineage=None, cache=None):
    """Derived sources that rest on a deletion fact, by the lineage they carry or by the one
    `lineage` gives them (`lineage`: that step's own finding, when the caller has it; `cache`, what
    was read of the history already). Only a tombstone counts: an archived, corrected, replaced or
    superseded input leaves them as they are (CR5-MM-02)."""
    found = set()
    for row in conn.execute("SELECT id,data FROM sources WHERE json_extract(data,'$.derived_from') IS NOT NULL"):
        keys = []
        for ref in (_json(row["data"]) or {}).get("derived_from") or ():
            if not isinstance(ref, dict):
                continue
            if isinstance(ref.get("record_id"), str):
                keys.append(ref["record_id"])
            if isinstance(ref.get("source_id"), str):
                keys.append(ref["source_id"])
                if not isinstance(ref.get("record_id"), str):
                    keys.append(_root(ref["source_id"]))
        if keys and _tombstoned(conn, keys):
            found.add(row["id"])
    entries = lineage["sources"] if lineage else _lineage_sources(conn, {} if cache is None else cache)
    found.update(entry["source_id"] for entry in entries if entry.get("deleted"))
    return sorted(found)


def _pending(lineage):
    """What `lineage` is about to register, as `erase_set` reads it before it is registered: the
    root records that will depend on a record, and the root records whose sources become derived."""
    dependents, roots = {}, {}
    for entry in lineage["sources"]:
        if entry.get("root"):
            roots[entry["root"]] = entry["source_id"]
            for rid in entry["records"]:
                dependents.setdefault(rid, set()).add(entry["root"])
    return {"dependents": dependents, "roots": roots}


def _present(conn, table, ids):
    ids = sorted(ids)
    return {row[0] for start in range(0, len(ids), 500) for row in conn.execute(
        f"SELECT id FROM {table} WHERE id IN ({','.join('?' for _ in ids[start:start + 500])})", ids[start:start + 500])}


def _closure(conn, doomed, pending=None):
    """What deleting every one of `doomed` takes, as it is there now: the records, the sources, and
    the derived sources among them other than `doomed` -- the scale of the only step here that
    deletes anything, for the gate to hold (CL6-MM-02)."""
    from .maintenance import erase_set

    records, sources = set(), set()
    for sid in doomed:
        taken, gone = erase_set(conn, sid, pending)
        records |= taken
        sources |= gone
    records, sources = _present(conn, "records", records), _present(conn, "sources", sources)
    roots = set((pending or {}).get("roots", {}).values())
    others = sorted(sources - set(doomed))
    derived = {sid for sid in others if sid in roots} | {row[0] for start in range(0, len(others), 500) for row in conn.execute(
        f"SELECT id FROM sources WHERE json_extract(data,'$.derived_from') IS NOT NULL AND id IN"
        f" ({','.join('?' for _ in others[start:start + 500])})", others[start:start + 500])}
    return records, sources, derived


def _counts(conn):
    return conn.execute("SELECT (SELECT COUNT(*) FROM records),(SELECT COUNT(*) FROM sources)").fetchone()


# The derived sources a reerase takes, by the writer of each, as the release's operator reads them
# (CL6D-MM-02): only these four write a lineage, so `other` is always nothing.
KINDS = ("reflections", "reports", "creations", "results", "other")


def _kinds(conn, sids):
    found = dict.fromkeys(KINDS, 0)
    ids = sorted(sids)
    for start in range(0, len(ids), 500):
        page = ids[start:start + 500]
        for namespace, event in conn.execute(
                "SELECT namespace,CASE WHEN json_valid(data) THEN json_extract(data,'$.metadata.host_event') END"
                f" FROM sources WHERE id IN ({','.join('?' for _ in page)})", page):
            found["reflections" if namespace == "kin-reflection" else "reports" if namespace == "kin-exploration"
                  else "creations" if namespace == "kin-runtime" and event == "artifact-created"
                  else "results" if namespace == "kin-runtime" and event == "task-result" else "other"] += 1
    return found


def _by_target(conn, doomed):
    """Of the reflections a deletion takes, those whose understanding's own evidence all stands:
    taken for what their appraisal was about (CL6-MM-06) -- a batch that held a message deleted
    since. They are right to go, and many of them is no cascade (CL6D-MM-02)."""
    count = 0
    for sid in doomed:
        row = conn.execute("SELECT data FROM sources WHERE id=? AND namespace='kin-reflection'", (sid,)).fetchone()
        if row is None:
            continue
        cited = _named(((_json(row["data"]) or {}).get("metadata") or {}).get("evidence_ids"))
        if not _tombstoned(conn, [key for i in cited for key in (i, *([_root(i)] if i.startswith("src_") else []))]):
            count += 1
    return count


def _record_kinds(conn, records, sources):
    """The records a reerase takes: its sources' own -- their root records, and any part of one --
    and the rest, the notes and summaries that cite what goes or were built on it (CL6D-MM-02)."""
    roots = {_root(sid) for sid in sources}
    own = records & roots
    rest = sorted(records - own)
    for start in range(0, len(rest), 500):
        page = rest[start:start + 500]
        own |= {rid for rid, parent in conn.execute(
            f"SELECT id,parent_id FROM records WHERE id IN ({','.join('?' for _ in page)})", page) if parent in roots}
    return {"own": len(own), "notes": len(records) - len(own)}


class Exceeded(RuntimeError):
    """reerase would take, or took, more than the dry run of the same run said: it stops, before
    deleting anything when it can tell in time (CL6D-MM-02)."""


def _within(plan, found, what):
    over = [f"{name} {value} > {plan.get(name, 0)}" for name, value in found.items() if value > plan.get(name, 0)]
    if over:
        raise Exceeded(f"reerase {what} more than its plan said: " + ", ".join(over))


def _receipts(conn, ids, *, write=False):
    """Stored receipts, session sets and metrics that name any of `ids`. One pass over each table
    whatever the number of tombstones: the identifiers a row names, against the set."""
    count = 0
    for table, column in RECEIPT_TABLES:
        if not ids or not _table(conn, table):
            continue
        doomed = [key for key, text in conn.execute(f"SELECT rowid,{column} FROM {table}")
                  if isinstance(text, str) and not ids.isdisjoint(named_in_json(text))]
        count += len(doomed)
        if write:
            for start in range(0, len(doomed), 500):
                page = doomed[start:start + 500]
                conn.execute(f"DELETE FROM {table} WHERE rowid IN ({','.join('?' for _ in page)})", page)
    return count


def plan_reerase(conn, lineage=None, *, after_lineage=False):
    """What a run would change, not what merely names a tombstone: an earlier run leaves
    tombstone references behind on purpose, and a second run finds nothing left (CR-MEM-06).
    The derived sources it would delete count with what their deletion takes: `records` and
    `sources`, every one of them, and `derived_in_closure`, the derived sources taken beside the
    ones that rest on a deletion (CL6-MM-02), each by kind (CL6D-MM-02). With `after_lineage` --
    `lineage` runs first in the same run -- that is counted as it will be once `lineage` has
    registered what it found."""
    from kin_mind.erasure import erase, erased_ids, history_covered, history_owed, mentions

    from .models import now

    lineage = lineage or _lineage(conn)
    doomed = _resting_on_deletions(conn, lineage)
    records, sources, derived = _closure(conn, doomed, _pending(lineage) if after_lineage else None)
    ids = frozenset(set(erased_ids(conn)) | records | sources)
    # By kind as well, so the operator can tell a deletion that took what it should from one that
    # spread: the reflections taken for what their appraisal was about, the sources' own records
    # beside the notes that cite them (CL6D-MM-02).
    base = {"tombstones": len(erased_ids(conn)), "derived_sources": len(doomed), "derived_by_kind": _kinds(conn, doomed),
            "reflections_by_target": _by_target(conn, doomed), "records": len(records),
            "records_by_kind": _record_kinds(conn, records, sources), "sources": len(sources),
            "derived_in_closure": len(derived), "closure_by_kind": _kinds(conn, derived),
            # Every derived source the store holds, by the same kinds, before anything is deleted: what
            # share of each the deletes take (the CL6F review, 4.1).
            "store_by_kind": {**dict.fromkeys(KINDS, 0), **(lineage.get("totals") or {})}, "receipts": _receipts(conn, ids)}
    if not ids or not _table(conn, "mind_state"):
        return {**base, "layers": {}, "derived_rows": 0, "history_ids": 0, "history_rows": 0, "history_passes_owed": 0}
    layers = erase(conn, *_split(ids), now(), write=False, stopped=True)
    waiting = ids - history_covered(conn)
    rows = len(mentions(conn, "mind_events", waiting, "rowid AS key")) if waiting and _table(conn, "mind_events") else 0
    return {**base, "layers": layers, "derived_rows": sum(layers.values()),
            "history_ids": len(waiting), "history_rows": rows, "history_passes_owed": history_owed(conn)}


def apply_reerase(engine, cache=None, plan=None):
    """First every derived source that rests on a deletion fact goes by the delete's own rule --
    its closure, the mind's layers, its history -- as a delete of that input would have taken it;
    the records and sources those deletes took are counted. Held to `plan`, what the dry run of the
    same run said: deletes that would take more are not made, and deletes that took more stop the
    run (`Exceeded`, CL6D-MM-02).
    Then the same erase for every tombstone, receipts that name one removed as a delete removes
    them, and the history rewrite for what it has not finished and does not owe: a rerun after a
    success writes nothing, and one after a failure takes up only what is left, reusing the
    passes already under way (CR-MEM-06)."""
    from kin_mind.erasure import erase, erased_ids, history_covered, queue_history

    from .models import now

    with engine.db.connect() as conn:
        doomed = _resting_on_deletions(conn, cache=cache)
        if plan is not None:
            # What these deletes would take now that `lineage` has registered what it found, against
            # what the dry run said they would: more, and nothing is deleted (CL6D-MM-02).
            records, sources, _ = _closure(conn, doomed)
            _within(plan, {"derived_sources": len(doomed), "records": len(records), "sources": len(sources)}, "would take")
        before = _counts(conn)
    deleted = 0
    for sid in doomed:
        with engine.db.connect() as conn:
            present = conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()
        if present:  # an earlier one's closure may have taken it already
            engine.delete(sid)
            deleted += 1
    with engine.db.connect() as conn:
        # What those deletes took, counted: nothing else in the stopped window writes (CL6-MM-02).
        taken = [was - left for was, left in zip(before, _counts(conn))]
    if plan is not None:
        _within(plan, {"records": taken[0], "sources": taken[1]}, "took")
    with engine.db.connect(write=True) as conn:
        ids = erased_ids(conn)
        counts = erase(conn, *_split(ids), now(), again=True, stopped=True)
        receipts = _receipts(conn, ids, write=True)
        if counts:
            # As after a delete: an FTS5 delete leaves the words in the index's segments until
            # they are merged, and the node indexes were just rewritten.
            from .jobs import purge_text_indexes

            purge_text_indexes(conn)
        waiting = ids - history_covered(conn)
        job = queue_history(engine, conn, waiting, reuse=True) if waiting else None
    return {"derived_sources": deleted, "records": taken[0], "sources": taken[1], "receipts": receipts, "layers": counts,
            "history_ids": len(waiting), "history_job": job}


# --- evidence --------------------------------------------------------------------------------

def _evidence_scopes(conn):
    from kin_mind import evidence_keys
    from kin_mind.autonomy_schema import optimized

    if not _table(conn, "mind_memory_config") or not evidence_keys.installed(conn):
        return []
    return [scope for (scope,) in conn.execute("SELECT scope FROM mind_memory_config ORDER BY scope")
            if optimized(conn, scope, evidence_keys.INDEX_FLAG) and optimized(conn, scope, evidence_keys.LEGACY_FLAG)]


def plan_evidence(engine):
    from kin_mind import evidence_keys

    from .models import now

    with engine.db.connect() as conn:
        scopes = _evidence_scopes(conn)
    found = []
    for scope in scopes:
        # What the two reads use of a mind, and not a `Mind`, whose constructor runs its DDL.
        mind = SimpleNamespace(engine=engine, scope=Scope.model_validate(json.loads(scope)), clock=now)
        filling = evidence_keys.backfill(mind, apply=False)
        checked = evidence_keys.verify(mind)
        found.append({"scope": scope, "would_insert": filling["would_insert"], "state": checked["state"],
                      "missing": checked["missing_count"], "extra": checked["extra_count"],
                      "mismatched": checked["mismatched_count"]})
    return found


def apply_evidence(engine, plan):
    from kin_mind import evidence_keys
    from kin_mind.state import Mind

    switched = []
    for entry in plan:
        mind = Mind(engine, Scope.model_validate(json.loads(entry["scope"])))
        done = evidence_keys.backfill(mind, apply=True)
        if done["state"] == "complete" and done["verification"]["caught_up"]:
            with engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_memory_config SET data=json_set(data,'$." + evidence_keys.LEGACY_FLAG
                             + "',json('false')) WHERE scope=?", (entry["scope"],))
            switched.append(entry["scope"])
    return {"legacy_scan_off": len(switched), "left_on": len(plan) - len(switched)}


# --- quarantine ------------------------------------------------------------------------------

def _gone(conn, identifier):
    if identifier.startswith("src_"):
        return not conn.execute("SELECT 1 FROM sources WHERE id=? AND deleted=0", (identifier,)).fetchone()
    if identifier.startswith("mem_"):
        return not conn.execute("SELECT 1 FROM records WHERE id=? AND deleted=0", (identifier,)).fetchone()
    return False


def plan_quarantine(conn):
    from kin_mind.model_lanes import label

    if not _table(conn, "mind_appraisals"):
        return {"quarantined": 0, "by_reason": {}, "evidence_gone": []}
    reasons, gone, total = {}, [], 0
    for row in conn.execute("SELECT id,scope,data FROM mind_appraisals WHERE state='needs-repair' ORDER BY id"):
        total += 1
        data = json.loads(row["data"])
        reason = label(data.get("repair_reason") or data.get("error") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        ids = [i for i in data.get("evidence_ids") or [] if isinstance(i, str)]
        if ids and any(_gone(conn, i) for i in ids):
            gone.append({"id": row["id"], "scope": row["scope"]})
    return {"quarantined": total, "by_reason": dict(sorted(reasons.items())), "evidence_gone": gone}


def apply_quarantine(engine, plan):
    from kin_mind.recovery import recover_quarantined
    from kin_mind.state import Mind

    retired = 0
    scopes = {}
    for row in plan["evidence_gone"]:
        scopes.setdefault(row["scope"], []).append(row["id"])
    for scope, ids in scopes.items():
        mind = Mind(engine, Scope.model_validate(json.loads(scope)))
        for start in range(0, len(ids), 50):
            page = ids[start:start + 50]
            done = recover_quarantined(mind, job_ids=page, command_id=f"{COMMAND}:retire:{digest(page)[:16]}",
                                       source=f"{COMMAND}: evidence gone", retire=True)
            retired += len(done.get("retired", []))
    return {"retired": retired}


# --- the command -----------------------------------------------------------------------------

def run(root, *, apply=False, steps=DEFAULT_STEPS, still=False, wait=SNAPSHOT_WAIT):
    """The repair's report. `still`: a dry run of a clone of the store (`snapshot`) rather than of
    the store, which is then never opened: nothing is written beside it, not even SQLite's side
    files, and the clone goes when the run is done."""
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        raise Refused(f"Unknown steps: {', '.join(unknown)}")
    store = Path(root).expanduser() / "memory.sqlite3"
    if not store.is_file():
        # A mistyped root would otherwise become a new, empty store, and the report would say
        # there was nothing to repair.
        raise Refused(f"No memory store at {store}")
    if not still:
        return _run(root, store, apply=apply, steps=steps)
    if apply:
        raise Refused("A snapshot is for a dry run: an apply repairs the store itself")
    into = Path(tempfile.mkdtemp(prefix="kin-repair-snapshot-")).resolve()
    try:
        if into.is_relative_to(store.parent.resolve()):
            raise Refused(f"The temporary directory {into} is inside the root")
        taken = snapshot(store, into, wait=wait)
        report = _run(root, into / store.name, steps=steps, beside=store.parent)
        return {**report, "snapshot": taken}
    finally:
        shutil.rmtree(into, ignore_errors=True)


def _run(root, store, *, apply=False, steps=DEFAULT_STEPS, beside=None):
    from . import Engine

    # Only an apply builds the engine, and with it this release's structure; the dry run reads
    # the store exactly as it is -- or a clone of it, with the rest of the root where it lies.
    engine = Engine(store.parent) if apply else SimpleNamespace(db=ReadOnlyStore(store, beside))
    report = {"command": COMMAND, "root": str(Path(root)), "applied": apply, "steps": {}}
    with engine.db.connect() as conn:
        origins = plan_origins(conn) if "origins" in steps else []
        # Found once for both steps that read it; what is read of the history is kept for the
        # applies (CL6-MM-09).
        cache = {}
        lineage = _lineage(conn, cache) if {"lineage", "reerase"} & set(steps) else None
        plans = {
            "origins": {"sources": len(origins),
                        "by_namespace": _count(origins, "namespace"), "by_origin": _count(origins, "origin")}
            if "origins" in steps else None,
            "maintenance": plan_maintenance(conn, origins) if "maintenance" in steps else None,
            "versions": plan_versions(conn) if "versions" in steps else None,
            "queues": plan_queues(conn) if "queues" in steps else None,
            "indexes": plan_indexes(conn) if "indexes" in steps else None,
            "lineage": plan_lineage(conn, lineage) if "lineage" in steps else None,
            "reerase": plan_reerase(conn, lineage, after_lineage="lineage" in steps) if "reerase" in steps else None,
            "quarantine": plan_quarantine(conn) if "quarantine" in steps else None,
        }
    if "spool" in steps:
        plans["spool"] = plan_spool(engine)
    if "evidence" in steps:
        plans["evidence"] = plan_evidence(engine)
    for step in STEPS:
        if step not in steps:
            continue
        plan = plans[step]
        entry = {"plan": _summary(step, plan)}
        if apply:
            if step == "origins":
                entry["done"] = {"rows_written": apply_origins(engine, origins)}
            elif step == "maintenance":
                entry["done"] = apply_maintenance(engine, plan)
            elif step == "versions":
                entry["done"] = {"revisions_written": apply_versions(engine, plan)}
            elif step == "queues":
                entry["done"] = apply_queues(engine, plan)
            elif step == "indexes":
                entry["done"] = apply_indexes(engine, plan)
            elif step == "spool":
                entry["done"] = apply_spool(engine)
            elif step == "evidence":
                entry["done"] = apply_evidence(engine, plan)
            elif step == "lineage":
                entry["done"] = apply_lineage(engine, cache)
            elif step == "reerase":
                entry["done"] = apply_reerase(engine, cache, plan)
            elif step == "quarantine":
                entry["done"] = apply_quarantine(engine, plan)
        report["steps"][step] = entry
    return report


def _count(rows, key):
    found = {}
    for row in rows:
        found[row[key]] = found.get(row[key], 0) + 1
    return dict(sorted(found.items()))


def _summary(step, plan):
    """What the report shows of a plan: counts, and identifiers where there are few."""
    if step == "maintenance":
        return {"unindex": len(plan["unindex"]), "unqueue": len(plan["unqueue"]), "archive": len(plan["archive"]),
                "kept_because_cited": plan["kept_because_cited"]}
    if step == "versions":
        return {"groups": len(plan), "namespaces": _count(plan, "namespace"),
                "records_to_supersede": sum(len(group["source_ids"]) - 1 for group in plan)}
    if step == "queues":
        return {key: len(value) for key, value in plan.items()}
    if step == "evidence":
        return {"scopes": len(plan), "verified": sum(1 for e in plan if e["state"] == "verified"),
                "would_insert": sum(e["would_insert"] for e in plan),
                "disagreements": sum(e["missing"] + e["extra"] + e["mismatched"] for e in plan)}
    if step == "quarantine":
        return {"quarantined": plan["quarantined"], "by_reason": plan["by_reason"],
                "evidence_gone": len(plan["evidence_gone"])}
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m eventmem.core.repair", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, help="the memory root (the directory holding memory.sqlite3)")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is written")
    parser.add_argument("--steps", default=",".join(DEFAULT_STEPS),
                        help=f"comma-separated, from {', '.join(STEPS)}; default {','.join(DEFAULT_STEPS)}")
    parser.add_argument("--output", help="also write the report here (0600)")
    parser.add_argument("--snapshot", action="store_true",
                        help="dry run a clone of the store taken while it is still, for a store in use: nothing is written beside it")
    parser.add_argument("--wait", type=float, default=SNAPSHOT_WAIT,
                        help=f"how long --snapshot waits for the store to be still, in seconds (default {SNAPSHOT_WAIT:g})")
    args = parser.parse_args(argv)
    try:
        report = run(args.root, apply=args.apply, steps=tuple(s for s in args.steps.split(",") if s), still=args.snapshot, wait=args.wait)
    except Refused as refusal:
        print(f"{COMMAND}: refused: {refusal}", file=sys.stderr)
        return 2
    except Exceeded as stopped:
        print(f"{COMMAND}: stopped: {stopped}", file=sys.stderr)
        return 1
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.write_text(text)
        path.chmod(0o600)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
