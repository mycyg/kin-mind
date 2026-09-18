"""How the appraisal that just ran means to be present in the next few replies.

Until now the chat model learned how Kin feels from a lookup table: fixed sentences chosen by where
a number fell. Nothing that was actually judged about the moment reached the wording. Here the
appraisal that already happens may also state an intent — a stance, at most two topics worth staying
with, at most two things to leave alone, and how long that is meant to hold. The host checks only
that the material is real: evidence of this same evaluation, traits the ledger carries now at the
revision it showed, concerns this scope holds. What the stance says is Kin's own, and it is never
quoted: it says how to be present, and the persona still decides the words.

An intent is used while it is fresh and never after. A new owner message is deliberately not what
ends it: the reply to a message is written before the appraisal of that message, so an intent that
expired on arrival could never be used at all. It ends when its own minutes run out, when the
evidence under it moved, when a trait it cited was revised or revoked, when continuity is not
active, or when the persona changed. In every one of those cases the callers fall back to the
table, byte for byte.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.persona import load_persona, persona_metadata

from . import appraisal, trait_refs
from .autonomy_schema import optimized
from .evidence_classes import never_evidence
from .state import timestamp
from .traits import EFFECTIVE, Traits, installed

SECTION = SWITCH = "expression_intent"
# One current intent per scope, and every accepted one kept readable beside it.
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS mind_expression_intents("
    " scope TEXT PRIMARY KEY,id TEXT NOT NULL,at TEXT NOT NULL,valid_until TEXT NOT NULL,"
    " data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS mind_expression_intent_log("
    " id TEXT PRIMARY KEY,scope TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS mind_expression_intent_history"
    " ON mind_expression_intent_log(scope,at)",
)
# Why the wording falls back to the table. Static reasons; no row is ever deleted for one.
NO_INTENT, EXPIRED, INACTIVE = "no-intent", "intent-expired", "continuity-inactive"
PERSONA, EVIDENCE, TRAIT = "persona-changed", "intent-evidence-stale", "intent-trait-moved"
# Concerns an intent may still point at. A closed one is history, not somewhere to stay.
OPEN = ("active", "easing")


def _ensure(conn):
    """Created inside the caller's transaction: `executescript` would commit what a refused
    section has to be able to roll back."""
    for statement in SCHEMA:
        conn.execute(statement)


def identity(persona):
    """What the intent was stated under. A persona contract that changed voids it."""
    return digest(persona) if persona else "none"


# --- what the host will vouch for -------------------------------------------------------------

def _refs(conn, mind, ids, allowed):
    """Evidence this evaluation was shown, still at the version it was shown at."""
    refs = mind._evidence(conn, ids) if ids else []
    shown = {ref["record_id"]: ref["revision"] for ref in allowed or []}
    for ref in refs:
        if never_evidence(ref) or ref["record_id"] not in shown:
            raise Conflict("An intent cites evidence this evaluation was not shown",
                           code="intent-evidence-unknown", target=ref["record_id"])
        if shown[ref["record_id"]] != ref["revision"]:
            raise Conflict("Intent evidence changed after it was supplied", kind="runtime",
                           code="intent-evidence-changed", target=ref["record_id"],
                           expected=shown[ref["record_id"]], actual=ref["revision"])
    return refs


def _cited(conn, mind, ids):
    """The traits an intent may name: ones the ledger carries now, kept with the revision it had
    when it was named. A later revision, or a revocation, is what makes the intent stale."""
    if not ids:
        return []
    if not installed(conn):
        raise Missing("Trait is missing or outside this scope", code="trait-unknown")
    ledger, found = Traits(mind), []
    for identifier in dict.fromkeys(ids):
        trait = ledger.get(conn, identifier)
        if trait["status"] not in EFFECTIVE:
            raise Conflict("An intent names a trait the ledger no longer carries", kind="runtime",
                           code="intent-trait-not-current", target=identifier)
        found.append({"trait_id": identifier, "revision": trait["revision"]})
    return found


def _topics(state, topics):
    """A topic may point at a concern this scope is actually carrying, and at nothing else."""
    found = []
    for topic in topics:
        if topic.concern_id is not None:
            concern = (state.get("concerns") or {}).get(topic.concern_id)
            if not concern or concern["status"] not in OPEN:
                raise Missing("An intent topic names a concern this scope does not carry",
                              code="intent-concern-unknown", target=topic.concern_id)
        found.append({"topic": topic.topic, "concern_id": topic.concern_id})
    return found


def commit_intent(commit):
    """`expression_intent`: the material checked, the stance stored as it was written."""
    mind, conn, value = commit.mind, commit.conn, commit.value
    at = mind.clock()
    refs = _refs(conn, mind, value.evidence_ids, commit.sources)
    traits = _cited(conn, mind, value.trait_refs)
    topics = _topics(commit.state, value.continue_topics)
    _ensure(conn)
    identifier = "intent_" + digest([commit.event_id, SECTION])[:32]
    intent = {"id": identifier, "stance": value.stance, "continue_topics": topics,
              "avoid": list(value.avoid), "valid_minutes": value.valid_minutes,
              "valid_until": (timestamp(at) + timedelta(minutes=value.valid_minutes)).isoformat(),
              "at": at, "evidence": refs, "evidence_ids": sorted({r["record_id"] for r in refs}),
              "trait_refs": traits, "persona": identity(persona_metadata(load_persona(mind.engine, mind.scope))),
              "event_id": commit.event_id, "job_id": commit.job_id, "receipt": commit.receipt,
              "agent_version": commit.version, "stimulus": commit.stimulus}
    conn.execute("INSERT INTO mind_expression_intents VALUES(?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                 "id=excluded.id,at=excluded.at,valid_until=excluded.valid_until,data=excluded.data",
                 (mind.scope.key(), identifier, at, intent["valid_until"], dumps(intent)))
    conn.execute("INSERT OR REPLACE INTO mind_expression_intent_log VALUES(?,?,?,?)",
                 (identifier, mind.scope.key(), at, dumps(intent)))
    # The intent already re-reads the ledger for itself before every reply; this is so that one
    # place answers the other question — what did this trait hold up, once it moves. One intent is
    # in force at a time, so the one this replaces leaves no row behind.
    trait_refs.clear_kind(conn, mind.scope.key(), "intent")
    trait_refs.record(conn, mind.scope.key(), "intent", identifier,
                      {ref["trait_id"]: ref["revision"] for ref in traits},
                      dependent_revision=intent["valid_until"], at=at)
    return intent


# --- whether it may still shape the wording ------------------------------------------------------

def stale_reason(intent, at, *, evidence_fresh=True, traits_current=True,
                 continuity_active=True, persona=None):
    """None while an intent may shape the wording, otherwise the static reason it may not.

    Pure, and the moment is given rather than read, so every caller decides this the same way and
    a test can move the clock. A new owner message is deliberately not one of these reasons."""
    if not intent:
        return NO_INTENT
    if not continuity_active:
        return INACTIVE
    if timestamp(at) > timestamp(intent["valid_until"]):
        return EXPIRED
    if intent.get("persona") != identity(persona):
        return PERSONA
    if not evidence_fresh:
        return EVIDENCE
    if not traits_current:
        return TRAIT
    return None


def _traits_current(conn, mind, refs):
    """Every trait the intent named, still at the revision it named and still in force."""
    if not refs:
        return True
    if not installed(conn):
        return False
    ledger = Traits(mind)
    for ref in refs:
        try:
            trait = ledger.get(conn, ref["trait_id"])
        except Missing:
            return False
        if trait["revision"] != ref["revision"] or trait["status"] not in EFFECTIVE:
            return False
    return True


def stored(conn, scope):
    """This scope's current intent, or None. A database no intent has reached holds none."""
    try:
        row = conn.execute("SELECT data FROM mind_expression_intents WHERE scope=?", (scope,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


def current(conn, mind, at, *, continuity_active=True, persona=None):
    """The stored intent with the host's own answer about it, or None when there is none."""
    intent = stored(conn, mind.scope.key())
    if not intent:
        return None
    reason = stale_reason(intent, at, continuity_active=continuity_active, persona=persona)
    if reason is None:
        # Only an intent still inside its own minutes costs a freshness read of what it cited.
        reason = stale_reason(intent, at, continuity_active=continuity_active, persona=persona,
                              evidence_fresh=mind._fresh(conn, intent["evidence"]),
                              traits_current=_traits_current(conn, mind, intent["trait_refs"]))
    return {**intent, "stale_reason": reason}


def shown(intent):
    """The intent as a reader is given it: what to do, never the evidence rows behind it."""
    return {k: intent[k] for k in
            ("id", "stance", "continue_topics", "avoid", "valid_until", "evidence_ids", "trait_refs")}


def view(conn, mind, at, *, continuity_active=True, persona=None):
    """What a projection needs: the fresh intent to use, and the short status the next evaluation
    is shown — including the host's static answer when the last one was refused. None at all while
    the switch is off, so everything downstream stays exactly as it was."""
    if not optimized(conn, mind.scope.key(), SWITCH):
        return None
    intent = current(conn, mind, at, continuity_active=continuity_active, persona=persona)
    status = {k: intent[k] for k in ("id", "stance", "valid_until", "stale_reason")} if intent else {}
    refused = appraisal.last_refusal(conn, mind.scope.key(), SECTION).get(SECTION)
    if refused:
        status["last_refusal"] = refused
    return {"use": shown(intent) if intent and not intent["stale_reason"] else None,
            "status": status or None}


def manifest_entries(conn, mind, at, *, continuity_active=True, persona=None):
    """The ids this module contributes to the input-manifest class `intent`: what a stored
    proposal was shown, and whether it still holds. The intent has no revision of its own, so the
    moment it stops being usable is what a reader compares."""
    intent = current(conn, mind, at, continuity_active=continuity_active, persona=persona)
    if not intent:
        return {}
    return {intent["id"]: {"revision": intent["valid_until"], "needs_review": bool(intent["stale_reason"]),
                           "stale_reason": intent["stale_reason"],
                           "trait_refs": [ref["trait_id"] for ref in intent["trait_refs"]]}}


# An intent formed before the owner wrote again waits for the next round; what it was about has
# moved on. The module claims its section here, and apply() stays as it is.
appraisal.register_audit_section(SECTION, commit_intent)
