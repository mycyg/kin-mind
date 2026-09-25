"""The one-time data repair that goes with the 2026-09-24 memory-kernel release.

    python -m eventmem.core.repair --root <MemoryPalace>            # dry run: writes nothing
    python -m eventmem.core.repair --root <MemoryPalace> --apply    # the default steps
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps reerase
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps quarantine

Run it with the memory service stopped, after the backup the deploy takes. Every step is
idempotent: it asks what is already done rather than counting what it did, so a second run
reports nothing left and writes nothing. The dry run opens the store read-only and builds
nothing: no table, index or metadata of this release, no cache under the root (CR-MEM-05).
A root without a `memory.sqlite3` is refused in both modes rather than becoming a new store.

Exit status: 0 with the JSON report on stdout (also in `--output`, 0600); 2 when refused — an
unknown step or no store at the root — with one line on stderr and nothing on stdout; anything
else (1) is a failure with a traceback, and an `--apply` that failed may have finished the steps
before the failing one, each of which a rerun finds done. Nothing here deletes a record, a source, a revision or
a history row; records leave active use by a new revision (`archive`, `superseded`), queue rows
change state, and derived indexes are rebuilt. The report carries identifiers, namespaces and
counts only — never a line of content.

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
              reflection's `evidence_ids`, an exploration report's run's `evidence_ids` (only
              a report with a result), a creation event's `input_source_ids`. Settled runs and
              the mind's command receipts name what they rest on as well (`result.evidence_ids`,
              `rests_on`), so a delete finds the words they hold. Only a delete follows a
              dependency; an archive, a correction, a replacement or a newer version of an input
              leaves the derived source as it is. An input already deleted is named, and
              `reerase` erases the derived source; an input the store never held, or holds in
              another scope, is left out and counted. Nothing is erased here (CR5-MM-02).
- `reerase`   Opt-in. Deletes made before this release left their words in the mind's derived
              layers and state history; this runs the same erase for every tombstone and
              queues the history rewrite (K4-01, K4-20, K4-21). A derived source whose lineage
              names a deletion fact is deleted as a delete of that input would have taken it,
              and stored receipts, sessions and metrics that name a deletion fact go, as a
              delete removes them (CR5-MM-02). It rewrites history rows, so it is named
              explicitly; run it after `lineage`, in the same run or a later one. What an earlier
              run already erased is left alone, and the history is queued only for identifiers
              it has neither finished nor still owes a pass for, so a second run changes nothing
              (CR-MEM-06).
- `quarantine` Opt-in. Quarantined appraisals whose evidence is gone are retired (state
              `superseded`, reason kept, no model call); the others are listed by reason for a
              decision, never resumed here, because a resume pays for a model call (K4-06, DB1-05).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .db import digest
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
IDENTIFIER = re.compile(r"\b(?:src|mem)_[0-9a-f]{32}\b")


class Refused(ValueError):
    """The command was asked for something it will not do; nothing was read or written."""


class ReadOnlyStore:
    """The store as the dry run sees it (CR-MEM-05): a read-only connection per read, and nothing
    created or initialised on the way in. `Engine()` would run this release's DDL, fill in
    metadata and pin the tokenizer cache under the root before the report said a word. The only
    files that may appear are the `-wal` and `-shm` SQLite coordinates its readers with."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
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
            named.update(IDENTIFIER.findall(text))
    if _table(conn, "mind_appraisals"):
        for (text,) in conn.execute("SELECT data FROM mind_appraisals WHERE state IN ('pending','running','batched')"):
            named.update(IDENTIFIER.findall(text))
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
    return [value for value in values or () if isinstance(value, str) and IDENTIFIER.fullmatch(value)]


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


def _derived_before(conn):
    """Sources written from others before this release -- no `derived_from` -- with what the
    store recorded they were written from."""
    found = []
    for row in conn.execute("SELECT id,scope,data FROM sources WHERE namespace='kin-reflection'"
                            " AND json_extract(data,'$.derived_from') IS NULL ORDER BY id"):
        metadata = (_json(row["data"]) or {}).get("metadata") or {}
        found.append(("reflections", row["id"], row["scope"], _named(metadata.get("evidence_ids"))))
    runs = {}
    if _table(conn, "mind_explorations"):
        for (text,) in conn.execute("SELECT data FROM mind_explorations"):
            data = _json(text) or {}
            if isinstance(data.get("source_id"), str):
                runs[data["source_id"]] = data
    for row in conn.execute("SELECT id,scope FROM sources WHERE namespace='kin-exploration'"
                            " AND json_extract(data,'$.derived_from') IS NULL ORDER BY id"):
        run = runs.get(row["id"])
        written = run if run is not None else _json(_content(conn, _root(row["id"]))) or {}
        if written.get("result") is None:
            continue  # a run that reported nothing wrote no words: no derived source
        found.append(("reports", row["id"], row["scope"], _named((run or {}).get("evidence_ids"))))
    events = {}
    if _table(conn, "mind_runtime_events"):
        for (text,) in conn.execute("SELECT data FROM mind_runtime_events WHERE kind IN (%s)"
                                    % ",".join("?" for _ in CREATION_EVENTS), CREATION_EVENTS):
            data = _json(text) or {}
            if isinstance(data.get("source_id"), str):
                events[data["source_id"]] = data
    for row in conn.execute("SELECT id,scope FROM sources WHERE namespace='kin-runtime'"
                            " AND json_extract(data,'$.metadata.host_event') IN (%s)"
                            " AND json_extract(data,'$.derived_from') IS NULL ORDER BY id"
                            % ",".join("?" for _ in CREATION_EVENTS), CREATION_EVENTS):
        event = events.get(row["id"]) or _json(_content(conn, _root(row["id"]))) or {}
        inputs = _named(event.get("input_source_ids"))
        if inputs:  # an artifact the host only observed was written from nothing stored
            found.append(("creations", row["id"], row["scope"], inputs))
    return found


def _lineage(conn):
    """What `lineage` would write, by what it is: the derived sources with their references and
    dependencies, the runs and the receipts with the identifiers they would name."""
    sources = []
    for kind, sid, scope, inputs in _derived_before(conn):
        refs, records, deleted, unresolved = [], [], [], 0
        for identifier in dict.fromkeys(inputs):
            state, ref, rests = _resolve(conn, identifier, scope)
            if state == "unresolved":
                unresolved += 1
                continue
            refs.append(ref)
            records.extend(rests)
            if state == "deleted":
                deleted.append(identifier)
        root = _root(sid)
        if not conn.execute("SELECT 1 FROM records WHERE id=? AND deleted=0", (root,)).fetchone():
            root, records = None, []
        new = [rid for rid in dict.fromkeys(records) if rid != root and not conn.execute(
            "SELECT 1 FROM dependencies WHERE record_id=? AND evidence_id=?", (root, rid)).fetchone()]
        sources.append({"kind": kind, "source_id": sid, "root": root, "refs": refs, "records": new,
                        "deleted": deleted, "unresolved": unresolved})
    runs = []
    if _table(conn, "mind_plan_runs"):
        for row in conn.execute("SELECT id,data FROM mind_plan_runs WHERE CASE WHEN json_valid(data) THEN"
                                " json_type(data,'$.result')='object' AND json_type(data,'$.result.evidence_ids') IS NULL END"
                                " ORDER BY id"):
            evidence = ((_json(row["data"]) or {}).get("decision") or {}).get("evidence") or []
            runs.append({"id": row["id"], "evidence_ids": sorted({ref[key] for ref in evidence if isinstance(ref, dict)
                                                                   for key in ("source_id", "record_id")
                                                                   if isinstance(ref.get(key), str)})})
    receipts = []
    history = [name for name in ("mind_events", "mind_events_v1") if _table(conn, name)]
    if history:
        for row in conn.execute("SELECT id,json_extract(result,'$.event_id') AS event FROM commands"
                                " WHERE id LIKE 'mind:%' AND CASE WHEN json_valid(result) THEN"
                                " json_type(result,'$.event_id')='text' AND json_type(result,'$.rests_on') IS NULL END"
                                " ORDER BY id"):
            evidence = None
            for name in history:
                found = conn.execute(f"SELECT CASE WHEN json_valid(data) THEN json_extract(data,'$.request.evidence_ids') END"
                                     f" FROM {name} WHERE id=?", (row["event"],)).fetchone()
                if found and found[0]:
                    evidence = _json(found[0])
                    break
            receipts.append({"id": row["id"], "rests_on": sorted(set(_named(evidence)))})
    return {"sources": sources, "runs": runs, "receipts": receipts}


def plan_lineage(conn):
    found = _lineage(conn)
    sources = found["sources"]
    return {"reflections": sum(1 for s in sources if s["kind"] == "reflections"),
            "reports": sum(1 for s in sources if s["kind"] == "reports"),
            "creations": sum(1 for s in sources if s["kind"] == "creations"),
            "dependencies": sum(len(s["records"]) for s in sources),
            "inputs_deleted": sum(1 for s in sources if s["deleted"]),
            "inputs_unresolved": sum(s["unresolved"] for s in sources),
            "without_inputs": sum(1 for s in sources if not s["refs"]),
            "runs": len(found["runs"]), "receipts": len(found["receipts"])}


def apply_lineage(engine):
    """In one write: every derived source not yet given its lineage, its dependencies, and the
    runs and receipts not yet naming what they rest on. A rerun finds nothing left."""
    from .db import dumps

    written = {"sources": 0, "dependencies": 0, "runs": 0, "receipts": 0}
    with engine.db.connect(write=True) as conn:
        found = _lineage(conn)
        for entry in found["sources"]:
            row = conn.execute("SELECT data FROM sources WHERE id=?", (entry["source_id"],)).fetchone()
            data = _json(row["data"]) if row else None
            if data is None or "derived_from" in data:
                continue
            data["derived_from"] = entry["refs"]
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


def _resting_on_deletions(conn):
    """Derived sources that rest on a deletion fact, by the lineage they carry or by the one
    `lineage` gives them. Only a tombstone counts: an archived, corrected, replaced or superseded
    input leaves them as they are (CR5-MM-02)."""
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
    found.update(entry["source_id"] for entry in _lineage(conn)["sources"] if entry["deleted"])
    return sorted(found)


def _receipts(conn, ids, *, write=False):
    """Stored receipts, session sets and metrics that name any of `ids`. One pass over each table
    whatever the number of tombstones: the identifiers a row names, against the set."""
    count = 0
    for table, column in RECEIPT_TABLES:
        if not ids or not _table(conn, table):
            continue
        doomed = [key for key, text in conn.execute(f"SELECT rowid,{column} FROM {table}")
                  if isinstance(text, str) and not ids.isdisjoint(IDENTIFIER.findall(text))]
        count += len(doomed)
        if write:
            for start in range(0, len(doomed), 500):
                page = doomed[start:start + 500]
                conn.execute(f"DELETE FROM {table} WHERE rowid IN ({','.join('?' for _ in page)})", page)
    return count


def plan_reerase(conn):
    """What a run would change, not what merely names a tombstone: an earlier run leaves
    tombstone references behind on purpose, and a second run finds nothing left (CR-MEM-06).
    The derived sources it would delete count with what their deletion takes."""
    from kin_mind.erasure import erase, erased_ids, history_covered, history_owed, mentions

    from .maintenance import erase_set
    from .models import now

    doomed = _resting_on_deletions(conn)
    ids = set(erased_ids(conn))
    for sid in doomed:
        records, sources = erase_set(conn, sid)
        ids.update(records, sources)
    ids = frozenset(ids)
    base = {"tombstones": len(erased_ids(conn)), "derived_sources": len(doomed), "receipts": _receipts(conn, ids)}
    if not ids or not _table(conn, "mind_state"):
        return {**base, "layers": {}, "derived_rows": 0, "history_ids": 0, "history_rows": 0, "history_passes_owed": 0}
    layers = erase(conn, *_split(ids), now(), write=False)
    waiting = ids - history_covered(conn)
    rows = len(mentions(conn, "mind_events", waiting, "rowid AS key")) if waiting and _table(conn, "mind_events") else 0
    return {**base, "layers": layers, "derived_rows": sum(layers.values()),
            "history_ids": len(waiting), "history_rows": rows, "history_passes_owed": history_owed(conn)}


def apply_reerase(engine):
    """First every derived source that rests on a deletion fact goes by the delete's own rule --
    its closure, the mind's layers, its history -- as a delete of that input would have taken it.
    Then the same erase for every tombstone, receipts that name one removed as a delete removes
    them, and the history rewrite for what it has not finished and does not owe: a rerun after a
    success writes nothing, and one after a failure takes up only what is left, reusing the
    passes already under way (CR-MEM-06)."""
    from kin_mind.erasure import erase, erased_ids, history_covered, queue_history

    from .models import now

    with engine.db.connect() as conn:
        doomed = _resting_on_deletions(conn)
    deleted = 0
    for sid in doomed:
        with engine.db.connect() as conn:
            present = conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()
        if present:  # an earlier one's closure may have taken it already
            engine.delete(sid)
            deleted += 1
    with engine.db.connect(write=True) as conn:
        ids = erased_ids(conn)
        counts = erase(conn, *_split(ids), now(), again=True)
        receipts = _receipts(conn, ids, write=True)
        if counts:
            # As after a delete: an FTS5 delete leaves the words in the index's segments until
            # they are merged, and the node indexes were just rewritten.
            from .jobs import purge_text_indexes

            purge_text_indexes(conn)
        waiting = ids - history_covered(conn)
        job = queue_history(engine, conn, waiting, reuse=True) if waiting else None
    return {"derived_sources": deleted, "receipts": receipts, "layers": counts, "history_ids": len(waiting),
            "history_job": job}


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

def run(root, *, apply=False, steps=DEFAULT_STEPS):
    from . import Engine

    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        raise Refused(f"Unknown steps: {', '.join(unknown)}")
    store = Path(root).expanduser() / "memory.sqlite3"
    if not store.is_file():
        # A mistyped root would otherwise become a new, empty store, and the report would say
        # there was nothing to repair.
        raise Refused(f"No memory store at {store}")
    # Only an apply builds the engine, and with it this release's structure; the dry run reads
    # the store exactly as it is.
    engine = Engine(store.parent) if apply else SimpleNamespace(db=ReadOnlyStore(store))
    report = {"command": COMMAND, "root": str(Path(root)), "applied": apply, "steps": {}}
    with engine.db.connect() as conn:
        origins = plan_origins(conn) if "origins" in steps else []
        plans = {
            "origins": {"sources": len(origins),
                        "by_namespace": _count(origins, "namespace"), "by_origin": _count(origins, "origin")}
            if "origins" in steps else None,
            "maintenance": plan_maintenance(conn, origins) if "maintenance" in steps else None,
            "versions": plan_versions(conn) if "versions" in steps else None,
            "queues": plan_queues(conn) if "queues" in steps else None,
            "indexes": plan_indexes(conn) if "indexes" in steps else None,
            "lineage": plan_lineage(conn) if "lineage" in steps else None,
            "reerase": plan_reerase(conn) if "reerase" in steps else None,
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
                entry["done"] = apply_lineage(engine)
            elif step == "reerase":
                entry["done"] = apply_reerase(engine)
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
    args = parser.parse_args(argv)
    try:
        report = run(args.root, apply=args.apply, steps=tuple(s for s in args.steps.split(",") if s))
    except Refused as refusal:
        print(f"{COMMAND}: refused: {refusal}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.write_text(text)
        path.chmod(0o600)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
