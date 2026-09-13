"""Compatibility name for curiosity admission; selection receipts are not timers."""

import json

from eventmem.core.db import digest, dumps

from .actions import ActionEvents


class ExplorationCadence:
    def __init__(self, mind):
        self.mind = mind
        with mind.engine.db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mind_exploration_selections(id TEXT PRIMARY KEY,scope TEXT NOT NULL,created_at TEXT NOT NULL,next_at TEXT NOT NULL,data TEXT NOT NULL)"
            )

    def status(self):
        with self.mind.engine.db.connect() as conn:
            if (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='mind_explorations'"
                ).fetchone()
                and conn.execute(
                    "SELECT 1 FROM mind_explorations WHERE scope=? AND state='running'",
                    (self.mind.scope.key(),),
                ).fetchone()
            ):
                return {"state": "waiting", "reason": "exploration-in-progress"}
        return ActionEvents(self.mind).exploration_candidate()

    def reserve(self, agent_version, *, reason="Curiosity selected a sourced question"):
        if not isinstance(agent_version, str) or not 1 <= len(agent_version) <= 200:
            raise ValueError("A configuration version is required")
        candidate = self.status()
        if candidate["state"] != "ready":
            return candidate
        desire = candidate["desire"]
        identifier = (
            "selection_"
            + digest([self.mind.scope.key(), desire["id"], desire["revision"]])[:32]
        )
        at = self.mind.clock()
        receipt = {
            "id": identifier,
            "kind": "internal-exploration-selection",
            "agent_version": agent_version,
            "created_at": at,
            "desire_id": desire["id"],
            "desire_revision": desire["revision"],
            "reason": reason,
        }
        with self.mind.engine.db.connect(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mind_exploration_selections VALUES(?,?,?,?,?)",
                (identifier, self.mind.scope.key(), at, at, dumps(receipt)),
            )
        # The executor claims the desire and worker slot in one transaction.
        # Interrupted selection can resume with this same ID.
        return {**candidate, "selection_id": identifier, "brief": desire["content"]}

    def recent(self, limit=3):
        with self.mind.engine.db.connect() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT data FROM mind_exploration_selections WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT ?",
                    (self.mind.scope.key(), limit),
                )
            ]
