"""Idempotent operational-lane migration, after the owning workers stop."""
import json
import time

from eventmem.core.db import Conflict, Missing, digest, dumps

from .memory import MemoryContinuity


def migrate_operational(mind, *, workers_stopped):
    if workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    from .appraisal import Appraisals
    Appraisals(mind)
    memory = MemoryContinuity(mind)
    name = "operational-lanes-v1"
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            return json.loads(previous[0])
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
                conn.execute("UPDATE mind_appraisals SET state='superseded',lease=0 WHERE id=? AND state IN ('pending','running')", (item["job_id"],))
            superseded.append(row["id"])
        released = conn.execute("UPDATE mind_appraisals SET state='pending',lease=0,available=? WHERE scope=? AND state='running'", (time.time(), mind.scope.key())).rowcount
        conn.execute("INSERT INTO mind_action_schedule VALUES(?,?,0,?) ON CONFLICT(scope) DO UPDATE SET next_review=excluded.next_review,data=excluded.data",
                     (mind.scope.key(), mind.clock(), dumps({"reason": "recovery-current-state-review", "migration": name})))
        result = {"state": "migrated", "at": mind.clock(), "superseded_idle_events": superseded, "released_terminated_leases": released}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def recover_history(mind, *, job_ids, command_id, source, workers_stopped, replacements=None):
    """Resume approved historical jobs, preserving failures and original IDs.

    Reused structured results still pass the normal transactional validators.
    This operation neither writes emotions nor submits a chat/send operation.
    """
    if workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    if not command_id or not source or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
        raise ValueError("Recovery requires a sourced command and unique bounded jobs")
    from .appraisal import Appraisal, Appraisals
    Appraisals(mind)
    replacements = replacements or {}
    if set(replacements) - set(job_ids):
        raise ValueError("Replacement outside the approved batch")
    name = "history-recovery:" + command_id
    fingerprint = digest([job_ids, source, replacements])
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
            chosen = replacements.get(identifier) or {"proposal": data.get("proposed_result"), "receipt": data.get("receipt"), "sources": data.get("evaluated_sources", [])}
            proposal = Appraisal.model_validate(chosen["proposal"]) if chosen.get("proposal") else None
            data.setdefault("recovery_history", []).append({"command_id": command_id, "source": source, "at": mind.clock(),
                "attempts": row["attempts"], "error": data.get("error"), "proposed_result": data.get("proposed_result"), "receipt": data.get("receipt")})
            if proposal and chosen.get("receipt", {}).get("model") == "deepseek-flash":
                refs = chosen.get("sources", [])
                if refs and not mind._fresh(conn, refs):
                    raise Conflict("Recovery proposal sources need review")
                data.update(seed_memory=proposal.memory.model_dump(), seed_receipt={**chosen["receipt"], "recovery_command": command_id},
                            seed_sources=refs, seed_rejected=False)
            else:
                data["seed_rejected"] = True
            data.pop("frozen_memory_context", None)
            conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                         (time.time(), dumps(data), identifier))
            resumed.append(identifier)
        result = {"state": "resumed", "resumed": resumed, "already_complete": completed, "at": mind.clock(), "fingerprint": fingerprint}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result
