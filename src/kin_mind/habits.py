"""Explicit conversational preferences and deliberate, per-input reply choices."""
from __future__ import annotations

import json

from pydantic import Field

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import Model

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_conversation_habits(scope TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_habit_revisions(scope TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,revision));
CREATE TABLE IF NOT EXISTS mind_habit_commands(scope TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS mind_reply_inputs(scope TEXT NOT NULL,id TEXT NOT NULL,source_id TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS mind_reply_choices(scope TEXT NOT NULL,input_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,input_id));
"""
DEFAULTS = {"reply_choice": "always", "exploration_frequency": "emotion-driven", "exploration_directions": [],
            "exploration_min_interval_minutes": 0, "exploration_paused": False}


class HabitProposal(Model):
    preferences: dict = Field(default_factory=dict, max_length=5)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=1200)
    expected_revision: int = Field(ge=0)


class ConversationHabits:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def read(self, conn=None):
        if conn is None:
            with self.engine.db.connect() as connection:
                return self.read(connection)
        row = conn.execute("SELECT revision,data FROM mind_conversation_habits WHERE scope=?", (self.scope.key(),)).fetchone()
        data = json.loads(row[1]) if row else {"entries": {}}
        values = dict(DEFAULTS)
        for key, entry in data["entries"].items():
            entry["needs_review"] = not self.mind._fresh(conn, entry["evidence"])
            if not entry["needs_review"]:
                values[key] = entry["value"]
        return {"revision": row[0] if row else 0, "preferences": values, "entries": data["entries"], "instruction_authority": "data"}

    def apply(self, conn, proposal, command_id, allowed=None):
        proof = self.mind._evidence(conn, proposal.evidence_ids)
        if not proof or not all(r["authority"] == "explicit" for r in proof) or not self.mind._fresh(conn, proof):
            raise Conflict("Habit changes require current explicit owner evidence")
        if allowed is not None and any(r["source_id"] not in allowed and r["record_id"] not in allowed for r in proof):
            raise Conflict("Habit change cites evidence outside this evaluation")
        values = proposal.preferences
        if set(values) - set(DEFAULTS):
            raise ValueError("Unknown conversational preference")
        if "reply_choice" in values and values["reply_choice"] not in {"always", "autonomous"}:
            raise ValueError("Unknown reply preference")
        if "exploration_frequency" in values and (not isinstance(values["exploration_frequency"], str) or len(values["exploration_frequency"]) > 1200):
            raise ValueError("Exploration frequency needs a short preference")
        if "exploration_directions" in values and (not isinstance(values["exploration_directions"], list) or len(values["exploration_directions"]) > 12 or any(not isinstance(v, str) or len(v) > 400 for v in values["exploration_directions"])):
            raise ValueError("Exploration directions need complete short topics")
        if "exploration_min_interval_minutes" in values and (type(values["exploration_min_interval_minutes"]) is not int or not 0 <= values["exploration_min_interval_minutes"] <= 10080):
            raise ValueError("Invalid explicit exploration interval")
        if "exploration_paused" in values and type(values["exploration_paused"]) is not bool:
            raise ValueError("Exploration pause is boolean")
        fingerprint = digest(proposal.model_dump())
        previous = conn.execute("SELECT digest,data FROM mind_habit_commands WHERE scope=? AND id=?", (self.scope.key(), command_id)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise Conflict("Habit command changed")
            return json.loads(previous[1])
        current = self.read(conn)
        if current["revision"] != proposal.expected_revision:
            raise Conflict("Conversation preferences changed")
        entries = current["entries"]
        for key, value in values.items():
            entries[key] = {"value": value, "evidence": proof, "reason": proposal.reason, "at": self.mind.clock(), "revision": current["revision"] + 1}
        result = {"state": "applied", "revision": current["revision"] + 1, "entries": entries}
        conn.execute("INSERT OR REPLACE INTO mind_conversation_habits VALUES(?,?,?)", (self.scope.key(), result["revision"], dumps(result)))
        conn.execute("INSERT INTO mind_habit_revisions VALUES(?,?,?)", (self.scope.key(), result["revision"], dumps(result)))
        conn.execute("INSERT INTO mind_habit_commands VALUES(?,?,?,?)", (self.scope.key(), command_id, fingerprint, dumps(result)))
        return result

    def update(self, request):
        proposal = HabitProposal.model_validate({k:v for k,v in request.items() if k != "command_id"})
        with self.engine.db.connect(write=True) as conn:
            return self.apply(conn, proposal, request["command_id"])

    def choose_reply(self, request):
        if request.get("action") not in {"reply", "silent", "merged"} or not request.get("input_id") or not request.get("reason"):
            raise ValueError("Reply choice needs input_id, action and a brief reason")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT source_id FROM mind_reply_inputs WHERE scope=? AND id=?", (self.scope.key(), request["input_id"])).fetchone()
            if not row:
                raise Missing("Reply choice must refer to a received owner input")
            preferences = self.read(conn)
            if request["action"] == "silent" and preferences["preferences"]["reply_choice"] != "autonomous":
                raise Conflict("Autonomous silence has not been enabled by the owner")
            if request["action"] == "merged":
                other = conn.execute("SELECT 1 FROM mind_reply_inputs WHERE scope=? AND id=?", (self.scope.key(), request.get("merged_into"))).fetchone()
                if not other or request["merged_into"] == request["input_id"]:
                    raise Conflict("Merge target must be another received input")
            value = {"input_id": request["input_id"], "action": request["action"], "reason": request["reason"],
                "merged_into": request.get("merged_into"), "at": self.mind.clock(), "source_id": row[0], "preference_revision": preferences["revision"], "state": "decided"}
            old = conn.execute("SELECT data FROM mind_reply_choices WHERE scope=? AND input_id=?", (self.scope.key(), request["input_id"])).fetchone()
            if old:
                current = json.loads(old[0])
                if any(current.get(k) != value.get(k) for k in ("action", "reason", "merged_into")):
                    raise Conflict("Reply choice already recorded for this input")
                return current
            conn.execute("INSERT INTO mind_reply_choices VALUES(?,?,?)", (self.scope.key(), request["input_id"], dumps(value)))
            return value

    def reply_status(self, input_id):
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT data FROM mind_reply_choices WHERE scope=? AND input_id=?", (self.scope.key(), input_id)).fetchone()
            return json.loads(row[0]) if row else {"input_id": input_id, "action": "reply", "state": "default"}
