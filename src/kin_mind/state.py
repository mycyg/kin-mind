"""Transactional affect and desires in the existing private memory database.

Snapshots are projections, not measurements. Events, configuration revisions and
source revisions remain inspectable. No model or transport runs inside this store.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, FiniteFloat, StrictInt, field_validator, model_validator

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.idempotency import stamp as fingerprint
from eventmem.core.models import Model, Scope, now, utc
from eventmem.core.persona import load_persona, persona_metadata, validate_trait_changes
from eventmem.core.self_knowledge import SelfKnowledge, metadata

from .continuity import SCHEMA as CONTINUITY_SCHEMA
from .continuity import Continuity, RhythmProposal, Understanding
from .profile import DIMENSIONS, default_profile, interaction_style

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_state(
 scope TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_events(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, revision INTEGER NOT NULL,
 kind TEXT NOT NULL, occurred_at TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(scope,revision));
CREATE INDEX IF NOT EXISTS mind_history ON mind_events(scope,revision DESC);
CREATE TABLE IF NOT EXISTS mind_contacts(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS mind_contact_active ON mind_contacts(scope)
 WHERE state IN ('drafting','pending','unconfirmed');
CREATE TABLE IF NOT EXISTS mind_action_events(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,created_at TEXT NOT NULL,
 state TEXT NOT NULL,data TEXT NOT NULL);
"""


class Evolution(Model):
    claim_id: str | None = None
    assessment_id: str | None = None
    revert_event_id: str | None = None
    baseline_changes: dict[str, FiniteFloat] = Field(default_factory=dict)
    half_life_changes: dict[str, FiniteFloat] = Field(default_factory=dict)
    traits: dict[str, str] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def shape(self):
        if self.revert_event_id:
            if self.baseline_changes or self.half_life_changes or self.traits:
                raise ValueError(
                    "A reversion cannot also propose new personality changes"
                )
        elif not self.claim_id or not self.assessment_id:
            raise ValueError(
                "Evolution requires a hypothesis and a behavioral assessment"
            )
        return self


class Motivation(Model):
    target: StrictInt = Field(ge=0, le=100)
    half_life_minutes: Literal[20, 60, 180]
    reason: str = Field(min_length=1, max_length=1200)


class AffectiveEvent(Model):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    values: dict[str, StrictInt] = Field(default_factory=dict, max_length=20)
    motivations: dict[str, Motivation] = Field(default_factory=dict, max_length=2)
    reason: str = Field(min_length=1, max_length=1200)
    origin: Literal["interaction", "exploration", "reflection"] = "interaction"
    evolution: Evolution | None = None
    understanding: Understanding | None = None
    rhythm: RhythmProposal | None = None

    @field_validator("values")
    @classmethod
    def scores(cls, values):
        if any(not 0 <= value <= 100 for value in values.values()):
            raise ValueError("Scores must be integers from 0 to 100")
        return values

    @model_validator(mode="after")
    def separate_evolution(self):
        if set(self.motivations) - {"initiative", "curiosity"}:
            raise ValueError("Motivation applies to initiative and curiosity")
        if self.evolution and (self.values or self.motivations or self.understanding or self.rhythm):
            raise ValueError("Separate state observations from personality evolution")
        return self


class DesireChange(Model):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    action: Literal[
        "create", "update", "start", "wait", "resume", "complete", "abandon"
    ]
    desire_id: str | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    topic: str | None = Field(default=None, max_length=500)
    kind: Literal["contact", "explore", "create"] | None = None
    strength: StrictInt | None = Field(default=None, ge=0, le=100)
    expires_at: str | None = None
    completion: str | None = Field(default=None, min_length=1, max_length=1000)
    concern_ids: list[str] | None = Field(default=None, max_length=10)
    exploration_target: Literal["knowledge", "computer"] | None = None
    exploration_id: str | None = Field(default=None, max_length=100)
    reason: str = Field(min_length=1, max_length=1200)
    wait_condition: Literal["time", "new_evidence", "owner_reply"] | None = None
    retry_after_seconds: StrictInt = Field(default=1800, ge=300, le=21600)

    _time = field_validator("expires_at")(lambda v: utc(v) if v else v)

    @model_validator(mode="after")
    def shape(self):
        if self.wait_condition is not None and self.action != "wait":
            raise ValueError("Only waiting desires have a resume condition")
        if self.action == "create":
            if self.exploration_target and self.kind != "explore":
                raise ValueError("Only exploration wishes have an exploration target")
            if self.exploration_id and self.kind != "contact":
                raise ValueError("Only contact wishes link a communication decision")
            if self.desire_id or any(
                getattr(self, k) is None
                for k in (
                    "content",
                    "topic",
                    "kind",
                    "strength",
                    "expires_at",
                    "completion",
                )
            ):
                raise ValueError(
                    "New desires require content, topic, kind, strength, expiry and completion"
                )
        elif not self.desire_id:
            raise ValueError("A desire ID is required")
        return self


class ContactDecision(Model):
    action: Literal["wait", "abandon"]
    reason: str = Field(min_length=1, max_length=1200)
    condition: Literal["time", "new_evidence", "owner_reply"] = "new_evidence"
    retry_after_seconds: StrictInt = Field(default=1800, ge=300, le=21600)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def project(entry, at):
    elapsed = max(0, (timestamp(at) - timestamp(entry["at"])).total_seconds())
    value = entry["target"] + (entry["score"] - entry["target"]) * 0.5 ** (
        elapsed / (entry["half_life_hours"] * 3600)
    )
    return min(100.0, max(0.0, value))


class Mind(Continuity):
    def __init__(self, engine, scope: Scope, clock=now):
        self.engine, self.scope, self.clock = engine, scope, clock
        from .autonomy_schema import SCHEMA as AUTONOMY_SCHEMA
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA + CONTINUITY_SCHEMA + AUTONOMY_SCHEMA)

    def _load(self, conn):
        row = conn.execute(
            "SELECT * FROM mind_state WHERE scope=?", (self.scope.key(),)
        ).fetchone()
        if not row:
            raise Missing(
                "Initialize the role profile before reading or changing state"
            )
        return json.loads(row["data"])

    def _save(self, conn, state):
        conn.execute(
            "INSERT INTO mind_state VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,data=excluded.data",
            (self.scope.key(), state["revision"], dumps(state)),
        )
        self.engine.db.bump(conn)

    def _evidence(self, conn, ids):
        refs = []
        for identifier in sorted(set(ids)):
            if identifier.startswith("src_"):
                sid = identifier
                rid = "mem_" + digest([sid, "root"])[:32]
            else:
                record = self.engine._get(conn, identifier)
                if record["scope"] != self.scope.model_dump():
                    raise Conflict("Evidence belongs to another scope")
                if not record["source_ids"]:
                    raise Conflict("Evidence must retain its source")
                for sid in record["source_ids"]:
                    refs.append(self._reference(conn, sid, identifier))
                continue
            refs.append(self._reference(conn, sid, rid))
        return refs

    def _reference(self, conn, sid, rid):
        row = conn.execute(
            "SELECT * FROM sources WHERE id=? AND deleted=0", (sid,)
        ).fetchone()
        if not row:
            raise Missing("Evidence source is unavailable")
        if row["scope"] != self.scope.key():
            raise Conflict("Evidence source belongs to another scope")
        data = json.loads(row["data"])
        if timestamp(row["occurred_at"]) > timestamp(self.clock()):
            raise Conflict("Future evidence cannot describe an observed state")
        record = self.engine._get(conn, rid)
        if (
            record["scope"] != self.scope.model_dump()
            or record["status"] != "active"
            or not SelfKnowledge._current(record, self.clock())
        ):
            raise Conflict("Evidence is not current", code="evidence-not-current", target=rid)
        return {
            "source_id": sid,
            "hash": row["hash"],
            "record_id": rid,
            "revision": record["revision"],
            "authority": data["authority"],
            "session": row["session"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "namespace": row["namespace"],
            "source_key": row["source_key"],
            "metadata": data.get("metadata", {}),
        }

    def _fresh(self, conn, refs):
        for ref in refs:
            try:
                current = self._reference(conn, ref["source_id"], ref["record_id"])
            except (Missing, Conflict):
                return False
            if current["hash"] != ref["hash"] or current["revision"] != ref["revision"]:
                return False
            newer = conn.execute(
                "SELECT 1 FROM sources WHERE namespace=? AND source_key=? AND scope=? AND deleted=0 AND id<>? AND received_at>(SELECT received_at FROM sources WHERE id=?) LIMIT 1",
                (
                    ref["namespace"],
                    ref["source_key"],
                    self.scope.key(),
                    ref["source_id"],
                    ref["source_id"],
                ),
            ).fetchone()
            if newer:
                return False
        return True

    def _entry_fresh(self, conn, entry):
        if entry.get("interpretation_unverified"):
            return False
        if not self._fresh(conn, entry["evidence"]):
            return False
        if entry.get("claim_id"):
            sk = SelfKnowledge(self.engine, self.scope)
            for kind in ("claim", "assessment"):
                try:
                    row = sk._get(conn, entry[kind + "_id"], kind)
                except (Missing, Conflict):
                    return False
                if (
                    row["status"] not in {"active", "unverified"}
                    or row["revision"] != entry[kind + "_revision"]
                    or not sk._fresh(conn, row)
                ):
                    return False
        return True

    def initialize(self, *, agent_version, evidence_ids, command_id="initialize"):
        payload = {"agent_version": agent_version, "evidence_ids": evidence_ids}
        with self.engine.db.connect(write=True) as conn:

            def run():
                if conn.execute(
                    "SELECT 1 FROM mind_state WHERE scope=?", (self.scope.key(),)
                ).fetchone():
                    raise Conflict(
                        "A profile already exists; initialization cannot reset it"
                    )
                refs = self._evidence(conn, evidence_ids)
                if any(ref["authority"] != "explicit" for ref in refs):
                    raise Conflict(
                        "Role initialization requires explicit source evidence"
                    )
                at, profile = self.clock(), default_profile()
                event_id = "mind_" + digest([self.scope.key(), command_id])[:32]
                values = {
                    k: {
                        "score": v["baseline"],
                        "target": v["baseline"],
                        "baseline": v["baseline"],
                        "half_life_hours": v["half_life_hours"],
                        "at": at,
                        "basis": "role_default",
                        "evidence": refs,
                        "event_id": event_id,
                        "agent_version": agent_version,
                    }
                    for k, v in profile["dimensions"].items()
                }
                state = {
                    "revision": 1,
                    "profile": profile,
                    "profile_version": digest(profile)[:16],
                    "dimensions": values,
                    "desires": {},
                    "traits": {},
                    "profile_reviews": {},
                    "last_evolution_day": None,
                    "created_at": at,
                    "updated_at": at,
                    "agent_version": agent_version,
                }
                self._save(conn, state)
                self._history(conn, event_id, state, "initialize", payload)
                return {"event_id": event_id, "revision": 1}

            return self.engine.command(conn, self._key(command_id), payload, run)

    def _key(self, command_id):
        return "mind:" + digest([self.scope.key(), command_id])

    def _history(self, conn, event_id, state, kind, payload):
        conn.execute(
            "INSERT INTO mind_events VALUES(?,?,?,?,?,?)",
            (
                event_id,
                self.scope.key(),
                state["revision"],
                kind,
                self.clock(),
                dumps({"request": payload, "snapshot": state}),
            ),
        )

    def _mutate(self, request, kind, fn, *, rebase=None):
        payload = request.model_dump() if hasattr(request, "model_dump") else request
        with self.engine.db.connect(write=True) as conn:
            from .autonomy_schema import optimized
            # The expected revision is this command's precondition, checked inside run(); it
            # is not what the command is. A retry that reread the state keeps its identity.
            stamp = fingerprint("mind-state", self.scope.key(), payload,
                                enabled=optimized(conn, self.scope.key(), "idempotency_fingerprint"))

            def run():
                state = self._load(conn)
                if state["revision"] != payload["expected_revision"] and not (rebase and rebase(conn, state)):
                    raise Conflict(
                        "Mind revision changed; read current state before updating",
                        code="mind-revision-changed", target=self.scope.key(),
                        expected=payload["expected_revision"], actual=state["revision"],
                    )
                event_id = (
                    "mind_" + digest([self.scope.key(), payload["command_id"]])[:32]
                )
                result = fn(conn, state, event_id) or {}
                state["revision"] += 1
                state["updated_at"] = self.clock()
                state["agent_version"] = payload["agent_version"]
                self._save(conn, state)
                self._history(conn, event_id, state, kind, payload)
                return dict(event_id=event_id, revision=state["revision"], **result)

            return self.engine.command(
                conn, self._key(payload["command_id"]), payload, run, stamp=stamp
            )

    def configure_autonomy(self, request):
        """Explicit user policy; never an inferred emotion or personality update."""
        def apply(conn, state, event_id):
            refs = self._evidence(conn, request["evidence_ids"])
            if not refs or any(r["authority"] != "explicit" for r in refs):
                raise Conflict("Autonomy policy requires explicit user evidence")
            self._retarget(conn, state, self.clock())
            state["autonomy"] = {
                "initiative_target": 85,
                "open_exploration": True,
                "topic_selected_by": "Kin",
                "evidence": refs,
                "configured_at": self.clock(),
                "event_id": event_id,
                "reason": request["reason"],
            }
            state["profile_version"] = digest([state["profile"], state["autonomy"]])[:16]
            self._retarget(conn, state, self.clock())
            return {"autonomy": state["autonomy"]}
        return self._mutate(request, "autonomy-policy", apply)

    def _autonomy(self, conn, state):
        policy = state.get("autonomy", {})
        return policy if policy and self._fresh(conn, policy["evidence"]) else {}

    def configure_contact(self, request):
        """Change an explicit owner preference; keep scores and delivery history intact."""
        if type(request.get("wait_for_reply")) is not bool:
            raise ValueError("wait_for_reply must be a boolean")
        for key in ("command_id", "agent_version", "reason"):
            if not isinstance(request.get(key), str) or not request[key].strip():
                raise ValueError(f"{key} is required")

        def apply(conn, state, event_id):
            refs = self._evidence(conn, request["evidence_ids"])
            if not refs or any(r["authority"] != "explicit" for r in refs):
                raise Conflict("Contact preferences require explicit user evidence")
            state["profile"]["contact"]["wait_for_reply"] = request["wait_for_reply"]
            state["contact_preference"] = {
                "wait_for_reply": request["wait_for_reply"], "evidence": refs,
                "event_id": event_id, "reason": request["reason"],
                "configured_at": self.clock(),
            }
            state["profile_version"] = digest([
                state["profile"], state.get("autonomy"), state.get("behavior"),
                state["contact_preference"],
            ])[:16]
            return {"contact_preference": state["contact_preference"]}

        return self._mutate(request, "contact-preference", apply)

    def configure_behavior(self, request):
        """Install sourced expression/contact policy without changing any score."""
        if request.get("style") not in {"affectionate-direct", "contextual"}:
            raise ValueError("Unknown interaction style")
        start = request.get("quiet_start_hour")
        if start is not None and (type(start) is not int or not 0 <= start <= 23):
            raise ValueError("Invalid quiet-hour start")

        def apply(conn, state, event_id):
            refs = self._evidence(conn, request["evidence_ids"])
            if not refs or any(r["authority"] != "explicit" for r in refs):
                raise Conflict("Behavior policy requires explicit user evidence")
            state["behavior"] = {"style": request["style"], "evidence": refs,
                                 "event_id": event_id, "reason": request["reason"]}
            for key in ("definition", "increase", "decrease", "expression"):
                state["profile"]["dimensions"]["flirtation"][key] = DIMENSIONS["flirtation"][key]
            if start is not None:
                state["profile"]["contact"]["quiet_start"] = start
            state["profile_version"] = digest([state["profile"], state.get("autonomy"), state["behavior"]])[:16]
            return {"behavior": state["behavior"]}

        return self._mutate(request, "behavior-policy", apply)

    def _desire_ready(self, conn, desire, at, *, state=None):
        from .autonomy_schema import enabled
        if enabled(conn, self.scope.key()):
            receipt = desire.get("decision_receipt", {})
            current = state or self._load(conn)
            if receipt.get("provider") != "deepseek" or receipt.get("agent_version") != current["agent_version"]:
                return False
            from .plans import AutonomousPlans
            if not AutonomousPlans(self).linked_ready(conn, desire):
                return False
        if state is None and (desire.get("exploration_id") or desire.get("concern_revisions")):
            state = self._load(conn)
        if desire.get("exploration_id"):
            from .exploration_decisions import require_share
            try:
                decision = require_share(self, conn, state, desire["exploration_id"])
                if desire.get("sharing_revision") != decision["revision"]:
                    return False
            except Conflict:
                return False
        return (
            desire["kind"] == "contact"
            and desire["status"] == "wanted"
            and timestamp(desire["expires_at"]) > timestamp(at)
            and self._fresh(conn, desire["evidence"])
            and (not desire.get("concern_revisions") or self._concern_links_fresh(conn, state, desire))
        )

    def _initiative_value(self, conn, state, at):
        entry = state["dimensions"]["initiative"]
        if entry.get("motivation") or state.get("action_policy"):
            return project(entry, at)
        cursor, end = entry["at"], at
        value = entry["score"]
        wishes = [
            d
            for d in state["desires"].values()
            if d["kind"] == "contact"
            and d["status"] == "wanted"
            and self._fresh(conn, d["evidence"])
            and timestamp(d["expires_at"]) > timestamp(cursor)
        ]
        boundaries = sorted(
            {
                d["expires_at"]
                for d in wishes
                if timestamp(d["expires_at"]) < timestamp(end)
            },
            key=timestamp,
        )
        for boundary in [*boundaries, end]:
            target = max(
                [
                    entry["baseline"],
                    *[
                        max(d["strength"], self._autonomy(conn, state).get("initiative_target", 0))
                        for d in wishes
                        if timestamp(d["expires_at"]) > timestamp(cursor)
                    ],
                ]
            )
            value = project(
                dict(entry, score=value, at=cursor, target=target), boundary
            )
            cursor = boundary
        return value

    def _retarget(self, conn, state, at):
        current = state["dimensions"]["initiative"]
        if current.get("motivation") or state.get("action_policy"):
            return
        value = self._initiative_value(conn, state, at)
        target = max(
            [
                state["profile"]["dimensions"]["initiative"]["baseline"],
                *[
                    max(d["strength"], self._autonomy(conn, state).get("initiative_target", 0))
                    for d in state["desires"].values()
                    if self._desire_ready(conn, d, at, state=state)
                ],
            ]
        )
        current.update(
            score=value,
            at=at,
            target=target,
            half_life_hours=state["profile"]["dimensions"]["initiative"][
                "half_life_hours"
            ],
        )

    def record(self, request: AffectiveEvent):
        return self._mutate(
            request,
            "evolution" if request.evolution else "affect",
            lambda conn, state, eid: self._apply_event(conn, state, request, eid),
        )

    def _apply_event(self, conn, state, request, event_id, continuity_sources=None):
        refs = self._evidence(conn, request.evidence_ids)
        if request.evolution:
            return self._evolve(conn, state, request, refs, event_id)
        unknown = set(request.values) - set(state["dimensions"])
        if unknown:
            raise ValueError("Unknown affective dimension")
        # A single underlying event cannot be scored again under another command.
        evidence_key = digest(sorted({(r["source_id"], r["hash"]) for r in refs}))
        duplicate = conn.execute(
            "SELECT 1 FROM mind_events WHERE scope=? AND kind='affect' AND json_extract(data,'$.snapshot.last_evidence_key')=? LIMIT 1",
            (self.scope.key(), evidence_key),
        ).fetchone()
        if duplicate:
            raise Conflict(
                "This evidence was already appraised; use a source correction or new evidence"
            )
        previous_values = {key: project(entry, self.clock()) for key, entry in state["dimensions"].items()}
        if continuity_sources is None and (request.understanding or request.rhythm):
            continuity_sources = self._continuity_sources(conn, state, refs)
        for key in request.values.keys() | request.motivations.keys():
            spec = state["profile"]["dimensions"][key]
            previous = state["dimensions"][key]
            score = request.values.get(key, project(previous, self.clock()))
            motivation = request.motivations.get(key)
            state["dimensions"][key] = {
                "score": score,
                "baseline": spec["baseline"],
                "target": spec["baseline"],
                "half_life_hours": spec["half_life_hours"],
                "at": self.clock(),
                "basis": "event_inferred",
                "evidence": refs,
                "reason": request.reason,
                "event_id": event_id,
                "agent_version": request.agent_version,
            }
            if motivation or previous.get("motivation"):
                setting = motivation.model_dump() if motivation else previous["motivation"]
                state["dimensions"][key].update(
                    target=setting["target"], half_life_hours=setting["half_life_minutes"] / 60,
                    motivation={**setting, "episode_id": event_id},
                )
        state["last_evidence_key"] = evidence_key
        self._apply_continuity(conn, state, request, event_id, refs, previous_values, continuity_sources)
        self._retarget(conn, state, self.clock())

    def manage_desire(self, request: DesireChange):
        return self._mutate(
            request,
            "desire",
            lambda conn, state, eid: self._apply_desire(conn, state, request, eid),
        )

    def _apply_desire(self, conn, state, request, event_id):
        refs = self._evidence(conn, request.evidence_ids)
        if request.exploration_id:
            from .exploration_decisions import require_share
            decision = require_share(self, conn, state, request.exploration_id)
            if request.action == "create" and any(d.get("exploration_id") == request.exploration_id
                                                  and d.get("sharing_revision") == decision["revision"]
                                                  for d in state["desires"].values()):
                raise Conflict("This sharing decision already has a contact intent")
        link_only = request.action == "update" and request.concern_ids is not None and all(getattr(request, k) is None for k in ("content", "topic", "strength", "expires_at", "completion"))
        at = self.clock()
        self._retarget(conn, state, at)
        if request.action == "create":
            if timestamp(request.expires_at) <= timestamp(at):
                raise ValueError("A new desire must have a future expiry")
            did = "desire_" + digest([self.scope.key(), request.command_id])[:32]
            state["desires"][did] = dict(
                id=did,
                status="wanted",
                revision=1,
                created_at=at,
                **{
                    k: getattr(request, k)
                    for k in (
                        "content",
                        "topic",
                        "kind",
                        "strength",
                        "expires_at",
                        "completion",
                    )
                },
                exploration_target=request.exploration_target or "knowledge",
                exploration_id=request.exploration_id,
            )
        else:
            did = request.desire_id
            if did not in state["desires"]:
                raise Missing("Desire is outside this scope or missing")
            desire = state["desires"][did]
            if request.exploration_target and desire["kind"] != "explore":
                raise Conflict("Only exploration wishes have an exploration target")
            if request.exploration_id and desire["kind"] != "contact":
                raise Conflict("Only contact wishes link a communication decision")
            if desire.get("exploration_id") and request.action in {"start", "resume", "update"}:
                from .exploration_decisions import require_share
                linked_result = request.exploration_id or desire["exploration_id"]
                decision = require_share(self, conn, state, linked_result)
                if any(d["id"] != did and d.get("exploration_id") == linked_result
                       and d.get("sharing_revision") == decision["revision"] for d in state["desires"].values()):
                    raise Conflict("This sharing decision already has another contact intent")
            if desire["status"] in {"completed", "abandoned"}:
                raise Conflict(
                    "A finished desire stays in history; create a new desire"
                )
            transitions = {
                "start": "in_progress",
                "wait": "waiting",
                "resume": "wanted",
                "complete": "completed",
                "abandon": "abandoned",
            }
            if request.action in transitions:
                desire["status"] = transitions[request.action]
            if not link_only:
                desire.pop("contact_wait", None)
                desire.pop("contact_failures", None)
            if request.action == "wait":
                desire["contact_wait"] = self._wait_details(
                    ContactDecision(action="wait", reason=request.reason,
                                    condition=request.wait_condition or "new_evidence",
                                    retry_after_seconds=request.retry_after_seconds), at)
            if request.action == "update":
                for k in ("content", "topic", "strength", "expires_at", "completion", "exploration_target", "exploration_id"):
                    if getattr(request, k) is not None:
                        desire[k] = getattr(request, k)
            desire["revision"] += 1
        desire = state["desires"][did]
        if desire.get("exploration_id") and request.action in {"create", "start", "resume", "update"}:
            desire["sharing_revision"] = state["exploration_decisions"][desire["exploration_id"]]["revision"]
        if request.concern_ids is not None:
            links = self._resolve_concern_links(conn, state, request.concern_ids)
            desire.update(concern_ids=list(links), concern_revisions=links)
        elif desire.get("concern_ids") and request.action in {"start", "resume", "update"}:
            # An explicit wish reassessment also acknowledges current concern revisions.
            links = self._resolve_concern_links(conn, state, desire["concern_ids"])
            desire["concern_revisions"] = links
        desire.update(
            evidence=desire["evidence"] if link_only else refs,
            reason=desire["reason"] if link_only else request.reason,
            updated_at=at,
            event_id=event_id,
            agent_version=request.agent_version,
        )
        self._retarget(conn, state, at)
        return {"desire_id": did, "desire_revision": desire["revision"]}

    def _evolve(self, conn, state, request, refs, event_id):
        validate_trait_changes(load_persona(self.engine, self.scope), request.evolution.traits)
        if request.evolution.revert_event_id:
            return self._revert(conn, state, request, refs, event_id)
        spec = state["profile"]["evolution"]
        day = (
            timestamp(self.clock())
            .astimezone(ZoneInfo(spec["timezone"]))
            .date()
            .isoformat()
        )
        if state["last_evolution_day"] == day:
            raise Conflict("Personality was already evaluated today")
        interactions = {
            r["hash"]
            for r in refs
            if r["authority"] == "explicit"
            and r["metadata"].get("role") == "user"
            and r["metadata"].get("host_event") == "message"
        }
        if len(interactions) < spec["minimum_interactions"]:
            raise Conflict(
                "Personality changes require three independent user interactions"
            )
        sk, proposal = SelfKnowledge(self.engine, self.scope), request.evolution
        claim = sk._get(conn, proposal.claim_id, "claim")
        assessment = sk._get(conn, proposal.assessment_id, "assessment")
        prediction = sk._get(conn, metadata(assessment)["prediction_id"], "prediction")
        if (
            metadata(claim).get("basis") != "hypothesis"
            or metadata(prediction).get("claim_id") != claim["id"]
            or metadata(assessment).get("outcome") is None
            or prediction["revision"] != 1
            or metadata(prediction).get("claim_revision") != claim["revision"]
            or any(
                r["status"] not in {"active", "unverified"}
                or metadata(r).get("agent_version") != request.agent_version
                or not sk._fresh(conn, r)
                for r in (claim, prediction, assessment)
            )
        ):
            raise Conflict(
                "A current, version-matched prospective behavioral check is required"
            )
        before = deepcopy(state["profile"])
        proof = {
            "evidence": refs,
            "claim_id": claim["id"],
            "claim_revision": claim["revision"],
            "assessment_id": assessment["id"],
            "assessment_revision": assessment["revision"],
            "basis": "hypothesis_trial",
            "event_id": event_id,
        }
        for key, value in proposal.baseline_changes.items():
            old = state["profile"]["dimensions"].get(key)
            if (
                old is None
                or not 0 <= value <= 100
                or abs(value - old["baseline"]) > spec["max_baseline_delta"]
            ):
                raise ValueError("Baseline change exceeds the daily limit")
            old["baseline"] = value
        for key, value in proposal.half_life_changes.items():
            old = state["profile"]["dimensions"].get(key)
            if (
                old is None
                or value <= 0
                or abs(value / old["half_life_hours"] - 1)
                > spec["max_half_life_ratio"] + 1e-9
            ):
                raise ValueError("Half-life change exceeds the daily limit")
            old["half_life_hours"] = value
        for key, value in proposal.traits.items():
            if (
                not key.strip()
                or not value.strip()
                or len(key) > 100
                or len(value) > 1200
            ):
                raise ValueError("Personality trait is empty or too long")
            state["traits"][key] = dict(text=value, **proof)
        # Configuration changes start new trajectories at the current effective value.
        # Earlier event parameters remain frozen in the event snapshots.
        for key in proposal.baseline_changes.keys() | proposal.half_life_changes.keys():
            old = state["dimensions"][key]
            new = state["profile"]["dimensions"][key]
            state["dimensions"][key] = dict(
                old,
                score=project(old, self.clock()),
                at=self.clock(),
                baseline=new["baseline"],
                target=new["baseline"],
                half_life_hours=new["half_life_hours"],
                **proof,
            )
            state["profile_reviews"][key] = proof
        state["last_evolution_day"] = day
        state["profile_version"] = digest([state["profile"], state["traits"]])[:16]
        self._retarget(conn, state, self.clock())
        return {
            "previous_profile": before,
            "profile_version": state["profile_version"],
            "basis": "hypothesis_trial",
            "claim_status": claim["status"],
        }

    def _revert(self, conn, state, request, refs, event_id):
        if any(r["authority"] != "explicit" for r in refs):
            raise Conflict(
                "A personality reversion requires explicit user correction evidence"
            )
        row = conn.execute(
            "SELECT revision,occurred_at FROM mind_events WHERE id=? AND scope=? AND kind='evolution'",
            (request.evolution.revert_event_id, self.scope.key()),
        ).fetchone()
        if not row:
            raise Missing("Evolution event is outside this scope or missing")
        if not any(
            timestamp(r["occurred_at"]) >= timestamp(row["occurred_at"]) for r in refs
        ):
            raise Conflict("Reversion evidence must follow the change")
        later = conn.execute(
            "SELECT 1 FROM mind_events WHERE scope=? AND kind='evolution' AND revision>?",
            (self.scope.key(), row["revision"]),
        ).fetchone()
        if later:
            raise Conflict(
                "A later personality revision exists; review it before reverting"
            )
        before = conn.execute(
            "SELECT data FROM mind_events WHERE scope=? AND revision=?",
            (self.scope.key(), row["revision"] - 1),
        ).fetchone()
        previous = json.loads(before[0])["snapshot"]
        for key, old in state["dimensions"].items():
            spec = previous["profile"]["dimensions"][key]
            old.update(
                score=project(old, self.clock()),
                at=self.clock(),
                baseline=spec["baseline"],
                target=spec["baseline"],
                half_life_hours=spec["half_life_hours"],
            )
            if key in state.get("profile_reviews", {}):
                for proof_key in (
                    "claim_id",
                    "claim_revision",
                    "assessment_id",
                    "assessment_revision",
                ):
                    old.pop(proof_key, None)
                old.update(
                    evidence=refs,
                    event_id=event_id,
                    basis="configuration_reverted",
                    reason=request.reason,
                    agent_version=request.agent_version,
                )
        validate_trait_changes(load_persona(self.engine, self.scope), previous["traits"])
        state["profile"], state["traits"] = previous["profile"], previous["traits"]
        state["profile_reviews"] = previous.get("profile_reviews", {})
        state["profile_version"] = digest(
            [state["profile"], state["traits"], event_id]
        )[:16]
        self._retarget(conn, state, self.clock())
        return {
            "reverted_event_id": request.evolution.revert_event_id,
            "profile_version": state["profile_version"],
        }

    def _view(self, conn, state, at):
        desires = []
        for desire in state["desires"].values():
            d = deepcopy(desire)
            d["needs_review"] = not self._fresh(conn, d["evidence"])
            d["expired"] = timestamp(d["expires_at"]) <= timestamp(at)
            desires.append(d)
        values = {}
        for key, entry in state["dimensions"].items():
            fresh = self._entry_fresh(conn, entry)
            if key in state.get("profile_reviews", {}):
                fresh = fresh and self._entry_fresh(conn, state["profile_reviews"][key])
            projected = project(entry, at)
            if key == "initiative":
                projected = self._initiative_value(conn, state, at)
            values[key] = {
                "label": state["profile"]["dimensions"][key]["label"],
                "value": round(projected),
                "projected_value": projected,
                "raw_value": entry["score"],
                "baseline": entry["baseline"],
                "half_life_hours": entry["half_life_hours"],
                "target": entry["target"],
                "basis": entry["basis"],
                "observed_at": entry["at"],
                "needs_review": not fresh,
                "event_id": entry["event_id"],
                "agent_version": entry["agent_version"],
                "evidence_ids": [r["record_id"] for r in entry["evidence"]],
                "reason": entry.get("reason", "初始化角色底色，尚无状态观测"),
                "motivation": entry.get("motivation"),
            }
        traits = {
            k: dict(v, needs_review=not self._entry_fresh(conn, v))
            for k, v in state["traits"].items()
        }
        # The ledger is the writer once it is switched on. It answers in the dict shape every
        # older reader already knows, and carries its own projection beside it.
        from .traits import ledger_view
        ledger = ledger_view(conn, self, at)
        if ledger:
            traits = {**traits, **ledger["legacy"]}
        contact = deepcopy(state["profile"]["contact"])
        preference = state.get("contact_preference")
        if preference:
            contact["preference"] = {
                "event_id": preference["event_id"],
                "configured_at": preference["configured_at"],
                "reason": preference["reason"],
                "needs_review": not self._fresh(conn, preference["evidence"]),
                "evidence_ids": [r["record_id"] for r in preference["evidence"]],
            }
        view = {
            "revision": state["revision"],
            "scope": self.scope.model_dump(),
            "as_of": at,
            "agent_version": state["agent_version"],
            "profile_version": state["profile_version"],
            "dimensions": values,
            "session_advice": deepcopy(state.get("session_advice")),
            "desires": desires,
            "traits": traits,
            "contact": contact,
            "exploration": state["profile"]["exploration"],
            "autonomy": self._autonomy(conn, state),
            "last_evolution_day": state["last_evolution_day"],
            "action_policy": ({**state["action_policy"], "needs_review": not self._fresh(conn, state["action_policy"]["evidence"])} if state.get("action_policy") else None),
            "action_events": [{**json.loads(r["data"]), "id": r["id"], "kind": r["kind"], "state": r["state"]}
                              for r in conn.execute("SELECT * FROM mind_action_events WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT 8", (self.scope.key(),))],
        }
        if ledger:
            # Only while the switch is on: with it off this view is what it was, key for key.
            view["trait_ledger"] = {k: v for k, v in ledger.items() if k != "legacy"}
        return view

    def read(self, *, as_of=None, history=0, query=""):
        if not 0 <= history <= 100:
            raise ValueError("History must be between 0 and 100")
        with self.engine.db.connect() as conn:
            at = utc(as_of) if as_of else self.clock()
            result = self._view(conn, self._load(conn), at)
            result["persona_contract"] = persona_metadata(load_persona(self.engine, self.scope))
            owner = conn.execute(
                "SELECT occurred_at FROM sources WHERE scope=? AND namespace='kin-owner-input' "
                "AND deleted=0 AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 1",
                (self.scope.key(), at),
            ).fetchone()
            accepted = conn.execute(
                "SELECT data FROM mind_contacts WHERE scope=? AND state='accepted' "
                "AND json_extract(data,'$.updated_at')<=? ORDER BY json_extract(data,'$.updated_at') DESC LIMIT 1",
                (self.scope.key(), at),
            ).fetchone()
            last_owner = owner[0] if owner else None
            last_contact = json.loads(accepted[0]).get("updated_at") if accepted else None
            awaiting = bool(last_contact and (not last_owner or timestamp(last_contact) > timestamp(last_owner)))
            result["interaction_timing"] = {
                "last_owner_message_at": last_owner, "last_proactive_accepted_at": last_contact,
                "awaiting_reply": awaiting,
                "owner_silence_seconds": max(0, (timestamp(at)-timestamp(last_owner)).total_seconds()) if last_owner else None,
                "unanswered_contact_seconds": max(0, (timestamp(at)-timestamp(last_contact)).total_seconds()) if awaiting else 0,
            }
            result["decision_runtime"] = None
            if conn.execute("SELECT name FROM sqlite_master WHERE name='mind_appraisals'").fetchone():
                receipt = conn.execute("SELECT json_extract(data,'$.receipt') FROM mind_appraisals WHERE scope=? AND json_extract(data,'$.receipt') IS NOT NULL ORDER BY json_extract(data,'$.receipt.verified_at') DESC LIMIT 1", (self.scope.key(),)).fetchone()
                if receipt:
                    details = json.loads(receipt[0])
                    result["decision_runtime"] = {k: details.get(k) for k in ("provider", "model", "reasoning", "request_id", "verified_at")}
            state = self._load(conn)
            from .exploration_decisions import decision_view
            result["exploration_decisions"] = decision_view(self, conn, state)
            behavior = state.get("behavior", {})
            if behavior and self._fresh(conn, behavior["evidence"]):
                result["interaction_style"] = interaction_style(result["dimensions"], behavior)
            elif behavior:
                result["interaction_style"] = {"needs_review": True, "reason": "Expression preference source requires review"}
            self._continuity_view(conn, state, result, at, query)
            if history:
                result["history"] = [
                    dict(
                        id=r["id"],
                        kind=r["kind"],
                        revision=r["revision"],
                        occurred_at=r["occurred_at"],
                        **json.loads(r["data"]),
                    )
                    for r in conn.execute(
                        "SELECT * FROM mind_events WHERE scope=? ORDER BY revision DESC LIMIT ?",
                        (self.scope.key(), history),
                    )
                ]
            return result

    @staticmethod
    def _wait_details(decision, at, owner_epoch=None):
        return {"condition": decision.condition, "reason": decision.reason,
                "retry_at": (timestamp(at) + timedelta(seconds=decision.retry_after_seconds)).isoformat()
                    if decision.condition == "time" else None,
                "owner_epoch": owner_epoch, "since": at}

    def reconsider_contacts(self, *, owner_epoch):
        """Resume only declared conditions. No model, new evidence, or owner activity is invented."""
        with self.engine.db.connect(write=True) as conn:
            state, at = self._load(conn), self.clock()
            due = []
            for desire in state["desires"].values():
                wait = desire.get("contact_wait", {})
                if (desire["kind"] != "contact" or desire["status"] != "waiting"
                        or timestamp(desire["expires_at"]) <= timestamp(at)
                        or not self._fresh(conn, desire["evidence"])):
                    continue
                replied = False
                if wait.get("condition") == "owner_reply":
                    if wait.get("owner_epoch"):
                        replied = bool(owner_epoch and owner_epoch != wait["owner_epoch"])
                    else:
                        replied = bool(conn.execute(
                            "SELECT 1 FROM sources WHERE scope=? AND deleted=0 AND occurred_at>? "
                            "AND json_extract(data,'$.authority')='explicit' "
                            "AND json_extract(data,'$.metadata.role')='user' "
                            "AND json_extract(data,'$.metadata.host_event')='message' "
                            "AND json_extract(data,'$.metadata.channel') IN ('wechat','feishu') LIMIT 1",
                            (self.scope.key(), wait.get("since", at)),
                        ).fetchone())
                if (wait.get("condition") == "time" and wait.get("retry_at") and timestamp(wait["retry_at"]) <= timestamp(at)) or replied:
                    due.append(desire)
            if not due:
                return {"state": "unchanged", "resumed": []}
            self._retarget(conn, state, at)
            eid = "mind_" + digest([self.scope.key(), "contact-resumed", [(d["id"], d["revision"]) for d in due]])[:32]
            for desire in due:
                wait = desire.pop("contact_wait")
                desire.update(status="wanted", revision=desire["revision"] + 1,
                              updated_at=at, event_id=eid,
                              reason="Declared contact condition became ready: " + wait["condition"])
            self._retarget(conn, state, at)
            state.update(revision=state["revision"] + 1, updated_at=at)
            self._save(conn, state)
            result = {"state": "resumed", "resumed": [d["id"] for d in due]}
            self._history(conn, eid, state, "contact-resumed", result)
            return result

    def contact_candidate(self):
        with self.engine.db.connect() as conn:
            state = self._load(conn)
            view = self._view(conn, state, self.clock())
            if view["contact"].get("preference", {}).get("needs_review"):
                return {"eligible": False, "reason": "contact-preference-needs-review"}
            if (view.get("action_policy") or {}).get("needs_review"):
                return {"eligible": False, "reason": "action-policy-needs-review"}
            waiting = [{"id": d["id"], "expired": d["expired"], "needs_review": d["needs_review"],
                        **d.get("contact_wait", {"condition": "new_evidence", "reason": d.get("reason", "")})}
                       for d in view["desires"] if d["kind"] == "contact" and d["status"] == "waiting"]
            active = conn.execute(
                "SELECT id,state,data FROM mind_contacts WHERE scope=? AND state IN ('drafting','pending','unconfirmed')",
                (self.scope.key(),),
            ).fetchone()
            if active:
                return {
                    "eligible": False,
                    "reason": "attempt-in-progress",
                    "attempt_id": active["id"],
                    "state": active["state"],
                    "owner_epoch": json.loads(active["data"])["owner_epoch"],
                }

            if self._action_review_pending(conn):
                return {"eligible": False, "reason": "action-appraisal-pending"}
            from .autonomy_schema import enabled
            semantic = enabled(conn, self.scope.key())
            if not semantic and view["dimensions"]["initiative"]["needs_review"]:
                return {"eligible": False, "reason": "state-needs-review"}
            if not semantic and view["dimensions"]["initiative"]["projected_value"] < view["contact"]["threshold"]:
                return {"eligible": False, "reason": "below-threshold", "initiative": view["dimensions"]["initiative"]["value"], "waiting_desires": waiting}
            ready = [
                d for d in view["desires"] if self._desire_ready(conn, d, self.clock()) and not self._action_review_pending(conn, d)
            ]
            if not ready:
                return {"eligible": False, "reason": "no-actionable-desire", "waiting_desires": waiting}
            desire = min(
                ready, key=lambda d: (-d["strength"], d["created_at"], d["id"])
            )
            return {
                "eligible": True,
                "reason": "draft-required",
                "revision": state["revision"],
                "initiative": view["dimensions"]["initiative"]["value"],
                "desire": desire,
            }

    def claim_contact(self, *, owner_epoch):
        # Recheck under the same SQLite lock as reservation, without opening another
        # store or starting a model. Duplicate triggers cannot acquire another slot.
        with self.engine.db.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT id FROM mind_contacts WHERE scope=? AND state IN ('drafting','pending','unconfirmed')",
                (self.scope.key(),),
            ).fetchone()
            if existing:
                raise Conflict("An unresolved contact attempt already exists")

            if self._action_review_pending(conn):
                raise Conflict("Action appraisal is pending")
            state = self._load(conn)
            view = self._view(conn, state, self.clock())
            from .autonomy_schema import enabled
            semantic = enabled(conn, self.scope.key())
            ready = [
                d for d in view["desires"] if self._desire_ready(conn, d, self.clock()) and not self._action_review_pending(conn, d)
            ]
            if (
                not ready
                or (view.get("action_policy") or {}).get("needs_review")
                or view["contact"].get("preference", {}).get("needs_review")
                or (not semantic and (view["dimensions"]["initiative"]["needs_review"]
                or view["dimensions"]["initiative"]["projected_value"] < view["contact"]["threshold"]))
            ):
                raise Conflict("The contact threshold or desire is no longer current")
            desire = min(
                ready, key=lambda d: (-d["strength"], d["created_at"], d["id"])
            )
            aid = (
                "kin-mind-"
                + digest(
                    [
                        self.scope.key(),
                        desire["id"],
                        desire["revision"],
                        owner_epoch,
                        state["revision"],
                    ]
                )[:40]
            )
            previous = conn.execute(
                "SELECT data FROM mind_contacts WHERE id=?", (aid,)
            ).fetchone()
            if previous:
                return json.loads(previous[0])
            attempt = {
                "id": aid,
                "state": "drafting",
                "owner_epoch": owner_epoch,
                "desire_id": desire["id"],
                "desire_revision": desire["revision"],
                "created_at": self.clock(),
                "desire": desire,
            }
            conn.execute(
                "INSERT INTO mind_contacts VALUES(?,?,?,?)",
                (aid, self.scope.key(), "drafting", dumps(attempt)),
            )
            return attempt

    def check_contact(self, attempt_id, owner_epoch):
        with self.engine.db.connect() as conn:
            attempt = self._attempt(conn, attempt_id)
            state = self._load(conn)
            desire = state["desires"].get(attempt["desire_id"])
            view = self._view(conn, state, self.clock())
            value = view["dimensions"]["initiative"]
            from .autonomy_schema import enabled
            semantic = enabled(conn, self.scope.key())
            valid = (
                attempt["state"] == "drafting"
                and not (view.get("action_policy") or {}).get("needs_review")
                and not view["contact"].get("preference", {}).get("needs_review")
                and attempt["owner_epoch"] == owner_epoch
                and desire
                and desire["revision"] == attempt["desire_revision"]
                and self._desire_ready(conn, desire, self.clock())
                and (semantic or (not value["needs_review"]
                and value["projected_value"] >= state["profile"]["contact"]["threshold"]))
            )
            return {
                "eligible": bool(valid),
                "reason": "ready" if valid else "candidate-changed",
                "attempt": attempt,
            }

    def _attempt(self, conn, aid):
        row = conn.execute(
            "SELECT data FROM mind_contacts WHERE id=? AND scope=?",
            (aid, self.scope.key()),
        ).fetchone()
        if not row:
            raise Missing("Contact attempt is outside this scope or missing")
        return json.loads(row[0])

    def _action_review_pending(self, conn, desire=None):
        rows = conn.execute("SELECT kind,data FROM mind_action_events WHERE scope=? AND state IN ('pending','queued')", (self.scope.key(),)).fetchall()
        config = conn.execute("SELECT data FROM mind_memory_config WHERE scope=?", (self.scope.key(),)).fetchone() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_memory_config'").fetchone() else None
        if not (config and json.loads(config[0]).get("operational_lanes")):
            return bool(rows)
        # A timer or another idea does not revoke a verified intent. Explicit
        # intent/source dependencies still hold it until their review completes.
        if not desire:
            return False
        refs = {v for r in desire.get("evidence", []) for v in (r["record_id"], r["source_id"])}
        for row in rows:
            data = json.loads(row["data"])
            if data.get("desire_id") == desire["id"] or refs.intersection(data.get("invalidated_source_ids", [])):
                return True
        return False

    def settle_contact(self, *, attempt_id, state, message_id=None, message_ids=None, reason="", decision=None, partial=False, canceled_bubbles=0, aborted_before_send=False):
        decision = ContactDecision.model_validate(decision) if decision is not None else None
        if decision and state != "canceled":
            raise ValueError("Wish decisions apply only before sending")
        if state not in {"pending", "accepted", "unconfirmed", "canceled"}:
            raise ValueError("Unknown contact delivery state")
        if state == "accepted" and not (
            isinstance(message_id, str) and message_id.strip()
        ):
            raise ValueError("A platform message ID is required")
        with self.engine.db.connect(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if aborted_before_send and (state != "canceled" or message_id or attempt.get("message_id") or attempt.get("message_ids")):
                raise ValueError("Only a host-verified wholly unsent batch can be canceled")
            if type(partial) is not bool or type(canceled_bubbles) is not int or canceled_bubbles < 0:
                raise ValueError("Invalid partial delivery receipt")
            if attempt["state"] == "accepted":
                if state != "accepted" or attempt["message_id"] != message_id:
                    raise Conflict("Accepted delivery cannot be rewritten")
                return attempt
            if attempt["state"] == "canceled" or (
                state == "canceled" and attempt["state"] != "drafting" and not aborted_before_send
            ):
                raise Conflict(
                    "A possible send requires reconciliation, not cancellation"
                )
            attempt.update(state=state, updated_at=self.clock(), reason=reason)
            if message_id:
                attempt.update(message_id=message_id, visibility="unverified")
            if message_ids:
                if not all(isinstance(x, str) and x.strip() for x in message_ids):
                    raise ValueError("Message identifiers must be nonempty strings")
                attempt["message_ids"] = message_ids
            if partial:
                attempt.update(partial=True, canceled_bubbles=canceled_bubbles)
            conn.execute(
                "UPDATE mind_contacts SET state=?,data=? WHERE id=?",
                (state, dumps(attempt), attempt_id),
            )
            if state == "canceled" and (decision or reason in {"draft-empty", "draft-failed"}):
                current = self._load(conn)
                desire = current["desires"].get(attempt["desire_id"])
                # An empty draft is a decision to wait, not a failed send. Do not
                # overwrite a wish that changed while the model was drafting.
                if desire and desire["revision"] == attempt["desire_revision"] and desire["status"] == "wanted":
                    self._retarget(conn, current, self.clock())
                    eid = "mind_" + digest([attempt_id, "draft-decision"])[:32]
                    if reason == "draft-failed":
                        failures = desire.get("contact_failures", 0) + 1
                        desire["contact_failures"] = failures
                        decision = ContactDecision(action="wait", reason="Draft generation or parsing failed",
                            condition="time" if failures < 3 else "new_evidence",
                            retry_after_seconds=300 * failures)
                    decision = decision or ContactDecision(action="wait", reason="Legacy empty draft; a new related source is required")
                    desire.update(status="abandoned" if decision.action == "abandon" else "waiting", revision=desire["revision"] + 1,
                                  updated_at=self.clock(), event_id=eid,
                                  reason=decision.reason)
                    if decision.action == "wait":
                        desire["contact_wait"] = self._wait_details(decision, self.clock(), attempt["owner_epoch"])
                    else:
                        desire.pop("contact_wait", None)
                    attempt["decision"] = decision.model_dump()
                    conn.execute("UPDATE mind_contacts SET data=? WHERE id=?", (dumps(attempt), attempt_id))
                    self._retarget(conn, current, self.clock())
                    current["revision"] += 1
                    current["updated_at"] = self.clock()
                    self._save(conn, current)
                    self._history(conn, eid, current, "contact-deferred", attempt)
            if state == "accepted":
                current = self._load(conn)
                desire = current["desires"][attempt["desire_id"]]
                from .plans import AutonomousPlans
                AutonomousPlans(self).settle_linked(conn, desire, {"id": attempt_id,
                    "complete": not partial, "kind": "delivery", "message_ids": message_ids or [message_id],
                    "partial": partial, "visibility": "unverified"})
                desire.update(
                    status="completed",
                    delivery={
                        "state": "partial" if partial else "accepted",
                        "message_id": message_id,
                        "message_ids": message_ids or [message_id],
                        "canceled_bubbles": canceled_bubbles,
                        "visibility": "unverified",
                    },
                    revision=desire["revision"] + 1,
                    updated_at=self.clock(),
                )
                # Acceptance is an execution fact. DeepSeek evaluates satisfaction;
                # the transport never assigns an emotion score or invents a thought.
                event_key = "delivery_" + digest([attempt_id, "accepted"])[:32]
                conn.execute("INSERT OR IGNORE INTO mind_action_events VALUES(?,?,?,?,?,?)", (
                    event_key, self.scope.key(), "delivery", self.clock(), "pending",
                    dumps({"attempt_id": attempt_id, "desire_id": desire["id"],
                           "evidence_ids": [r["record_id"] for r in desire["evidence"]],
                           "message_ids": message_ids or [message_id],
                           "partial": partial, "canceled_bubbles": canceled_bubbles,
                           "agent_version": current["agent_version"],
                           "visibility": "unverified"}),
                ))
                current["revision"] += 1
                current["updated_at"] = self.clock()
                self._save(conn, current)
                self._history(
                    conn,
                    "mind_" + digest([attempt_id, "accepted"])[:32],
                    current,
                    "delivery",
                    attempt,
                )
            return attempt
