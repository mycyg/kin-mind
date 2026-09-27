"""Read progress separately from liveness, without loading private messages."""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from eventmem.core.integrity import verify_interpreter, verify_source_root

# Beside the store, what Kin's read-only memory server (the host's kin_memory_mcp.py) could not work
# out for a fork's memory reads. Each such read cut its fork turn short, so what the turn made was
# neither reused nor cached; failing every time, it switches both off without a word (CL8-MM-07).
FORK_READ_STATUS = "kin-fork-reads.json"


def fork_read_errors(root, now=None):
    """The count the server keeps in FORK_READ_STATUS: in all, over the last 7 days, and the last
    one's kind, place and tool -- labels, never data. None while there has been none."""
    try:
        errors = json.loads((Path(root) / FORK_READ_STATUS).read_text())["rests_on_errors"]
        days = errors.get("days") if isinstance(errors.get("days"), dict) else {}
        last = errors.get("last") if isinstance(errors.get("last"), dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"unreadable": True}
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=6)).date().isoformat()
    return {"total": errors.get("total") if type(errors.get("total")) is int else None,
            "last_7_days": sum(n for day, n in days.items() if isinstance(day, str) and day >= since and type(n) is int),
            "last_at": str(errors.get("last_at"))[:40] if errors.get("last_at") else None,
            "last": {key: last[key][:200] for key in ("error", "where", "raised", "tool") if isinstance(last.get(key), str)}}


DAY = 86400.0
# The states an embed job waits in: queued, backing off, and being worked on.
WAITING = ("pending", "retry", "running")


def _seconds(value):
    """Unix seconds of a stored time -- ISO text with or without a zone (UTC then), with `T` or a
    space, or a number -- or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()


def _hours(then, now):
    return None if then is None else round(max(0.0, now - then) / 3600, 1)


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def liveness(conn, scope, *, now=None):
    """Whether the work is moving, read from the store and nothing else (OPS-02): embeddings --
    what failed for good in the last day, what waits and since when, the last one made -- the
    appraisals in quarantine, and when Kin last formed a wish, explored and reached out.
    Counts, times, states and job errors (sanitized by the queue) only; never a line of content.
    `scope` is the scope's key. Read-only: the host's health reads it through `main` from a
    connection that cannot write."""
    now = time.time() if now is None else now
    facts = {"checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    if _table(conn, "jobs"):
        # A failure on a revision its record has since moved past, or on a record since deleted or
        # retired, needs no vector: that revision is never shown again, and repair's queue plan passes
        # it by the same test. Only what still needs one counts as a failure of the last day; the rest
        # is said apart. A job that names no record it can be checked against counts, as before.
        passed = ("EXISTS(SELECT 1 FROM records r WHERE r.id=json_extract(j.payload,'$.record_id') AND NOT "
                  "(r.revision=json_extract(j.payload,'$.revision') AND r.deleted=0 AND r.status IN ('active','unverified')))"
                  if _table(conn, "records") else "0")
        failed = [(row[0], _seconds(row[1]), bool(row[2])) for row in conn.execute(
            f"SELECT j.error,j.updated_at,{passed} FROM jobs j WHERE j.kind='embed' AND j.state='failed' "
            "ORDER BY j.updated_at DESC LIMIT 5000")]
        day = [(error, at, gone) for error, at, gone in failed if at is not None and at >= now - DAY]
        recent = [(error, at) for error, at, gone in day if not gone]
        waiting, oldest = conn.execute(
            f"SELECT COUNT(*),MIN(created_at) FROM jobs WHERE kind='embed' AND state IN ({','.join('?' * len(WAITING))})",
            WAITING).fetchone()
        waiting_error = conn.execute(
            "SELECT error FROM jobs WHERE kind='embed' AND state='retry' AND error IS NOT NULL ORDER BY updated_at DESC LIMIT 1").fetchone()
        unconfigured = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='waiting_config'").fetchone()[0]
        complete = conn.execute("SELECT MAX(updated_at) FROM jobs WHERE kind='embed' AND state='complete'").fetchone()[0]
        facts["embeddings"] = {
            "failed_total": len(failed), "failed_24h": len(recent), "failed_superseded_24h": len(day) - len(recent),
            "last_failure": {"at": datetime.fromtimestamp(recent[0][1], timezone.utc).isoformat(),
                             "error": (recent[0][0] or "")[:240]} if recent else None,
            "waiting": waiting, "oldest_waiting_hours": _hours(_seconds(oldest), now),
            "waiting_error": (waiting_error[0] or "")[:240] if waiting_error else None,
            "waiting_config": unconfigured,
            "last_complete_at": complete, "hours_since_last_complete": _hours(_seconds(complete), now),
        }
    if _table(conn, "mind_appraisals"):
        from .model_lanes import label
        rows = conn.execute("SELECT available,json_extract(data,'$.attempt_started_at'),json_extract(data,'$.repair_reason') "
                            "FROM mind_appraisals WHERE scope=? AND state='needs-repair'", (scope,)).fetchall()
        available = [row[0] for row in rows if isinstance(row[0], (int, float))]
        started = [_seconds(row[1]) for row in rows]
        reasons = {}
        for row in rows:
            reasons[label(row[2]) if row[2] else "unknown"] = reasons.get(label(row[2]) if row[2] else "unknown", 0) + 1
        facts["quarantine"] = {
            "count": len(rows), "oldest_hours": _hours(min(available), now) if available else None,
            "newest_hours": _hours(max(available), now) if available else None,
            "new_24h": sum(1 for at in started if at is not None and at >= now - DAY), "reasons": reasons,
        }
    last = {}
    if _table(conn, "mind_state"):
        created = []
        row = conn.execute("SELECT data FROM mind_state WHERE scope=?", (scope,)).fetchone()
        if row:
            desires = (json.loads(row[0]).get("desires") or {})
            created += [_seconds(desire.get("created_at")) for desire in desires.values() if isinstance(desire, dict)]
        if _table(conn, "mind_desire_archive"):
            created += [_seconds(value) for (value,) in conn.execute(
                "SELECT json_extract(data,'$.created_at') FROM mind_desire_archive WHERE scope=?", (scope,))]
        created = [at for at in created if at is not None]
        last["wish_created_at"] = datetime.fromtimestamp(max(created), timezone.utc).isoformat() if created else None
    if _table(conn, "mind_explorations"):
        last["exploration_at"] = conn.execute("SELECT MAX(created_at) FROM mind_explorations WHERE scope=?", (scope,)).fetchone()[0]
    if _table(conn, "mind_contacts"):
        sent = conn.execute("SELECT json_extract(data,'$.updated_at') FROM mind_contacts WHERE scope=? AND state='accepted' "
                            "ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()
        last["contact_sent_at"] = sent[0] if sent else None
    names = {"wish_created_at": "wish", "exploration_at": "exploration", "contact_sent_at": "contact"}
    facts["last"] = {**last, "hours_since": {names[key]: _hours(_seconds(value), now) for key, value in last.items()}}
    return facts


def embedding_service(conn, root, *, timeout=1.5, client=None):
    """Whether the local embedding service answers now (OPS-02): the one the store's embedding role
    names, when that role is local. It is started on demand, so silence alone is no fault; a caller
    weighs it with the embed jobs waiting. The credential is read from its file and sent to that
    loopback port only; the answer is reduced to whether it is ours and whether the model is loaded."""
    row = conn.execute("SELECT data FROM settings WHERE key='models'").fetchone() if _table(conn, "settings") else None
    try:
        role = (json.loads(row[0]) or {}).get("embedding") or {} if row else {}
    except ValueError:
        role = {}
    if not role.get("local_embedding"):
        return {"local": False}
    address = urlsplit(str(role.get("endpoint") or ""))
    if address.hostname != "127.0.0.1" or not address.port:
        return {"local": True, "reachable": False, "reason": "endpoint-not-loopback"}
    import httpx

    try:
        credential = (Path(root) / "embedding-token").read_text().strip()
    except OSError:
        credential = ""
    try:
        with (client or httpx.Client(timeout=timeout, trust_env=False)) as http:
            answer = http.get(f"http://127.0.0.1:{address.port}/health", headers={"Authorization": "Bearer " + credential})
    except Exception as exc:  # noqa: BLE001 - any failure to answer is "not reachable", by its class
        return {"local": True, "reachable": False, "port": address.port, "reason": type(exc).__name__}
    try:
        body = answer.json()
    except ValueError:
        body = {}
    ours = answer.status_code == 200 and body.get("service") == "memorypalace-embedding"
    return {"local": True, "reachable": True, "port": address.port, "status": answer.status_code, "ours": ours,
            "loaded": body.get("loaded") if ours else None}


def read_only(root):
    """A connection to the store at `root` that cannot write: opened read-only and query-only."""
    path = Path(root) / "memory.sqlite3"
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect("file:" + quote(str(path.resolve()), safe="/") + "?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def main(argv=None):
    """`python -m kin_mind.operational_status liveness --root <MemoryPalace> --scope <scope JSON>`: the
    liveness facts and the embedding service's answer, as one JSON line, from a read-only connection."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("action", choices=["liveness"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--scope", required=True, help="the deployment's scope, as mind-config names it")
    parser.add_argument("--no-probe", action="store_true", help="leave the embedding service unasked")
    args = parser.parse_args(argv)
    from eventmem.core.models import Scope

    scope = Scope.model_validate(json.loads(args.scope)).key()
    conn = read_only(args.root)
    try:
        facts = liveness(conn, scope)
        if not args.no_probe:
            facts.setdefault("embeddings", {})["service"] = embedding_service(conn, args.root)
    finally:
        conn.close()
    print(json.dumps(facts, ensure_ascii=False, sort_keys=True))
    return 0


def operational_status(mind, config=None):
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
        autonomy = {}
        for table, label in (("mind_plans", "plans"), ("mind_plan_runs", "executions"), ("mind_procedures", "procedures")):
            if table in tables:
                field = "state" if table == "mind_plan_runs" else "status"
                autonomy[label] = {r[0]: r[1] for r in conn.execute(f"SELECT {field},COUNT(*) FROM {table} WHERE scope=? GROUP BY {field}", (scope,))}
        lanes = None
        if "mind_model_leases" in tables:
            from .model_lanes import status
            # Lanes, capacity with its source and the current holders: labels, never text.
            lanes = status(conn)
            autonomy["background_model_slots"] = lanes["lanes"]["background"]["held"]
        if "mind_reinforcement" in tables:
            autonomy["effective_use_events"] = conn.execute("SELECT COUNT(*) FROM mind_reinforcement WHERE scope=?", (scope,)).fetchone()[0]
        if "mind_strength_observations" in tables:
            autonomy["strength_observation_days"] = {r[0]: r[1] for r in conn.execute("SELECT version,COUNT(*) FROM mind_strength_observations WHERE scope=? GROUP BY version", (scope,))}
        # A running row whose lease ran out, or whose worker cannot be shown alive, is stuck, not
        # running: counted apart so a health check sees it (K2-19).
        if "mind_plan_runs" in tables:
            autonomy["expired_running_executions"] = conn.execute(
                "SELECT COUNT(*) FROM mind_plan_runs WHERE scope=? AND state='running' AND lease_until<?",
                (scope, time.time())).fetchone()[0]
        if "mind_explorations" in tables:
            from .liveness import live_explorations
            running = [r[0] for r in conn.execute("SELECT id FROM mind_explorations WHERE scope=? AND state='running'", (scope,))]
            alive = set(live_explorations(conn, scope)) if running else set()
            autonomy["stale_running_explorations"] = sum(1 for identifier in running if identifier not in alive)
        from .isolation_migration import status as isolation_status
        from .erasure import status as erasure_status
        memory = {"evidence_isolation": isolation_status(conn, scope), "history_erase": erasure_status(conn, scope)}
        # Quarantined appraisals have no automatic way out: how many, since when, and why, as
        # static labels, so an operator can retire or resume them (K4-06, DB1-05).
        from .model_lanes import label
        count, oldest = conn.execute("SELECT COUNT(*),MIN(available) FROM mind_appraisals WHERE scope=? AND state='needs-repair'",
                                     (scope,)).fetchone()
        reasons = {}
        for reason, n in conn.execute("SELECT json_extract(data,'$.repair_reason'),COUNT(*) FROM mind_appraisals "
                                      "WHERE scope=? AND state='needs-repair' GROUP BY 1", (scope,)):
            reasons[label(reason) if reason else "unknown"] = reasons.get(label(reason) if reason else "unknown", 0) + n
        memory["quarantined"] = {"count": count, "oldest_available_unix": oldest, "reasons": reasons}
        memory["fork_read_errors"] = fork_read_errors(mind.engine.db.root)
        # Whether the work is moving (OPS-02): the phone's self-check reads this with the rest.
        moving = liveness(conn, scope)
        moving.setdefault("embeddings", {})["service"] = embedding_service(conn, mind.engine.db.root)
    action = json.loads(schedule["data"]) if schedule else {}
    latest = json.loads(last["data"]) if last else {}
    # Which copy of the source answered this call, and which other copies are still
    # reachable on the path. The start-up check already refused a foreign one, so a
    # `verified` verdict here is the boring case; what an operator comes for is
    # `shadows`, which names a stale editable install before it ever wins a race.
    source = verify_source_root((config or {}).get("source_root"))
    # And whether the configuration still names an interpreter that exists. This one
    # is about the next spawn, not about this process, and it says so itself: a host
    # whose configured Python has been deleted keeps answering here while every
    # worker it tries to start fails, which is exactly how that goes unnoticed.
    source["interpreter"] = verify_interpreter((config or {}).get("python"))
    return {"checked_at": mind.clock(), "autonomy": autonomy, "model_lanes": lanes, "queues": [dict(r) for r in rows],
            "source": source,
            "timing_contract": "oldest_available_unix is queue age, not current execution start; a missing attempt_started_at is unknown. Use lease_expires_unix for running-worker deadline.",
            "action": {"last_success": action.get("last_success"), "next_review": schedule["next_review"] if schedule else None,
                       "revision": schedule["revision"] if schedule else None, "model": action.get("receipt", {}).get("model")},
            "enrichment": {"cursor": cursor["seq"] if cursor else None, "revision": cursor["revision"] if cursor else None,
                           "last_success": enriched["at"] if enriched else None, "last_job": enriched["id"] if enriched else None},
            "last_completed_review": {"id": last["id"], "model": latest.get("receipt", {}).get("model")} if last else None,
            "exploration": dict(exploration) if exploration else None,
            "contact": {"id": contact["id"], "state": contact["state"], "at": json.loads(contact["data"]).get("updated_at")} if contact else None,
            "memory": memory, "liveness": moving}


if __name__ == "__main__":
    sys.exit(main())
