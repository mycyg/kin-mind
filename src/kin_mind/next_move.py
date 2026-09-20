"""The move an appraisal says it is making: recorded, checked, and never acted on.

A personality shows in choices and not only in wording, so the same appraisal that reports a mood
may also say what it is doing now and what that rests on. This module audits that statement and
does nothing else. It orders nothing and readies nothing: every wish and every plan step is still
chosen exactly as it was before any move was ever recorded.

- The model declares one of three words, `reply`, `quiet` or `rest`. The fuller word is the host's.
  It reads what this same appraisal really committed -- a step it set running, a wish it left
  wanting, a request it made of the owner -- and derives `explore`, `create`, `invite` or `ask`
  from that. With nothing to read, the derived move is the declared one.
- A move is **inconsistent** when it says nothing is happening while this commit started something
  that leaves for the owner now, or when it points at a wish or a step this appraisal neither
  committed nor left standing.
- A ground is **forged** when the host cannot find what it names. What it may name: evidence this
  evaluation was shown, a ledger trait that is still effective, a concern that is still current,
  or a correction the ledger recorded.

Either fault refuses this section alone. The mood, the wishes and the decisions of the same
appraisal commit as usual, the static code is recorded, and no follow-up call is made.
`mind_next_moves` is append-only: a row is never updated and never deleted.
"""

from __future__ import annotations

import json
import sqlite3

from eventmem.core.db import Conflict, Missing, digest, dumps

from . import appraisal, trait_refs, traits
from .autonomy_schema import optimized
from .evidence_classes import never_evidence
from .state import timestamp

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS mind_next_moves("
    " id TEXT PRIMARY KEY,scope TEXT NOT NULL,event_id TEXT NOT NULL,job_id TEXT,at TEXT NOT NULL,"
    " declared TEXT NOT NULL,derived TEXT NOT NULL,data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS mind_next_move_recent ON mind_next_moves(scope,at)",
)

# What the host reads the fuller move off, from what this same appraisal committed. Nothing in
# these tables decides anything; they only give the move its longer name.
#   a decision that set a plan step running -> the step's actor
#   a wish this commit left wanting         -> the wish's kind
#   a request this commit made of the owner -> the owner request's kind
# A step whose actor is the owner is absent by construction: the host refuses to execute on the
# owner's behalf, so a question reaches the ledger as the concern's own owner request instead.
STEP_MOVE = {"explore": "explore", "create": "create", "contact": "invite"}
WISH_MOVE = {"explore": "explore", "create": "create", "contact": "invite"}
REQUEST_MOVE = {"help": "ask", "invitation": "invite", "request": "ask"}
# Which one the host reads first when a single commit did several of these: what leaves for the
# owner now, then what was asked of the owner, then what is made, then what is looked up.
DERIVED_ORDER = ("invite", "ask", "create", "explore")
# The two words that say nothing is happening.
QUIET_MOVES = ("quiet", "rest")
# A wish that has ended carries no move, and neither does a step.
FINISHED_WISH = ("completed", "abandoned")
FINISHED_STEP = ("completed", "abandoned")
# A concern still open enough to stand behind a move.
CURRENT_CONCERN = ("active", "easing")
# How many moves one operator read shows by default.
SHOWN_MOVES = 20


def _ensure(conn):
    """Created inside whatever transaction the caller holds. `executescript` would commit it, and
    a section of an appraisal has to be able to roll back everything it wrote."""
    for statement in SCHEMA:
        conn.execute(statement)


def _decided(conn, event_id):
    """The action decisions this commit really applied, each with the step it decided.

    A committed decision writes one plan-history row under this event's own command id, and the
    step it decided carries a decision id derived from that same command. A decision that was held,
    or one whose whole section was refused, leaves neither.
    """
    prefix, found = event_id + ":decision:", []
    try:
        rows = conn.execute("SELECT command_id,data FROM mind_plan_history WHERE substr(command_id,1,?)=?"
                            " ORDER BY command_id", (len(prefix), prefix)).fetchall()
    except sqlite3.OperationalError:
        return found
    for row in rows:
        plan = json.loads(row["data"])
        for step in plan.get("steps", []):
            decision = step.get("decision") or {}
            if decision.get("id") == "decision_" + digest([row["command_id"], plan["id"], step["id"]])[:32]:
                found.append({"plan_id": plan["id"], "step_id": step["id"],
                              "actor": step.get("actor"), "action": decision.get("action")})
    return found


def _committed(conn, state, event_id):
    """The real objects this same appraisal committed, in the three shapes a move can be read off.

    Read from what the host wrote, not from the proposal: a refused section rolled its writes back
    and a held item was never applied, so neither appears here.
    """
    return {
        "decisions": _decided(conn, event_id),
        "wishes": [{"id": desire["id"], "kind": desire.get("kind")}
                   for desire in (state.get("desires") or {}).values()
                   if desire.get("event_id") == event_id and desire.get("status") == "wanted"],
        "owner_requests": [{"id": concern["id"], "kind": (concern.get("owner_request") or {}).get("kind")}
                           for concern in (state.get("concerns") or {}).values()
                           if concern.get("event_id") == event_id and concern.get("owner_request")],
    }


def _derive(declared, committed):
    """The fuller word for what this commit started, or the declared word when it started nothing
    the tables above map."""
    found = {STEP_MOVE[entry["actor"]] for entry in committed["decisions"]
             if entry["action"] == "execute" and entry["actor"] in STEP_MOVE}
    found |= {WISH_MOVE[entry["kind"]] for entry in committed["wishes"] if entry["kind"] in WISH_MOVE}
    found |= {REQUEST_MOVE[entry["kind"]] for entry in committed["owner_requests"] if entry["kind"] in REQUEST_MOVE}
    return next((move for move in DERIVED_ORDER if move in found), declared)


def _outward(committed):
    """What this commit started that leaves for the owner now.

    A wish and an owner request are wants the host still gates by the current semantic decision,
    authorization, readiness and quiet hours, so either can stand beside a quiet move. Only the
    explicit legacy mode adds the contact score threshold. A contact step this commit set running
    is already on its way, and cannot.
    """
    return [entry for entry in committed["decisions"]
            if entry["action"] == "execute" and entry["actor"] == "contact"]


def _steps(conn, scope):
    """{step id: plan id} for the steps of this scope's active plans that have not ended."""
    found = {}
    try:
        rows = conn.execute("SELECT data FROM mind_plans WHERE scope=? AND status='active'", (scope,)).fetchall()
    except sqlite3.OperationalError:
        return found
    for row in rows:
        plan = json.loads(row[0])
        for step in plan.get("steps", []):
            if step.get("state") not in FINISHED_STEP:
                found.setdefault(step["id"], plan["id"])
    return found


def _binding(conn, mind, state, value, committed, event_id, at):
    """What `wish_ref` and `step_ref` name. Each must be an item this appraisal committed, or one
    it left standing; anything else is a move bound to nothing."""
    bound = {}
    if value.wish_ref:
        desire = (state.get("desires") or {}).get(value.wish_ref)
        mine = bool(desire) and desire.get("event_id") == event_id
        standing = bool(desire) and desire["status"] not in FINISHED_WISH and timestamp(desire["expires_at"]) > timestamp(at)
        if not mine and not standing:
            raise Conflict("This move does not match what this appraisal committed",
                           code="next-move-inconsistent", target=value.wish_ref)
        bound["wish"] = {"id": value.wish_ref, "kind": desire.get("kind"), "status": desire["status"],
                         "committed": mine}
    if value.step_ref:
        decided = next((entry for entry in committed["decisions"] if entry["step_id"] == value.step_ref), None)
        open_steps = _steps(conn, mind.scope.key())
        if not decided and value.step_ref not in open_steps:
            raise Conflict("This move does not match what this appraisal committed",
                           code="next-move-inconsistent", target=value.step_ref)
        bound["step"] = {"id": value.step_ref, "plan_id": decided["plan_id"] if decided else open_steps[value.step_ref],
                         "actor": decided["actor"] if decided else None, "committed": bool(decided)}
    return bound


def _ground(conn, mind, state, sources, identifier):
    """What one ground names, or None when the host cannot find it.

    Evidence first, against the set this evaluation was supplied, which is what the other audited
    sections resolve their references against; an internal event is never evidence. Then the
    ledger, which answers for both an effective trait and the correction that ended one. Then the
    mind's own concerns, by id or by the key a concern link would be resolved from.
    """
    shown = {ref["record_id"] for ref in sources or []}
    try:
        refs = mind._evidence(conn, [identifier])
    except (Conflict, Missing, KeyError):
        refs = []
    for ref in refs:
        if ref["record_id"] in shown and not never_evidence(ref):
            return {"kind": "evidence", "ref": identifier, "record_id": ref["record_id"], "revision": ref["revision"]}
    if traits.installed(conn):
        try:
            trait = traits.Traits(mind).get(conn, identifier)
        except Missing:
            trait = None
        if trait and trait["status"] in traits.EFFECTIVE:
            return {"kind": "trait", "ref": identifier, "revision": trait["revision"], "status": trait["status"]}
        if trait and trait.get("tombstone"):
            # The rows `Traits.corrections()` lists: what a correction ended, and on whose word.
            return {"kind": "correction", "ref": identifier, "revision": trait["revision"],
                    "basis": trait["tombstone"].get("basis"), "at": trait["tombstone"].get("at")}
    concerns = state.get("concerns") or {}
    cid = identifier if identifier in concerns else "concern_" + digest([mind.scope.key(), identifier])[:32]
    concern = concerns.get(cid)
    if concern and concern.get("status") in CURRENT_CONCERN:
        return {"kind": "concern", "ref": identifier, "id": cid, "revision": concern["revision"]}
    return None


def commit_move(commit):
    """`next_move`: the last section of the commit, so it may cite anything the rest of it applied."""
    mind, conn, value, state = commit.mind, commit.conn, commit.value, commit.state
    at, scope = mind.clock(), mind.scope.key()
    committed = _committed(conn, state, commit.event_id)
    if value.move in QUIET_MOVES and _outward(committed):
        raise Conflict("This move does not match what this appraisal committed",
                       code="next-move-inconsistent", target=value.move)
    bound = _binding(conn, mind, state, value, committed, commit.event_id, at)
    grounds = []
    for identifier in value.grounds:
        found = _ground(conn, mind, state, commit.sources, identifier)
        if not found:
            raise Conflict("This move rests on something the host cannot find",
                           code="next-move-forged-grounds", target=identifier)
        grounds.append(found)
    _ensure(conn)
    # Append-only, and one move per appraisal: a replay of the same event writes nothing more.
    conn.execute("INSERT OR IGNORE INTO mind_next_moves VALUES(?,?,?,?,?,?,?,?)",
                 ("move_" + digest([commit.event_id, "next-move"])[:32], scope, commit.event_id,
                  commit.job_id, at, value.move, _derive(value.move, committed),
                  dumps({"grounds": grounds, "binding": bound, "committed": committed,
                         # The agent's own words, stored as they were written.
                         "alternative": value.alternative, "reason": value.reason,
                         "receipt": commit.receipt, "agent_version": commit.version})))
    # This row is a record and nothing else. What it says about a wish or a step resting on a trait
    # does outlive it, because a correction has to be able to find what stood on that trait.
    trait_refs.attach(commit, bound, grounds)


def recent(mind, *, limit=SHOWN_MOVES):
    """The moves this mind stated, newest first, for an operator who wants to see them.

    Read only, and it changes nothing. `alternative` and `reason` are the agent's own words and are
    shown as written; everything else is the host's own finding.
    """
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        enabled = optimized(conn, scope, "next_move_audit")
        try:
            rows = conn.execute("SELECT * FROM mind_next_moves WHERE scope=? ORDER BY at DESC,id LIMIT ?",
                                (scope, max(1, min(100, limit)))).fetchall()
        except sqlite3.OperationalError:
            rows = []
        moves = []
        for row in rows:
            data = json.loads(row["data"])
            moves.append({"id": row["id"], "at": row["at"], "event_id": row["event_id"], "job_id": row["job_id"],
                          "declared": row["declared"], "derived": row["derived"],
                          "grounds": data["grounds"], "binding": data["binding"],
                          # Ids and kinds: what the host read the derived word off.
                          "committed": data.get("committed"),
                          "alternative": data["alternative"], "reason": data["reason"],
                          "receipt": data.get("receipt")})
        return {"moves": moves, "enabled": enabled,
                "last_refusal": appraisal.last_refusal(conn, scope, "next_move")}
