"""Read progress separately from liveness, without loading private messages."""
import json


def operational_status(mind):
    with mind.engine.db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scope = mind.scope.key()
        rows = conn.execute("SELECT state,CASE WHEN json_extract(data,'$.stimulus') IN ('memory-enrichment','memory-backfill') THEN 'enrichment' WHEN json_extract(data,'$.stimulus')='session-maintenance' THEN 'maintenance' ELSE 'action' END lane,COUNT(*) count,MIN(available) oldest_available_unix,MIN(json_extract(data,'$.attempt_started_at')) attempt_started_at,MIN(CASE WHEN state='running' THEN lease END) lease_expires_unix,MAX(attempts) max_attempts FROM mind_appraisals WHERE scope=? GROUP BY state,lane", (scope,)).fetchall()
        last = conn.execute("SELECT id,data FROM mind_appraisals WHERE scope=? AND state='complete' ORDER BY json_extract(data,'$.receipt.verified_at') DESC LIMIT 1", (scope,)).fetchone()
        schedule = conn.execute("SELECT next_review,revision,data FROM mind_action_schedule WHERE scope=?", (scope,)).fetchone()
        cursor = conn.execute("SELECT seq,next_review,revision,data FROM mind_semantic_cursor WHERE scope=?", (scope,)).fetchone()
        enriched = conn.execute("SELECT id,json_extract(data,'$.receipt.verified_at') at FROM mind_appraisals WHERE scope=? AND state='complete' AND json_extract(data,'$.stimulus') IN ('memory-enrichment','memory-backfill') ORDER BY at DESC LIMIT 1", (scope,)).fetchone()
        exploration = conn.execute("SELECT id,state,created_at FROM mind_explorations WHERE scope=? ORDER BY created_at DESC LIMIT 1", (scope,)).fetchone() if "mind_explorations" in tables else None
        contact = conn.execute("SELECT id,state,data FROM mind_contacts WHERE scope=? AND state='accepted' ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()
    action = json.loads(schedule["data"]) if schedule else {}
    latest = json.loads(last["data"]) if last else {}
    return {"checked_at": mind.clock(), "queues": [dict(r) for r in rows],
            "timing_contract": "oldest_available_unix is queue age, not current execution start; a missing attempt_started_at is unknown. Use lease_expires_unix for running-worker deadline.",
            "action": {"last_success": action.get("last_success"), "next_review": schedule["next_review"] if schedule else None,
                       "revision": schedule["revision"] if schedule else None, "model": action.get("receipt", {}).get("model")},
            "enrichment": {"cursor": cursor["seq"] if cursor else None, "revision": cursor["revision"] if cursor else None,
                           "last_success": enriched["at"] if enriched else None, "last_job": enriched["id"] if enriched else None},
            "last_completed_review": {"id": last["id"], "model": latest.get("receipt", {}).get("model")} if last else None,
            "exploration": dict(exploration) if exploration else None,
            "contact": {"id": contact["id"], "state": contact["state"], "at": json.loads(contact["data"]).get("updated_at")} if contact else None}
