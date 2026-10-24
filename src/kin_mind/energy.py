"""One shared, host-estimated reserve. Activity receipts, not model scores or timers.

The tiny ledger deduplicates logical actions across recipients and transport retries.
Reads project elapsed recovery without writes. Nothing here authorizes or blocks work.
"""
import json
import math
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eventmem.core.db import dumps
from .rhythm import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_energy_state(
 scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_energy_events(
 scope TEXT NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,applied_at TEXT NOT NULL,
 delta REAL NOT NULL,config_version INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,event_id));
CREATE INDEX IF NOT EXISTS mind_energy_recent ON mind_energy_events(scope,applied_at);
"""
DEFAULTS = {"enabled": False, "initial": 75.0, "recovery_per_hour": 1.0,
            "rest_recovery_per_hour": 8.0, "awake_fatigue_per_hour": 2.0,
            "rest_fatigue_recovery_per_hour": 10.0, "max_fatigue": 40.0,
            "timezone": "Asia/Singapore", "night_penalty": 20.0,
            "costs": {"reply": 1.5, "explore": 4.0, "work": 6.0},
            "state_notes": "", "expression_preferences": ""}


def _load(conn, scope):
    row = conn.execute("SELECT revision,data FROM mind_energy_state WHERE scope=?", (scope,)).fetchone()
    if not row:
        return 0, None
    data = json.loads(row[1])
    data["config"] = {**DEFAULTS, **data["config"]}
    data.setdefault("fatigue", 0.0)
    data.setdefault("mode", "awake")
    data.setdefault("active_executions", {})
    return row[0], data


def _project(data, at):
    elapsed = max(0.0, (stamp(at) - stamp(data["as_of"])).total_seconds() / 3600)
    cfg = data["config"]
    if not cfg["enabled"]:
        return data["value"], data["fatigue"]
    resting = data["mode"] == "resting" and not data["active_executions"]
    recovery = cfg["rest_recovery_per_hour"] if resting else cfg["recovery_per_hour"]
    fatigue_rate = -cfg["rest_fatigue_recovery_per_hour"] if resting else cfg["awake_fatigue_per_hour"]
    # Recovery caps before expenditure; long offline intervals use the last
    # recorded phase, never invented sleep and never a per-hour replay loop.
    return (min(100.0, data["value"] + recovery * elapsed),
            min(cfg["max_fatigue"], max(0.0, data["fatigue"] + fatigue_rate * elapsed)))


def _night_penalty(cfg, at):
    local = stamp(at).astimezone(ZoneInfo(cfg["timezone"]))
    hour = local.hour + local.minute / 60 + local.second / 3600
    # A local-clock context curve, not a sleep schedule or an action gate:
    # 22 -> 02 ramp up, 02 -> 06 plateau, 06 -> 08 ramp down.
    factor = (hour - 22) / 4 if hour >= 22 else ((hour + 2) / 4 if hour < 2
             else 1.0 if hour < 6 else (8 - hour) / 2 if hour < 8 else 0.0)
    return cfg["night_penalty"] * factor


def _at(data, now):
    return max(stamp(now), stamp(data["as_of"])).astimezone(timezone.utc).isoformat()


def _settle(data, at):
    reserve, fatigue = _project(data, at)
    data.update(value=reserve, fatigue=fatigue, as_of=at)


def phase_changed(conn, mind, phase):
    """Same transaction as a real rhythm proposal; repeats do not reset its clock."""
    revision, data = _load(conn, mind.scope.key())
    mode = "resting" if phase == "resting" else "awake"
    if not data or data["mode"] == mode:
        return
    _settle(data, _at(data, mind.clock()))
    data["mode"] = mode
    conn.execute("UPDATE mind_energy_state SET revision=?,data=? WHERE scope=?",
                 (revision + 1, dumps(data), mind.scope.key()))


def view(conn, scope, at):
    revision, data = _load(conn, scope)
    if not data:
        return {"enabled": False, "basis": "host-estimated-role-state"}
    cfg = data["config"]
    at = _at(data, at)
    since = (stamp(at) - timedelta(hours=1)).isoformat()
    counts = {kind: 0 for kind in cfg["costs"]}
    for kind, count in conn.execute("SELECT kind,COUNT(*) FROM mind_energy_events WHERE scope=? AND applied_at>=? AND applied_at<=? GROUP BY kind", (scope, since, at)):
        counts[kind] = count
    reserve, fatigue = _project(data, at)
    night = _night_penalty(cfg, at)
    return {"enabled": cfg["enabled"], "value": round(max(0.0, reserve - fatigue - night), 2),
            "reserve": round(reserve, 2), "fatigue": round(fatigue, 2), "night_penalty": round(night, 2),
            "mode": "awake" if data["active_executions"] else data["mode"], "rhythm_mode": data["mode"],
            "active_execution_count": len(data["active_executions"]), "timezone": cfg["timezone"],
            "local_time": stamp(at).astimezone(ZoneInfo(cfg["timezone"])).isoformat(),
            "as_of": max(stamp(at), stamp(data["as_of"])).isoformat(), "revision": revision,
            "basis": "host-estimated-role-state", "config_version": data["config_version"],
            "recovery_per_hour": cfg["recovery_per_hour"], "costs": cfg["costs"],
            "rest_recovery_per_hour": cfg["rest_recovery_per_hour"],
            "awake_fatigue_per_hour": cfg["awake_fatigue_per_hour"],
            "rest_fatigue_recovery_per_hour": cfg["rest_fatigue_recovery_per_hour"],
            "max_fatigue": cfg["max_fatigue"], "night_penalty_max": cfg["night_penalty"],
            "recent_costs": counts, "state_notes": cfg["state_notes"],
            "expression_preferences": cfg["expression_preferences"]}


def configure(mind, request):
    if not isinstance(request, dict) or set(request) - set(DEFAULTS):
        raise ValueError("Unknown energy configuration")
    with mind.engine.db.connect(write=True) as conn:
        revision, data = _load(conn, mind.scope.key())
        previous = data["config"] if data else DEFAULTS
        if "costs" in request and not isinstance(request["costs"], dict):
            raise ValueError("Energy costs must be a mapping")
        cfg = {**previous, **request, "costs": {**previous["costs"], **request.get("costs", {})}}
        if type(cfg["enabled"]) is not bool or set(cfg["costs"]) != {"reply", "explore", "work"}:
            raise ValueError("Invalid energy configuration")
        for value in [*(cfg[key] for key in ("initial", "recovery_per_hour", "rest_recovery_per_hour", "awake_fatigue_per_hour", "rest_fatigue_recovery_per_hour", "max_fatigue", "night_penalty")), *cfg["costs"].values()]:
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("Energy parameters must be finite values from 0 to 100")
        try:
            ZoneInfo(cfg["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
            raise ValueError("Energy timezone must be an IANA timezone") from error
        for name in ("state_notes", "expression_preferences"):
            if not isinstance(cfg[name], str) or len(cfg[name]) > 4000:
                raise ValueError("Energy notes must be text up to 4000 characters")
        if data and cfg == previous:
            return {"state": "configured", "energy_context": view(conn, mind.scope.key(), mind.clock())}
        at = max(stamp(mind.clock()), stamp(data["as_of"])).astimezone(timezone.utc).isoformat() if data else stamp(mind.clock()).astimezone(timezone.utc).isoformat()
        value, fatigue = _project(data, at) if data else (cfg["initial"], 0.0)
        rhythm = (mind._load(conn) or {}).get("rhythm") or {}
        mode = data["mode"] if data else ("resting" if (rhythm.get("phase") or rhythm.get("base_phase")) == "resting" else "awake")
        data = {"value": value, "fatigue": min(fatigue, cfg["max_fatigue"]), "mode": mode,
                "active_executions": (data or {}).get("active_executions", {}),
                "as_of": at, "config": cfg, "config_version": (data or {}).get("config_version", 0) + 1}
        conn.execute("INSERT INTO mind_energy_state VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,data=excluded.data", (mind.scope.key(), revision + 1, dumps(data)))
        return {"state": "configured", "energy_context": view(conn, mind.scope.key(), at)}


def record(conn, mind, kind, activity_id, *, metadata=None):
    """Called by trusted execution/response hooks, in their owning write transaction."""
    if kind not in DEFAULTS["costs"] or not isinstance(activity_id, str) or not activity_id.strip() or len(activity_id) > 240:
        raise ValueError("Energy activity requires a supported kind and stable logical id")
    scope, now = mind.scope.key(), mind.clock()
    revision, data = _load(conn, scope)
    if not data or not data["config"]["enabled"]:
        return {"state": "disabled", "energy_context": view(conn, scope, now)}
    at = max(stamp(now), stamp(data["as_of"])).astimezone(timezone.utc).isoformat()
    event_id = kind + ":" + activity_id
    cost = data["config"]["costs"][kind]
    inserted = conn.execute("INSERT OR IGNORE INTO mind_energy_events VALUES(?,?,?,?,?,?,?)",
                            (scope, event_id, kind, at, -cost, data["config_version"], dumps(metadata or {}))).rowcount
    if inserted:
        _settle(data, at)
        data.update(value=max(0.0, data["value"] - cost), last_event_id=event_id)
        conn.execute("UPDATE mind_energy_state SET revision=?,data=? WHERE scope=?", (revision + 1, dumps(data), scope))
    return {"state": "applied" if inserted else "duplicate", "energy_context": view(conn, scope, at)}


def execution(conn, mind, kind, activity_id, execution_id, execution_state):
    """A real executor receipt; terminal state lives in the existing cost event.

    A finish is accepted only from a host with proof that execution happened.
    Thus a finish arriving before its start can account once and mark it terminal,
    never leaving a late start busy. This does not authorize or cancel the job.
    """
    if kind not in {"work", "explore"} or execution_state not in {"started", "finished"} or not isinstance(execution_id, str) or not execution_id.strip() or len(execution_id) > 240:
        raise ValueError("Energy execution requires work/explore and a stable execution id/state")
    scope = mind.scope.key()
    if execution_state == "started":
        # Recovery may settle the actual run before a durable host outbox
        # replays its start. Existing terminal facts dominate that late receipt.
        table = "mind_plan_runs" if kind == "work" else "mind_explorations"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            terminal = conn.execute("SELECT state,data FROM " + table + " WHERE scope=? AND id=?", (scope, execution_id)).fetchone()
            prior_key = json.loads(terminal["data"]).get("energy_activity_id") if terminal else None
            if terminal and terminal["state"] != "running" and (not prior_key or prior_key == activity_id):
                execution_state = "finished"
    result = record(conn, mind, kind, activity_id)
    revision, data = _load(conn, scope)
    event_id = kind + ":" + activity_id
    row = conn.execute("SELECT data FROM mind_energy_events WHERE scope=? AND event_id=?", (scope, event_id)).fetchone()
    if not data or not row:
        return result
    receipt = json.loads(row[0])
    executions = receipt.setdefault("executions", {})
    prior = executions.get(execution_id)
    if prior == "finished" or prior == execution_state:
        return result
    at = _at(data, mind.clock())
    _settle(data, at)
    executions[execution_id] = execution_state
    if execution_state == "started":
        data["active_executions"][execution_id] = {"kind": kind, "activity_id": activity_id}
    else:
        data["active_executions"].pop(execution_id, None)
    conn.execute("UPDATE mind_energy_events SET data=? WHERE scope=? AND event_id=?", (dumps(receipt), scope, event_id))
    conn.execute("UPDATE mind_energy_state SET revision=?,data=? WHERE scope=?", (revision + 1, dumps(data), scope))
    return {**result, "energy_context": view(conn, scope, at)}


def finish_recorded_execution(conn, mind, kind, activity_id, execution_id):
    """Settle an existing receipt, including a host fallback the plan never saw."""
    if not activity_id:
        return
    row = conn.execute("SELECT data FROM mind_energy_events WHERE scope=? AND event_id=?",
                       (mind.scope.key(), kind + ":" + activity_id)).fetchone()
    if row and json.loads(row[0]).get("executions", {}).get(execution_id) == "started":
        execution(conn, mind, kind, activity_id, execution_id, "finished")


def activity(mind, request):
    # Public only to the host dispatch, never exported as a model tool.
    keys = set(request)
    if keys not in ({"kind", "activity_id"}, {"kind", "activity_id", "execution_id", "execution_state"}):
        raise ValueError("Host activity requires kind/activity_id and optionally both execution receipt fields")
    with mind.engine.db.connect(write=True) as conn:
        if "execution_id" in request:
            return execution(conn, mind, **request)
        return record(conn, mind, request["kind"], request["activity_id"])
