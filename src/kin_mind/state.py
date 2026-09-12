"""Transactional affect and desires in the existing private memory database.

Snapshots are projections, not measurements. Events, configuration revisions and
source revisions remain inspectable. No model or transport runs inside this store.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, FiniteFloat, StrictInt, field_validator, model_validator

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import Model, Scope, now, utc
from eventmem.core.self_knowledge import SelfKnowledge, metadata

from .profile import default_profile

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


class AffectiveEvent(Model):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    values: dict[str, StrictInt] = Field(default_factory=dict, max_length=20)
    reason: str = Field(min_length=1, max_length=1200)
    origin: Literal["interaction", "exploration", "reflection"] = "interaction"
    evolution: Evolution | None = None

    @field_validator("values")
    @classmethod
    def scores(cls, values):
        if any(not 0 <= value <= 100 for value in values.values()):
            raise ValueError("Scores must be integers from 0 to 100")
        return values

    @model_validator(mode="after")
    def separate_evolution(self):
        if self.evolution and self.values:
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
    reason: str = Field(min_length=1, max_length=1200)

    _time = field_validator("expires_at")(lambda v: utc(v) if v else v)

    @model_validator(mode="after")
    def shape(self):
        if self.action == "create":
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


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def project(entry, at):
    elapsed = max(0, (timestamp(at) - timestamp(entry["at"])).total_seconds())
    value = entry["target"] + (entry["score"] - entry["target"]) * 0.5 ** (
        elapsed / (entry["half_life_hours"] * 3600)
    )
    return min(100.0, max(0.0, value))


class Mind:
    def __init__(self, engine, scope: Scope, clock=now):
        self.engine, self.scope, self.clock = engine, scope, clock
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

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
            raise Conflict("Evidence is not current")
        return {
            "source_id": sid,
            "hash": row["hash"],
            "record_id": rid,
            "revision": record["revision"],
            "authority": data["authority"],
            "session": row["session"],
            "occurred_at": row["occurred_at"],
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

    def _mutate(self, request, kind, fn):
        payload = request.model_dump() if hasattr(request, "model_dump") else request
        with self.engine.db.connect(write=True) as conn:

            def run():
                state = self._load(conn)
                if state["revision"] != payload["expected_revision"]:
                    raise Conflict(
                        "Mind revision changed; read current state before updating"
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
                conn, self._key(payload["command_id"]), payload, run
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

    def _desire_ready(self, conn, desire, at):
        return (
            desire["kind"] == "contact"
            and desire["status"] == "wanted"
            and timestamp(desire["expires_at"]) > timestamp(at)
            and self._fresh(conn, desire["evidence"])
        )

    def _initiative_value(self, conn, state, at):
        entry = state["dimensions"]["initiative"]
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
        value = self._initiative_value(conn, state, at)
        target = max(
            [
                state["profile"]["dimensions"]["initiative"]["baseline"],
                *[
                    max(d["strength"], self._autonomy(conn, state).get("initiative_target", 0))
                    for d in state["desires"].values()
                    if self._desire_ready(conn, d, at)
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

    def _apply_event(self, conn, state, request, event_id):
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
        for key, score in request.values.items():
            spec = state["profile"]["dimensions"][key]
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
        state["last_evidence_key"] = evidence_key
        self._retarget(conn, state, self.clock())

    def manage_desire(self, request: DesireChange):
        return self._mutate(
            request,
            "desire",
            lambda conn, state, eid: self._apply_desire(conn, state, request, eid),
        )

    def _apply_desire(self, conn, state, request, event_id):
        refs = self._evidence(conn, request.evidence_ids)
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
            )
        else:
            did = request.desire_id
            if did not in state["desires"]:
                raise Missing("Desire is outside this scope or missing")
            desire = state["desires"][did]
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
            if request.action == "update":
                for k in ("content", "topic", "strength", "expires_at", "completion"):
                    if getattr(request, k) is not None:
                        desire[k] = getattr(request, k)
            desire["revision"] += 1
        desire = state["desires"][did]
        desire.update(
            evidence=refs,
            reason=request.reason,
            updated_at=at,
            event_id=event_id,
            agent_version=request.agent_version,
        )
        self._retarget(conn, state, at)
        return {"desire_id": did, "desire_revision": desire["revision"]}

    def _evolve(self, conn, state, request, refs, event_id):
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
            }
        traits = {
            k: dict(v, needs_review=not self._entry_fresh(conn, v))
            for k, v in state["traits"].items()
        }
        return {
            "revision": state["revision"],
            "scope": self.scope.model_dump(),
            "as_of": at,
            "agent_version": state["agent_version"],
            "profile_version": state["profile_version"],
            "dimensions": values,
            "desires": desires,
            "traits": traits,
            "contact": state["profile"]["contact"],
            "exploration": state["profile"]["exploration"],
            "autonomy": self._autonomy(conn, state),
            "last_evolution_day": state["last_evolution_day"],
        }

    def read(self, *, as_of=None, history=0):
        if not 0 <= history <= 100:
            raise ValueError("History must be between 0 and 100")
        with self.engine.db.connect() as conn:
            result = self._view(
                conn, self._load(conn), utc(as_of) if as_of else self.clock()
            )
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

    def contact_candidate(self):
        with self.engine.db.connect() as conn:
            state = self._load(conn)
            view = self._view(conn, state, self.clock())
            active = conn.execute(
                "SELECT id,state FROM mind_contacts WHERE scope=? AND state IN ('drafting','pending','unconfirmed')",
                (self.scope.key(),),
            ).fetchone()
            if active:
                return {
                    "eligible": False,
                    "reason": "attempt-in-progress",
                    "attempt_id": active["id"],
                    "state": active["state"],
                }
            if view["dimensions"]["initiative"]["needs_review"]:
                return {"eligible": False, "reason": "state-needs-review"}
            if view["dimensions"]["initiative"]["value"] < view["contact"]["threshold"]:
                return {"eligible": False, "reason": "below-threshold"}
            ready = [
                d for d in view["desires"] if self._desire_ready(conn, d, self.clock())
            ]
            if not ready:
                return {"eligible": False, "reason": "no-actionable-desire"}
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
            state = self._load(conn)
            view = self._view(conn, state, self.clock())
            ready = [
                d for d in view["desires"] if self._desire_ready(conn, d, self.clock())
            ]
            if (
                not ready
                or view["dimensions"]["initiative"]["needs_review"]
                or view["dimensions"]["initiative"]["value"]
                < view["contact"]["threshold"]
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
            value = self._view(conn, state, self.clock())["dimensions"]["initiative"]
            valid = (
                attempt["state"] == "drafting"
                and attempt["owner_epoch"] == owner_epoch
                and desire
                and desire["revision"] == attempt["desire_revision"]
                and self._desire_ready(conn, desire, self.clock())
                and not value["needs_review"]
                and value["value"] >= state["profile"]["contact"]["threshold"]
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

    def settle_contact(self, *, attempt_id, state, message_id=None, reason=""):
        if state not in {"pending", "accepted", "unconfirmed", "canceled"}:
            raise ValueError("Unknown contact delivery state")
        if state == "accepted" and not (
            isinstance(message_id, str) and message_id.strip()
        ):
            raise ValueError("A platform message ID is required")
        with self.engine.db.connect(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["state"] == "accepted":
                if state != "accepted" or attempt["message_id"] != message_id:
                    raise Conflict("Accepted delivery cannot be rewritten")
                return attempt
            if attempt["state"] == "canceled" or (
                state == "canceled" and attempt["state"] != "drafting"
            ):
                raise Conflict(
                    "A possible send requires reconciliation, not cancellation"
                )
            attempt.update(state=state, updated_at=self.clock(), reason=reason)
            if message_id:
                attempt.update(message_id=message_id, visibility="unverified")
            conn.execute(
                "UPDATE mind_contacts SET state=?,data=? WHERE id=?",
                (state, dumps(attempt), attempt_id),
            )
            if state == "canceled" and reason == "draft-empty":
                current = self._load(conn)
                desire = current["desires"].get(attempt["desire_id"])
                # An empty draft is a decision to wait, not a failed send. Do not
                # overwrite a wish that changed while the model was drafting.
                if desire and desire["revision"] == attempt["desire_revision"] and desire["status"] == "wanted":
                    self._retarget(conn, current, self.clock())
                    eid = "mind_" + digest([attempt_id, "draft-empty"])[:32]
                    desire.update(status="waiting", revision=desire["revision"] + 1,
                                  updated_at=self.clock(), event_id=eid,
                                  reason="Kin returned an empty draft; wait for a sourced reconsideration")
                    self._retarget(conn, current, self.clock())
                    current["revision"] += 1
                    current["updated_at"] = self.clock()
                    self._save(conn, current)
                    self._history(conn, eid, current, "contact-deferred", attempt)
            if state == "accepted":
                current = self._load(conn)
                desire = current["desires"][attempt["desire_id"]]
                desire.update(
                    status="completed",
                    delivery={
                        "state": "accepted",
                        "message_id": message_id,
                        "visibility": "unverified",
                    },
                    revision=desire["revision"] + 1,
                    updated_at=self.clock(),
                )
                entry = current["dimensions"]["initiative"]
                entry.update(
                    score=current["profile"]["contact"]["reset"],
                    at=self.clock(),
                    event_id="mind_" + digest([attempt_id, "accepted"])[:32],
                    basis="delivery_observed",
                    reason="平台接收主动消息；手机已读未验证",
                )
                self._retarget(conn, current, self.clock())
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
