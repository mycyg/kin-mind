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

from .autonomy_models import ActionDecision, PlanChange
from .autonomy_schema import enabled
from .state import timestamp


def local_time(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Singapore"))
    return dt.isoformat()


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

    def _refs(self, conn, ids, allowed=None):
        refs = self.mind._evidence(conn, ids)
        if not refs or not self.mind._fresh(conn, refs):
            raise Conflict("Plan evidence needs review")
        if allowed is not None:
            manifest = {r["record_id"]: r["revision"] for r in allowed}
            if any(manifest.get(r["record_id"]) != r["revision"] for r in refs):
                raise Conflict("Plan uses evidence not supplied to this evaluation")
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
                raise Conflict("Plan changed during evaluation")
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
        plan.update(evidence=refs, reason=p.reason, next_review_at=local_time(p.next_review_at) or self.mind.clock(),
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
            prior = conn.execute("SELECT digest,result FROM commands WHERE id=?", (key,)).fetchone()
            if prior:
                if prior[0] != digest(raw):
                    raise Conflict("A command ID cannot be reused for a different change")
                return json.loads(prior[1])
            plan = self.change(conn, raw, command)
            conn.execute("INSERT INTO commands VALUES(?,?,?)", (key, digest(raw), dumps(plan)))
            return plan

    def decide(self, conn, proposal, command, receipt, allowed):
        d = ActionDecision.model_validate(proposal)
        if receipt.get("provider") != "deepseek" or receipt.get("reasoning") != "high":
            raise Conflict("Autonomous action requires a verified DeepSeek high decision")
        row = conn.execute("SELECT id FROM mind_plans WHERE scope=? AND (id=? OR json_extract(data,'$.key')=?)", (self.scope, d.plan_id, d.plan_id)).fetchone()
        plan = self.get(conn, row[0] if row else d.plan_id)
        if plan["revision"] != d.expected_revision or plan["status"] != "active":
            raise Conflict("Decision plan revision is no longer active")
        refs = self._refs(conn, d.evidence_ids, allowed)
        step = next((s for s in plan["steps"] if s["id"] == d.step_id), None)
        if not step or step["state"] in {"running", "completed", "unconfirmed", "abandoned"}:
            raise Conflict("Step is not available for this decision")
        available_artifacts = {a["sha256"]: a for s in plan["steps"] if s["state"] == "completed"
            for r in s["receipts"] if r.get("verified") for a in r.get("artifacts", [])}
        if d.artifact_hashes and (step["actor"] != "contact" or set(d.artifact_hashes) - set(available_artifacts)):
            raise Conflict("Delivery selection requires host-verified artifacts from completed steps")
        step["delivery_artifacts"] = [available_artifacts[h] for h in dict.fromkeys(d.artifact_hashes)]
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
        step["decision"] = {**d.model_dump(), "id": "decision_" + digest([command, plan["id"], step["id"]])[:32],
                            "plan_revision": plan["revision"], "step_revision": step["revision"], "evidence": refs,
                            "owner_epoch": self.owner_epoch(conn), "receipt": receipt, "at": self.mind.clock(),
                            "agent_version": self.mind._load(conn)["agent_version"]}
        plan["last_reviewed_owner_epoch"] = self.owner_epoch(conn)
        plan["next_review_at"] = local_time(d.next_review_at) or (timestamp(self.mind.clock()) + timedelta(minutes=20)).isoformat()
        # Other independent decisions are re-fenced to this atomic plan revision.
        for other in plan["steps"]:
            if other.get("decision"):
                other["decision"]["plan_revision"] = plan["revision"]
        if all(s["state"] in {"completed", "abandoned"} for s in plan["steps"]):
            plan["status"] = "completed"
        self._save(conn, plan, command)
        return plan

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

    def read(self, identifier=None, *, status=None, cursor=0, limit=24, history=False):
        with self.engine.db.connect() as conn:
            if identifier:
                plans = [self.get(conn, identifier)]
            else:
                rows = conn.execute("SELECT data FROM mind_plans WHERE scope=? " + ("AND status=? " if status else "") +
                                    "ORDER BY next_review,id LIMIT ? OFFSET ?", (self.scope, *([status] if status else []), min(100, max(1, limit)), max(0, cursor))).fetchall()
                plans = [json.loads(r[0]) for r in rows]
            for plan in plans:
                plan["needs_review"] = not self.mind._fresh(conn, plan["evidence"])
                for step in plan["steps"]:
                    step["waiting_reason"] = self.waiting_reason(conn, plan, step)
                if history:
                    plan["history"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_plan_history WHERE id=? ORDER BY revision", (plan["id"],))]
        return {"plans": plans, "next_cursor": cursor + len(plans) if len(plans) == limit else None, "timezone": "Asia/Singapore"}

    def tick(self, actions):
        """Clock/input changes enqueue one versioned review, never a catch-up send."""
        emitted = []
        with self.engine.db.connect(write=True) as conn:
            if not enabled(conn, self.scope, "autonomous_plans"):
                return emitted
            epoch = self.owner_epoch(conn)
            for row in conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status='active'", (self.scope,)).fetchall():
                plan = json.loads(row[0])
                reasons = [self.waiting_reason(conn, plan, s) for s in plan["steps"] if s["state"] == "ready"]
                source_changed = not self.mind._fresh(conn, plan["evidence"])
                changed = source_changed or plan.get("last_reviewed_owner_epoch") != epoch or plan.get("agent_version") != self.mind._load(conn)["agent_version"] or any(r in {"configuration-changed", "new-owner-evidence", "source-needs-review", "missed-window-review-required", "window-review-required", "procedure-needs-review"} for r in reasons)
                due = plan.get("next_review_at") and timestamp(plan["next_review_at"]) <= timestamp(self.mind.clock())
                if not (due or changed):
                    continue
                sources = [r["record_id"] for r in plan["evidence"]]
                # Invalid old evidence is retained in the plan, not passed as
                # authoritative current evidence to the action queue.
                valid_sources = []
                for i in sources:
                    try:
                        if self.mind._fresh(conn, self.mind._evidence(conn, [i])):
                            valid_sources.append(i)
                    except (Missing, Conflict):
                        pass
                sources = valid_sources
                current_versions = []
                for ref in plan["evidence"]:
                    current = conn.execute("SELECT revision,deleted FROM records WHERE id=? AND scope=?", (ref["record_id"], self.scope)).fetchone()
                    current_versions.append([ref["record_id"], list(current) if current else None])
                emitted.append(actions.emit(conn, "plan-review", [plan["id"], plan["revision"], epoch, current_versions],
                    {"plan_id": plan["id"], "plan_revision": plan["revision"], "evidence_ids": sources,
                     "agent_version": self.mind._load(conn)["agent_version"], "reason": "due-or-changed-evidence"}))
        return emitted

    def claim(self, actor, owner, *, foreground=False):
        if actor not in {"create", "explore", "contact"}:
            raise ValueError("Only Kin executors can claim a step")
        with self.engine.db.connect(write=True) as conn:
            if foreground or not enabled(conn, self.scope, "autonomous_plans") or (actor == "create" and not enabled(conn, self.scope, "creative_execution")):
                return {"state": "waiting", "reason": "foreground-or-feature-disabled"}
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_foreground_leases'").fetchone() and conn.execute("SELECT 1 FROM mind_foreground_leases WHERE scope=? AND expires_at>?", (self.scope, time.time())).fetchone():
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
                raise Conflict("Execution fence mismatch")
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
            for row in conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status='active'", (self.scope,)).fetchall():
                plan = json.loads(row[0])
                for step in plan["steps"]:
                    if step["actor"] not in {"contact", "explore"} or self.waiting_reason(conn, plan, step):
                        continue
                    did = step.get("desire_id") or (plan.get("desire_id") if len(plan["steps"]) == 1 else None)
                    desire = state["desires"].get(did)
                    if desire and desire.get("plan_decision_id") == step["decision"]["id"]:
                        continue
                    if desire and desire["status"] in {"in_progress", "completed"}:
                        continue
                    command = step["decision"]["id"] + ":wish"
                    args = dict(command_id=command, agent_version=state["agent_version"], expected_revision=state["revision"],
                        evidence_ids=[r["record_id"] for r in step["decision"]["evidence"]], reason=step["decision"]["reason"],
                        content=step["goal"], topic=plan["goal"][:500], kind=step["actor"], strength=50,
                        expires_at=plan["next_review_at"], completion=step["completion"])
                    if desire:
                        args.update(action="update", desire_id=did)
                    else:
                        args.update(action="create")
                    result = self.mind._apply_desire(conn, state, DesireChange(**args), command)
                    did = result["desire_id"]
                    state["desires"][did].update(status="wanted", plan_id=plan["id"], plan_step_id=step["id"],
                        plan_decision_id=step["decision"]["id"], decision_receipt=step["decision"]["receipt"],
                        delivery_artifacts=step.get("delivery_artifacts", []))
                    step["desire_id"] = did
                    # Linking a host delivery handle is not a semantic revision.
                    conn.execute("UPDATE mind_plans SET data=? WHERE id=?", (dumps(plan), plan["id"]))
                    created.append(did)
            if created:
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
