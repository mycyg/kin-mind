"""Source-backed continuity within Mind's existing transaction and revision log."""

import json
import re
from copy import deepcopy
from datetime import timezone
from typing import Literal

from pydantic import Field, FiniteFloat, StrictBool, StrictInt, model_validator

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import Model

from .affect_layers import build_view
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
    meaning: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    importance: StrictInt = Field(ge=0, le=100)
    confidence: FiniteFloat = Field(ge=0, le=1)
    basis: Literal["explicit", "inferred", "internal_thought"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class RhythmProposal(Model):
    phase: Literal["awake", "settling", "drowsy", "resting", "roused", "recovering"]
    base_phase: Literal["awake", "settling", "drowsy", "resting", "roused", "recovering"] | None = None
    attention_for: Literal["owner_message", "task", "urgent"] | None = None
    return_to: Literal["awake", "settling", "drowsy", "resting", "roused", "recovering"] | None = None
    current_activity: str | None = Field(default=None, max_length=600)
    note: str | None = Field(default=None, max_length=1200)
    alertness: StrictInt = Field(ge=0, le=100)
    target: StrictInt = Field(ge=0, le=100)
    half_life_minutes: Literal[20, 60, 180]
    # Legacy callers may still say why. The host never requires or invents a cause for a phase.
    reason: str = Field(default="", max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class OwnerRequest(Model):
    kind: Literal["help", "invitation", "request"]
    action: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    completion: str = Field(min_length=1)
    status: Literal["proposed", "accepted", "waiting", "completed", "declined"] = "proposed"


class ConcernBase(Model):
    """A concern change as every caller states it. The appraisal's proposal adds its window below;
    the host's and the MCP server's `ConcernChange` take exactly what they always took."""
    action: Literal["create", "update", "ease", "resolve", "reopen", "archive"]
    concern_id: str | None = None
    key: str | None = Field(default=None, min_length=1, max_length=200)
    kind: (
        Literal["care", "anticipation", "curiosity", "distress", "shared_plan"] | None
    ) = None
    content: str | None = Field(default=None, min_length=1)
    topic: str | None = Field(default=None, min_length=1)
    target: str | None = Field(default=None)
    intensity: StrictInt | None = Field(default=None, ge=0, le=100)
    basis: Literal["explicit", "inferred", "internal_thought"] | None = None
    confidence: FiniteFloat | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="")
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


# `timed_concerns` (default off): "下次聊到时记得问". The two fields exist in the model, and the schema
# the appraisal is offered carries them only while the switch is on (appraisal.appraisal_schema); the
# commit ignores them while it is off, so off is exactly the concern it was.
TIMED_FIELDS = ("surface_after", "surface_until")


class ConcernProposal(ConcernBase):
    surface_after: str | None = Field(default=None, min_length=1, max_length=40)
    surface_until: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def window(self):
        after, until = (surface_moment(getattr(self, name)) for name in TIMED_FIELDS)
        if after and until and after >= until:
            raise ValueError("A concern's window opens before it closes")
        return self


def surface_moment(value):
    """A window's edge as a moment: ISO 8601, Asia/Singapore when it names no zone."""
    if value is None:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo("Asia/Singapore"))


def timed_entering(conn, scope, concerns, at):
    """The timed concerns entering their window at `at`: open, not waiting for review, past
    `surface_after` (or since their window was set, with none), not past `surface_until`, and not
    yet delivered -- no context delivery accepted into a native window since the window opened names
    them (`mind_context_deliveries`, the existing receipts). Reads only."""
    def aware(moment):
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    now = aware(stamp(at))
    opened = {}
    for concern in concerns:
        if not (concern.get("surface_after") or concern.get("surface_until")):
            continue
        if concern.get("status") not in {"active", "easing"} or concern.get("needs_review"):
            continue
        after, until = surface_moment(concern.get("surface_after")), surface_moment(concern.get("surface_until"))
        since = max(filter(None, (after, aware(stamp(concern["surface_set_at"])) if concern.get("surface_set_at") else None)), default=None)
        if (after and now < after) or (until and now > until) or since is None:
            continue
        opened[concern["id"]] = since
    if not opened:
        return ()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_context_deliveries'").fetchone():
        return tuple(opened)
    earliest = min(opened.values()).astimezone(timezone.utc).isoformat()
    delivered = set()
    for (data,) in conn.execute(
            # `at` is the row's last write, which for an accepted one is its acceptance or later.
            "SELECT data FROM mind_context_deliveries WHERE scope=? AND state='accepted'"
            " AND julianday(at)>=julianday(?)", (scope, earliest)):
        value = json.loads(data)
        accepted = aware(stamp(value["accepted_at"])) if value.get("accepted_at") else None
        text = value.get("text") or ""
        for identifier, since in opened.items():
            if accepted and accepted >= since and identifier in text:
                delivered.add(identifier)
    return tuple(identifier for identifier in opened if identifier not in delivered)


class ContinuityCommand(Model):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


class ConcernChange(ConcernBase):
    command_id: str = Field(min_length=1, max_length=200)
    agent_version: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class ContinuityConfig(ContinuityCommand):
    features: dict[
        Literal["interpretation", "concerns", "expression", "rhythm"], StrictBool
    ]
    activation: Literal["shadow", "active"] = "active"
    reason: str = Field(min_length=1)


def evidence_key(ref):
    # These are ordinary database references, not proof of what the cited text should contain.
    return dumps([ref["source_id"], ref["record_id"], ref["revision"]])


def subjective_concern(change, current=None):
    """Kin's own attitude, apart from a shared plan or actual owner participation."""
    return ((getattr(change, "basis", None) or (current or {}).get("basis")) == "internal_thought"
            and (getattr(change, "kind", None) or (current or {}).get("kind")) != "shared_plan"
            and not getattr(change, "owner_request", None) and not (current or {}).get("owner_request"))


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


def select_concerns(concerns, query="", limit=3, intent=None, entering=()):
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
    if entering:
        # A timed concern entering its window (`timed_concerns`, `timed_entering`) goes ahead of all
        # of that, in the order it was given; the rest keep the order above.
        first = {identifier: index for index, identifier in enumerate(entering)}
        active.sort(key=lambda c: first.get(c["id"], len(first)))
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
            rebase=lambda conn, state: subjective_concern(
                request, state.get("concerns", {}).get(request.concern_id)),
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

    def _subjective_refs(self, conn, ids, allowed=None):
        """A subjective state needs no cause. Explicit citations still have to be real and shown.

        In particular, the current appraisal's triggering source is not automatically made into
        evidence of *why* Kin feels or chooses something.
        """
        return self._proposal_refs(conn, ids, [], allowed) if ids else []

    def _concern_fresh(self, conn, entry):
        return (self._subjective_available(conn, entry["evidence"])
                if subjective_concern(None, entry) else self._fresh(conn, entry["evidence"]))

    def _attention_event(self, conn, input_id, source_id=None):
        """The existing real input envelope, never a new event or an inferred experience."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
            return None
        row = conn.execute(
            "SELECT e.id,e.data FROM mind_runtime_events e JOIN mind_reply_inputs i "
            "ON i.scope=e.scope AND i.id=json_extract(e.data,'$.id') "
            "AND i.source_id=json_extract(e.data,'$.source_id') "
            "WHERE e.scope=? AND i.id=? AND e.kind='owner-message' "
            "AND COALESCE(json_extract(e.data,'$.historical'),0)=0",
            (self.scope.key(), input_id)).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        if source_id is not None and data.get("source_id") != source_id:
            return None
        return row["id"], data

    def _mark_attention(self, conn, input_id, source_id, attention_for, event_id):
        """Host classification after routing, outside the original observation/digest.

        Routing can occur after ingestion, and must work while the independent rhythm is off or
        forming. Only this computed metadata changes; the original source, text, occurrence time,
        immutable digest and receipt are preserved.
        """
        found = self._attention_event(conn, input_id, source_id)
        if not found:
            # The initial ingest invokes _attend before its runtime envelope has been inserted.
            return False
        identifier, data = found
        current = data.get("attention_marker") or {}
        if current.get("state") == "active":
            priority = {"owner_message": 1, "task": 2, "urgent": 3}
            old = current.get("attention_for")
            if priority.get(old, 0) >= priority[attention_for]:
                return False
        data["attention_marker"] = {"attention_for": attention_for, "state": "active",
                                    "at": self.clock(), "event_id": event_id}
        conn.execute("UPDATE mind_runtime_events SET data=? WHERE scope=? AND id=?",
                     (dumps(data), self.scope.key(), identifier))
        return True

    def _finish_attention_marker(self, conn, input_id, event_id):
        found = self._attention_event(conn, input_id)
        if not found:
            return False
        identifier, data = found
        marker = data.get("attention_marker")
        if isinstance(marker, dict):
            if marker.get("state") != "active":
                return False
        elif data.get("attention_for") in {"task", "urgent"}:
            marker = {"attention_for": data["attention_for"]}
        else:
            return False
        data["attention_marker"] = {**marker, "state": "finished", "finished_at": self.clock(),
                                    "finished_event_id": event_id}
        conn.execute("UPDATE mind_runtime_events SET data=? WHERE scope=? AND id=?",
                     (dumps(data), self.scope.key(), identifier))
        return True

    def _attend(self, conn, state, input_id, attention_for, event_id):
        """Host seam inside an existing input transaction. No emotion or base phase is reset.

        The input must already have been received for this scope. Calling this is an attention
        marker, not permission to run a task, send a message, or settle an actual commitment.
        Returns whether the computed marker or rhythm changed; the caller records its transaction.
        """
        if attention_for not in {"owner_message", "task", "urgent"}:
            raise ValueError("Attention must name owner_message, task or urgent")
        row = conn.execute("SELECT source_id FROM mind_reply_inputs WHERE scope=? AND id=?",
                           (self.scope.key(), input_id)).fetchone()
        if not row:
            raise Missing("Attention must refer to a received owner input")
        refs = self._evidence(conn, [row[0]])
        if (not self._fresh(conn, refs) or not refs or any(
                ref["authority"] != "explicit" or ref.get("metadata", {}).get("role") != "user"
                for ref in refs)):
            raise Conflict("Attention requires a current received owner input")
        marker_changed = self._mark_attention(conn, input_id, row[0], attention_for, event_id)
        entry = state.get("rhythm")
        if not entry or not self._continuity_flags(conn, state)["rhythm"]:
            return marker_changed
        current = entry.get("attention_for")
        priority = {None: 0, "owner_message": 1, "task": 2, "urgent": 3}
        if priority[current] > priority[attention_for]:
            return marker_changed
        if current == attention_for and entry.get("attention_input_id") == input_id:
            return marker_changed
        phase = entry.get("base_phase") or entry["phase"]
        entry.update(base_phase=phase, attention_for=attention_for,
                     return_to=entry.get("return_to") or phase, attention_input_id=input_id,
                     attention_at=self.clock(), attention_event_id=event_id)
        return True

    def attend(self, input_id, attention_for="owner_message"):
        """Persist temporary input attention, including explicit task/urgent host input markers."""
        with self.engine.db.connect(write=True) as conn:
            state = self._load(conn)
            event_id = "mind_" + digest([self.scope.key(), "attention", input_id, attention_for,
                                        state["revision"] + 1])[:32]
            changed = self._attend(conn, state, input_id, attention_for, event_id)
            if changed:
                state.update(revision=state["revision"] + 1, updated_at=self.clock())
                self._save(conn, state)
                self._history(conn, event_id, state, "attention", {"input_id": input_id, "attention_for": attention_for})
            return {"changed": changed, "rhythm": deepcopy(state.get("rhythm"))}

    def finish_attention(self, input_id):
        """Release this input's computed marker and only its current rhythm attention.

        The original base phase and activity remain available for the next independent review.
        Task completion and transport receipts are handled by their existing contracts.
        """
        with self.engine.db.connect(write=True) as conn:
            state = self._load(conn)
            entry = state.get("rhythm") or {}
            event_id = "mind_" + digest([self.scope.key(), "attention-finished", input_id, state["revision"] + 1])[:32]
            marker_changed = self._finish_attention_marker(conn, input_id, event_id)
            rhythm_changed = entry.get("attention_input_id") == input_id and bool(entry.get("attention_for"))
            if rhythm_changed:
                entry.update(attention_for=None, return_to=None, attention_input_id=None,
                             attention_cleared_at=self.clock())
            changed = marker_changed or rhythm_changed
            if changed:
                state.update(revision=state["revision"] + 1, updated_at=self.clock())
                self._save(conn, state)
                self._history(conn, event_id, state, "attention-finished", {"input_id": input_id})
            return {"changed": changed, "rhythm": deepcopy(state.get("rhythm"))}

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
        concerns = state.setdefault("concerns", {})
        cid = (("concern_" + digest([self.scope.key(), change.key])[:32])
               if change.action == "create" else change.concern_id)
        current = concerns.get(cid)
        subjective = subjective_concern(change, current)
        refs = list(
            {
                evidence_key(r): r
                for r in (self._subjective_refs(conn, change.evidence_ids, allowed) if subjective
                          else self._proposal_refs(conn, change.evidence_ids, fallback or [], allowed))
            }.values()
        )
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
        timing = {}
        if any(getattr(change, name, None) for name in TIMED_FIELDS):
            from .autonomy_schema import enabled
            if enabled(conn, self.scope.key(), "timed_concerns"):
                from .plans import local_time
                timing = {name: local_time(getattr(change, name)) for name in TIMED_FIELDS if getattr(change, name, None)}
        # When to bring a concern up again is Kin's own judgment too, like easing it: an update that
        # only moves its window needs no new source (`timed_concerns`).
        retimed = change.action == "update" and bool(current) and any(current.get(k) != v for k, v in timing.items())
        settles = {"ease": "easing", "archive": "archived"}.get(change.action)
        if current and not subjective and not new_refs and not retimed and (not settles or current["status"] == settles):
            # Easing or archiving is Kin's own judgment about a concern and needs no new source
            # (K3-18); it is a replay only when the concern already stands that way.
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
                "A closed concern requires an explicit reopen"
            )
        if change.action == "reopen" and current["status"] not in {
            "resolved",
            "archived",
        }:
            raise Conflict("Only a closed concern can reopen")
        if change.action == "resolve" and not new_refs and not subjective:
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
        if timing:
            # A stated edge replaces the stored one; an edge left out stays as it was.
            window = {**{name: entry.get(name) for name in TIMED_FIELDS}, **timing}
            after, until = (surface_moment(window[name]) for name in TIMED_FIELDS)
            if after and until and after >= until:
                raise Conflict("A concern's window opens before it closes")
            entry.update(window, surface_set_at=at)
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
            if not self._concern_fresh(conn, entry) or low_confidence(entry):
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
                or not self._concern_fresh(conn, concern)
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
            evidence = (self._subjective_refs(conn, proposal.evidence_ids, allowed)
                        if proposal.basis == "internal_thought"
                        else self._proposal_refs(conn, proposal.evidence_ids, refs, allowed))
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
            evidence = self._subjective_refs(conn, proposal.evidence_ids, allowed)
            old = state.get("rhythm") or {}
            phase = proposal.base_phase or proposal.phase
            state["rhythm"] = {
                **proposal.model_dump(exclude={"evidence_ids"}),
                "phase": proposal.phase,
                "base_phase": phase,
                "return_to": (proposal.return_to or phase) if proposal.attention_for else None,
                "current_activity": (proposal.current_activity if proposal.current_activity is not None
                                     else old.get("current_activity")),
                "previous": {key: old.get(key) for key in
                             ("base_phase", "phase", "attention_for", "return_to", "current_activity", "at", "event_id", "note")}
                            if old else None,
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
                view["needs_review"] = low_confidence(entry) or not self._concern_fresh(conn, entry)
                view["evidence_ids"] = [r["record_id"] for r in entry["evidence"]]
                view.pop("evidence", None)
                view.pop("resolution_evidence", None)
                result["concerns"].append(view)
        assessment = state.get("last_assessment") if flags["interpretation"] else None
        if assessment:
            assessment = deepcopy(assessment)
            refs = assessment.pop("evidence")
            assessment["evidence_ids"] = [r["record_id"] for r in refs]
            assessment["needs_review"] = not self._subjective_available(conn, refs)
            if assessment.get("understanding"):
                inner = assessment["understanding"]
                available = (self._subjective_available if inner.get("basis") == "internal_thought" else self._fresh)
                inner["needs_review"] = low_confidence(inner) or not available(conn, inner["evidence"])
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
        # `timed_concerns` (default off): one entering its window goes first, until a context
        # receipt shows it reached a native window. Off, nothing here is read.
        from .autonomy_schema import enabled as switched_on
        entering = (timed_entering(conn, self.scope.key(), result["concerns"], at)
                    if result["concerns"] and switched_on(conn, self.scope.key(), "timed_concerns") else ())
        result["selected_concerns"] = select_concerns(result["concerns"], topic, intent=stated, entering=entering)
        if flags["rhythm"]:
            interactions = interaction_windows(conn, self.scope.key(), at)
            entry = state.get("rhythm")
            result["rhythm"] = rhythm_view(
                entry,
                at,
                interactions,
                fresh=not entry or self._subjective_available(conn, entry["evidence"]),
            )
        else:
            result["rhythm"] = {"status": "disabled", "mode": "independent"}
        # 心境、心绪、余韵、心跳呼吸: derived here from the scores and the rhythm, shown beside the
        # expression and never fed into it -- compile_expression below reads the scores alone.
        result["affect_layers"] = build_view(state, result["dimensions"], result["rhythm"], at,
                                             (result.get("contact") or {}).get("timezone"))
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
        else:
            result["expression"] = None
        for desire in result["desires"]:
            desire["concern_needs_review"] = not self._concern_links_fresh(
                conn, state, desire
            )
