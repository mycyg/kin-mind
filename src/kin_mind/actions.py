"""Durable internal stimuli. A clock crossing queues one appraisal, not a send."""

import json

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import SourceInput

from .state import project


class ActionEvents:
    def __init__(self, mind):
        self.mind = mind

    def emit(self, conn, kind, key, data):
        identifier = "action_" + digest([self.mind.scope.key(), kind, key])[:32]
        conn.execute(
            "INSERT OR IGNORE INTO mind_action_events VALUES(?,?,?,?,?,?)",
            (
                identifier,
                self.mind.scope.key(),
                kind,
                self.mind.clock(),
                "pending",
                dumps(data),
            ),
        )
        return identifier

    def configure(self, request):
        def apply(conn, state, eid):
            refs = self.mind._evidence(conn, request["evidence_ids"])
            if not refs or any(r["authority"] != "explicit" for r in refs):
                raise Conflict("Action policy requires explicit owner evidence")
            current = state["dimensions"]["initiative"]
            current.update(
                score=self.mind._initiative_value(conn, state, self.mind.clock()),
                at=self.mind.clock(),
            )
            state["action_policy"] = {
                "version": request["agent_version"],
                "trigger": "affect",
                "provider": "deepseek-flash",
                "reasoning": "max",
                "configured_at": self.mind.clock(),
                "evidence": refs,
                "reason": request["reason"],
                "event_id": eid,
            }
            state["profile"]["contact"].pop("reset", None)
            state["profile"]["contact"]["reset_policy"] = "deepseek-appraisal"
            state["profile"]["exploration"] = {
                "trigger": "curiosity",
                "threshold": 75,
                "budget_seconds": 1200,
            }
            state["profile_version"] = digest(
                [state["profile"], state["action_policy"]]
            )[:16]
            self.emit(
                conn,
                "bootstrap",
                eid,
                {
                    "evidence_ids": request["evidence_ids"],
                    "agent_version": request["agent_version"],
                    "reason": request["reason"],
                },
            )
            return {
                "state": "configured",
                "appraisal": "pending",
                "policy": state["action_policy"],
            }

        return self.mind._mutate(request, "action-policy", apply)

    def crossings(self):
        queued = []
        with self.mind.engine.db.connect(write=True) as conn:
            state = self.mind._load(conn)
            if not state.get("action_policy") or not self.mind._fresh(
                conn, state["action_policy"]["evidence"]
            ):
                return queued
            for name in ("initiative", "curiosity"):
                entry = state["dimensions"][name]
                motive = entry.get("motivation")
                threshold = state["profile"][
                    "contact" if name == "initiative" else "exploration"
                ].get("threshold", 75)
                # A high level is not a new event. Repeated reads and restarts keep
                # the same episode key; an appraisal starting high cannot self-loop.
                if not motive or not entry["score"] < threshold <= project(
                    entry, self.mind.clock()
                ):
                    continue
                if not self.mind._entry_fresh(conn, entry):
                    continue
                key = self.emit(
                    conn,
                    "drive-crossing",
                    [name, motive["episode_id"]],
                    {
                        "dimension": name,
                        "value": project(entry, self.mind.clock()),
                        "threshold": threshold,
                        "episode_id": motive["episode_id"],
                        "evidence_ids": [r["record_id"] for r in entry["evidence"]],
                        "agent_version": state["agent_version"],
                        "reason": motive["reason"],
                    },
                )
                queued.append(key)
        return queued

    def drain(self, jobs):
        with self.mind.engine.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mind_action_events WHERE scope=? AND state IN ('pending','queued') ORDER BY created_at,id LIMIT 16",
                (self.mind.scope.key(),),
            ).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            with self.mind.engine.db.connect(write=True) as conn:
                try:
                    fresh = self.mind._fresh(
                        conn, self.mind._evidence(conn, data.get("evidence_ids", []))
                    )
                except (Conflict, Missing, KeyError):
                    fresh = False
                if not fresh:
                    conn.execute(
                        "UPDATE mind_action_events SET state='needs-review' WHERE id=?",
                        (row["id"],),
                    )
                    continue
            if not data.get("job_id"):
                source = self.mind.engine.receive(
                    SourceInput(
                        namespace="mind-internal-event",
                        key=row["id"],
                        scope=self.mind.scope,
                        authority="model",
                        kind="observation",
                        session="internal-mind",
                        occurred_at=row["created_at"],
                        extract=False,
                        text=dumps({"kind": row["kind"], **data}),
                        metadata={
                            "host_event": "internal-" + row["kind"],
                            "role": "assistant",
                            "internal": True,
                            "event_id": row["id"],
                        },
                    )
                )
                ids = list(dict.fromkeys([source["id"], *data.get("evidence_ids", [])]))
                job = jobs.enqueue(
                    ids,
                    data["agent_version"],
                    origin="reflection",
                    stimulus=row["kind"],
                )
                data.update(job_id=job["id"], source_id=source["id"])
            status = jobs.status(data["job_id"])
            with self.mind.engine.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE mind_action_events SET state=?,data=? WHERE id=?",
                    (
                        "complete" if status["state"] == "complete" else "queued",
                        dumps(data),
                        row["id"],
                    ),
                )

    def review_unselected(self):
        view = self.mind.read()
        if (
            not view.get("action_policy")
            or view["action_policy"]["needs_review"]
            or view["dimensions"]["curiosity"]["value"] < 75
        ):
            return
        with self.mind.engine.db.connect(write=True) as conn:
            for d in view["desires"]:
                if (
                    d["kind"] == "explore"
                    and d["status"] == "wanted"
                    and not d["expired"]
                    and not d["needs_review"]
                    and d.get("decision_receipt", {}).get("provider") != "deepseek"
                ):
                    self.emit(
                        conn,
                        "wish-review",
                        [d["id"], d["revision"]],
                        {
                            "desire_id": d["id"],
                            "evidence_ids": [r["record_id"] for r in d["evidence"]],
                            "agent_version": view["agent_version"],
                        },
                    )

    def exploration_candidate(self):
        from datetime import timedelta

        from .habits import ConversationHabits
        from .state import timestamp
        preferences = ConversationHabits(self.mind).read()["preferences"]
        if preferences["exploration_paused"]:
            return {"state": "waiting", "reason": "owner-paused-exploration"}
        interval = preferences["exploration_min_interval_minutes"]
        if interval:
            with self.mind.engine.db.connect() as conn:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone()
                previous = conn.execute("SELECT MAX(created_at) FROM mind_explorations WHERE scope=?", (self.mind.scope.key(),)).fetchone()[0] if exists else None
            if previous and timestamp(previous)+timedelta(minutes=interval)>timestamp(self.mind.clock()):
                return {"state": "waiting", "reason": "owner-exploration-interval", "next_at": (timestamp(previous)+timedelta(minutes=interval)).isoformat()}
        view = self.mind.read()
        if (view.get("action_policy") or {}).get("needs_review"):
            return {"state": "waiting", "reason": "action-policy-needs-review"}
        score = view["dimensions"]["curiosity"]
        if score["needs_review"] or score["projected_value"] < view["exploration"].get(
            "threshold", 75
        ):
            return {
                "state": "waiting",
                "reason": "curiosity-below-threshold-or-stale",
                "curiosity": score["value"],
            }
        with self.mind.engine.db.connect() as conn:
            if self.mind._action_review_pending(conn):
                return {"state": "waiting", "reason": "action-appraisal-pending"}
        choices = [
            d
            for d in view["desires"]
            if d["kind"] == "explore"
            and d["status"] == "wanted"
            and not d["expired"]
            and not d["needs_review"]
            and not d.get("concern_needs_review")
        ]
        if view.get("action_policy"):
            choices = [
                d
                for d in choices
                if d.get("decision_receipt", {}).get("provider") == "deepseek"
            ]
        if not choices:
            return {"state": "waiting", "reason": "no-exploration-intent"}
        desire = min(choices, key=lambda d: (-d["strength"], d["created_at"], d["id"]))
        return {
            "state": "ready",
            "curiosity": score["value"],
            "desire": desire,
            "revision": view["revision"],
        }
