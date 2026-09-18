"""The behavior verification chain: a hypothesis, the predictions that could refute it, and what
may settle them.

Nothing ever verified anything. No caller created a claim or a prediction; the daily review asked
for an assessment made under an agent version that had changed dozens of times since; and a
personality proposal returned by an ordinary appraisal was dropped without a word. Here the host
records a hypothesis and its predictions inside the appraisal's own transaction, stamped with what
that configuration was. An outcome is accepted only from what the host itself resolved — a verified
execution receipt, or evidence of this same evaluation — and only when the prediction came first;
the same moment is not first. Kin's own report that a prediction came true settles nothing.

A non-empty `evolution` from an ordinary appraisal is kept as a pending proposal. The daily action
merges it host side with no model call: a confirmed prediction under the same stamp, the day's
limits unchanged, one separate evolution event. What the stamp no longer matches is marked stale
with the ingredient that moved, and stays readable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.self_knowledge import (
    AssessmentInput,
    ClaimInput,
    PredictionInput,
    SelfKnowledge,
    metadata,
)

from . import appraisal, compat
from .evidence_classes import episode_key, never_evidence, verified_behavior, window_of
from .rhythm import interaction_windows, stamp as moment
from .state import AffectiveEvent, Evolution

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS mind_evolution_proposals("
    " id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,"
    " compat TEXT NOT NULL,data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS mind_evolution_proposal_state"
    " ON mind_evolution_proposals(scope,state,created_at)",
)
# Where a hypothesis of this chain came from, for a self-knowledge read that asks by context.
CLAIM_CONTEXT = "behavior check in an ordinary appraisal"
# The section carries no probability and the host will not invent a calibration number: an unscored
# forecast is recorded at even odds, with no generic estimate, so it never enters a paired score.
UNSCORED_PROBABILITY = 0.5
OUTCOMES = {"confirmed": True, "refuted": False, "inconclusive": None}
# Why a day's merge did not happen. Static reasons; the proposals stay where they are.
NO_PROPOSAL, INCOMPATIBLE = "need-personality-proposal", "proposal-incompatible"
NO_EPISODES, NO_CHECK = "need-three-independent-interactions", "need-prospective-behavioral-check"


def _ensure(conn):
    """Create the table inside whatever transaction the caller holds. `executescript` would commit
    it, and a section of an appraisal must be able to roll back everything it wrote."""
    for statement in SCHEMA:
        conn.execute(statement)


def _refs(conn, mind, ids, allowed):
    """Evidence this evaluation was shown, still at the version it was shown at."""
    refs = mind._evidence(conn, ids) if ids else []
    if not refs:
        raise Conflict("The chain needs evidence this evaluation was shown", code="chain-evidence-unknown")
    shown = {ref["record_id"]: ref["revision"] for ref in allowed or []}
    for ref in refs:
        if never_evidence(ref) or ref["record_id"] not in shown:
            raise Conflict("The chain needs evidence this evaluation was shown",
                           code="chain-evidence-unknown", target=ref["record_id"])
        if shown[ref["record_id"]] != ref["revision"]:
            raise Conflict("Chain evidence changed after it was supplied", code="chain-evidence-changed",
                           target=ref["record_id"], expected=shown[ref["record_id"]], actual=ref["revision"])
    return refs


def _receipt(conn, scope, identifier):
    """An execution the host can resolve, and the source that records it."""
    row = conn.execute("SELECT data FROM mind_plan_runs WHERE scope=? AND id=?", (scope, identifier)).fetchone()
    if row:
        run = json.loads(row[0])
        return {"kind": "plan-run", "state": run.get("state"), "result": run.get("result")}, (run.get("result") or {}).get("source_id")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
        # Either the stored id or the host's own event id: the model is shown the second one.
        row = conn.execute("SELECT data FROM mind_runtime_events WHERE scope=? AND kind IN ('task-result','delivery')"
                           " AND (id=? OR json_extract(data,'$.id')=?)", (scope, identifier, identifier)).fetchone()
        if row:
            event = json.loads(row[0])
            return {key: event.get(key) for key in ("kind", "state", "verified", "message_id")}, event.get("source_id")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone():
        row = conn.execute("SELECT state,data FROM mind_explorations WHERE scope=? AND id=?", (scope, identifier)).fetchone()
        if row:
            return {"kind": "exploration", "state": row["state"]}, json.loads(row["data"]).get("source_id")
    raise Conflict("A result must name an execution of this scope", code="result-unknown", target=identifier)


def _receipts(conn, mind, ids):
    """Execution receipts as evidence. What Kin wrote about what it did is Kin's own statement;
    only the receipt of the execution itself says the behavior happened."""
    refs = []
    for identifier in ids:
        receipt, source_id = _receipt(conn, mind.scope.key(), identifier)
        if not verified_behavior(receipt) or not source_id:
            raise Conflict("This receipt does not show the behavior happened",
                           code="result-unverified", target=identifier)
        refs += mind._evidence(conn, [source_id])
    return refs


def _traits(conn, mind, refs):
    """The traits a hypothesis says it rests on, while the ledger is the authority on them. A trait
    that is not in effect cannot be what a behavior is predicted from, so it is refused here rather
    than followed later into an invalidation that has nothing to invalidate."""
    from .autonomy_schema import optimized
    from .traits import EFFECTIVE, Traits
    if not refs or not optimized(conn, mind.scope.key(), "trait_ledger"):
        return []
    ledger = Traits(mind)
    for identifier in refs:
        # Missing carries `trait-unknown` from the ledger itself; an ended trait is this refusal.
        if ledger.get(conn, identifier)["status"] not in EFFECTIVE:
            raise Conflict("A hypothesis rests only on a trait that is in effect",
                           code="trait-not-effective", target=identifier)
    return sorted(set(refs))


def commit_hypothesis(commit):
    """`self_hypothesis`: one claim and the predictions that could refute it, in this transaction."""
    mind, conn, value = commit.mind, commit.conn, commit.value
    if not value.predictions:
        raise Conflict("A hypothesis needs a prediction that could refute it",
                       code="hypothesis-without-prediction")
    refs = _refs(conn, mind, value.evidence_ids, commit.sources)
    current = compat.stamp(mind, conn, commit.state)
    knowledge = SelfKnowledge(mind.engine, mind.scope, clock=mind.clock)
    claim = knowledge.claim_in(conn, ClaimInput(
        command_id=commit.event_id + ":hypothesis", aspect=value.statement[:200], context=CLAIM_CONTEXT,
        agent_version=commit.version, claim=value.statement, basis="hypothesis",
        evidence_ids=sorted({ref["record_id"] for ref in refs})), compat=current,
        rests_on=_traits(conn, mind, value.trait_refs))
    for index, prediction in enumerate(value.predictions):
        if prediction.evidence_ids:
            _refs(conn, mind, prediction.evidence_ids, commit.sources)
        knowledge.predict_in(conn, PredictionInput(
            command_id=commit.event_id + ":prediction:" + str(index), claim_id=claim["id"],
            expected_revision=claim["revision"], case_id=digest([commit.event_id, index])[:32],
            behavior=prediction.statement, information=value.reason, probability=UNSCORED_PROBABILITY),
            compat=current, window_hours=prediction.test_window_hours)


def commit_outcomes(commit):
    """`prediction_outcomes`: what the host verified settles a prediction, and nothing else does."""
    mind, conn = commit.mind, commit.conn
    current = compat.stamp(mind, conn, commit.state)
    knowledge = SelfKnowledge(mind.engine, mind.scope, clock=mind.clock)
    for index, outcome in enumerate(commit.value):
        prediction = knowledge._get(conn, outcome.prediction_id, "prediction")
        if not compat.holds(metadata(prediction).get("compat"), current):
            raise Conflict("This prediction was made under another configuration",
                           code="prediction-incompatible", target=prediction["id"])
        refs = [*_receipts(conn, mind, outcome.result_ids),
                *(_refs(conn, mind, outcome.evidence_ids, commit.sources) if outcome.evidence_ids else [])]
        if not refs:
            raise Conflict("An outcome needs evidence the host verified", code="outcome-not-verifiable")
        for ref in refs:
            if ref["authority"] not in {"explicit", "operation"} or never_evidence(ref):
                raise Conflict("An outcome needs evidence the host verified",
                               code="outcome-not-verifiable", target=ref["record_id"])
            if ref["occurred_at"] <= prediction["valid_from"]:
                raise Conflict("Outcome evidence must be later than the prediction",
                               code="outcome-not-later", target=ref["record_id"])
        knowledge.assess_in(conn, AssessmentInput(
            command_id=commit.event_id + ":outcome:" + str(index), prediction_id=prediction["id"],
            expected_revision=prediction["revision"], outcome=OUTCOMES[outcome.outcome],
            evidence_ids=sorted({ref["record_id"] for ref in refs}), note=outcome.reason), compat=current)


def hold_evolution(commit):
    """A personality proposal an ordinary appraisal returned: kept as pending, never applied here.

    The day's merge checks the chain and the limits. Dropping this silently, as the previous
    release did, is what kept the whole chain out of production."""
    from .autonomy_schema import optimized
    mind, conn, value = commit.mind, commit.conn, commit.value
    if value.revert_event_id:
        raise Conflict("A reversion is a host action, not an appraisal proposal",
                       code="evolution-revert-not-proposed")
    if value.traits and optimized(conn, mind.scope.key(), "trait_ledger"):
        # Refused where it was proposed, so a proposal the merge could only refuse is never stored.
        raise Conflict("The trait ledger is the only writer of traits", code="traits-owned-by-ledger")
    _ensure(conn)
    current = compat.stamp(mind, conn, commit.state)
    identifier = "evolution_" + digest([commit.event_id, "proposal"])[:32]
    conn.execute("INSERT OR REPLACE INTO mind_evolution_proposals VALUES(?,?,?,?,?,?)",
                 (identifier, mind.scope.key(), "pending", mind.clock(), current["key"],
                  dumps({"evolution": value.model_dump(), "event_id": commit.event_id, "job_id": commit.job_id,
                         "agent_version": commit.version, "reason": commit.proposal.reason,
                         "receipt": commit.receipt, "compat": current})))


def pending(conn, mind, current=None):
    """Proposals still waiting for a merge, newest first, each with why it cannot be used yet."""
    try:
        rows = conn.execute("SELECT * FROM mind_evolution_proposals WHERE scope=? AND state IN ('pending','stale')"
                            " ORDER BY created_at DESC,id", (mind.scope.key(),)).fetchall()
    except sqlite3.OperationalError:
        return []
    items = []
    for row in rows:
        data = json.loads(row["data"])
        items.append({"id": row["id"], "state": row["state"], "created_at": row["created_at"],
                      "evolution": data["evolution"], "reason": data.get("reason"),
                      "stale_reason": data.get("stale_reason") if current is None
                      else compat.stale_reason(data.get("compat"), current)})
    return items


def owner_episodes(mind, conn, refs):
    """How many separate times the owner spoke, among these references.

    One interaction window is one episode, however many messages it holds and however many
    namespaces carried them. A message outside every window keeps the content hash it used to be
    counted by, so this can only be stricter than counting hashes."""
    windows = interaction_windows(conn, mind.scope.key(), mind.clock())["recent_windows"]
    episodes = set()
    for ref in refs:
        if (ref["authority"] != "explicit" or never_evidence(ref)
                or ref["metadata"].get("role") != "user" or ref["metadata"].get("host_event") != "message"):
            continue
        window = window_of(windows, ref["occurred_at"])
        episodes.add(episode_key(window=window) if window else episode_key(root="hash:" + ref["hash"]))
    return episodes


def open_predictions(conn, mind, *, limit=4):
    """The predictions still waiting for an outcome: what was predicted, the window it was given,
    and whether the configuration that made it still holds.

    The projection that owns the model's `state` calls this and shows the result as
    `open_predictions`; nothing here reads or writes anything of that projection."""
    current = compat.stamp(mind, conn)
    # Two passes over the self-knowledge rows, not one per candidate: this runs on the way into
    # every appraisal that offers the section.
    settled = {row[0] for row in conn.execute(
        "SELECT json_extract(data,'$.attributes.self_knowledge.prediction_id') FROM records"
        " WHERE scope=? AND deleted=0 AND json_extract(data,'$.attributes.self_knowledge.entry')='assessment'",
        (mind.scope.key(),)).fetchall()}
    rows = conn.execute(
        "SELECT data FROM records WHERE scope=? AND deleted=0 AND status='active'"
        " AND json_extract(data,'$.attributes.self_knowledge.entry')='prediction'"
        " ORDER BY received_at DESC,id LIMIT ?", (mind.scope.key(), limit + len(settled))).fetchall()
    items = []
    for row in rows:
        if len(items) == limit:
            break
        record = json.loads(row[0])
        if record["id"] in settled:
            continue
        info = metadata(record)
        hours, ends = info.get("test_window_hours"), None
        if hours:
            ends = (moment(record["valid_from"]) + timedelta(hours=hours)).isoformat()
        reason = compat.stale_reason(info.get("compat"), current)
        items.append({"id": record["id"], "revision": record["revision"], "statement": record["content"],
                      "claim_id": info.get("claim_id"), "made_at": record["valid_from"],
                      "test_window_hours": hours, "window_ends_at": ends,
                      "expired": bool(ends) and moment(mind.clock()) > moment(ends),
                      "compat": "stale" if reason else "current", "stale_reason": reason})
    return items


def manifest_entries(conn, mind, *, limit=4):
    """The ids this chain contributes to the input-manifest class `predictions`: exactly what the
    context was shown, with the revision and whether its configuration still holds. A stored
    proposal that cited a prediction rests on both."""
    return {item["id"]: {"revision": item["revision"], "needs_review": item["compat"] != "current",
                         "compat": item["compat"]} for item in open_predictions(conn, mind, limit=limit)}


def _confirmed(conn, knowledge, proposal, current):
    """A prediction of this configuration that actually came true, cited by this proposal. The
    revisions, the freshness and the claim's own status are checked again by the commit itself."""
    try:
        claim = knowledge._get(conn, proposal["claim_id"], "claim")
        assessment = knowledge._get(conn, proposal["assessment_id"], "assessment")
        prediction = knowledge._get(conn, metadata(assessment)["prediction_id"], "prediction")
    except (Conflict, Missing, KeyError, TypeError):
        return False
    return (metadata(assessment).get("outcome") is True
            and metadata(prediction).get("claim_id") == claim["id"]
            and all(compat.holds(metadata(row).get("compat"), current)
                    for row in (claim, prediction, assessment)))


def merge_daily(mind, agent_version):
    """The daily action, host side and with no model call.

    It reads what an ordinary appraisal already proposed, checks that the configuration has not
    moved under it and that a prediction of that configuration came true, and commits the proposal
    as its own evolution event. Every limit `Mind` enforces stays where it is: at most one evolution
    a day, a baseline step of 2, a half-life step of a tenth, and a reversion that still works."""
    scope = mind.scope.key()
    with mind.engine.db.connect(write=True) as conn:
        _ensure(conn)
        state = mind._load(conn)
        day = moment(mind.clock()).astimezone(ZoneInfo(state["profile"]["evolution"]["timezone"])).date().isoformat()
        if conn.execute("SELECT 1 FROM mind_daily_reviews WHERE scope=? AND day=?", (scope, day)).fetchone():
            return {"state": "already-evaluated", "day": day}
        current = compat.stamp(mind, conn, state)
        proposals = pending(conn, mind, current)
        for item in proposals:
            if item["stale_reason"] and item["state"] == "pending":
                # Readable, with the reason, for as long as the row lives. Never deleted.
                conn.execute("UPDATE mind_evolution_proposals SET state='stale',data=json_set(data,'$.stale_reason',?)"
                             " WHERE scope=? AND id=?", (item["stale_reason"], scope, item["id"]))
                item["state"] = "stale"
        usable = [item for item in proposals if not item["stale_reason"]]
        if not usable:
            return {"state": "waiting", "day": day, "reason": INCOMPATIBLE if proposals else NO_PROPOSAL,
                    "proposals": [{k: item[k] for k in ("id", "state", "stale_reason")} for item in proposals]}
        rows = conn.execute(
            "SELECT id FROM sources WHERE scope=? AND deleted=0 AND json_extract(data,'$.authority')='explicit'"
            " AND json_extract(data,'$.metadata.host_event')='message' AND json_extract(data,'$.metadata.role')='user'"
            " ORDER BY received_at DESC LIMIT 60", (scope,)).fetchall()
        refs = mind._evidence(conn, [row["id"] for row in rows]) if rows else []
        unique = {ref["hash"]: ref for ref in refs if mind._fresh(conn, [ref])}
        shown = list(unique.values())[:20]
        if len(owner_episodes(mind, conn, shown)) < state["profile"]["evolution"]["minimum_interactions"]:
            return {"state": "waiting", "day": day, "reason": NO_EPISODES}
        knowledge = SelfKnowledge(mind.engine, mind.scope, clock=mind.clock)
        chosen = next((item for item in usable if _confirmed(conn, knowledge, item["evolution"], current)), None)
        if not chosen:
            return {"state": "waiting", "day": day, "reason": NO_CHECK}
        revision, ids = state["revision"], [ref["record_id"] for ref in shown]
        conn.execute("INSERT INTO mind_daily_reviews VALUES(?,?,?,?)", (scope, day, "merging", "{}"))
    data = {"proposal_id": chosen["id"], "compat": current["key"], "model_calls": 0}
    try:
        data["result"] = mind.record(AffectiveEvent(
            command_id="daily:" + day, agent_version=agent_version, expected_revision=revision,
            evidence_ids=ids, reason=chosen["reason"], origin="reflection",
            evolution=Evolution(**chosen["evolution"])))
        state_name = "complete"
    except Exception as error:  # noqa: BLE001 - host boundary; the proposal stays pending for review
        state_name, data["error"] = "needs-review", type(error).__name__
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_daily_reviews SET state=?,data=? WHERE scope=? AND day=?",
                     (state_name, dumps(data), scope, day))
        if state_name == "complete":
            conn.execute("UPDATE mind_evolution_proposals SET state='applied',data=json_set(data,'$.applied_event_id',?)"
                         " WHERE scope=? AND id=?", (data["result"]["event_id"], scope, chosen["id"]))
    return dict(state=state_name, day=day, **data)


appraisal.register_audit_section("self_hypothesis", commit_hypothesis)
appraisal.register_audit_section("prediction_outcomes", commit_outcomes)
