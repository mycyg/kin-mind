"""Versioned plans, semantic decisions and fenced host execution.

An appraisal may arrange or authorize work. Only a host result can settle a
Kin step. Owner participation remains a separate, sourced semantic decision.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.idempotency import record, unchanged
from eventmem.core.idempotency import stamp as fingerprint

from .autonomy_models import ActionDecision, PlanChange
from .autonomy_schema import enabled, optimized
from .state import project, timestamp


def local_time(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Singapore"))
    return dt.isoformat()


def receipt_ids(step):
    # Receipts are append-only. An owner response has no host identifier, so its
    # stable serialization identifies it.
    return [r.get("id") or r.get("run_id") or "receipt_" + digest(r)[:32] for r in step.get("receipts", [])]


def fence(entry):
    # A newer decision or an execution always changes at least one of these.
    return entry and {k: entry[k] for k in ("revision", "state", "decision_id")}


# Why a ready step may no longer run as decided; each asks for a review, not for a retry.
REVIEW_REASONS = {"configuration-changed", "new-owner-evidence", "source-needs-review", "missed-window-review-required",
                  "window-review-required", "procedure-needs-review"}
# Ledger marker: the review that took this reason finished without being able to show the plan.
UNSHOWN = "unshown:"


class AutonomousPlans:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope.key()

    def get(self, conn, identifier):
        row = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND id=?", (self.scope, identifier)).fetchone()
        if not row:
            raise Missing("Plan is missing or outside this scope")
        return json.loads(row[0])

    def _save(self, conn, plan, command):
        plan["updated_at"] = self.mind.clock()
        conn.execute("INSERT INTO mind_plans VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "revision=excluded.revision,status=excluded.status,next_review=excluded.next_review,updated_at=excluded.updated_at,data=excluded.data",
                     (plan["id"], self.scope, plan["revision"], plan["status"], plan.get("next_review_at"), plan["updated_at"], dumps(plan)))
        conn.execute("INSERT INTO mind_plan_history VALUES(?,?,?,?)", (plan["id"], plan["revision"], command, dumps(plan)))
        self.engine.db.bump(conn)

    def owner_epoch(self, conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
            return 0
        return conn.execute("SELECT COALESCE(MAX(seq),0) FROM mind_runtime_events WHERE scope=? AND kind='owner-message' "
                            "AND COALESCE(json_extract(data,'$.historical'),0)=0", (self.scope,)).fetchone()[0]

    def _target(self, conn, plan_id):
        row = conn.execute("SELECT id FROM mind_plans WHERE scope=? AND (id=? OR json_extract(data,'$.key')=?)", (self.scope, plan_id, plan_id)).fetchone()
        return self.get(conn, row[0] if row else plan_id)

    def basis(self, plan, step, epoch):
        """What a decision about this step relies on beyond the step's own revision.

        Stable JSON serialization only; strings are compared exactly as stored.
        """
        by_id = {s["id"]: s for s in plan["steps"]}
        return digest({
            "definition": {k: step.get(k) for k in ("actor", "preconditions", "depends_on", "not_before", "not_after", "completion", "owner_request_id")},
            "dependencies": [[i, by_id[i]["revision"], by_id[i]["state"], receipt_ids(by_id[i])] if i in by_id else [i, None, None, None]
                             for i in step.get("depends_on", [])],
            "receipts": receipt_ids(step), "goal": plan.get("goal"), "motivation": plan.get("motivation"),
            "evidence": [[r["record_id"], r["revision"]] for r in plan.get("evidence", [])], "owner_epoch": epoch})

    def _entry(self, plan, epoch):
        return {"revision": plan["revision"], "steps": {s["id"]: {"revision": s["revision"], "state": s["state"],
            "decision_id": s.get("decision", {}).get("id"), "strength": s.get("strength"),
            "delivery_artifacts": [a["sha256"] for a in s.get("delivery_artifacts", [])],
            "basis": self.basis(plan, s, epoch)} for s in plan["steps"]}}

    def manifest(self, conn, plans):
        """The host's record of the plan view a decision-maker was actually shown."""
        epoch = self.owner_epoch(conn)
        return {"owner_epoch": epoch, "plans": {p["id"]: self._entry(p, epoch) for p in plans}}

    def refresh_view(self, conn, view, plan, command):
        """Decisions that follow the evaluation's own plan change are made against that revision.

        An idempotent create that wrote nothing vouches for nothing the model was not shown.
        Without a recorded view the revision check alone applies, as before.
        """
        if view.get("owner_epoch") is not None and conn.execute("SELECT 1 FROM mind_plan_history WHERE id=? AND revision=? AND command_id=?",
                                                               (plan["id"], plan["revision"], command)).fetchone():
            # The owner epoch stays the one the model saw, not the one at commit.
            view["plans"][plan["id"]] = self._entry(plan, view["owner_epoch"])

    def _refs(self, conn, ids, allowed=None):
        refs = self.mind._evidence(conn, ids)
        if not refs or not self.mind._fresh(conn, refs):
            raise Conflict("Plan evidence needs review")
        if allowed is not None:
            # Two different faults used to share one message: evidence this evaluation
            # never saw is the proposal's own (the host decides it alone), while evidence
            # that moved since it was supplied is a version the next attempt can read.
            manifest = {r["record_id"]: r["revision"] for r in allowed}
            outside = next((r for r in refs if r["record_id"] not in manifest), None)
            if outside:
                raise Conflict("Plan uses evidence not supplied to this evaluation", target=outside["record_id"])
            moved = next((r for r in refs if manifest[r["record_id"]] != r["revision"]), None)
            if moved:
                raise Conflict("Plan evidence changed after it was supplied", target=moved["record_id"],
                               expected=manifest[moved["record_id"]], actual=moved["revision"])
        return refs

    def change(self, conn, proposal, command, *, allowed=None, receipt=None):
        p = PlanChange.model_validate(proposal)
        refs = self._refs(conn, p.evidence_ids, allowed)
        identifier = p.id or "plan_" + digest([self.scope, p.desire_id or p.key])[:32]
        if p.action == "create":
            existing = conn.execute("SELECT data FROM mind_plans WHERE id=? AND scope=?", (identifier, self.scope)).fetchone()
            if existing:
                return json.loads(existing[0])
            plan = {"id": identifier, "key": p.key, "revision": 1, "status": "active", "created_at": self.mind.clock(),
                    "desire_id": p.desire_id, "steps": [], "timezone": "Asia/Singapore"}
        else:
            plan = self.get(conn, identifier)
            if plan["revision"] != p.expected_revision:
                raise Conflict("Plan changed during evaluation", target=identifier,
                               expected=p.expected_revision, actual=plan["revision"])
            if plan["status"] in {"canceled", "completed"}:
                raise Conflict("Terminal plans preserve their history; create a new goal")
            plan["revision"] += 1
        if p.action == "pause":
            plan["status"] = "paused"
        elif p.action == "cancel":
            plan["status"] = "canceled"
        elif p.action == "resume":
            plan["status"] = "active"
        for key in ("goal", "motivation"):
            if getattr(p, key) is not None:
                plan[key] = getattr(p, key)
        if p.steps is not None:
            previous = {s["id"]: s for s in plan["steps"]}
            updated = []
            for s in p.steps:
                definition = s.model_dump()
                definition.update(not_before=local_time(s.not_before), not_after=local_time(s.not_after))
                if s.not_before and s.not_after and timestamp(definition["not_before"]) >= timestamp(definition["not_after"]):
                    raise ValueError("Time window must end after it begins")
                old = previous.get(s.id)
                if old and old["state"] in {"running", "completed", "unconfirmed"}:
                    if any(old.get(k) != v for k, v in definition.items()):
                        raise Conflict("Executed or reserved steps cannot be overwritten")
                    updated.append(old)
                else:
                    updated.append({**definition, "state": old.get("state", "pending") if old else "pending",
                                    "revision": old.get("revision", 0) + 1 if old else 1,
                                    "owner_status": old.get("owner_status", "proposed") if old else "proposed",
                                    "receipts": old.get("receipts", []) if old else []})
            removed = set(previous) - {s.id for s in p.steps}
            if any(previous[i]["state"] in {"running", "completed", "unconfirmed"} for i in removed):
                raise Conflict("Keep executed steps in plan history")
            plan["steps"] = updated
        review_at = local_time(p.next_review_at)
        if not review_at or timestamp(review_at) <= timestamp(self.mind.clock()):
            # As when omitted: review this revision now. A time already past may have fired
            # before, and a due review wakes only once per distinct time.
            review_at = self.mind.clock()
        plan.update(evidence=refs, reason=p.reason, next_review_at=review_at,
                    agent_version=self.mind._load(conn)["agent_version"], decision_receipt=receipt,
                    last_reviewed_owner_epoch=self.owner_epoch(conn))
        # Any plan revision revokes all prior execution permissions, not results.
        for step in plan["steps"]:
            step.pop("decision", None)
        self._save(conn, plan, command)
        return plan

    def manage(self, request):
        command = request["command_id"]
        raw = PlanChange.model_validate({k: v for k, v in request.items() if k != "command_id"}).model_dump()
        key = "plan_command_" + digest([self.scope, command])[:32]
        with self.engine.db.connect(write=True) as conn:
            # The expected revision fences the change; it is not what the change is. A client
            # that retries the same change after rereading the plan gets its original receipt.
            stamp = fingerprint("plan-change", self.scope, raw,
                                enabled=optimized(conn, self.scope, "idempotency_fingerprint"))
            prior = conn.execute("SELECT digest,result FROM commands WHERE id=?", (key,)).fetchone()
            if prior:
                if not unchanged(conn, stamp, key, legacy=(prior[0], digest(raw))):
                    raise Conflict("A command ID cannot be reused for a different change",
                                   kind="runtime", code="payload-changed", target=command)
                return json.loads(prior[1])
            plan = self.change(conn, raw, command)
            conn.execute("INSERT INTO commands VALUES(?,?,?)", (key, digest(raw), dumps(plan)))
            record(conn, stamp, key, self.mind.clock())
            return plan

    def decide(self, conn, proposal, command, receipt, allowed, *, unchanged_view=False, rebased=False):
        """unchanged_view: the caller proved the step and its basis equal the view the model was shown.
        rebased: that proof, not the model's stale expected_revision, fenced this decision."""
        d = ActionDecision.model_validate(proposal)
        if receipt.get("provider") != "deepseek" or receipt.get("reasoning") != "high":
            raise Conflict("Autonomous action requires a verified DeepSeek high decision")
        plan = self._target(conn, d.plan_id)
        if plan["revision"] != d.expected_revision or plan["status"] != "active":
            raise Conflict("Decision plan revision is no longer active", target=plan["id"],
                           expected=d.expected_revision, actual=plan["revision"])
        refs = self._refs(conn, d.evidence_ids, allowed)
        step = next((s for s in plan["steps"] if s["id"] == d.step_id), None)
        if not step or step["state"] in {"running", "completed", "unconfirmed", "abandoned"}:
            raise Conflict("Step is not available for this decision")
        available_artifacts = {a["sha256"]: a for s in plan["steps"] if s["state"] == "completed"
            for r in s["receipts"] if r.get("verified") for a in r.get("artifacts", [])}
        if d.artifact_hashes and (step["actor"] != "contact" or set(d.artifact_hashes) - set(available_artifacts)):
            raise Conflict("Delivery selection requires host-verified artifacts from completed steps")
        artifacts = [available_artifacts[h] for h in dict.fromkeys(d.artifact_hashes)]
        version, epoch = self.mind._load(conn)["agent_version"], self.owner_epoch(conn)
        stored = step.get("decision") or {}
        record_only = (unchanged_view and d.action == "wait" and step["state"] == "waiting" and stored.get("action") == "wait"
                       and (d.strength is None or d.strength == step.get("strength"))
                       and [a["sha256"] for a in artifacts] == [a["sha256"] for a in step.get("delivery_artifacts", [])]
                       and d.procedure_ids == stored.get("procedure_ids", []) and d.conditions_met == stored.get("conditions_met", [])
                       and optimized(conn, self.scope, "plan_review_record_only"))
        now = timestamp(self.mind.clock())
        review_at = local_time(d.next_review_at) or (now + timedelta(minutes=20)).isoformat()
        if timestamp(review_at) <= now:
            # A due review wakes once per distinct time. A time already past may be one that has
            # fired, which would leave this plan without any due review: treat it as omitted.
            # The decision and the audit row keep the model's own value.
            review_at = (now + timedelta(minutes=20)).isoformat()
        if rebased and plan.get("next_review_at") and timestamp(plan["next_review_at"]) < timestamp(review_at):
            # This decision never saw the intervening revision; it cannot postpone that revision's
            # review. Where that review was asked for at once, ask at once again, as a time of its own.
            review_at = plan["next_review_at"] if timestamp(plan["next_review_at"]) > now else self.mind.clock()
        if record_only:
            # The same wait again is a review, not a new plan revision: nothing another
            # evaluation holds is invalidated, and the stored decision stays current.
            previous = plan.get("next_review_at")
            plan.update(next_review_at=review_at, last_reviewed_owner_epoch=epoch, agent_version=version)
            self._record_review(conn, plan, command, "unchanged-wait", {"step_id": step["id"], "step_revision": step["revision"],
                "decision_id": stored.get("id"), "decision": d.model_dump(), "evidence": refs, "receipt": receipt, "owner_epoch": epoch,
                "agent_version": version, "previous_next_review_at": previous, "next_review_at": review_at, "rebased": rebased})
            return plan
        step["delivery_artifacts"] = artifacts
        if d.action.startswith("owner_"):
            if step["actor"] != "owner" or not any(r["authority"] == "explicit" and r.get("metadata", {}).get("role") == "user" for r in refs):
                raise Conflict("Owner participation requires actual owner evidence")
            step["owner_status"] = d.action.removeprefix("owner_")
            step["state"] = {"owner_accepted": "waiting", "owner_completed": "completed", "owner_declined": "abandoned"}[d.action]
            step["receipts"].append({"kind": "owner_response", "status": step["owner_status"], "evidence": refs, "decision_receipt": receipt})
        elif d.action == "abandon":
            step["state"] = "abandoned"
        elif d.action == "wait":
            step["state"] = "waiting"
        elif step["actor"] == "owner":
            raise Conflict("The host cannot execute on the owner's behalf")
        else:
            if set(step["preconditions"]) - set(d.conditions_met):
                raise Conflict("DeepSeek must explicitly account for each precondition")
            from .procedures import Procedures
            for pid in d.procedure_ids:
                Procedures(self.mind).require_current(conn, pid)
            step["state"] = "ready"
        plan["revision"] += 1
        step["revision"] += 1
        if d.strength is not None:
            step["strength"] = d.strength
        step["decision"] = {**d.model_dump(), "id": "decision_" + digest([command, plan["id"], step["id"]])[:32],
                            "plan_revision": plan["revision"], "step_revision": step["revision"], "evidence": refs,
                            "owner_epoch": epoch, "receipt": receipt, "at": self.mind.clock(),
                            "agent_version": version}
        plan["last_reviewed_owner_epoch"] = epoch
        plan["next_review_at"] = review_at
        # tick() compares this with the state's version; change() is not the only reviewer.
        plan["agent_version"] = version
        # Other independent decisions are re-fenced to this atomic plan revision.
        for other in plan["steps"]:
            if other.get("decision"):
                other["decision"]["plan_revision"] = plan["revision"]
        if all(s["state"] in {"completed", "abandoned"} for s in plan["steps"]):
            plan["status"] = "completed"
        self._save(conn, plan, command)
        return plan

    def _record_review(self, conn, plan, command, kind, detail):
        """A review that is not a revision: no history row, whose key is (id, revision)."""
        conn.execute("UPDATE mind_plans SET next_review=?,data=? WHERE id=?", (plan.get("next_review_at"), dumps(plan), plan["id"]))
        conn.execute("INSERT INTO mind_plan_reviews VALUES(?,?,?,?,?,?,?)",
                     (plan["id"], command, self.scope, kind, plan["revision"], self.mind.clock(), dumps(detail)))
        self.engine.db.bump(conn)

    def decide_batch(self, conn, decisions, command, receipt, allowed, view, *, version=None, job_id=None):
        """Apply one evaluation's decisions against the plan view it was actually shown.

        A decision whose step or basis moved after that view is held, never raised: the
        rest of the evaluation commits and one coalesced review asks again. command is
        the prefix of each decision's command ID.
        """
        shown, bases, held, reviews = (view or {}).get("plans", {}), {}, [], {}
        for index, decision in enumerate(decisions):
            d = ActionDecision.model_validate(decision)
            plan = self._target(conn, d.plan_id)
            base = bases.get(plan["id"])
            if base is None:
                # Every decision of this batch is judged against the same pre-batch plan, so an
                # earlier decision of the batch is not mistaken for someone else's change.
                base = bases[plan["id"]] = {"expected": d.expected_revision, "revision": plan["revision"],
                                            "steps": self._entry(plan, self.owner_epoch(conn))["steps"]}
            elif d.expected_revision != base["expected"]:
                raise Conflict("Inconsistent plan decision base revision", target=plan["id"],
                               expected=base["expected"], actual=d.expected_revision)
            stale = base["expected"] != base["revision"]
            seen, actual = shown.get(plan["id"], {}).get("steps", {}).get(d.step_id), base["steps"].get(d.step_id)
            if plan["status"] != "active":
                code = "plan-inactive"
            elif not seen:
                # No host record of what was shown: only an exact revision is proof, as before.
                code = "view-missing" if stale else None
            elif fence(seen) != fence(actual):
                code = "step-touched"
            elif seen["basis"] != actual["basis"]:
                code = "basis-changed"
            else:
                code = None
            if code:
                held.append({"plan_id": plan["id"], "step_id": d.step_id, "code": code,
                             "expected": {"plan_revision": d.expected_revision, "step": fence(seen), "basis": seen and seen["basis"]},
                             "actual": {"plan_revision": base["revision"], "status": plan["status"], "step": fence(actual), "basis": actual and actual["basis"]}})
                if plan["status"] == "active":
                    reviews.setdefault(plan["id"], []).append({"step_id": d.step_id, "code": code})
                continue
            self.decide(conn, d.model_copy(update={"expected_revision": plan["revision"]}), command + str(index), receipt, allowed,
                        unchanged_view=bool(seen), rebased=stale)
        if reviews:
            from .actions import ActionEvents
            actions = ActionEvents(self.mind)
            for identifier, codes in reviews.items():
                # Only a review that has yet to read the plan can carry the question: this evaluation's
                # own event is about to finish, and a running one may have read the plan before this commit.
                if not self._review_in_flight(conn, identifier, exclude_job=job_id)[1]:
                    self._emit_review(conn, actions, self.get(conn, identifier), [identifier, "held", command], "held-decision",
                                      version or self.mind._load(conn)["agent_version"], held=codes)
        return held

    def register_review(self, conn, plan_ids, command, receipt, version=None):
        """A completed review saw these plans under this configuration.

        Held or absent decisions must not leave the plan recorded under a configuration that
        has already reviewed it; the wake-up ledger only keeps that from waking a review twice.
        """
        version, registered = version or self.mind._load(conn)["agent_version"], []
        for identifier in plan_ids:
            row = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND id=? AND status='active'", (self.scope, identifier)).fetchone()
            plan = json.loads(row[0]) if row else None
            if not plan or plan.get("agent_version") == version:
                continue
            previous, plan["agent_version"] = plan.get("agent_version"), version
            self._record_review(conn, plan, command, "version-seen", {"previous_agent_version": previous, "agent_version": version, "receipt": receipt})
            registered.append(identifier)
        return registered

    def _dead_reviews(self, conn, plan_id):
        """Review events of this plan that will never read it; the reasons they answered for are open again.

        drain() sets aside a pending or queued event once its evidence is no longer current, and
        its evaluation can neither load nor commit on that evidence. An evaluation that already
        finished, or was set aside for repair, did answer: its reasons stay answered.
        """
        jobs, dead = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_appraisals'").fetchone(), set()
        for row in conn.execute("SELECT id,state,data FROM mind_action_events WHERE scope=? AND kind='plan-review' AND state IN ('pending','queued','needs-review') "
                                "AND json_extract(data,'$.plan_id')=?", (self.scope, plan_id)).fetchall():
            data = json.loads(row["data"])
            state = conn.execute("SELECT state FROM mind_appraisals WHERE id=?", (data["job_id"],)).fetchone() if jobs and data.get("job_id") else None
            if state and state[0] not in {"pending", "running"}:
                continue
            try:
                current = row["state"] != "needs-review" and self.mind._fresh(conn, self.mind._evidence(conn, data.get("evidence_ids", [])))
            except (Conflict, Missing, KeyError):
                current = False
            if not current:
                dead.add(row["id"])
        return dead

    def _review_in_flight(self, conn, plan_id, *, exclude_job=None, dead=None):
        """(event id, unstarted) of this plan's live pending/queued review; (None, False) without one.

        unstarted: its evaluation has yet to read anything (not drained, or its job is pending
        with no context kept from an earlier attempt), so it will see every fact that is true
        now. A running evaluation may have read the plan before that fact. So may a retry that
        keeps its frozen context: it reads the plan again, but still discards its plan decisions
        over any owner message newer than that context. An unstarted review is preferred.
        """
        jobs, started = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_appraisals'").fetchone(), None
        dead = self._dead_reviews(conn, plan_id) if dead is None else dead
        for row in conn.execute("SELECT id,data FROM mind_action_events WHERE scope=? AND kind='plan-review' AND state IN ('pending','queued') "
                                "AND json_extract(data,'$.plan_id')=? ORDER BY created_at,id", (self.scope, plan_id)).fetchall():
            if row["id"] in dead:
                continue
            job = json.loads(row["data"]).get("job_id")
            if not job:
                return row["id"], True
            if job == exclude_job:
                continue
            state = conn.execute("SELECT state,json_extract(data,'$.frozen_memory_context') IS NOT NULL FROM mind_appraisals WHERE id=?", (job,)).fetchone() if jobs else None
            if not state or state[0] not in {"pending", "running"}:
                # drain() has yet to close an event whose appraisal finished or was set aside.
                continue
            if state[0] == "pending" and not state[1]:
                return row["id"], True
            started = started or row["id"]
        return started, False

    def _emit_review(self, conn, actions, plan, key, reason, version, **extra):
        # Invalid old evidence is retained in the plan, not passed as
        # authoritative current evidence to the action queue.
        sources = []
        for ref in plan["evidence"]:
            try:
                if self.mind._fresh(conn, self.mind._evidence(conn, [ref["record_id"]])):
                    sources.append(ref["record_id"])
            except (Missing, Conflict):
                pass
        return actions.emit(conn, "plan-review", key, {"plan_id": plan["id"], "plan_revision": plan["revision"], "evidence_ids": sources,
            "agent_version": version, "reason": reason, **extra})

    def waiting_reason(self, conn, plan, step):
        if not enabled(conn, self.scope, "autonomous_plans"):
            return "plans-disabled"
        if plan["status"] != "active":
            return "plan-" + plan["status"]
        if step["state"] != "ready":
            return "step-" + step["state"]
        decision = step.get("decision", {})
        if decision.get("action") != "execute" or decision.get("plan_revision") != plan["revision"]:
            return "decision-required"
        if decision.get("agent_version") != self.mind._load(conn)["agent_version"]:
            return "configuration-changed"
        if decision.get("owner_epoch") != self.owner_epoch(conn):
            return "new-owner-evidence"
        if not self.mind._fresh(conn, plan["evidence"]) or not self.mind._fresh(conn, decision["evidence"]):
            return "source-needs-review"
        now = timestamp(self.mind.clock())
        if timestamp(plan["next_review_at"]) <= now:
            return "review-due"
        if step.get("not_before") and timestamp(step["not_before"]) > now:
            return "time-window-not-open"
        if step.get("not_before") and timestamp(decision["at"]) < timestamp(step["not_before"]):
            return "window-review-required"
        if step.get("not_after") and timestamp(step["not_after"]) <= now:
            return "missed-window-review-required"
        by_id = {s["id"]: s for s in plan["steps"]}
        if any(by_id[i]["state"] != "completed" or not by_id[i]["receipts"] for i in step["depends_on"]):
            return "dependency-unfinished"
        from .procedures import Procedures
        for pid in decision.get("procedure_ids", []):
            try:
                Procedures(self.mind).require_current(conn, pid)
            except (Conflict, Missing):
                return "procedure-needs-review"
        return None

    def review_target(self, job_id):
        """The review event this appraisal answers and the plan it was woken for; None for any other appraisal."""
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT id,data FROM mind_action_events WHERE scope=? AND kind='plan-review' AND json_extract(data,'$.job_id')=? "
                               "ORDER BY created_at,id LIMIT 1", (self.scope, job_id)).fetchone()
        return {"event_id": row["id"], "plan_id": json.loads(row["data"]).get("plan_id")} if row else None

    def reopen_wakeups(self, conn, target):
        """A review that could not show its own plan answered for nothing: the reasons it took are open again.

        Deleting them would not reopen anything. The same reasons give the same event key, and that
        event exists and is complete. Kept under a marker no event carries, tick() finds them open and
        takes them over, which is an event of its own.
        """
        if not conn.execute("SELECT 1 FROM mind_plans WHERE scope=? AND id=?", (self.scope, target["plan_id"])).fetchone():
            return conn.execute("DELETE FROM mind_plan_wakeups WHERE scope=? AND plan_id=?", (self.scope, target["plan_id"])).rowcount
        return conn.execute("UPDATE mind_plan_wakeups SET event_id=? WHERE scope=? AND plan_id=? AND event_id=?",
                            (UNSHOWN + target["event_id"], self.scope, target["plan_id"], target["event_id"])).rowcount

    def read(self, identifier=None, *, status=None, cursor=0, limit=24, history=False, manifest=False, first=None):
        with self.engine.db.connect() as conn:
            if identifier:
                plans = [self.get(conn, identifier)]
            else:
                rows = conn.execute("SELECT data FROM mind_plans WHERE scope=? " + ("AND status=? " if status else "") +
                                    "ORDER BY next_review,id LIMIT ? OFFSET ?", (self.scope, *([status] if status else []), min(100, max(1, limit)), max(0, cursor))).fetchall()
                plans = [json.loads(r[0]) for r in rows]
            taken = len(plans)
            lead = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND id=? AND status='active'", (self.scope, first)).fetchone() if first and not identifier else None
            if lead:
                # The plan a review was woken for leads its own view, however many plans are due before it.
                # The window keeps its size, and the cursor counts only plans taken in their usual order.
                usual = [p for p in plans if p["id"] != first]
                rest = usual[:min(100, max(1, limit)) - 1]
                taken, plans = len(rest) + (len(usual) < len(plans)), [json.loads(lead[0]), *rest]
            for plan in plans:
                plan["needs_review"] = not self.mind._fresh(conn, plan["evidence"])
                for step in plan["steps"]:
                    step["waiting_reason"] = self.waiting_reason(conn, plan, step)
                if history:
                    plan["history"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_plan_history WHERE id=? ORDER BY revision", (plan["id"],))]
            # Same snapshot as the plans themselves, so it describes exactly what is returned.
            shown = self.manifest(conn, plans) if manifest else None
        result = {"plans": plans, "next_cursor": cursor + taken if len(plans) == limit else None, "timezone": "Asia/Singapore"}
        return {**result, "manifest": shown} if manifest else result

    def _source_state(self, conn, ref):
        """What freshness of this evidence rests on: a later state of it is a different fact."""
        current = conn.execute("SELECT revision,deleted FROM records WHERE id=? AND scope=?", (ref["record_id"], self.scope)).fetchone()
        # A newer version of the same source leaves the old record untouched, and a validity
        # window ends without any write; the record's own version shows neither.
        newer = conn.execute("SELECT id FROM sources WHERE namespace=? AND source_key=? AND scope=? AND deleted=0 AND id<>? AND received_at>"
                             "(SELECT received_at FROM sources WHERE id=?) ORDER BY received_at DESC,id DESC LIMIT 1",
                             (ref["namespace"], ref["source_key"], self.scope, ref["source_id"], ref["source_id"])).fetchone()
        return [ref["record_id"], list(current) if current else None, newer[0] if newer else None, self.mind._fresh(conn, [ref])]

    def _wakeups(self, conn, plan, epoch, version):
        """Every reason to look at this plan again that is true now, each as a stable key.

        A key names the fact itself, never the plan revision or a count of reviews: a review
        that leaves the fact as it was must not wake another one.
        """
        reasons = []
        if plan.get("next_review_at") and timestamp(plan["next_review_at"]) <= timestamp(self.mind.clock()):
            reasons.append(["due", plan["next_review_at"]])
        if not self.mind._fresh(conn, plan["evidence"]):
            reasons.append(["sources", [self._source_state(conn, ref) for ref in plan["evidence"]]])
        if plan.get("last_reviewed_owner_epoch") != epoch:
            reasons.append(["owner_epoch", epoch])
        if plan.get("agent_version") != version:
            reasons.append(["agent_version", version])
        for step in plan["steps"]:
            reason = self.waiting_reason(conn, plan, step) if step["state"] == "ready" else None
            if reason in REVIEW_REASONS:
                reasons.append(["step", step["id"], reason, step["revision"]])
        return reasons

    def tick(self, actions):
        """Clock/input changes enqueue one versioned review, never a catch-up send.

        Edge-triggered: a reason wakes a review once, when no review has been started for it.
        A condition that merely stays true after its review wakes nothing.
        """
        emitted = []
        with self.engine.db.connect(write=True) as conn:
            if not enabled(conn, self.scope, "autonomous_plans"):
                return emitted
            epoch, version = self.owner_epoch(conn), self.mind._load(conn)["agent_version"]
            for row in conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status='active'", (self.scope,)).fetchall():
                plan = json.loads(row[0])
                reasons = self._wakeups(conn, plan, epoch, version)
                if not reasons:
                    continue
                answered = dict(conn.execute("SELECT key_digest,event_id FROM mind_plan_wakeups WHERE scope=? AND plan_id=?", (self.scope, plan["id"])).fetchall())
                dead = self._dead_reviews(conn, plan["id"])
                new = [r for r in reasons if answered.get(digest(r)) is None or answered[digest(r)] in dead or answered[digest(r)].startswith(UNSHOWN)]
                review, unstarted = self._review_in_flight(conn, plan["id"], dead=dead)
                if new and not review:
                    # Taking over from a review that never read the plan is an event of its own.
                    taken = sorted({answered[digest(r)] for r in new if digest(r) in answered})
                    review, unstarted = self._emit_review(conn, actions, plan, [plan["id"], "wake", sorted(digest(r) for r in new), taken],
                        "due-or-changed-evidence", version, wakeups=reasons), True
                if new and unstarted:
                    # That review has yet to read the plan, so it sees every reason true now. A running
                    # one may have read it earlier: its reasons stay open and wake a review of their own.
                    for reason in new:
                        conn.execute("INSERT OR REPLACE INTO mind_plan_wakeups VALUES(?,?,?,?,?,?)",
                                     (self.scope, plan["id"], digest(reason), dumps(reason), self.mind.clock(), review))
                if review:
                    emitted.append(review)
        return emitted

    def claim(self, actor, owner, *, foreground=False):
        if actor not in {"create", "explore", "contact"}:
            raise ValueError("Only Kin executors can claim a step")
        with self.engine.db.connect(write=True) as conn:
            if foreground or not enabled(conn, self.scope, "autonomous_plans") or (actor == "create" and not enabled(conn, self.scope, "creative_execution")):
                return {"state": "waiting", "reason": "foreground-or-feature-disabled"}
            from .model_lanes import foreground_active
            # Machine-wide, like every other yield to the user: a scope isolates data, not attention.
            if foreground_active(conn, self.scope):
                return {"state": "waiting", "reason": "user-work-priority"}
            if actor == "create" and not enabled(conn, self.scope, "records"):
                return {"state": "waiting", "reason": "result-memory-disabled"}
            active = conn.execute("SELECT id FROM mind_plan_runs WHERE scope=? AND actor=? AND state IN ('running','unconfirmed')", (self.scope, actor)).fetchone()
            if active:
                return {"state": "waiting", "reason": "executor-reserved", "run_id": active[0]}
            rows = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status='active' ORDER BY next_review,id", (self.scope,)).fetchall()
            for row in rows:
                plan = json.loads(row[0])
                for step in plan["steps"]:
                    if step["actor"] != actor or self.waiting_reason(conn, plan, step):
                        continue
                    run_id = "plan_run_" + digest([self.scope, plan["id"], step["id"], step["revision"]])[:32]
                    attempt = {"id": run_id, "plan_id": plan["id"], "step_id": step["id"], "plan_revision": plan["revision"],
                               "step_revision": step["revision"], "actor": actor, "decision": step["decision"], "state": "running",
                               "started_at": self.mind.clock(), "owner": owner, "fence": 1}
                    conn.execute("INSERT INTO mind_plan_runs VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, self.scope, plan["id"], step["id"], actor, "running", time.time() + 90, owner, 1, dumps(attempt)))
                    step.update(state="running", run_id=run_id)
                    plan["revision"] += 1
                    self._save(conn, plan, "claim:" + run_id)
                    return {"state": "claimed", "run": attempt, "plan": plan, "step": step}
        return {"state": "waiting", "reason": "no-current-decision"}

    def renew(self, run_id, owner, fence):
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM mind_plan_runs WHERE scope=? AND id=?", (self.scope, run_id)).fetchone()
            if not row or row["owner"] != owner or row["fence"] != fence or row["state"] != "running" or row["lease_until"] <= time.time():
                raise Conflict("Execution lease expired or was replaced")
            plan = self.get(conn, row["plan_id"])
            decision = json.loads(row["data"])["decision"]
            step = next(s for s in plan["steps"] if s["id"] == row["step_id"])
            if (plan["status"] != "active" or step.get("decision", {}).get("id") != decision["id"]
                    or decision["agent_version"] != self.mind._load(conn)["agent_version"]
                    or decision["owner_epoch"] != self.owner_epoch(conn)
                    or not self.mind._fresh(conn, decision["evidence"]) or not self.mind._fresh(conn, plan["evidence"])):
                return {"state": "interrupt", "reason": "plan-or-evidence-changed"}
            conn.execute("UPDATE mind_plan_runs SET lease_until=? WHERE id=?", (time.time() + 90, run_id))
            return {"state": "renewed"}

    def settle(self, run_id, owner, fence, *, state, result):
        """Host-only: verified result manifests, never a model 'done' claim."""
        if state not in {"completed", "interrupted", "failed", "unconfirmed"}:
            raise ValueError("Unsupported executor result")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM mind_plan_runs WHERE scope=? AND id=?", (self.scope, run_id)).fetchone()
            if not row or row["owner"] != owner or row["fence"] != fence:
                raise Conflict("Execution fence mismatch", target=run_id, expected=fence,
                               actual=row["fence"] if row else None)
            run = json.loads(row["data"])
            if row["state"] != "running":
                if run.get("result") == result and row["state"] == state:
                    return run
                raise Conflict("An executor outcome cannot be overwritten")
            if row["lease_until"] <= time.time() and state in {"completed", "unconfirmed"}:
                raise Conflict("Late executor result is isolated")
            if state == "completed" and not (result.get("verified") is True and result.get("source_id")):
                raise Conflict("Completion requires a verified host result source")
            if result.get("source_id"):
                self._refs(conn, [result["source_id"]])
            plan = self.get(conn, row["plan_id"])
            step = next(s for s in plan["steps"] if s["id"] == row["step_id"])
            if step.get("run_id") != run_id:
                raise Conflict("Step has another execution")
            if state == "completed" and (plan["status"] != "active" or step.get("decision", {}).get("id") != run["decision"]["id"]
                    or run["decision"]["owner_epoch"] != self.owner_epoch(conn)
                    or not self.mind._fresh(conn, run["decision"]["evidence"])):
                raise Conflict("Completion lost its current decision or evidence")
            run.update(state=state, result=result, finished_at=self.mind.clock())
            conn.execute("UPDATE mind_plan_runs SET state=?,lease_until=0,data=? WHERE id=?", (state, dumps(run), run_id))
            step.update(state="completed" if state == "completed" else "unconfirmed" if state == "unconfirmed" else "waiting",
                        revision=step["revision"] + 1)
            step.pop("decision", None)
            step["receipts"].append({"run_id": run_id, "state": state, **result})
            plan.update(revision=plan["revision"] + 1, next_review_at=self.mind.clock())
            if all(s["state"] in {"completed", "abandoned"} for s in plan["steps"]):
                plan["status"] = "completed"
            self._save(conn, plan, "settle:" + run_id)
            return run

    def recover(self, *, workers_stopped=False):
        if not workers_stopped:
            raise Conflict("Recovery requires verified termination of previous executors")
        recovered = []
        with self.engine.db.connect(write=True) as conn:
            for row in conn.execute("SELECT * FROM mind_plan_runs WHERE scope=? AND state='running'", (self.scope,)).fetchall():
                run = json.loads(row["data"])
                # Contact delivery may have happened before the crash. Preserve
                # uncertainty until the existing transport reconciles receipts.
                state = "unconfirmed" if row["actor"] == "contact" else "interrupted"
                run.update(state=state, reason="host-restart", checkpoint_retained=True, fence=row["fence"] + 1)
                conn.execute("UPDATE mind_plan_runs SET state=?,lease_until=0,fence=fence+1,data=? WHERE id=?", (state, dumps(run), row["id"]))
                plan = self.get(conn, row["plan_id"])
                step = next(s for s in plan["steps"] if s["id"] == row["step_id"])
                step.update(state="unconfirmed" if state == "unconfirmed" else "waiting", revision=step["revision"] + 1)
                step.pop("decision", None)
                plan.update(revision=plan["revision"] + 1, next_review_at=self.mind.clock())
                self._save(conn, plan, "recover:" + row["id"])
                recovered.append(row["id"])
        return {"recovered": recovered}

    def migrate_desires(self):
        """Preserve existing identities; migration grants no execution decision."""
        migrated = []
        with self.engine.db.connect(write=True) as conn:
            current = self.mind._load(conn)
            for desire in current["desires"].values():
                if desire["status"] not in {"wanted", "waiting", "in_progress"} or not self.mind._fresh(conn, desire["evidence"]):
                    continue
                if timestamp(desire["expires_at"]) <= timestamp(self.mind.clock()):
                    continue
                plan = self.change(conn, {"action": "create", "key": desire["id"], "desire_id": desire["id"],
                    "goal": desire["content"], "motivation": desire.get("reason") or "Continue an existing sourced wish",
                    "reason": "Identity-preserving migration; execution awaits review", "evidence_ids": [r["record_id"] for r in desire["evidence"]],
                    "steps": [{"id": "continue", "actor": desire["kind"], "goal": desire["content"], "completion": desire["completion"]}]}, "migrate:" + desire["id"])
                migrated.append(plan["id"])
        return {"plan_ids": migrated}

    def sync_wishes(self):
        """Bridge current decisions to the existing contact/research ledgers."""
        from .state import DesireChange
        created = []
        with self.engine.db.connect(write=True) as conn:
            if not enabled(conn, self.scope, "autonomous_plans"):
                return created
            state = self.mind._load(conn)
            for row in conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status IN ('active','completed')", (self.scope,)).fetchall():
                plan = json.loads(row[0])
                for step in plan["steps"]:
                    decision = step.get("decision", {})
                    if step["actor"] not in {"contact", "explore"} or not decision:
                        continue
                    ready = not self.waiting_reason(conn, plan, step)
                    if not ready:
                        if decision.get("action") not in {"wait", "abandon"}:
                            continue
                        if (decision.get("plan_revision") != plan["revision"]
                            or decision.get("agent_version") != state["agent_version"]
                            or decision.get("owner_epoch") != self.owner_epoch(conn)
                            or not self.mind._fresh(conn, plan["evidence"] + decision["evidence"])):
                            continue
                    did = step.get("desire_id") or (plan.get("desire_id") if len(plan["steps"]) == 1 else None)
                    desire = state["desires"].get(did)
                    if not ready and not desire:
                        continue
                    if desire and desire.get("plan_decision_id") == step["decision"]["id"]:
                        continue
                    if desire and desire["status"] in {"in_progress", "completed", "abandoned"}:
                        continue
                    strength = step.get("strength")
                    if strength is None:
                        dimension = "initiative" if step["actor"] == "contact" else "curiosity"
                        strength = desire["strength"] if desire else round(project(state["dimensions"][dimension], self.mind.clock()))
                    command = step["decision"]["id"] + ":wish"
                    args = dict(command_id=command, agent_version=state["agent_version"], expected_revision=state["revision"],
                        evidence_ids=[r["record_id"] for r in step["decision"]["evidence"]], reason=step["decision"]["reason"],
                        content=step["goal"], topic=plan["goal"][:500], kind=step["actor"], strength=strength,
                        expires_at=plan["next_review_at"], completion=step["completion"])
                    if desire:
                        args.update(action="update", desire_id=did)
                    else:
                        args.update(action="create")
                    result = self.mind._apply_desire(conn, state, DesireChange(**args), command)
                    did = result["desire_id"]
                    status = "wanted" if ready else "abandoned" if decision["action"] == "abandon" else "waiting"
                    state["desires"][did].update(status=status, plan_id=plan["id"], plan_step_id=step["id"],
                        plan_decision_id=step["decision"]["id"], decision_receipt=step["decision"]["receipt"],
                        delivery_artifacts=step.get("delivery_artifacts", []))
                    step["desire_id"] = did
                    # Linking a host delivery handle is not a semantic revision.
                    conn.execute("UPDATE mind_plans SET data=? WHERE id=?", (dumps(plan), plan["id"]))
                    created.append(did)
            if created:
                self.mind._retarget(conn, state, self.mind.clock())
                state.update(revision=state["revision"] + 1, updated_at=self.mind.clock())
                self.mind._save(conn, state)
        return created

    def linked_ready(self, conn, desire):
        if not desire.get("plan_id"):
            return True
        plan = self.get(conn, desire["plan_id"])
        step = next((s for s in plan["steps"] if s["id"] == desire.get("plan_step_id")), None)
        return bool(step and step.get("decision", {}).get("id") == desire.get("plan_decision_id") and not self.waiting_reason(conn, plan, step))

    def settle_linked(self, conn, desire, receipt):
        if not desire.get("plan_id"):
            return
        plan = self.get(conn, desire["plan_id"])
        step = next(s for s in plan["steps"] if s["id"] == desire["plan_step_id"])
        if any(r.get("id") == receipt["id"] for r in step["receipts"]):
            return
        step["receipts"].append(receipt)
        step.update(state="completed" if receipt.get("complete") else "waiting", revision=step["revision"] + 1)
        step.pop("decision", None)
        plan.update(revision=plan["revision"] + 1, next_review_at=self.mind.clock())
        if plan["status"] == "active" and all(s["state"] in {"completed", "abandoned"} for s in plan["steps"]):
            plan["status"] = "completed"
        self._save(conn, plan, "linked:" + receipt["id"])
