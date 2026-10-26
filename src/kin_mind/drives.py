"""Kin's slow drives beside energy: intimacy (with 小光 only) and boredom.

Like energy, time and host activity receipts move them, never a model score. Intimacy is
read and reported only inside 小光's own private conversation (`owner_private`); every other
audience sees boredom alone. Nothing here sends, authorizes or blocks anything.
"""
import json
import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eventmem.core.db import dumps
from .rhythm import stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_drive_state(
 scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_drive_events(
 scope TEXT NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,event_id));
"""
OWNER = "person:xiaoguang"
DEFAULTS = {"enabled": False, "timezone": "Asia/Singapore",
            "intimacy_initial": 30.0, "intimacy_per_hour": 1.5, "intimacy_night_factor": 1.5,
            "intimacy_threshold": 70.0, "intimacy_after_release": 10.0, "decline_cooldown_hours": 6.0,
            "boredom_initial": 20.0, "boredom_per_hour": 5.0, "boredom_high_energy_factor": 1.5,
            "boredom_threshold": 60.0,
            # What one logical activity eases; a reply answers someone, a contact only reaches out.
            "boredom_relief": {"reply": 8.0, "contact": 4.0, "explore": 35.0, "work": 25.0, "play": 35.0}}
# Local hours in which an awake Kin's intimacy rises faster (22:00 -> 03:00).
NIGHT = (22, 3)


def owner_private():
    """This turn is 小光's own private conversation, as the trusted companion view names it.

    No view (an unscoped maintenance read, or a host without companions) is not proof of it.
    """
    from .companion_view import ACTIVE_VIEW
    view = ACTIVE_VIEW.get()
    return bool(view) and view.person == OWNER


def without_intimacy(value):
    """A state read for anyone but 小光: no adult-intimacy dimension, its style, or what lingers of it."""
    if not isinstance(value, dict):
        return value
    value = {k: v for k, v in value.items() if k != "interaction_style"}
    if isinstance(value.get("dimensions"), dict):
        value["dimensions"] = {k: v for k, v in value["dimensions"].items() if k != "flirtation"}
    layers = value.get("affect_layers")
    if isinstance(layers, dict):
        tone, lingering = layers.get("undertone"), layers.get("lingering")
        value["affect_layers"] = {**layers,
            **({"undertone": {**tone, "leaning": {k: v for k, v in tone["leaning"].items() if k != "flirtation"}}}
               if isinstance(tone, dict) and isinstance(tone.get("leaning"), dict) else {}),
            **({"lingering": None} if isinstance(lingering, dict) and lingering.get("dimension") == "flirtation" else {})}
    return value


def _load(conn, scope):
    row = conn.execute("SELECT revision,data FROM mind_drive_state WHERE scope=?", (scope,)).fetchone()
    if not row:
        return 0, None
    data = json.loads(row[1])
    cfg = data["config"]
    data["config"] = {**DEFAULTS, **cfg, "boredom_relief": {**DEFAULTS["boredom_relief"], **cfg.get("boredom_relief", {})}}
    return row[0], data


def _save(conn, scope, revision, data):
    conn.execute("INSERT INTO mind_drive_state VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                 (scope, revision + 1, dumps(data)))


def _at(data, now):
    return max(stamp(now), stamp(data["as_of"])).astimezone(timezone.utc).isoformat()


def _night_hours(start, end, zone):
    """Hours of [start, end) inside the local night window; a week back at most (the drive saturates long before)."""
    start = max(start, end - timedelta(days=7))
    total, day = 0.0, start.astimezone(zone).date() - timedelta(days=1)
    while day <= end.astimezone(zone).date():
        night = datetime.combine(day, time(NIGHT[0]), zone)
        dawn = datetime.combine(day + timedelta(days=1), time(NIGHT[1]), zone)
        total += max(0.0, (min(end, dawn) - max(start, night)).total_seconds() / 3600)
        day += timedelta(days=1)
    return total


def _intimacy(data, at):
    cfg, entry = data["config"], data["intimacy"]
    start, end = stamp(data["as_of"]), stamp(at)
    if not cfg["enabled"] or end <= start:
        return entry["value"]
    night = 0.0 if data["mode"] == "resting" else _night_hours(start, end, ZoneInfo(cfg["timezone"]))
    hours = (end - start).total_seconds() / 3600 + (cfg["intimacy_night_factor"] - 1) * night
    return min(100.0, entry["value"] + cfg["intimacy_per_hour"] * hours)


def _boredom(data, at):
    cfg, entry = data["config"], data["boredom"]
    start, end = stamp(data["as_of"]), stamp(at)
    # Asleep or resting, nothing gets boring.
    if not cfg["enabled"] or end <= start or data["mode"] == "resting":
        return entry["value"]
    factor = cfg["boredom_high_energy_factor"] if data.get("energy_tier") == "high" else 1.0
    return min(100.0, entry["value"] + cfg["boredom_per_hour"] * factor * (end - start).total_seconds() / 3600)


def _energy_tier(conn, scope, at):
    from .energy import view as energy_view
    current = energy_view(conn, scope, at)
    return current.get("tier") if current.get("enabled") else None


def _settle(conn, scope, data, at):
    data["intimacy"]["value"] = _intimacy(data, at)
    data["boredom"]["value"] = _boredom(data, at)
    data.update(as_of=at, energy_tier=_energy_tier(conn, scope, at))


def view(conn, scope, at, *, private=False):
    """The drives as one read. `private` (小光's own private turn) adds intimacy; nothing else does.

    Boredom's `value` and `last_engaging_at` are the interface other features read.
    """
    revision, data = _load(conn, scope)
    if not data:
        return {"enabled": False}
    cfg, at = data["config"], _at(data, at)
    boredom = round(_boredom(data, at), 2)
    result = {"enabled": cfg["enabled"], "revision": revision, "as_of": at, "mode": data["mode"],
              "boredom": {"value": boredom, "threshold": cfg["boredom_threshold"], "bored": boredom >= cfg["boredom_threshold"],
                          "last_engaging_at": data["boredom"].get("last_engaging_at")}}
    if private:
        value, declined = round(_intimacy(data, at), 2), data["intimacy"].get("declined_at")
        until = stamp(declined) + timedelta(hours=cfg["decline_cooldown_hours"]) if declined else None
        result["intimacy"] = {"value": value, "threshold": cfg["intimacy_threshold"], "high": value >= cfg["intimacy_threshold"],
                              "cooldown_until": until.isoformat() if until and stamp(at) < until else None,
                              "cooldown_hours": cfg["decline_cooldown_hours"], "released_at": data["intimacy"].get("released_at")}
    return result


def configure(mind, request):
    """Operator action `drives-configure`: the same shape and checks as energy's."""
    if not isinstance(request, dict) or set(request) - set(DEFAULTS):
        raise ValueError("Unknown drive configuration")
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        revision, data = _load(conn, scope)
        previous = data["config"] if data else DEFAULTS
        if "boredom_relief" in request and not isinstance(request["boredom_relief"], dict):
            raise ValueError("Boredom relief must be a mapping")
        cfg = {**previous, **request, "boredom_relief": {**previous["boredom_relief"], **request.get("boredom_relief", {})}}
        numbers = [value for key, value in cfg.items() if key not in {"enabled", "timezone", "boredom_relief"}] + list(cfg["boredom_relief"].values())
        if type(cfg["enabled"]) is not bool or set(cfg["boredom_relief"]) != set(DEFAULTS["boredom_relief"]) or any(
                isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100 for value in numbers):
            raise ValueError("Drive parameters must be finite values from 0 to 100")
        try:
            ZoneInfo(cfg["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
            raise ValueError("Drive timezone must be an IANA timezone") from error
        if data and cfg == previous:
            return {"state": "configured", "drives": view(conn, scope, mind.clock())}
        if data:
            at = _at(data, mind.clock())
            _settle(conn, scope, data, at)
        else:
            at = stamp(mind.clock()).astimezone(timezone.utc).isoformat()
            rhythm = (mind._load(conn) or {}).get("rhythm") or {}
            data = {"mode": "resting" if (rhythm.get("phase") or rhythm.get("base_phase")) == "resting" else "awake",
                    "intimacy": {"value": cfg["intimacy_initial"]}, "boredom": {"value": cfg["boredom_initial"]},
                    "as_of": at, "energy_tier": _energy_tier(conn, scope, at)}
        data.update(config=cfg, config_version=data.get("config_version", 0) + 1)
        _save(conn, scope, revision, data)
        return {"state": "configured", "drives": view(conn, scope, at)}


def phase_changed(conn, mind, phase):
    """The rhythm proposal's own transaction: settle up to the change, then follow the new phase."""
    scope = mind.scope.key()
    revision, data = _load(conn, scope)
    mode = "resting" if phase == "resting" else "awake"
    if not data or data["mode"] == mode:
        return
    _settle(conn, scope, data, _at(data, mind.clock()))
    data["mode"] = mode
    _save(conn, scope, revision, data)


def engaged(conn, mind, kind, activity_id):
    """An engaging activity (a reply, a contact, exploring, work, play) eases boredom once.

    Called with every host activity receipt, in its transaction; retries of one logical
    activity ease it once. Play and other features record through the same `energy-activity`.
    """
    scope = mind.scope.key()
    revision, data = _load(conn, scope)
    if not data or not data["config"]["enabled"]:
        return
    at = _at(data, mind.clock())
    if not conn.execute("INSERT OR IGNORE INTO mind_drive_events VALUES(?,?,?,?,?)",
                        (scope, "engaged:" + kind + ":" + activity_id, "engaged", at, "{}")).rowcount:
        return
    _settle(conn, scope, data, at)
    relief = data["config"]["boredom_relief"].get(kind, 0.0)
    data["boredom"].update(value=max(0.0, data["boredom"]["value"] - relief), last_engaging_at=at)
    _save(conn, scope, revision, data)


def report(mind, request):
    """Kin's report from 小光's private turn (tool `report_private_moment`).

    `satisfied`: an adult moment with 小光 that went well and satisfied; the only thing that
    lowers intimacy. `declined`: 小光 said no or had no time; a cooldown before raising it again.
    The host checks this turn's audience and the named input's speaker and conversation.
    """
    if (not isinstance(request, dict) or set(request) != {"input_id", "outcome"} or request["outcome"] not in {"satisfied", "declined"}
            or not isinstance(request["input_id"], str) or not request["input_id"].strip()):
        raise ValueError("A report needs input_id and outcome satisfied or declined")
    if not owner_private():
        raise PermissionError("Only available in 小光's private conversation")
    from .companion_view import ACTIVE_VIEW
    from .habits import ConversationHabits
    ConversationHabits(mind)  # the received-input table
    conversation = ACTIVE_VIEW.get().conversation
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        row = conn.execute("SELECT s.data FROM mind_reply_inputs r JOIN sources s ON s.id=r.source_id WHERE r.scope=? AND r.id=? AND s.deleted=0",
                           (scope, request["input_id"])).fetchone()
        meta = json.loads(row[0]).get("metadata", {}) if row else None
        if (meta is None or (meta.get("speaker_person_id") or OWNER) != OWNER or (meta.get("recipient_person_id") or OWNER) != OWNER
                or meta.get("conversation_key", conversation) != conversation):
            raise PermissionError("The input is not 小光's own private message in this conversation")
        revision, data = _load(conn, scope)
        if not data or not data["config"]["enabled"]:
            return {"state": "disabled"}
        at = _at(data, mind.clock())
        if conn.execute("INSERT OR IGNORE INTO mind_drive_events VALUES(?,?,?,?,?)",
                        (scope, "report:" + request["input_id"] + ":" + request["outcome"], "intimacy-" + request["outcome"], at, "{}")).rowcount:
            _settle(conn, scope, data, at)
            if request["outcome"] == "satisfied":
                data["intimacy"].update(value=min(data["intimacy"]["value"], data["config"]["intimacy_after_release"]), released_at=at, declined_at=None)
            else:
                data["intimacy"]["declined_at"] = at
            _save(conn, scope, revision, data)
            state = "recorded"
        else:
            state = "duplicate"
        return {"state": state, "outcome": request["outcome"], "intimacy": view(conn, scope, at, private=True)["intimacy"]}
