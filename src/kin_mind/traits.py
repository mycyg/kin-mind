"""Traits a shared history formed, kept as a ledger the host can vouch for.

The model says what it noticed and what it concludes from it. The host checks only that the
material is real, that it is of the class it was given, and that it comes from separate times.
It never judges the trait itself: that sentence is Kin's own.

- An **observation** is one piece of evidence for or against one trait, from one episode. The
  table is append-only, its identity is the evidence and not the telling of it, and only a row's
  `state` ever changes. One trait takes one observation per episode and polarity: ten messages
  inside one interaction window are one time something happened, not ten.
- A **trait** acts from the moment it is proposed. `establish` says the shared history carries
  it, and on inference that costs at least two separate episodes and at least one supporting
  observation that is not Kin's own words.
- A **tombstone** is what a revoked trait leaves behind: the reason, the source that revoked it,
  and a history that stays readable. Proposing it again needs an owner statement newer than it.
- **Fading** is nobody's decision. A trait whose support has decayed away reads as fading, on the
  same curve effective use already decays by, and the next write of the ledger records it.
"""

from __future__ import annotations

import json
import math
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.persona import load_persona, validate_trait_changes
from eventmem.core.read_policy import ReadPolicy

from . import appraisal
from .autonomy_schema import optimized
from .evidence_classes import (
    episode_key,
    never_evidence,
    owner_statement,
    root_key,
    self_statement,
    verified_behavior,
    window_of,
)
from .rhythm import interaction_windows
from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_traits(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL,
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_trait_status ON mind_traits(scope,status,updated_at);
CREATE TABLE IF NOT EXISTS mind_trait_history(
 id TEXT NOT NULL, revision INTEGER NOT NULL, command_id TEXT NOT NULL, at TEXT NOT NULL,
 data TEXT NOT NULL, PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS mind_trait_observations(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, scope TEXT NOT NULL,
 trait_id TEXT NOT NULL, class TEXT NOT NULL, polarity TEXT NOT NULL, episode_key TEXT NOT NULL,
 root_key TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_trait_evidence ON mind_trait_observations(scope,trait_id,state);
CREATE UNIQUE INDEX IF NOT EXISTS mind_trait_episode
 ON mind_trait_observations(trait_id,episode_key,polarity);
CREATE TABLE IF NOT EXISTS mind_trait_dependents(
 scope TEXT NOT NULL, trait_id TEXT NOT NULL, trait_revision INTEGER NOT NULL, kind TEXT NOT NULL,
 dependent_id TEXT NOT NULL, dependent_revision INTEGER, state TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(scope,trait_id,kind,dependent_id));
"""

# A candidate already acts. `established` says the shared history carries it, `fading` that its
# support decayed, `needs_review` that a source moved under it, `revoked` that it is over.
EFFECTIVE = ("candidate", "established")
# The two audited sections this module owns, and whose refusals its projection carries back.
SECTIONS = ("trait_observations", "trait_decisions")
# The half-life effective use already decays by, with one support at one half-life as the floor.
# No support inside that period leaves a trait fading; this is not a threshold for promotion.
SUPPORT_HALF_LIFE_DAYS = 30
FADE_FLOOR = 0.5
# What the persona contract calls the same category. The contract itself decides which of them a
# trait may use; this only spells one category one way before asking it.
CATEGORY_ALIASES = {"兴趣": "interests", "审美": "aesthetics", "幽默": "humor",
                    "表达习惯": "expression_habits", "习惯": "habits", "价值": "values"}
# How much of the ledger one appraisal is shown. The projection is structured state and is never
# compressed, so it stays small by counting rather than by retelling.
SHOWN_TRAITS = 12
SHOWN_OBSERVATIONS = 4
SHOWN_CORRECTIONS = 6


def installed(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_traits'").fetchone())


def canonical(category):
    """Chinese and English names for one category reach the persona contract as one key."""
    return CATEGORY_ALIASES.get((category or "").strip(), (category or "").strip())


def local_day(at):
    return timestamp(at).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat()


def support_strength(observations, at):
    """What is left of the support, on the curve effective use already decays by."""
    total = 0.0
    for observation in observations:
        if observation["polarity"] != "support":
            continue
        days = max(0.0, (timestamp(at) - timestamp(observation["at"])).total_seconds() / 86400)
        total += math.exp2(-days / SUPPORT_HALF_LIFE_DAYS)
    return total


class Traits:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope.key()

    # --- storage -----------------------------------------------------------------------------

    def ensure(self, conn):
        # Statement by statement: a script would commit the transaction this commit runs inside.
        for statement in filter(None, (part.strip() for part in SCHEMA.split(";"))):
            conn.execute(statement)

    def get(self, conn, identifier):
        row = conn.execute("SELECT data FROM mind_traits WHERE scope=? AND id=?", (self.scope, identifier)).fetchone()
        if not row:
            raise Missing("Trait is missing or outside this scope", code="trait-unknown")
        return json.loads(row[0])

    def _find(self, conn, identifier):
        row = conn.execute("SELECT data FROM mind_traits WHERE scope=? AND id=?", (self.scope, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, conn, trait, command, at):
        trait["updated_at"] = at
        conn.execute("INSERT INTO mind_traits VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "category=excluded.category,status=excluded.status,revision=excluded.revision,"
                     "updated_at=excluded.updated_at,data=excluded.data",
                     (trait["id"], self.scope, trait["category"], trait["status"], trait["revision"], at, dumps(trait)))
        conn.execute("INSERT OR REPLACE INTO mind_trait_history VALUES(?,?,?,?,?)",
                     (trait["id"], trait["revision"], command, at, dumps(trait)))
        # The one place a trait is written, so it is the one place that has to tell what rested on
        # it. A revision it no longer has is not this trait any more; a cached judgment that rested
        # on it proves two requests were equal, never that the trait behind them still holds.
        from . import trait_refs
        from .judgment_cache import invalidate
        trait_refs.trait_moved(conn, self.scope, trait["id"], trait["revision"], at,
                               ended=trait["status"] not in EFFECTIVE)
        invalidate(conn, [trait["id"]])
        return trait

    def observations(self, conn, trait_id=None, *, state="valid"):
        where = "scope=? AND state=?" + (" AND trait_id=?" if trait_id else "")
        args = (self.scope, state) + ((trait_id,) if trait_id else ())
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT data FROM mind_trait_observations WHERE " + where + " ORDER BY seq", args).fetchall()]

    def _allowed_category(self, category, sample):
        """One gate for the category, the persona contract's own. A category the owner has not
        approved is refused here, before anything about it is stored."""
        name = canonical(category)
        policy = load_persona(self.engine, self.mind.scope)
        if policy and name not in policy["mutable_trait_keys"] and (category or "").strip() in policy["mutable_trait_keys"]:
            name = (category or "").strip()
        validate_trait_changes(policy, {name: sample})
        return name

    # --- what the material is ------------------------------------------------------------------

    def _policy(self, conn):
        return ReadPolicy.load(self.engine, self.mind.scope, conn=conn)

    def _window(self, conn, at, cache):
        """The interaction window an evidence item falls in. Asked as of that moment, so the
        window holding it is always the last one drawn and no recent-window cut can hide it."""
        if at not in cache:
            cache[at] = window_of(interaction_windows(conn, self.scope, at)["recent_windows"], at)
        return cache[at]

    def _receipt(self, conn, identifier):
        """A host-resolved execution, in the shape the shared predicate reads. Who wrote the text
        decides nothing: an exploration's report is Kin's own words, its receipt is behaviour."""
        row = conn.execute("SELECT state,data FROM mind_plan_runs WHERE scope=? AND id=?", (self.scope, identifier)).fetchone()
        if row:
            data = json.loads(row["data"])
            return {"kind": "plan-run", "state": row["state"], "result": data.get("result") or {},
                    "execution_id": identifier, "source_id": (data.get("result") or {}).get("source_id")}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone():
            row = conn.execute("SELECT state,data FROM mind_explorations WHERE scope=? AND id=?", (self.scope, identifier)).fetchone()
            if row:
                return {"kind": "exploration", "state": row["state"], "execution_id": identifier,
                        "source_id": json.loads(row["data"]).get("source_id")}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
            row = conn.execute("SELECT data FROM mind_runtime_events WHERE scope=? AND id=? AND kind IN "
                               "('task-result','delivery')", (self.scope, identifier)).fetchone()
            if row:
                data = json.loads(row[0])
                return {**data, "execution_id": data.get("task_id") or data.get("delivery_id") or identifier}
        return None

    def _material(self, conn, observation, refs, policy, cache):
        """Whether the cited material may stand for the class it was given, and which single time
        it came from. The episode is the host's: a caller may merge episodes, never split one."""
        found = observation.evidence_class
        if found == "verified_behavior":
            receipts = [self._receipt(conn, identifier) for identifier in observation.result_ids]
            if not receipts or not all(r and verified_behavior(r) for r in receipts):
                raise Conflict("Cited material cannot stand for the evidence class it was given",
                               code="trait-evidence-class")
            # The host's order, not the order they were cited in, so one citation is one episode.
            runs = sorted(r["execution_id"] for r in receipts)
            return episode_key(execution_id=runs[0]), "run_" + digest(runs)[:32], receipts
        if not refs:
            raise Conflict("A trait observation needs evidence of its own", code="trait-evidence-missing")
        for ref in refs:
            record = self.engine._get(conn, ref["record_id"])
            cited = [{"namespace": ref["namespace"], "authority": ref["authority"]}]
            passes = (owner_statement(record, policy, sources=cited) if found == "owner_statement"
                      else self_statement(record, sources=cited))
            if never_evidence(cited[0]) or not passes:
                raise Conflict("Cited material cannot stand for the evidence class it was given",
                               code="trait-evidence-class")
        first = min(refs, key=lambda r: (r["occurred_at"], r["source_id"]))
        window = self._window(conn, first["occurred_at"], cache)
        # Two ingestions of one utterance are one origin, whatever namespaces carried them.
        root = (root_key(text=self.engine._get(conn, first["record_id"])["content"], window=window)
                if found == "owner_statement" and window else root_key(source_id=first["source_id"]))
        return (episode_key(window=window) if window else episode_key(root=root)), root, []

    # --- observations ----------------------------------------------------------------------------

    def observe(self, conn, proposed, command, receipt, allowed, event_id):
        """Each piece of evidence, once, against the trait it speaks about."""
        from .plans import AutonomousPlans
        self.ensure(conn)
        at, policy, cache, stored = self.mind.clock(), self._policy(conn), {}, []
        for index, observation in enumerate(proposed):
            category = self._allowed_category(observation.category, observation.key)
            refs = (AutonomousPlans(self.mind)._refs(conn, observation.evidence_ids, allowed)
                    if observation.evidence_ids else [])
            episode, root, receipts = self._material(conn, observation, refs, policy, cache)
            identifier = "trait_" + digest([self.scope, category, observation.slug.strip()])[:32]
            oid = "obs_" + digest([identifier, observation.evidence_class, observation.polarity, root])[:32]
            row = {"id": oid, "trait_id": identifier, "key": observation.key, "category": category,
                   "slug": observation.slug.strip(), "class": observation.evidence_class,
                   "polarity": observation.polarity, "episode_key": episode, "root_key": root, "at": at,
                   "day": local_day(at), "state": "valid", "note": observation.note, "evidence": refs,
                   "evidence_ids": sorted({r["record_id"] for r in refs} | {r["source_id"] for r in refs}),
                   "result_ids": list(observation.result_ids), "receipts": receipts, "event_id": event_id,
                   "command_id": command + ":" + str(index), "receipt": receipt}
            stored.append(self._store(conn, row, identifier, episode, observation.polarity))
        touched = {row["trait_id"] for row in stored}
        for identifier in touched:
            self._recount(conn, identifier, at)
        self._settle(conn, at, skip=touched)
        return stored

    def _store(self, conn, row, identifier, episode, polarity):
        """One row per episode and polarity. A repeat of the same root is the same evidence and
        keeps the row it has; fresh evidence for a row a source change invalidated revives it."""
        kept = conn.execute("SELECT id,state FROM mind_trait_observations WHERE id=? OR "
                            "(trait_id=? AND episode_key=? AND polarity=?)",
                            (row["id"], identifier, episode, polarity)).fetchone()
        if kept and kept["state"] == "valid":
            return json.loads(conn.execute("SELECT data FROM mind_trait_observations WHERE id=?", (kept["id"],)).fetchone()[0])
        if kept:
            conn.execute("UPDATE mind_trait_observations SET class=?,root_key=?,at=?,state='valid',data=? WHERE id=?",
                         (row["class"], row["root_key"], row["at"], dumps({**row, "id": kept["id"]}), kept["id"]))
            return {**row, "id": kept["id"]}
        conn.execute("INSERT INTO mind_trait_observations(id,scope,trait_id,class,polarity,episode_key,root_key,at,state,data) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (row["id"], self.scope, identifier, row["class"], polarity, episode, row["root_key"],
                      row["at"], "valid", dumps(row)))
        return row

    # --- decisions --------------------------------------------------------------------------------

    def decide(self, conn, decisions, command, receipt, allowed, event_id):
        from .plans import AutonomousPlans
        self.ensure(conn)
        at, policy, cache, applied = self.mind.clock(), self._policy(conn), {}, []
        for index, decision in enumerate(decisions):
            refs = (AutonomousPlans(self.mind)._refs(conn, decision.evidence_ids, allowed)
                    if decision.evidence_ids else [])
            applied.append(self._decide(conn, decision, command + ":" + str(index), receipt, refs, policy, event_id, at, cache))
        self._settle(conn, at, skip={t["id"] for t in applied})
        return applied

    def _decide(self, conn, decision, command, receipt, refs, policy, event_id, at, cache):
        trait = self.get(conn, decision.trait_id) if decision.trait_id else None
        named = self._lookup(conn, decision.observation_refs, trait, cache)
        identity = None
        if trait is None:
            trait, identity = self._target(conn, named)
        if trait and trait.get("command_id") == command:
            return trait
        if trait and trait["status"] != "revoked" and trait["revision"] != (decision.expected_revision or 0):
            # Compare and swap on everything the ledger already holds, a re-proposal included: a
            # decision taken against a revision that has moved is not a decision about this trait.
            raise Conflict("Trait changed during evaluation", kind="runtime", code="trait-revision-changed",
                           target=trait["id"], expected=decision.expected_revision, actual=trait["revision"])
        owner = self._owner_words(conn, decision, refs, policy)
        if trait and trait["status"] == "revoked":
            # What an owner correction ended is over until the owner says something newer than it.
            newer = [r for r in owner if timestamp(r["occurred_at"]) > timestamp(trait["tombstone"]["at"])]
            if decision.action not in {"propose", "restore"} or not newer:
                raise Conflict("A revoked trait needs an owner statement newer than its tombstone",
                               code="trait-revoked", target=trait["id"])
        if decision.action == "revoke":
            return self._revoke(conn, trait, decision, command, receipt, refs, owner, event_id, at)
        if trait is None:
            if decision.action != "propose":
                raise Missing("Trait is missing or outside this scope", code="trait-unknown")
            trait = {**identity, "status": "candidate", "revision": 0, "created_at": at, "facts": None}
        self._allowed_category(trait["category"], decision.text)
        support = [o for o in self.observations(conn, trait["id"]) if o["polarity"] == "support"]
        if decision.action == "establish":
            self._established(conn, trait, decision, support, cache)
        if decision.action == "restore" and support_strength(support, at) < FADE_FLOOR:
            raise Conflict("A faded trait needs new support before it stands again",
                           code="trait-needs-support", target=trait["id"])
        if decision.basis in {"owner_instruction", "owner_correction"} and not owner:
            raise Conflict("An owner instruction or correction must quote the owner's own words",
                           code="trait-quote-unverified", target=trait["id"])
        # A proposal names a trait; it does not demote one the history already carries. Only what is
        # new, or what a correction ended and newer words brought back, starts again as a candidate.
        status = {"propose": "candidate" if trait["status"] in {"candidate", "revoked"} else trait["status"],
                  "establish": "established", "revise": trait["status"],
                  "fade": "fading", "restore": "candidate"}[decision.action]
        trait.update(text=decision.text, status=status, revision=trait["revision"] + 1, basis=decision.basis,
                     reason=decision.reason, evidence=refs, quote=decision.quote, command_id=command,
                     receipt=receipt, event_id=event_id, agent_version=self.mind._load(conn)["agent_version"],
                     observation_refs=[o["id"] for o in named], episodes=[e.model_dump() for e in decision.episodes],
                     owner_sources=[r["source_id"] for r in owner], action=decision.action)
        trait.pop("invalidation", None)
        # The tombstone belongs to the revision it ended; the history keeps it, this row does not.
        trait.pop("tombstone", None)
        if decision.action == "establish":
            trait["established_at"] = at
        self._save(conn, trait, command, at)
        return self._recount(conn, trait["id"], at)

    def _lookup(self, conn, refs, trait=None, cache=None):
        """What a decision may name an observation by: the id the ledger showed it, an evidence or
        result id that observation cites, or — once the trait is known — any evidence from the same
        episode as one of its observations. Naming more of one time is merging, which is allowed;
        the host answers with its own episode either way, so nothing here can split one."""
        rows = self.observations(conn, trait["id"] if trait else None)
        found = []
        for ref in refs:
            match = next((o for o in rows if ref == o["id"] or ref in o["evidence_ids"] or ref in o["result_ids"]), None)
            if not match and trait:
                match = self._by_episode(conn, rows, ref, cache if cache is not None else {})
            if not match:
                raise Conflict("A trait decision names an observation the ledger does not hold",
                               code="trait-observation-unknown")
            found.append(match)
        return found

    def _by_episode(self, conn, rows, ref, cache):
        """A source the ledger holds no observation of, but whose time falls inside an episode it
        does. The host answers with that episode's observation; the material itself stays unread."""
        row = conn.execute("SELECT occurred_at FROM sources WHERE scope=? AND id=? AND deleted=0",
                           (self.scope, ref)).fetchone()
        if not row:
            return None
        window = self._window(conn, row[0], cache)
        key = episode_key(window=window) if window else None
        return next((o for o in rows if key and o["episode_key"] == key), None)

    def _target(self, conn, named):
        """The trait a decision is about when it names none: the one its observations are about. A
        first proposal takes its category and slug from the evidence, never from free text."""
        if len({o["trait_id"] for o in named}) != 1:
            raise Conflict("A trait decision names an observation the ledger does not hold",
                           code="trait-observation-unknown")
        first = named[0]
        identity = {"id": first["trait_id"], "key": first["key"], "category": first["category"], "slug": first["slug"]}
        return self._find(conn, first["trait_id"]), identity

    def _established(self, conn, trait, decision, support, cache):
        """The narrow invariant: on inference, separate episodes, support that is not Kin's own,
        and the decision naming two of those observations and why they are different times."""
        if decision.basis != "inference":
            return
        episodes = [o["episode_key"] for o in
                    self._lookup(conn, [entry.ref for entry in decision.episodes], trait, cache)]
        if (len({o["episode_key"] for o in support}) < 2
                or not any(o["class"] != "self_statement" for o in support)
                or len(set(episodes)) < 2):
            raise Conflict("Establishing on inference needs separate episodes and support that is not Kin's own",
                           code="trait-single-episode", target=trait["id"])

    def _owner_words(self, conn, decision, refs, policy):
        """The cited evidence that really is the owner speaking, with the quote checked literally
        against the record it came from. The host reads the words, never their meaning."""
        found = []
        for ref in refs:
            record = self.engine._get(conn, ref["record_id"])
            cited = [{"namespace": ref["namespace"], "authority": ref["authority"]}]
            if never_evidence(cited[0]) or not owner_statement(record, policy, sources=cited):
                continue
            if decision.quote and decision.quote not in (record.get("content") or ""):
                continue
            found.append(ref)
        return found

    def _revoke(self, conn, trait, decision, command, receipt, refs, owner, event_id, at):
        """Only the owner ends a trait through an appraisal, in words the host found in the owner's own
        record. Evidence that merely turned is a `fade`."""
        if trait is None:
            raise Missing("Trait is missing or outside this scope", code="trait-unknown")
        if trait["status"] == "revoked":
            return trait
        if not owner:
            raise Conflict("An owner instruction or correction must quote the owner's own words",
                           code="trait-quote-unverified", target=trait["id"])
        return self._tombstone(conn, trait, command, at, reason=decision.reason, basis=decision.basis,
                               quote=decision.quote, evidence=refs, owner=owner, receipt=receipt, event_id=event_id)

    def _tombstone(self, conn, trait, command, at, *, reason, basis, quote=None, evidence=(), owner=(),
                   receipt=None, event_id=None, actor="appraisal"):
        trait.update(status="revoked", revision=trait["revision"] + 1, command_id=command, receipt=receipt,
                     event_id=event_id, action="revoke",
                     tombstone={"at": at, "reason": reason, "basis": basis, "quote": quote, "actor": actor,
                                "source_ids": [r["source_id"] for r in (owner or evidence)],
                                "owner_statement": bool(owner), "revoked_revision": trait["revision"]})
        # Everything that rested on this trait is left for review by the write itself: a revoked
        # trait has no revision anyone may still be standing on.
        self._save(conn, trait, command, at)
        return self.get(conn, trait["id"])

    # --- the facts the host counts -----------------------------------------------------------------

    def _settle(self, conn, at, skip=()):
        """A trait whose support decayed away is fading. Nobody decided that; it is written down
        here so the history says when, and every projection reads the same predicate before it."""
        for row in conn.execute("SELECT id FROM mind_traits WHERE scope=? AND status IN ('candidate','established')",
                                (self.scope,)).fetchall():
            if row[0] in skip:
                continue
            trait = self.get(conn, row[0])
            if support_strength(self.observations(conn, trait["id"]), at) >= FADE_FLOOR:
                continue
            trait.update(status="fading", revision=trait["revision"] + 1, faded_at=at)
            self._save(conn, trait, "fade:" + local_day(at), at)

    def _recount(self, conn, identifier, at):
        trait = self._find(conn, identifier)
        if not trait:
            return None
        trait["facts"] = self.facts(conn, identifier, at)
        conn.execute("UPDATE mind_traits SET data=? WHERE id=?", (dumps(trait), identifier))
        return trait

    def facts(self, conn, identifier, at):
        """Counts only. What they say about the trait is the model's to write, not the host's."""
        observations = self.observations(conn, identifier)
        found = {"support": {}, "counter": {}, "episodes": {"support": 0, "counter": 0, "non_self_support": 0},
                 "distinct_episodes": 0, "single_window": False, "first_day": None, "last_day": None,
                 "last_support_day": None, "last_counter_day": None, "counter_examples": 0,
                 "support_strength": round(support_strength(observations, at), 3)}
        for observation in observations:
            side = found[observation["polarity"]]
            side[observation["class"]] = side.get(observation["class"], 0) + 1
            day = observation["day"]
            found["first_day"] = min(found["first_day"] or day, day)
            found["last_day"] = max(found["last_day"] or day, day)
            key = "last_" + observation["polarity"] + "_day"
            found[key] = max(found[key] or day, day)
        for polarity in ("support", "counter"):
            found["episodes"][polarity] = len({o["episode_key"] for o in observations if o["polarity"] == polarity})
        found["episodes"]["non_self_support"] = len({o["episode_key"] for o in observations
                                                     if o["polarity"] == "support" and o["class"] != "self_statement"})
        found["distinct_episodes"] = len({o["episode_key"] for o in observations})
        found["single_window"] = found["distinct_episodes"] == 1
        found["counter_examples"] = sum(found["counter"].values())
        found["fading"] = found["support_strength"] < FADE_FLOOR
        return found

    # --- what a reader is shown ---------------------------------------------------------------------

    def _shown(self, conn, trait, at):
        facts = self.facts(conn, trait["id"], at)
        return {"id": trait["id"], "category": trait["category"], "slug": trait["slug"], "key": trait["key"],
                "text": trait.get("text", ""), "revision": trait["revision"], "basis": trait.get("basis"),
                "status": "fading" if facts["fading"] and trait["status"] in EFFECTIVE else trait["status"],
                "stored_status": trait["status"], "reason": trait.get("reason", ""),
                "updated_at": trait["updated_at"], "established_at": trait.get("established_at"),
                "needs_review": trait["status"] == "needs_review", "facts": facts,
                "observations": [{k: o[k] for k in ("id", "class", "polarity", "episode_key", "day")}
                                 for o in self.observations(conn, trait["id"])[-SHOWN_OBSERVATIONS:]]}

    def projection(self, conn, at):
        """Established and candidate apart, each with its counts and the ids a decision may name.
        The last refusal is here too, so the host's answer reaches the model without a second call."""
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT data FROM mind_traits WHERE scope=? AND status<>'revoked' ORDER BY updated_at DESC,id",
            (self.scope,)).fetchall()]
        shown = [self._shown(conn, trait, at) for trait in rows[:SHOWN_TRAITS]]
        refused = appraisal.last_refusal(conn, self.scope)
        return {"established": [t for t in shown if t["stored_status"] == "established"],
                "candidate": [t for t in shown if t["stored_status"] != "established"],
                # Only what this projection owns: why the host refused an observation or a decision.
                "last_refusal": {k: v for k, v in refused.items() if k in SECTIONS},
                "window": {"included": len(shown), "total": len(rows)}}

    def corrections(self, conn, limit=SHOWN_CORRECTIONS):
        """What a correction ended, with the source that ended it."""
        rows = [json.loads(r[0]) for r in conn.execute(
            "SELECT data FROM mind_traits WHERE scope=? AND status='revoked' ORDER BY updated_at DESC,id LIMIT ?",
            (self.scope, max(1, limit))).fetchall()]
        return [{"trait_id": t["id"], "key": t["key"], "category": t["category"], "text": t.get("text", ""),
                 "revision": t["revision"], **{k: t["tombstone"].get(k) for k in
                 ("at", "reason", "basis", "quote", "actor", "source_ids", "owner_statement")}} for t in rows]

    def legacy(self, conn):
        """The dict shape the older readers know, so nothing downstream has to learn a new one."""
        found = {}
        for row in conn.execute("SELECT data FROM mind_traits WHERE scope=? AND status IN ('candidate','established') "
                                "ORDER BY updated_at,id", (self.scope,)).fetchall():
            trait = json.loads(row[0])
            found[trait["key"]] = {"text": trait.get("text", ""), "basis": trait.get("basis"),
                                   "trait_id": trait["id"], "revision": trait["revision"],
                                   "status": trait["status"], "event_id": trait.get("event_id"),
                                   "evidence": trait.get("evidence", []), "needs_review": False}
        return found

    # --- the host's own routes -------------------------------------------------------------------------

    def read(self, identifier=None, *, limit=SHOWN_TRAITS, history=False):
        """Facts and the trait's own sentence. No evidence text and no host verdict in words."""
        with self.engine.db.connect() as conn:
            if not installed(conn):
                return {"traits": [], "corrections": [], "open_predictions": [], "state": "empty"}
            at, found = self.mind.clock(), []
            rows = [self.get(conn, identifier)] if identifier else [json.loads(r[0]) for r in conn.execute(
                "SELECT data FROM mind_traits WHERE scope=? ORDER BY updated_at DESC,id LIMIT ?",
                (self.scope, max(1, min(100, limit)))).fetchall()]
            for trait in rows:
                shown = {**self._shown(conn, trait, at), "tombstone": trait.get("tombstone")}
                if history:
                    shown["history"] = [{"revision": r["revision"], "at": r["at"], "command_id": r["command_id"],
                                         "status": json.loads(r["data"])["status"],
                                         "reason": json.loads(r["data"]).get("reason", "")} for r in conn.execute(
                        "SELECT revision,at,command_id,data FROM mind_trait_history WHERE id=? ORDER BY revision",
                        (trait["id"],)).fetchall()]
                found.append(shown)
            return {"traits": found, "corrections": self.corrections(conn),
                    "open_predictions": open_predictions(conn, self.mind),
                    "enabled": optimized(conn, self.scope, "trait_ledger")}

    def revoke(self, request):
        """The operator's own route, for a correction that must not wait for an appraisal."""
        with self.engine.db.connect(write=True) as conn:
            self.ensure(conn)
            at, trait = self.mind.clock(), self.get(conn, request["trait_id"])
            if trait["status"] == "revoked":
                return {"trait": trait, "state": "already-revoked"}
            if request.get("expected_revision") is not None and trait["revision"] != request["expected_revision"]:
                raise Conflict("Trait changed during evaluation", kind="runtime", code="trait-revision-changed",
                               target=trait["id"], expected=request["expected_revision"], actual=trait["revision"])
            refs, policy, owner = self.mind._evidence(conn, [request["source_id"]]), self._policy(conn), []
            for ref in refs:
                if owner_statement(self.engine._get(conn, ref["record_id"]), policy,
                                   sources=[{"namespace": ref["namespace"], "authority": ref["authority"]}]):
                    owner.append(ref)
            revoked = self._tombstone(conn, trait, "trait-revoke:" + trait["id"] + ":" + str(trait["revision"]), at,
                                      reason=request["reason"], basis="owner_correction" if owner else "operator",
                                      evidence=refs, owner=owner, actor="host")
            return {"trait": revoked, "state": "revoked"}

    def migrate(self, *, apply=False):
        """Whatever the older trait record holds becomes a candidate with its own provenance.
        Idempotent, and a dry run writes nothing."""
        with self.engine.db.connect(write=True) as conn:
            self.ensure(conn)
            at, state = self.mind.clock(), self.mind._load(conn)
            carried, present = [], []
            for key, value in (state.get("traits") or {}).items():
                category = canonical(key)
                identifier = "trait_" + digest([self.scope, category, key.strip()])[:32]
                if self._find(conn, identifier):
                    present.append(identifier)
                    continue
                carried.append(identifier)
                if not apply:
                    continue
                self._save(conn, {"id": identifier, "key": key, "category": category, "slug": key.strip(),
                    "text": value.get("text", ""), "status": "candidate", "revision": 1, "basis": "migrated",
                    "reason": "Carried from the earlier trait record", "evidence": value.get("evidence", []),
                    "quote": None, "command_id": "traits-migrate:" + identifier, "receipt": None,
                    "event_id": value.get("event_id"), "agent_version": state.get("agent_version"),
                    "observation_refs": [], "episodes": [], "owner_sources": [], "action": "propose",
                    "created_at": at, "facts": None,
                    "origin": {k: value[k] for k in ("claim_id", "assessment_id", "basis", "event_id") if k in value}},
                    "traits-migrate:" + identifier, at)
                self._recount(conn, identifier, at)
            return {"migration": "traits-ledger-v1", "state": "applied" if apply else "dry-run",
                    "carried": carried, "already_present": present, "total": len(state.get("traits") or {})}


def open_predictions(conn, mind):
    """What the behavior chain still has open, on the chain's own switch, without the revision the
    manifest needs. Empty while that switch is off, whatever the ledger's switch says."""
    if not optimized(conn, mind.scope.key(), "behavior_chain"):
        return []
    from .behavior_chain import open_predictions as chain
    return [{key: value for key, value in item.items() if key != "revision"}
            for item in chain(conn, mind, limit=4)]


def ledger_view(conn, mind, at):
    """What the state projection adds: the dict shape older readers know and the structured view an
    appraisal is shown, while the ledger is on; the predictions still open, while the chain is on.
    Each part answers to its own switch, and with both off there is nothing to add at all."""
    scope, view = mind.scope.key(), {}
    if optimized(conn, scope, "trait_ledger") and installed(conn):
        ledger = Traits(mind)
        view = {"legacy": ledger.legacy(conn), "traits": ledger.projection(conn, at),
                "corrections": ledger.corrections(conn), "open_predictions": []}
    if optimized(conn, scope, "behavior_chain"):
        # On its own switch, whatever the ledger's says: the paragraph that asks the model to settle
        # a prediction promises this key, so it is there to be empty rather than missing.
        view["open_predictions"] = open_predictions(conn, mind)
    return view or None


def manifest_entries(view):
    """The ids this ledger contributes to the input-manifest classes `traits` and `corrections`.

    Read off the projection the model was really shown, not the table: the manifest records what
    one attempt was given, and a trait the window left out is not something it rested on. Decision
    10 puts them here rather than in `profile_version`, so a trait that moved costs a reread of
    what rested on it and not of every stored proposal there is."""
    ledger = (view or {}).get("trait_ledger") or {}
    traits, shown = ledger.get("traits") or {}, {}
    for side in ("established", "candidate"):
        for trait in traits.get(side, []):
            shown[trait["id"]] = {"revision": trait["revision"], "status": trait["status"],
                                  "stored_status": trait["stored_status"], "needs_review": trait["needs_review"]}
    corrections = {entry["trait_id"]: {"revision": entry["revision"], "at": entry["at"]}
                   for entry in ledger.get("corrections") or []}
    return shown, corrections


def invalidate_source(conn, record):
    """A source moved under a trait. Its text and its history stay; what rested on it is reviewed."""
    if not installed(conn):
        return
    from . import trait_refs
    from .judgment_cache import invalidate
    changed = conn.execute(
        "UPDATE mind_trait_observations SET state='needs_review',data=json_set(data,'$.state','needs_review') "
        "WHERE state='valid' AND EXISTS(SELECT 1 FROM json_each(mind_trait_observations.data,'$.evidence') e "
        "WHERE json_extract(e.value,'$.record_id')=? AND json_extract(e.value,'$.revision')<>?) RETURNING trait_id",
        (record["id"], record["revision"])).fetchall()
    reviewed = conn.execute(
        "UPDATE mind_traits SET status='needs_review',data=json_set(data,'$.status','needs_review',"
        "'$.invalidation','source-version-changed') WHERE status IN ('candidate','established') AND ("
        "id IN (SELECT value FROM json_each(?)) OR EXISTS(SELECT 1 FROM json_each(mind_traits.data,'$.evidence') e "
        "WHERE json_extract(e.value,'$.record_id')=? AND json_extract(e.value,'$.revision')<>?)) RETURNING id,scope",
        (dumps([r[0] for r in changed]), record["id"], record["revision"])).fetchall()
    # The revision does not move here — it is the same sentence — but the trait is no longer in
    # force, so what rested on it stands exactly where a revoke leaves it.
    for row in reviewed:
        trait_refs.trait_moved(conn, row["scope"], row["id"], None, record["updated_at"], ended=True)
    invalidate(conn, [row["id"] for row in reviewed])


def commit_observations(commit):
    Traits(commit.mind).observe(commit.conn, commit.value, commit.event_id + ":observation",
                                commit.receipt, commit.sources, commit.event_id)


def commit_decisions(commit):
    Traits(commit.mind).decide(commit.conn, commit.value, commit.event_id + ":trait",
                               commit.receipt, commit.sources, commit.event_id)


# What was observed did happen, so it still commits when the owner wrote again while the appraisal
# ran; what was decided from it waits for the next round.
appraisal.register_audit_section("trait_observations", commit_observations, commits_on_new_interaction=True)
appraisal.register_audit_section("trait_decisions", commit_decisions)
