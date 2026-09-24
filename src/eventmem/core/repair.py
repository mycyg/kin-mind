"""The one-time data repair that goes with the 2026-09-24 memory-kernel release.

    python -m eventmem.core.repair --root <MemoryPalace>            # dry run: writes nothing
    python -m eventmem.core.repair --root <MemoryPalace> --apply    # the default steps
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps reerase
    python -m eventmem.core.repair --root <MemoryPalace> --apply --steps quarantine

Run it with the memory service stopped, after the backup the deploy takes. Every step is
idempotent: it asks what is already done rather than counting what it did, so a second run
reports nothing left and writes nothing. Nothing here deletes a record, a source, a revision or
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
- `reerase`   Opt-in. Deletes made before this release left their words in the mind's derived
              layers and state history; this runs the same erase for every tombstone and
              queues the history rewrite (K4-01, K4-20, K4-21). It rewrites history rows, so it
              is named explicitly.
- `quarantine` Opt-in. Quarantined appraisals whose evidence is gone are retired (state
              `superseded`, reason kept, no model call); the others are listed by reason for a
              decision, never resumed here, because a resume pays for a model call (K4-06, DB1-05).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

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

STEPS = ("origins", "maintenance", "versions", "queues", "indexes", "spool", "evidence", "reerase", "quarantine")
DEFAULT_STEPS = STEPS[:-2]
COMMAND = "repair-20260924"
# The maintenance notes this release archives: session-maintenance requests. Internal mind
# events stay active — appraisals and actions cite them as their stimulus — and only leave the
# index.
ARCHIVED_NAMESPACES = ("kin-session-maintenance",)
CHUNK = 200
IDENTIFIER = re.compile(r"\b(?:src|mem)_[0-9a-f]{32}\b")


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


# --- reerase ---------------------------------------------------------------------------------

def plan_reerase(conn):
    from kin_mind.erasure import erased_ids, mentions

    ids = erased_ids(conn)
    if not ids or not _table(conn, "mind_state"):
        return {"tombstones": len(ids), "history_rows": 0, "derived_rows": 0}
    history = len(mentions(conn, "mind_events", ids, "rowid AS key")) if _table(conn, "mind_events") else 0
    derived = sum(len(mentions(conn, table, ids, "rowid AS key")) for table in
                  ("mind_state", "mind_graph_nodes", "mind_graph_edges", "mind_context_cache")
                  if _table(conn, table))
    return {"tombstones": len(ids), "history_rows": history, "derived_rows": derived}


def apply_reerase(engine):
    from kin_mind.erasure import erase, erased_ids, queue_history

    from .models import now

    with engine.db.connect(write=True) as conn:
        ids = erased_ids(conn)
        records = frozenset(i for i in ids if i.startswith("mem_"))
        sources = frozenset(i for i in ids if i.startswith("src_"))
        counts = erase(conn, records, sources, now())
        job = queue_history(engine, conn, ids)
    return {"layers": counts, "history_job": job}


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
    from kin_mind.state import Mind

    with engine.db.connect() as conn:
        scopes = _evidence_scopes(conn)
    found = []
    for scope in scopes:
        mind = Mind(engine, Scope.model_validate(json.loads(scope)))
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
        raise ValueError(f"Unknown steps: {', '.join(unknown)}")
    engine = Engine(Path(root))
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
    report = run(args.root, apply=args.apply, steps=tuple(s for s in args.steps.split(",") if s))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.write_text(text)
        path.chmod(0o600)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
