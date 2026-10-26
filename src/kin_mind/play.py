"""Kin's own interests, and boredom as a reason to go and do something (2026-10-26).

An interest is a short topic Kin likes. Its strength fades while nothing touches it (half-life
`half_life_days`), grows when an activity on it was fun and drops when it was dull; where it came
from is kept: the persona, an exploration, or chat. An activity ends in `after_activity`, with Kin's
own word on how it went (an exploration result's `enjoyment`); its `new_interests` may add a few,
never past `max_interests`. An interest keeps the sources it grew from, so an erase takes its words.

Boredom (the drives module's, or estimated from the hours since anything engaging happened), energy,
the rhythm phase, what is running and today's count decide only when Kin is given an idle review to
think about what to do (`wake`): the usual idle review, woken early. There Kin is shown the
interests and boredom (`context`) and chooses among the wishes Kin could always make -- explore,
play (an exploration with `exploration_target: play`, smaller and lighter), create, contact -- or
rest. Nothing here starts, sends or blocks anything else; only play waits for the next day once
today's explorations are used up.
"""
import json
import math
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eventmem.core.db import digest, dumps
from eventmem.core.models import Model

from . import recipient
from .erasure import ERASED
from .rhythm import stamp

SCHEMA = "CREATE TABLE IF NOT EXISTS mind_interests(scope TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,id))"
# Operator-tunable through `play-configure`, kept in the engine's settings under this key.
SETTINGS = "kin_play"
DEFAULTS = {"enabled": True, "timezone": "Asia/Singapore", "boredom_threshold": 60, "energy_floor": 40,
            "explore_per_day": 6, "create_per_day": 3, "wake_minutes": 120, "play_budget_seconds": 480,
            "half_life_days": 14, "max_interests": 20,
            # Only without the drives module: boredom from the hours since anything engaging.
            "boredom_initial": 20, "boredom_per_hour": 5}
TOPIC_CHARS, SHOWN, FADED, NEW_STRENGTH, NEW_PER_ACTIVITY = 24, 8, 5.0, 30.0, 2
STEP, TOUCH = {"fun": 15.0, "meh": -10.0}, 5.0
SEED_STRENGTH, SEED_SHOWN, SEED_TAGS = {"persona": 50.0, "chat": 40.0}, 12, 8
SLEEPY = ("drowsy", "resting")
LAST_ENGAGING = (
    "SELECT MAX(created_at) FROM mind_explorations WHERE scope=?",
    "SELECT MAX(json_extract(data,'$.started_at')) FROM mind_plan_runs WHERE scope=?",
    "SELECT MAX(json_extract(data,'$.updated_at')) FROM mind_contacts WHERE scope=? AND state='accepted'",
    # The same predicate as rhythm's human-interaction index, so that index answers it.
    "SELECT MAX(occurred_at) FROM sources WHERE scope=? AND deleted=0 AND (namespace IN ('kin-owner-input','kin-companion-input') "
    "OR substr(namespace,1,5)='host:') AND json_extract(data,'$.authority')='explicit' AND json_extract(data,'$.metadata.role')='user' "
    "AND json_extract(data,'$.metadata.host_event')='message'",
)

# NEEDS 小光 OK: what an idle review that carries `autonomy_context.play` is told about it.
PROMPT = ("\nautonomy_context.play 是你此刻的兴趣和闲不闲。interests 是你喜欢的东西：strength 越高越上心，days_since 是多久没碰了，"
          "source 是它的来历（persona 本来就喜欢，exploration 逛着逛着喜欢上的，chat 聊天里来的）。boredom 是有多无聊，"
          "bored 为 true 就是有点闲得发慌、想找点事做了。这时可以挑一个兴趣认真看看（explore 愿望，topic 就写那个兴趣）；"
          "也可以上网随便逛逛，看看有意思的网站、社区和作品，回来写几句感受就好，不用写成研究（explore 愿望，exploration_target 写 play）；"
          "也可以动手做点东西（plan_changes 里排一个 create 步骤）；或者找想说话的人聊几句（contact 愿望）；不想动就继续歇着（next_move 写 rest）。"
          "left_today 里为 0 的那样今天先不做了。做不做、做哪样，都看你自己的心情。")
# NEEDS 小光 OK: what the executor of a play run is told besides the usual exploration rules.
PLAY_GUIDE = ("这一趟不是做研究，是出去玩一会儿：随便逛逛和题目沾边、你觉得有意思的网站、社区或作品，碰到喜欢的就多看一会儿。"
              "summary 用几句话写你自己的感受；findings 记下几样让你有感觉的东西就够了，不用面面俱到，也不用下结论；"
              "真正打开看过的页面照常放进 sources。想给小光或家人看看的，写进 suggested_share。\n")
# NEEDS 小光 OK: the migration's one model call, which only lists what is already written.
SEED_PROMPT = ("下面是 Kin 的人设，和 Kin 自己记下的偏好。把里面写到的、Kin 喜欢或者好奇的东西列出来，每样写成几个字的短词放进 topic；"
               "来自人设的 source 写 persona，来自自己记下的偏好写 chat。只列兴趣爱好和好奇的东西，不列人、关系、身份、规则或说话方式；"
               "最多十二样，没有就空着。")


class Seed(Model):
    topic: str
    source: str


class Seeds(Model):
    interests: list[Seed] = []


def config(conn):
    row = conn.execute("SELECT data FROM settings WHERE key=?", (SETTINGS,)).fetchone()
    return {**DEFAULTS, **(json.loads(row[0]) if row else {})}


def _key(text):
    return " ".join(str(text).split()).lower()


def _one(conn, sql, args):
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        return None


def _days(since, at):
    return max(0.0, (stamp(at) - stamp(since)).total_seconds() / 86400)


def _faded(strength, since, at, cfg):
    return strength * 0.5 ** (_days(since, at) / cfg["half_life_days"]) if cfg["half_life_days"] else strength


def interests(conn, scope, at, cfg):
    """The interests still alive, strongest first, each with its strength as of `at`."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_interests'").fetchone():
        return []
    found = []
    for (data,) in conn.execute("SELECT data FROM mind_interests WHERE scope=?", (scope,)):
        entry = json.loads(data)
        strength = _faded(entry["strength"], entry["as_of"], at, cfg)
        if strength >= FADED and entry["topic"] != ERASED:
            found.append({**entry, "strength": strength})
    return sorted(found, key=lambda e: (-e["strength"], e["topic"]))


def _save(conn, scope, entry):
    conn.execute("INSERT INTO mind_interests VALUES(?,?,?) ON CONFLICT(scope,id) DO UPDATE SET data=excluded.data",
                 (scope, entry["id"], dumps(entry)))


def _add(conn, scope, at, cfg, topic, strength, source, source_ids=(), engaged=None):
    """A new interest, unless it is one already, too long a topic, too faint, or weaker than every
    interest of a full list. What faded or was erased makes room first."""
    topic = " ".join(str(topic).split())
    identifier = "interest_" + digest([scope, _key(topic)])[:24]
    if not topic or len(topic) > TOPIC_CHARS or ERASED in topic or strength < FADED:
        return None
    live = interests(conn, scope, at, cfg)
    if any(entry["id"] == identifier for entry in live):
        return None
    kept = [entry["id"] for entry in live]
    conn.execute("DELETE FROM mind_interests WHERE scope=? AND id NOT IN (" + ",".join("?" * len(kept)) + ")", (scope, *kept))
    if len(live) >= cfg["max_interests"]:
        if not live or live[-1]["strength"] >= strength:
            return None
        conn.execute("DELETE FROM mind_interests WHERE scope=? AND id=?", (scope, live[-1]["id"]))
    entry = {"id": identifier, "topic": topic, "strength": float(strength), "as_of": at, "last_engaged_at": engaged,
             "source": source, "created_at": at, "source_ids": sorted(set(source_ids))}
    _save(conn, scope, entry)
    return entry


def after_activity(conn, mind, text, result=None, source_ids=()):
    """An activity on `text` (a wish's topic, a plan's goal) ended, in the caller's transaction. Each
    interest it names gets Kin's word on how it went -- fun, meh, or nothing said: a little -- and is
    touched now. Fun on a short topic that is no interest yet makes one; so may `new_interests`."""
    cfg, scope, at = config(conn), mind.scope.key(), mind.clock()
    if not cfg["enabled"]:
        return
    conn.execute(SCHEMA)
    result = result if isinstance(result, dict) else {}
    mood = result.get("enjoyment") if result.get("enjoyment") in STEP else None
    touched = False
    for entry in interests(conn, scope, at, cfg):
        if _key(entry["topic"]) in _key(text):
            entry.update(strength=min(100.0, max(0.0, entry["strength"] + STEP.get(mood, TOUCH))), as_of=at, last_engaged_at=at)
            _save(conn, scope, entry)
            touched = True
    grown = [text] if mood == "fun" and not touched else []
    grown += [topic for topic in result.get("new_interests") or [] if isinstance(topic, str)][:NEW_PER_ACTIVITY]
    for topic in grown:
        _add(conn, scope, at, cfg, topic, NEW_STRENGTH, "exploration", source_ids, engaged=at)


def boredom(conn, mind, at, cfg):
    """The drives module's boredom while it is there and on; else an estimate from the hours since
    anything engaging happened: an exploration, a creation, a contact sent, a message received."""
    try:
        from .drives import view
        current = view(conn, mind.scope.key(), at)
        if current.get("enabled"):
            return float(current["boredom"]["value"])
    except (ImportError, sqlite3.OperationalError):
        pass
    found = [value for value in (_one(conn, sql, (mind.scope.key(),)) for sql in LAST_ENGAGING) if value]
    hours = (stamp(at) - max(map(stamp, found))).total_seconds() / 3600 if found else 24 * 7
    return min(100.0, cfg["boredom_initial"] + cfg["boredom_per_hour"] * max(0.0, hours))


def energy(conn, scope, at):
    """Energy's value while it is on; None (taken as enough) otherwise."""
    from .energy import view
    current = view(conn, scope, at)
    return current["value"] if current.get("enabled") else None


def left(conn, scope, at, cfg):
    """What is left of today's explorations (play among them) and creations, by the local day."""
    zone = ZoneInfo(cfg["timezone"])
    since = datetime.combine(stamp(at).astimezone(zone).date(), time(), zone).astimezone(timezone.utc).isoformat()
    explored = _one(conn, "SELECT COUNT(*) FROM mind_explorations WHERE scope=? AND created_at>=?", (scope, since)) or 0
    created = _one(conn, "SELECT COUNT(*) FROM mind_plan_runs WHERE scope=? AND actor='create' "
                         "AND json_extract(data,'$.started_at')>=?", (scope, since)) or 0
    return {"explore_or_play": max(0, cfg["explore_per_day"] - explored), "create": max(0, cfg["create_per_day"] - created)}


def reading(conn, mind, state, at, cfg):
    scope, rhythm = mind.scope.key(), state.get("rhythm") or {}
    busy = any(_one(conn, "SELECT COUNT(*) FROM " + table + " WHERE scope=? AND state='running'", (scope,))
               for table in ("mind_explorations", "mind_plan_runs"))
    return {"boredom": boredom(conn, mind, at, cfg), "energy": energy(conn, scope, at),
            "awake": (rhythm.get("phase") or rhythm.get("base_phase")) not in SLEEPY, "busy": busy,
            "left": left(conn, scope, at, cfg)}


def ready(now, cfg):
    return (now["boredom"] >= cfg["boredom_threshold"] and (now["energy"] is None or now["energy"] >= cfg["energy_floor"])
            and now["awake"] and not now["busy"] and any(now["left"].values()))


def wake(mind, actions, memory):
    """`review_minute`'s: an idle review now when Kin is bored and up for something, at most one per
    `wake_minutes` after the last committed review."""
    settings = memory.settings()
    if not settings["idle"] or not settings["semantic"]:
        return None
    at, scope = mind.clock(), mind.scope.key()
    table = "mind_action_schedule" if settings["operational_lanes"] else "mind_semantic_cursor"
    with mind.engine.db.connect() as conn:
        cfg = config(conn)
        if not cfg["enabled"]:
            return None
        row = recipient.cursor(conn, scope, table) if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() else None
        last = json.loads((row or {}).get("data") or "{}").get("last_success")
        if last and stamp(at) - stamp(last) < timedelta(minutes=cfg["wake_minutes"]):
            return None
        state = mind._load(conn)
        refs = (state.get("action_policy") or {}).get("evidence") or []
        if not refs or not mind._fresh(conn, refs):
            return None
        now = reading(conn, mind, state, at, cfg)
        if not ready(now, cfg):
            return None
    with mind.engine.db.connect(write=True) as conn:
        return actions.emit(conn, "idle-review", ["boredom", last], {
            "evidence_ids": [r["record_id"] for r in refs], "agent_version": state["agent_version"],
            "reason": "Bored and up for something: look at what to do now, without inventing an owner message",
            "boredom": round(now["boredom"])})


def context(mind):
    """What an idle review is shown: boredom, the live interests and what is left today. None while off."""
    at, scope = mind.clock(), mind.scope.key()
    with mind.engine.db.connect() as conn:
        cfg = config(conn)
        if not cfg["enabled"]:
            return None
        now = reading(conn, mind, mind._load(conn), at, cfg)
        shown = [{"topic": e["topic"], "strength": round(e["strength"]), "source": e["source"],
                  "days_since": round(_days(e["last_engaged_at"], at), 1) if e.get("last_engaged_at") else None}
                 for e in interests(conn, scope, at, cfg)[:SHOWN]]
    return {"boredom": round(now["boredom"]), "bored": now["boredom"] >= cfg["boredom_threshold"],
            "interests": shown, "left_today": now["left"]}


def budget(mind, seconds):
    with mind.engine.db.connect() as conn:
        return min(seconds, max(1, int(config(conn)["play_budget_seconds"])))


def play_left(mind):
    with mind.engine.db.connect() as conn:
        return left(conn, mind.scope.key(), mind.clock(), config(conn))["explore_or_play"]


def configure(mind, request):
    """Operator action `play-configure`: set any of DEFAULTS; `{}` only reads. Answers numbers only."""
    if not isinstance(request, dict) or set(request) - set(DEFAULTS):
        raise ValueError("Unknown play setting")
    with mind.engine.db.connect() as conn:
        cfg = {**config(conn), **request}
    numbers = [value for key, value in cfg.items() if key not in ("enabled", "timezone")]
    if type(cfg["enabled"]) is not bool or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                               or not math.isfinite(value) or value < 0 for value in numbers):
        raise ValueError("Play settings are a switch, a timezone and non-negative numbers")
    try:
        ZoneInfo(cfg["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
        raise ValueError("Play timezone must be an IANA timezone") from error
    if request:
        mind.engine.settings(SETTINGS, cfg)
    at = mind.clock()
    with mind.engine.db.connect() as conn:
        now = reading(conn, mind, mind._load(conn), at, cfg)
        count = len(interests(conn, mind.scope.key(), at, cfg))
    return {"state": "configured", "play": cfg, "interests": count, "boredom": round(now["boredom"], 1),
            "energy": now["energy"], "awake": now["awake"], "busy": now["busy"], "left_today": now["left"], "ready": ready(now, cfg)}


def seed(mind, provider):
    """Operator action `interests-seed`, the migration after release: interests from the persona and
    Kin's own interest preferences (one model call, which only lists them) and from the topic tags of
    past explorations. Writes only into an empty list; answers counts."""
    from eventmem.core.persona import load_persona
    from .traits import EFFECTIVE, installed
    scope, at = mind.scope.key(), mind.clock()
    with mind.engine.db.connect() as conn:
        cfg = config(conn)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_interests'").fetchone() and \
                conn.execute("SELECT 1 FROM mind_interests WHERE scope=? LIMIT 1", (scope,)).fetchone():
            return {"state": "seeded-already"}
        tags = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone():
            for created, data in conn.execute("SELECT created_at,data FROM mind_explorations WHERE scope=? AND state='complete'", (scope,)):
                data = json.loads(data)
                for tag in (data.get("result") or {}).get("topic_tags") or []:
                    if isinstance(tag, str) and tag.strip() and ERASED not in tag:
                        entry = tags.setdefault(_key(tag), {"topic": tag, "count": 0, "latest": created, "ids": set()})
                        entry["count"] += 1
                        entry["latest"] = max(entry["latest"], created, key=stamp)
                        entry["ids"].update([data["source_id"]] if data.get("source_id") else [])
        preferences, chat_ids = [], set()
        if installed(conn):
            for (data,) in conn.execute("SELECT data FROM mind_traits WHERE scope=? AND status IN (" + ",".join("?" * len(EFFECTIVE)) + ")",
                                        (scope, *EFFECTIVE)):
                trait = json.loads(data)
                if trait.get("basis") == "self_preference" and trait.get("category") == "interests" and ERASED not in trait.get("text", ERASED):
                    preferences.append(trait["text"])
                    chat_ids.update(ref["source_id"] for ref in trait.get("evidence") or [] if ref.get("source_id"))
    persona = ((load_persona(mind.engine, mind.scope) or {}).get("instructions") or "").strip()
    found = []
    if persona or preferences:
        listed, _ = provider.structured("seed_interests", Seeds, SEED_PROMPT, {"persona": persona, "own_preferences": preferences},
                                        max_tokens=4096)
        found = [(item.topic, item.source) for item in listed.interests if item.source in SEED_STRENGTH][:SEED_SHOWN]
    counts = {"persona": 0, "chat": 0, "exploration": 0}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute(SCHEMA)
        for topic, source in found:
            if _add(conn, scope, at, cfg, topic, SEED_STRENGTH[source], source, chat_ids if source == "chat" else ()):
                counts[source] += 1
        for entry in sorted(tags.values(), key=lambda e: (-e["count"], e["topic"]))[:SEED_TAGS]:
            strength = _faded(min(70.0, 20.0 + 10.0 * entry["count"]), entry["latest"], at, cfg)
            if _add(conn, scope, at, cfg, entry["topic"], strength, "exploration", entry["ids"], engaged=entry["latest"]):
                counts["exploration"] += 1
    return {"state": "seeded", "counts": counts}
