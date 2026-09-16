"""Observed use frequency. Retrieval and maintenance cannot heat themselves."""
import json
import math
from datetime import timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, dumps

from .autonomy_schema import enabled, settings
from .state import timestamp

VERSION = "effective-use-30d-log1p-v1"
ORIGINS = {"user_query", "reply_reference", "verified_task"}


def input_key(conn, scope, requested=None):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_reply_inputs'").fetchone() and requested:
        row = conn.execute("SELECT source_id FROM mind_reply_inputs WHERE scope=? AND id=?", (scope, requested)).fetchone()
        if row:
            return row[0]
    if requested:
        return requested
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
        row = conn.execute("SELECT json_extract(data,'$.source_id') FROM mind_runtime_events WHERE scope=? AND kind='owner-message' "
                           "AND COALESCE(json_extract(data,'$.historical'),0)=0 ORDER BY seq DESC LIMIT 1", (scope,)).fetchone()
        if row:
            return row[0]
    return None


def record(conn, scope, identifier, use_key, origin, at, data=None):
    if origin not in ORIGINS or not enabled(conn, scope, "usage_reinforcement"):
        return False
    data = data or {}
    if origin == "verified_task" and not (data.get("verified") and data.get("result_id")):
        raise Conflict("Task reinforcement requires a verified outcome")
    # All steps of one plan share a single use key. All channels of one input
    # share its authenticated input ID; query text and channel are not identity.
    key = data.get("plan_id") or input_key(conn, scope, data.get("input_id") or use_key)
    if not key:
        raise ValueError("Effective use requires an idempotency key")
    return conn.execute("INSERT OR IGNORE INTO mind_reinforcement VALUES(?,?,?,?,?,?)",
        (scope, identifier, key, at, origin, dumps({**data, "weight_version": VERSION}))).rowcount == 1


def strengths(conn, scope, identifiers, at):
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        return {}
    result = {i: {"uses": 0, "decayed_uses": 0., "strength": 0., "last_used_at": None, "weight_version": VERSION} for i in identifiers}
    placeholders = ",".join("?" for _ in identifiers)
    for row in conn.execute(f"SELECT identifier,at FROM mind_reinforcement WHERE scope=? AND identifier IN ({placeholders})", [scope, *identifiers]):
        item = result[row["identifier"]]
        days = max(0., (timestamp(at) - timestamp(row["at"])).total_seconds() / 86400)
        item["uses"] += 1
        item["decayed_uses"] += math.exp2(-days / 30)
        item["last_used_at"] = max(item["last_used_at"] or row["at"], row["at"])
    for item in result.values():
        item["strength"] = math.log1p(item["decayed_uses"])
    return result


def observe(conn, scope, at, data):
    cfg = settings(conn, scope)
    if not cfg.get("usage_reinforcement"):
        return
    day = timestamp(at).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat()
    conn.execute("INSERT OR IGNORE INTO mind_strength_observations VALUES(?,?,?,?,?)", (scope, VERSION, day, at, dumps(data)))


def validate_enable(conn, scope, config, at):
    started = config.get("reinforcement_started_at")
    if not started or timestamp(at) - timestamp(started) < timedelta(days=7):
        raise Conflict("Frequency ranking needs seven full days of its own observation")
    rows = conn.execute("SELECT day FROM mind_strength_observations WHERE scope=? AND version=? AND observed_at>=? ORDER BY day DESC LIMIT 7", (scope, VERSION, started)).fetchall()
    if len(rows) < 7 or any((timestamp(rows[0][0] + "T00:00:00+08:00") - timestamp(r[0] + "T00:00:00+08:00")).days != i for i, r in enumerate(rows)):
        raise Conflict("Frequency ranking needs seven consecutive actual observation days")
    validation = config.get("reinforcement_validation") or {}
    if validation.get("weight_version") != VERSION or validation.get("critical_hits") != 21 or validation.get("hit_at_8", 0) < 44 or validation.get("background_heating") != 0 or not validation.get("owner_approved"):
        raise Conflict("Frequency ranking needs current replay and owner approval")
    if not validation.get("evaluated_at") or timestamp(validation["evaluated_at"]) < timestamp(started) + timedelta(days=7):
        raise Conflict("Frequency replay predates its observation window")


def order(mind, items, *, explicit=False):
    from .memory import MemoryContinuity
    cfg = MemoryContinuity(mind).settings()
    if not cfg.get("usage_reinforcement"):
        return items
    with mind.engine.db.connect() as conn:
        usage = strengths(conn, mind.scope.key(), [i["id"] for i in items], mind.clock())
    for item in items:
        item["usage_strength"] = usage[item["id"]]
    if explicit:
        return items
    protected = [i for i in items if i.get("required") or i.get("facts", {}).get("corrections")]
    movable = [(n, i) for n, i in enumerate(items) if i not in protected]
    # Bounded frequency contributes a small prior; semantic relevance and
    # current factual status remain authoritative. First version is shadow.
    movable.sort(key=lambda pair: -(1/(60+pair[0]) + .001*usage[pair[1]["id"]]["strength"]))
    proposed = [*protected, *[i for _, i in movable]]
    mind.engine.db.metric("reinforcement_shadow_reorder", int([i["id"] for i in proposed] != [i["id"] for i in items]),
        {"weight_version": VERSION, "before": [i["id"] for i in items[:8]], "after": [i["id"] for i in proposed[:8]],
         "active": cfg.get("reinforcement_ranking", False), "observed_at": mind.clock()})
    return proposed if cfg.get("reinforcement_ranking") else items
