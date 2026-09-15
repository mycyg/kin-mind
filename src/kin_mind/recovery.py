"""Idempotent operational-lane migration, after the owning workers stop."""
import json
import time

from eventmem.core.db import dumps

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
