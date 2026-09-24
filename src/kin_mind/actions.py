"""Durable internal stimuli. A clock crossing queues one appraisal, not a send."""

import json
from datetime import timedelta

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import SourceInput

from .state import project
from .model_runtime import verified_decision


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
            from .autonomy_schema import enabled
            semantic = enabled(conn, self.mind.scope.key())
            state["action_policy"] = {
                "version": request["agent_version"],
                "trigger": "semantic-decision" if semantic else "affect",
                "provider": "deepseek-flash",
                "reasoning": "high",
                "configured_at": self.mind.clock(),
                "evidence": refs,
                "reason": request["reason"],
                "event_id": eid,
            }
            state["profile"]["contact"].pop("reset", None)
            state["profile"]["contact"]["reset_policy"] = "deepseek-appraisal"
            state["profile"]["exploration"] = {
                "trigger": "semantic-decision" if semantic else "curiosity",
                "threshold": None if semantic else 75,
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
            queued.extend(self._due_reviews(conn, state))
            from .autonomy_schema import legacy_thresholds
            if not legacy_thresholds(conn, self.mind.scope.key()):
                # Scores are context. Only due reviews and new evidence wake DS.
                return queued
            if not state.get("action_policy") or not self.mind._fresh(
                conn, state["action_policy"]["evidence"]
            ):
                return queued
            for name in ("initiative", "curiosity"):
                entry = state["dimensions"][name]
                motive = entry.get("motivation")
                threshold = state["profile"][
                    "contact" if name == "initiative" else "exploration"
                ].get("threshold") or 75
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

    def _due_reviews(self, conn, state):
        """What has run its course asks Kin to look again; nothing is changed for her (K1-02, K1-04).
        A short-term drive past its end, and a rhythm phase past its half-life with no word from 小光
        since. Each asks once, keyed by what ended."""
        from .rhythm import stamp
        from .state import motivation_expiry, timestamp
        now = self.mind.clock()
        queued = []
        for name in ("initiative", "curiosity"):
            entry = state["dimensions"].get(name) or {}
            until = motivation_expiry(entry)
            if until and timestamp(until) <= timestamp(now) and entry.get("evidence") and self.mind._entry_fresh(conn, entry):
                queued.append(self.emit(conn, "motivation-review", [name, entry["motivation"].get("episode_id"), until], {
                    "dimension": name, "ended_at": until, "reason": "This short-term drive has run its course",
                    "evidence_ids": [r["record_id"] for r in entry["evidence"]], "agent_version": state["agent_version"]}))
        from .trait_refs import links_fresh
        for desire in state["desires"].values():
            # K1-19: a wish that rested on a trait which has since moved is not ready; Kin is asked
            # once per trait revision set whether it still holds.
            if desire.get("trait_revisions") and desire["status"] in {"wanted", "waiting"} and not links_fresh(conn, self.mind, desire):
                queued.append(self.emit(conn, "wish-review", [desire["id"], "trait", sorted(desire["trait_revisions"].items())], {
                    "desire_id": desire["id"], "reason": "A trait this wish rested on was revised",
                    "evidence_ids": [r["record_id"] for r in desire.get("evidence", [])], "agent_version": state["agent_version"]}))
        expired = sorted(d["id"] for d in state["desires"].values()
                         if d["status"] in {"wanted", "waiting", "in_progress"} and timestamp(d["expires_at"]) <= timestamp(now))
        if expired:
            # One question for the wishes whose window closed unsettled; asked again only when that set changes.
            fresh = []
            for identifier in expired[:12]:
                refs = state["desires"][identifier].get("evidence") or []
                if refs and self.mind._fresh(conn, refs):
                    fresh.extend(r["record_id"] for r in refs)
            queued.append(self.emit(conn, "expired-wish-review", [expired], {
                "desire_ids": expired[:12], "reason": "These wishes passed their window without being settled",
                "evidence_ids": list(dict.fromkeys(fresh))[:24], "agent_version": state["agent_version"]}))
        rhythm = state.get("rhythm")
        if rhythm and rhythm.get("evidence") and rhythm.get("half_life_minutes"):
            ends = stamp(rhythm["at"]) + timedelta(minutes=rhythm["half_life_minutes"])
            if ends <= stamp(now) and self.mind._fresh(conn, rhythm["evidence"]):
                queued.append(self.emit(conn, "rhythm-review", [rhythm.get("event_id"), ends.isoformat()], {
                    "phase": rhythm.get("phase"), "ended_at": ends.isoformat(),
                    "reason": "The rhythm phase Kin gave has passed its half-life",
                    "evidence_ids": [r["record_id"] for r in rhythm["evidence"]], "agent_version": state["agent_version"]}))
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
            # An event follows its appraisal to the end: a job set aside or superseded closes it
            # too, instead of writing it back to `queued` where it held a drain slot for ever (K4-19).
            ended = {"complete": "complete", "superseded": "superseded", "needs-repair": "needs-review"}
            with self.mind.engine.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE mind_action_events SET state=?,data=? WHERE id=?",
                    (
                        ended.get(status["state"], "queued"),
                        dumps(data),
                        row["id"],
                    ),
                )

    def review_unselected(self):
        view = self.mind.read()
        from .autonomy_schema import enabled, legacy_thresholds, optimized
        with self.mind.engine.db.connect() as conn:
            semantic = enabled(conn, self.mind.scope.key())
            legacy = legacy_thresholds(conn, self.mind.scope.key())
            versions = optimized(conn, self.mind.scope.key(), "wish_version_review")
        if (
            not view.get("action_policy")
            or view["action_policy"]["needs_review"]
            or (legacy and view["dimensions"]["curiosity"]["value"] < 75)
        ):
            return
        with self.mind.engine.db.connect(write=True) as conn:
            from . import compat
            current = compat.stamp(self.mind, conn)
            for d in view["desires"]:
                if d["status"] != "wanted" or d["expired"] or d["needs_review"]:
                    continue
                receipt = d.get("decision_receipt", {})
                if d["kind"] == "explore" and not verified_decision(receipt):
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
                elif (
                    versions
                    and semantic
                    and verified_decision(receipt)
                    and receipt.get("compat")
                    and not compat.holds(receipt["compat"], current)
                ):
                    # A wish decided under a configuration that has since changed in what decides
                    # behaviour is not ready any more. Ask once what to do with it, per wish and
                    # configuration. A deployment that only moves agent_version asks nothing (K1-13).
                    self.emit(
                        conn,
                        "wish-review",
                        [d["id"], "compat", current["key"]],
                        {
                            "desire_id": d["id"],
                            "evidence_ids": [r["record_id"] for r in d["evidence"]],
                            "agent_version": view["agent_version"],
                            "reason": "The decision behind this wish was made before " + compat.stale_reason(receipt["compat"], current),
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
        from .autonomy_schema import enabled, legacy_thresholds
        with self.mind.engine.db.connect() as conn:
            semantic = enabled(conn, self.mind.scope.key())
            legacy = legacy_thresholds(conn, self.mind.scope.key())
        if legacy and (score["needs_review"] or score["projected_value"] < (view["exploration"].get("threshold") or 75)):
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
        with self.mind.engine.db.connect() as conn:
            choices = [d for d in choices if not self.mind._action_review_pending(conn, d)]
            from .plans import AutonomousPlans
            choices = [d for d in choices if AutonomousPlans(self.mind).linked_ready(conn, d)]
        if view.get("action_policy"):
            with self.mind.engine.db.connect() as conn:
                choices = [
                    d
                    for d in choices
                    if verified_decision(d.get("decision_receipt", {}))
                    and (not semantic or self.mind.decision_current(conn, d.get("decision_receipt", {})))
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
