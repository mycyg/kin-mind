"""Persist exploration selection windows before invoking a model.

A declined, failed or interrupted selection still consumes its wake window.
These receipts are internal scheduler events, never owner inputs or findings.
"""

from __future__ import annotations

import json
from datetime import timedelta

from eventmem.core.db import digest, dumps

from .state import timestamp


class ExplorationCadence:
    def __init__(self, mind):
        self.mind = mind
        with mind.engine.db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mind_exploration_selections("
                "id TEXT PRIMARY KEY,scope TEXT NOT NULL,created_at TEXT NOT NULL,"
                "next_at TEXT NOT NULL,data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS mind_selection_scope "
                "ON mind_exploration_selections(scope,created_at DESC)"
            )

    def _status(self, conn, instant, interval):
        scope = self.mind.scope.key()
        selection = conn.execute(
            "SELECT id,next_at FROM mind_exploration_selections "
            "WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT 1", (scope,)
        ).fetchone()
        deadlines = [timestamp(selection["next_at"])] if selection else []
        research = None
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='mind_explorations'"
        ).fetchone():
            if conn.execute(
                "SELECT id FROM mind_explorations WHERE scope=? AND state='running'",
                (scope,),
            ).fetchone():
                return {"state": "waiting", "reason": "exploration-in-progress"}
            research = conn.execute(
                "SELECT created_at FROM mind_explorations "
                "WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT 1", (scope,)
            ).fetchone()
        if research:
            deadlines.append(timestamp(research["created_at"]) + timedelta(seconds=interval))
        if deadlines and instant < max(deadlines):
            return {
                "state": "waiting", "reason": "four-hour-exploration-cadence",
                "next_at": max(deadlines).isoformat(),
            }
        return {"state": "ready"}

    def status(self):
        interval = self.mind.read()["exploration"]["interval_seconds"]
        with self.mind.engine.db.connect() as conn:
            return self._status(conn, timestamp(self.mind.clock()), interval)

    def reserve(self, agent_version, *, reason="Scheduled exploration selection"):
        if not isinstance(agent_version, str) or not 1 <= len(agent_version) <= 200:
            raise ValueError("A configuration version is required")
        interval = self.mind.read()["exploration"]["interval_seconds"]
        with self.mind.engine.db.connect(write=True) as conn:
            instant = timestamp(self.mind.clock())
            status = self._status(conn, instant, interval)
            if status["state"] != "ready":
                return status
            created_at = instant.isoformat()
            next_at = (instant + timedelta(seconds=interval)).isoformat()
            selection_id = "selection_" + digest([self.mind.scope.key(), created_at])[:32]
            receipt = {
                "id": selection_id, "kind": "internal-exploration-selection",
                "agent_version": agent_version, "created_at": created_at,
                "next_at": next_at, "interval_seconds": interval, "reason": reason,
            }
            conn.execute(
                "INSERT INTO mind_exploration_selections VALUES(?,?,?,?,?)",
                (selection_id, self.mind.scope.key(), created_at, next_at, dumps(receipt)),
            )
            return {"state": "ready", "selection_id": selection_id, "next_at": next_at}

    def recent(self, limit=3):
        with self.mind.engine.db.connect() as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT data FROM mind_exploration_selections WHERE scope=? "
                "ORDER BY created_at DESC,id DESC LIMIT ?", (self.mind.scope.key(), limit)
            )]
