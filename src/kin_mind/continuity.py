"""Source-backed continuity within Mind's existing transaction and revision log."""

import re
from copy import deepcopy
from typing import Literal

from pydantic import Field, FiniteFloat, StrictBool, StrictInt, model_validator

from eventmem.core.db import Conflict, Missing, digest
from eventmem.core.models import Model

from .expression import compile_expression
from .rhythm import INTERACTION_SCHEMA, interaction_windows, rhythm_view, stamp

FEATURES = ("interpretation", "concerns", "expression", "rhythm")
CONFIDENCE_THRESHOLD = 0.65

SCHEMA = (
    INTERACTION_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS mind_concern_evidence(
 scope TEXT NOT NULL,concern_id TEXT NOT NULL,evidence_key TEXT NOT NULL,
 event_id TEXT NOT NULL,source_id TEXT NOT NULL,
 PRIMARY KEY(scope,concern_id,evidence_key));
"""
)


class Understanding(Model):
    meaning: str = Field(min_length=1, max_length=600)
    topic: str = Field(min_length=1, max_length=300)
    importance: StrictInt = Field(ge=0, le=100)
    confidence: FiniteFloat = Field(ge=0, le=1)
    basis: Literal["explicit", "inferred", "internal_thought"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class RhythmProposal(Model):
    phase: Literal["awake", "settling", "drowsy", "resting", "roused", "recovering"]
    alertness: StrictInt = Field(ge=0, le=100)
    target: StrictInt = Field(ge=0, le=100)
    half_life_minutes: Literal[20, 60, 180]
    reason: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class OwnerRequest(Model):
    kind: Literal["help", "invitation", "request"]
    action: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    completion: str = Field(min_length=1, max_length=500)
    status: Literal["proposed", "accepted", "waiting", "completed", "declined"] = "proposed"


class ConcernProposal(Model):
    action: Literal["create", "update", "ease", "resolve", "reopen", "archive"]
    concern_id: str | None = None
    key: str | None = Field(default=None, min_length=1, max_length=200)
    kind: (
        Literal["care", "anticipation", "curiosity", "distress", "shared_plan"] | None
    ) = None
    content: str | None = Field(default=None, min_length=1, max_length=1600)
    topic: str | None = Field(default=None, min_length=1, max_length=400)
    target: str | None = Field(default=None, max_length=300)
    intensity: StrictInt | None = Field(default=None, ge=0, le=100)
    basis: Literal["explicit", "inferred", "internal_thought"] | None = None
    confidence: FiniteFloat | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=1, max_length=600)
    owner_request: OwnerRequest | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.action == "create":
            if self.concern_id or any(
                getattr(self, key) is None
                for key in (
                    "key",
                    "kind",
                    "content",
                    "topic",
                    "intensity",
                    "basis",
                    "confidence",
                )
            ):
                raise ValueError(
                    "New concerns require a stable topic key, kind, content, topic, intensity, basis and confidence"
                )
        elif not self.concern_id:
            raise ValueError("Concern transitions require a concern ID")
        return self


class ContinuityCommand(Model):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


class ConcernChange(ConcernProposal):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


class ContinuityConfig(ContinuityCommand):
    features: dict[
        Literal["interpretation", "concerns", "expression", "rhythm"], StrictBool
    ]
    activation: Literal["shadow", "active"] = "active"
    reason: str = Field(min_length=1, max_length=1200)


def evidence_key(ref):
    # Derived summaries keep the original source ID; record IDs never add weight.
    return digest([ref["source_id"], ref["hash"]])


def low_confidence(value):
    return (
        value.get("basis") == "inferred"
        and value.get("confidence", 0) < CONFIDENCE_THRESHOLD
    )


def concern_projection(entry, at):
    result = deepcopy(entry)
    elapsed = max(0.0, (stamp(at) - stamp(entry["assessed_at"])).total_seconds()) / 3600
    if entry["status"] in {"active", "easing"}:
        delay = 0 if entry["status"] == "easing" else entry["easing_after_hours"]
        if elapsed >= delay:
            result["status"] = "easing"
        result["intensity"] = round(
            entry["intensity"]
            * 0.5 ** (max(0, elapsed - delay) / entry["half_life_hours"]),
            3,
        )
    return result


def select_concerns(concerns, query="", limit=3, intent=None):
    def tokens(text):
        text = text.lower()
        return set(re.findall(r"[a-z0-9_]+", text)) | {
            text[i : i + 2]
            for i in range(len(text) - 1)
            if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
        }

    wanted = tokens(query)
    active = [
        c
        for c in concerns
        if c["status"] in {"active", "easing"} and not c["needs_review"]
    ]
    active.sort(
        key=lambda c: (
            len(wanted & tokens(c["topic"] + " " + c["content"])),
            c["updated_at"],
            c["intensity"],
            c["id"],
        ),
        reverse=True,
    )
    if intent:
        # A fresh intent chooses first: the concern it named, or else the one its topic really
        # overlaps. The overlap ranking above stays, and fills whatever is left of the three.
        picked = []
        for entry in intent.get("continue_topics", []):
            found = next((c for c in active if c["id"] == entry.get("concern_id")), None)
            if found is None:
                asked = tokens(entry.get("topic") or "")
                found = next((c for c in active if asked & tokens(c["topic"] + " " + c["content"])), None)
            if found is not None and found["id"] not in picked:
                picked.append(found["id"])
        order = {identifier: index for index, identifier in enumerate(picked)}
        active.sort(key=lambda c: order.get(c["id"], len(order)))
    return [
        {
            k: c.get(k)
            for k in (
                "id",
                "revision",
                "kind",
                "content",
                "topic",
                "target",
                "intensity",
                "status",
                "basis",
                "confidence",
                "evidence_ids",
                "owner_request",
            )
        }
        for c in active[:limit]
    ]


class Continuity:
    """Mixin: every write runs through Mind._mutate and its SQLite transaction."""

    def _continuity_flags(self, conn, state):
        config = state.get("continuity", {})
        fresh = bool(config) and self._fresh(conn, config["evidence"])
        return {
            key: bool(config.get("features", {}).get(key)) and fresh for key in FEATURES
        }

    def configure_continuity(self, request: ContinuityConfig):
        def apply(conn, state, event_id):
            refs = self._evidence(conn, request.evidence_ids)
            if not self._fresh(conn, refs) or any(
                r["authority"] != "explicit" for r in refs
            ):
                raise Conflict(
                    "Continuity configuration requires current owner evidence"
                )
            old = state.get("continuity", {})
            flags = {key: old.get("features", {}).get(key, False) for key in FEATURES}
            flags.update(request.features)
            state["continuity"] = {
                "version": request.agent_version,
                "features": flags,
                "activation": request.activation,
                "evidence": refs,
                "event_id": event_id,
                "configured_at": self.clock(),
                "reason": request.reason,
            }
            state.setdefault("concerns", {})
            return {
                "features": flags,
                "activation": request.activation,
                "config_version": request.agent_version,
            }

        return self._mutate(request, "continuity-config", apply)

    def manage_concern(self, request: ConcernChange):
        return self._mutate(
            request,
            "concern",
            lambda conn, state, event_id: self._apply_concern(
                conn, state, request, event_id, request.agent_version
            ),
        )

    def _proposal_refs(self, conn, ids, fallback, allowed=None):
        refs = self._evidence(conn, ids) if ids else fallback
        if not refs or not self._fresh(conn, refs):
            raise Conflict("Continuity proposal requires current source evidence")
        if allowed is not None and {evidence_key(r) for r in refs} - {
            evidence_key(r) for r in allowed
        }:
            raise Conflict("Proposal cites evidence outside this appraisal")
        return refs

    def _continuity_sources(self, conn, state, refs):
        allowed = {evidence_key(r): r for r in refs}
        candidates = [
            *state.get("concerns", {}).values(),
            *(
                d
                for d in state.get("desires", {}).values()
                if d["status"] in {"wanted", "waiting", "in_progress"}
            ),
            *state["dimensions"].values(),
        ]
        checked = set(allowed)
        for entry in candidates:
            for ref in entry.get("evidence", []):
                key = evidence_key(ref)
                if key in checked:
                    continue
                checked.add(key)
                if self._fresh(conn, [ref]):
                    allowed[key] = ref
        return list(allowed.values())

    def _apply_concern(
        self, conn, state, change, event_id, version, fallback=None, allowed=None
    ):
        if not self._continuity_flags(conn, state)["concerns"]:
            raise Conflict("Concern feature is disabled")
        refs = list(
            {
                evidence_key(r): r
                for r in self._proposal_refs(
                    conn, change.evidence_ids, fallback or [], allowed
                )
            }.values()
        )
        concerns = state.setdefault("concerns", {})
        cid = (
            ("concern_" + digest([self.scope.key(), change.key])[:32])
            if change.action == "create"
            else change.concern_id
        )
        current = concerns.get(cid)
        if change.action != "create" and not current:
            raise Missing("Concern belongs to another scope or is missing")
        new_refs = [
            r
            for r in refs
            if not conn.execute(
                "SELECT 1 FROM mind_concern_evidence WHERE scope=? AND concern_id=? AND evidence_key=?",
                (self.scope.key(), cid, evidence_key(r)),
            ).fetchone()
        ]
        if current and not new_refs:
            return {
                "concern_id": cid,
                "concern_revision": current["revision"],
                "replayed": True,
            }
        if (
            current
            and current["status"] in {"resolved", "archived"}
            and change.action != "reopen"
        ):
            raise Conflict(
                "A closed concern requires an explicit reopen with new evidence"
            )
        if change.action == "reopen" and current["status"] not in {
            "resolved",
            "archived",
        }:
            raise Conflict("Only a closed concern can reopen")
        if change.action == "resolve" and not new_refs:
            raise Conflict("Resolution requires a new outcome or correction source")
        if change.owner_request:
            request = change.owner_request
            if request.status in {"accepted", "completed", "declined"} and not any(
                r["authority"] == "explicit" and r.get("metadata", {}).get("role") == "user"
                for r in new_refs
            ):
                raise Conflict("Owner participation needs a new explicit owner response")
        at = self.clock()
        entry = (
            concern_projection(current, at)
            if current
            else {
                "id": cid,
                "key": change.key,
                "revision": 0,
                "created_at": at,
                "status": "active",
                "recurrence_count": 0,
                "target": "",
                "easing_after_hours": 12,
                "half_life_hours": 48,
            }
        )
        for name in (
            "content",
            "topic",
            "kind",
            "target",
            "intensity",
            "basis",
            "confidence",
        ):
            value = getattr(change, name)
            if value is not None:
                entry[name] = value
        if change.owner_request:
            entry["owner_request"] = change.owner_request.model_dump()
        if entry["basis"] == "explicit" and not any(
            r["authority"] == "explicit" for r in refs
        ):
            raise Conflict("Explicit interpretation requires an explicit source")
        if change.action == "resolve":
            if entry.get("owner_request") and entry["owner_request"]["status"] not in {"completed", "declined"}:
                raise Conflict("Sending a request does not resolve the awaited owner outcome")
            if low_confidence(entry):
                raise Conflict("Unverified interpretation cannot resolve a concern")
            entry.update(status="resolved", intensity=0, resolution_evidence=refs)
        elif change.action == "archive":
            entry["status"] = "archived"
        elif change.action == "ease":
            entry["status"] = "easing"
        elif change.action == "reopen":
            entry.update(
                status="active", recurrence_count=entry["recurrence_count"] + 1
            )
        elif change.action in {"create", "update"}:
            entry["status"] = "active"
        # Reads never move this clock. New observations, including corrections, do.
        entry.update(
            revision=entry["revision"] + 1,
            assessed_at=at,
            updated_at=at,
            evidence=refs,
            reason=change.reason,
            event_id=event_id,
            agent_version=version,
            config_version=state["continuity"]["version"],
        )
        concerns[cid] = entry
        for ref in new_refs:
            conn.execute(
                "INSERT INTO mind_concern_evidence VALUES(?,?,?,?,?)",
                (self.scope.key(), cid, evidence_key(ref), event_id, ref["source_id"]),
            )
        return {"concern_id": cid, "concern_revision": entry["revision"]}

    def _resolve_concern_links(self, conn, state, identifiers):
        concerns = state.get("concerns", {})
        links = {}
        for identifier in identifiers:
            cid = (
                identifier
                if identifier in concerns
                else "concern_" + digest([self.scope.key(), identifier])[:32]
            )
            entry = concerns.get(cid)
            if not entry:
                raise Missing("Linked concern is missing from this scope")
            if not self._fresh(conn, entry["evidence"]) or low_confidence(entry):
                raise Conflict("Linked concern requires review")
            links[cid] = entry["revision"]
        return links

    def _concern_links_fresh(self, conn, state, desire):
        if (
            not self._continuity_flags(conn, state)["concerns"]
            or state.get("continuity", {}).get("activation") == "shadow"
        ):
            return True
        for cid, revision in desire.get("concern_revisions", {}).items():
            concern = state.get("concerns", {}).get(cid)
            if (
                not concern
                or concern["revision"] != revision
                or low_confidence(concern)
                or not self._fresh(conn, concern["evidence"])
            ):
                return False
        return True

    def _apply_continuity(
        self, conn, state, request, event_id, refs, previous, allowed=None
    ):
        flags = self._continuity_flags(conn, state)
        allowed = allowed or (
            self._continuity_sources(conn, state, refs)
            if request.understanding or request.rhythm
            else refs
        )
        changes = {
            key: {
                "before": round(previous[key], 3),
                "proposed": score,
                "applied": state["dimensions"][key]["score"],
                "delta": round(score - previous[key], 3),
            }
            for key, score in request.values.items()
        }
        summary = {
            "event_id": event_id,
            "at": self.clock(),
            "agent_version": request.agent_version,
            "reason": request.reason,
            "changes": changes,
            "evidence": refs,
        }
        if flags["interpretation"] and request.understanding:
            proposal = request.understanding
            evidence = self._proposal_refs(conn, proposal.evidence_ids, refs, allowed)
            if proposal.basis == "explicit" and not any(
                r["authority"] == "explicit" for r in evidence
            ):
                raise Conflict("Explicit interpretation requires an explicit source")
            summary["understanding"] = {
                **proposal.model_dump(exclude={"evidence_ids"}),
                "evidence": evidence,
            }
            for key in request.values:
                state["dimensions"][key]["interpretation_unverified"] = low_confidence(
                    summary["understanding"]
                )
        if flags["interpretation"]:
            state["last_assessment"] = summary
        if flags["rhythm"] and request.rhythm:
            proposal = request.rhythm
            evidence = self._proposal_refs(conn, proposal.evidence_ids, refs, allowed)
            state["rhythm"] = {
                **proposal.model_dump(exclude={"evidence_ids"}),
                "evidence": evidence,
                "at": self.clock(),
                "event_id": event_id,
                "config_version": state["continuity"]["version"],
            }

    def _continuity_view(self, conn, state, result, at, query=""):
        flags = self._continuity_flags(conn, state)
        config = state.get("continuity", {})
        result["continuity"] = {
            "features": flags,
            "version": config.get("version"),
            "activation": config.get("activation", "active"),
            "needs_review": bool(config) and not self._fresh(conn, config["evidence"]),
        }
        result["concerns"] = []
        if flags["concerns"]:
            for entry in state.get("concerns", {}).values():
                view = concern_projection(entry, at)
                view["needs_review"] = low_confidence(entry) or not self._fresh(
                    conn, entry["evidence"]
                )
                view["evidence_ids"] = [r["record_id"] for r in entry["evidence"]]
                view.pop("evidence", None)
                view.pop("resolution_evidence", None)
                result["concerns"].append(view)
        assessment = state.get("last_assessment") if flags["interpretation"] else None
        if assessment:
            assessment = deepcopy(assessment)
            refs = assessment.pop("evidence")
            assessment["evidence_ids"] = [r["record_id"] for r in refs]
            assessment["needs_review"] = not self._fresh(conn, refs)
            if assessment.get("understanding"):
                inner = assessment["understanding"]
                inner["needs_review"] = low_confidence(inner) or not self._fresh(
                    conn, inner["evidence"]
                )
                inner["evidence_ids"] = [r["record_id"] for r in inner.pop("evidence")]
        result["appraisal_summary"] = assessment
        topic = query or ((assessment or {}).get("understanding") or {}).get(
            "topic", ""
        )
        # The intent the last appraisal stated, while it is still fresh. Everything below falls
        # back to what it always did when it is not, and nothing here reads it while its switch
        # is off. A new owner message does not end an intent: see expression_intent.
        from .expression_intent import view as intent_view
        speaking = flags["expression"] and config.get("activation", "active") == "active"
        intent = intent_view(conn, self, at, continuity_active=speaking,
                             persona=result.get("persona_contract"))
        if intent and intent["status"]:
            result["continuity"]["expression_intent"] = intent["status"]
        stated = (intent or {}).get("use")
        result["selected_concerns"] = select_concerns(result["concerns"], topic, intent=stated)
        if flags["rhythm"]:
            interactions = interaction_windows(conn, self.scope.key(), at)
            entry = state.get("rhythm")
            result["rhythm"] = rhythm_view(
                entry,
                at,
                interactions,
                fresh=not entry or self._fresh(conn, entry["evidence"]),
            )
        else:
            result["rhythm"] = {"status": "disabled", "mode": "interaction-led"}
        if speaking:
            result["expression"] = compile_expression(
                result["dimensions"],
                rhythm=result["rhythm"],
                config_version=config.get("version"),
                persona=result.get("persona_contract"),
                intent=stated,
            )
            result["expression"]["concern_ids"] = [
                c["id"] for c in result["selected_concerns"]
            ]
            result["expression"]["fingerprint"] = digest(
                [result["expression"], result["selected_concerns"]]
            )
        else:
            result["expression"] = None
        for desire in result["desires"]:
            desire["concern_needs_review"] = not self._concern_links_fresh(
                conn, state, desire
            )
