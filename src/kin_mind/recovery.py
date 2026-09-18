"""Idempotent operational-lane migration, after the owning workers stop.

"After they stop" used to mean whatever the caller asserted in `workers_stopped`.
The parameter is still accepted, so every existing caller keeps working, but with
`liveness_checks` on it decides nothing: the lease ledger is asked instead. The two
commands that release every `running` row refuse while one of those rows is still
leased, and the historical resume, which only ever touched quarantined rows that no
worker can hold, asks for nothing at all.
"""
import json
import time

from eventmem.core.db import Conflict, Missing, digest, dumps

from . import liveness
from .memory import MemoryContinuity

# Retry bookkeeping an approved resume gives back, preserved in recovery_history.
RETRY_COUNTERS = ("error_signature", "error_repeats", "compression_waits", "compression_stalls",
                  "compression_parts", "transient_failures", "admission_waits", "light_attempts")
# What a conflict left for the next attempt to reuse or revalidate. A resumed row is judged afresh.
REUSE_FIELDS = ("reuse", "tier", "revalidation")


def migrate_operational(mind, *, workers_stopped):
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    from .appraisal import Appraisals
    jobs = Appraisals(mind)
    memory = MemoryContinuity(mind)
    name = "operational-lanes-v1"
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            return json.loads(previous[0])
        if evidence:
            # This releases every `running` row below, so one still under lease is
            # an evaluation this command would interrupt. A replay is already past
            # this point: it returned the recorded receipt and writes nothing.
            liveness.refuse_while_appraisal_leased(conn, mind.scope.key())
        config = memory.settings(conn) | {"operational_lanes": True}
        conn.execute("INSERT OR REPLACE INTO mind_memory_config VALUES(?,?)", (mind.scope.key(), dumps(config)))
        # Collapse overdue clock wakeups into one current judgment. Their old
        # evidence, errors and results remain readable, never replayed as chat.
        old = conn.execute("SELECT id,data FROM mind_action_events WHERE scope=? AND kind='idle-review' AND state IN ('pending','queued')", (mind.scope.key(),)).fetchall()
        superseded = []
        for row in old:
            item = json.loads(row["data"])
            conn.execute("UPDATE mind_action_events SET state='superseded' WHERE id=?", (row["id"],))
            if item.get("job_id"):
                job = conn.execute("SELECT data FROM mind_appraisals WHERE id=? AND state IN ('pending','running')", (item["job_id"],)).fetchone()
                conn.execute("UPDATE mind_appraisals SET state='superseded',lease=0 WHERE id=? AND state IN ('pending','running')", (item["job_id"],))
                if job:
                    # A superseded parent must release what it had absorbed.
                    jobs._settle_children(conn, item["job_id"], json.loads(job[0]), "superseded")
            superseded.append(row["id"])
        released = conn.execute("UPDATE mind_appraisals SET state='pending',lease=0,available=? WHERE scope=? AND state='running'", (time.time(), mind.scope.key())).rowcount
        conn.execute("INSERT INTO mind_action_schedule VALUES(?,?,0,?) ON CONFLICT(scope) DO UPDATE SET next_review=excluded.next_review,data=excluded.data",
                     (mind.scope.key(), mind.clock(), dumps({"reason": "recovery-current-state-review", "migration": name})))
        result = {"state": "migrated", "at": mind.clock(), "superseded_idle_events": superseded, "released_terminated_leases": released}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def recover_history(mind, *, job_ids, command_id, source, workers_stopped, replacements=None, admission_only=False):
    """Resume approved historical jobs, preserving failures and original IDs.

    Reused structured results still pass the normal transactional validators.
    This operation neither writes emotions nor submits a chat/send operation.

    It touches `needs-repair` rows only. A quarantined row is held by no worker and
    carries no lease, so there is nothing here for a shutdown to protect: with
    `liveness_checks` on, `workers_stopped` is accepted and ignored, exactly as
    `recover_quarantined` has always worked.
    """
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    if not command_id or not source or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
        raise ValueError("Recovery requires a sourced command and unique bounded jobs")
    from .appraisal import Appraisal, Appraisals
    Appraisals(mind)
    replacements = replacements or {}
    if set(replacements) - set(job_ids):
        raise ValueError("Replacement outside the approved batch")
    name = "history-recovery:" + command_id
    fingerprint = digest([job_ids, source, replacements, *([True] if admission_only else [])])
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            result = json.loads(previous[0])
            if result["fingerprint"] != fingerprint:
                raise Conflict("Recovery command belongs to another batch")
            return result
        resumed, completed = [], []
        for identifier in job_ids:
            row = conn.execute("SELECT * FROM mind_appraisals WHERE id=? AND scope=?", (identifier, mind.scope.key())).fetchone()
            if not row:
                raise Missing(identifier)
            data = json.loads(row["data"])
            if data.get("stimulus") not in {"memory-backfill", "memory-enrichment"}:
                raise Conflict("Recovery is limited to historical enrichment")
            if row["state"] == "complete":
                completed.append(identifier)
                continue
            if row["state"] != "needs-repair":
                raise Conflict("Only quarantined historical jobs can be resumed")
            if admission_only and data.get("error") not in {"deepseek-background-capacity", "deepseek-foreground-priority"}:
                raise Conflict("Recovery is limited to the approved admission waits")
            chosen = replacements.get(identifier) or {"proposal": data.get("proposed_result"), "receipt": data.get("receipt"), "sources": data.get("evaluated_sources", [])}
            proposal = Appraisal.model_validate(chosen["proposal"]) if chosen.get("proposal") and not admission_only else None
            data.setdefault("recovery_history", []).append({"command_id": command_id, "source": source, "at": mind.clock(),
                "attempts": row["attempts"], "error": data.get("error"), "proposed_result": data.get("proposed_result"), "receipt": data.get("receipt")})
            data.pop("seed_manifest", None)
            if proposal and chosen.get("receipt", {}).get("model") == "deepseek-flash":
                refs = chosen.get("sources", [])
                if refs and not mind._fresh(conn, refs):
                    raise Conflict("Recovery proposal sources need review")
                data.update(seed_memory=proposal.memory.model_dump(), seed_receipt={**chosen["receipt"], "recovery_command": command_id},
                            seed_sources=refs, seed_rejected=False)
                if identifier not in replacements and data.get("proposal_manifest"):
                    # The row's own stored judgment: the manifest it rests on says what it was shown,
                    # so the resumed attempt compares its read set like any other reuse.
                    data["seed_manifest"] = data["proposal_manifest"]
            else:
                data["seed_rejected"] = True
            if admission_only:
                # Previous usage and proposals stay in recovery_history. Refresh
                # source/configuration context; never replay an unrelated proposal.
                for field in ("error", "error_detail", "repair_reason", "receipt", "proposed_result",
                              "seed_memory", "seed_receipt", "seed_sources", "seed_manifest"):
                    data.pop(field, None)
                data["waiting_reason"] = "admission-recovered-current-review"
            data.pop("frozen_memory_context", None)
            # An approved resume restores the whole retry budget, like attempts=0.
            for field in (*RETRY_COUNTERS, *REUSE_FIELDS):
                data.pop(field, None)
            conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                         (time.time(), dumps(data), identifier))
            resumed.append(identifier)
        result = {"state": "resumed", "resumed": resumed, "already_complete": completed, "at": mind.clock(), "fingerprint": fingerprint}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def recover_quarantined(mind, *, job_ids, command_id, source):
    """Resume quarantined appraisals of any lane. Each one is judged afresh.

    A quarantined row is held by no worker (state `needs-repair`, no lease), so
    this needs no worker shutdown: one write transaction returns the batch to the
    queue and the claim query takes it from there atomically. The failure stays
    readable in `recovery_history`; no stored proposal is replayed as a seed and
    no model is called here.
    """
    if not command_id or not source or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
        raise ValueError("Recovery requires a sourced command and unique bounded jobs")
    from .appraisal import Appraisals
    Appraisals(mind)
    name = "quarantine-recovery:" + command_id
    fingerprint = digest([job_ids, source])
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            result = json.loads(previous[0])
            if result["fingerprint"] != fingerprint:
                raise Conflict("Recovery command belongs to another batch")
            return result
        resumed, completed = [], []
        for identifier in job_ids:
            row = conn.execute("SELECT * FROM mind_appraisals WHERE id=? AND scope=?", (identifier, mind.scope.key())).fetchone()
            if not row:
                raise Missing(identifier)
            data = json.loads(row["data"])
            if row["state"] == "complete":
                completed.append(identifier)
                continue
            if row["state"] != "needs-repair":
                raise Conflict("Only a quarantined appraisal can be resumed")
            data.setdefault("recovery_history", []).append({
                "command_id": command_id, "source": source, "at": mind.clock(), "attempts": row["attempts"],
                **{field: data.get(field) for field in ("error", "error_detail", "repair_reason",
                                                        "proposed_result", "receipt", "failed_call_receipt")},
                **{field: data[field] for field in RETRY_COUNTERS if field in data}})
            if data.get("seed_memory"):
                # The stored proposal stays audit data; this attempt judges again.
                data["seed_rejected"] = True
            for field in ("error", "error_detail", "repair_reason", "waiting_reason",
                          "frozen_memory_context", *RETRY_COUNTERS, *REUSE_FIELDS):
                data.pop(field, None)
            conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                         (time.time(), dumps(data), identifier))
            resumed.append(identifier)
        result = {"state": "resumed", "resumed": resumed, "already_complete": completed, "at": mind.clock(), "fingerprint": fingerprint}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def _committed_ancestor(conn, mind, child_id, parents):
    """The nearest ancestor of a batched job whose commit receipt really exists."""
    seen, current = {child_id}, parents.get(child_id)
    while current and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT data FROM mind_appraisals WHERE id=? AND scope=?", (current, mind.scope.key())).fetchone()
        receipt = conn.execute("SELECT result FROM commands WHERE id=?", (mind._key(current),)).fetchone()
        event_id = json.loads(receipt[0]).get("event_id") if receipt else None
        if row and event_id and conn.execute("SELECT 1 FROM mind_events WHERE id=? AND scope=?", (event_id, mind.scope.key())).fetchone():
            return current, event_id, json.loads(row[0])
        current = parents.get(current)
    return None, None, {}


def _source_committed(conn, mind, source_id):
    """Presence in the source index is not proof; its event must exist."""
    row = conn.execute("SELECT event_id FROM mind_semantic_sources WHERE scope=? AND source_id=?", (mind.scope.key(), source_id)).fetchone()
    return bool(row and conn.execute("SELECT 1 FROM mind_events WHERE id=? AND scope=?", (row[0], mind.scope.key())).fetchone())


def recover_batched(mind, *, command_id, workers_stopped):
    """Settle the jobs an interrupted parent left batched. One shot, idempotent.

    A child is completed only when a really committed ancestor evaluated its
    exact source version; everything else returns to the queue for a new
    judgment. This operation calls no model and writes no memory of its own.
    """
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    if not command_id:
        raise ValueError("Recovery requires a sourced command")
    from .appraisal import Appraisals
    Appraisals(mind)
    name = "batched-recovery:" + command_id
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            return json.loads(previous[0])
        if evidence:
            # A `batched` child is settled against its parent's committed work. A
            # parent still under lease may yet commit, so its children are not
            # orphans yet and this command would decide their fate too early.
            liveness.refuse_while_appraisal_leased(conn, mind.scope.key())
        parents = {}
        for row in conn.execute("SELECT id,data FROM mind_appraisals WHERE scope=? AND json_extract(data,'$.batch_ids') IS NOT NULL", (mind.scope.key(),)).fetchall():
            for child_id in json.loads(row["data"]).get("batch_ids", []):
                parents.setdefault(child_id, row["id"])
        settled = []
        for row in conn.execute("SELECT id,data FROM mind_appraisals WHERE scope=? AND state='batched' ORDER BY id", (mind.scope.key(),)).fetchall():
            data = json.loads(row["data"])
            ancestor, event_id, ancestor_data = _committed_ancestor(conn, mind, row["id"], parents)
            try:
                refs = mind._evidence(conn, data.get("evidence_ids", [])) if ancestor else []
            except (Conflict, Missing):
                refs = []
            manifest = {(r["source_id"], r["hash"], r["revision"]) for r in ancestor_data.get("evaluated_sources", [])}
            if not ancestor:
                state, reason = "pending", "no-committed-ancestor"
            elif not refs:
                state, reason = "pending", "evidence-unresolved"
            elif not mind._fresh(conn, refs):
                state, reason = "pending", "source-no-longer-current"
            elif any((r["source_id"], r["hash"], r["revision"]) not in manifest for r in refs):
                state, reason = "pending", "outside-ancestor-manifest"
            elif not all(_source_committed(conn, mind, r["source_id"]) for r in refs):
                state, reason = "pending", "commit-receipt-missing"
            else:
                state, reason = "complete", "settled-by-committed-ancestor"
            data["recovery_command"] = command_id
            if state == "complete":
                data["result"] = {"batch_id": ancestor, "event_id": event_id}
                if ancestor_data.get("receipt"):
                    data["receipt"] = ancestor_data["receipt"]
                conn.execute("UPDATE mind_appraisals SET state='complete',lease=0,data=? WHERE id=?", (dumps(data), row["id"]))
            else:
                conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,data=? WHERE id=?",
                             (time.time(), dumps(data), row["id"]))
            settled.append({"id": row["id"], "state": state, "reason": reason, "ancestor": ancestor,
                            "event_id": event_id if state == "complete" else None})
        result = {"state": "recovered", "at": mind.clock(), "rows": settled,
                  "completed": [s["id"] for s in settled if s["state"] == "complete"],
                  "requeued": [s["id"] for s in settled if s["state"] == "pending"]}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result
