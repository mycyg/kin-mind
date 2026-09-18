"""What a trait holds up, and what happens the moment that trait moves.

A trait acts. A wish is committed on it, a step is decided on it, an intent is stated on it. One
owner sentence can end it, and then every one of those is standing on something the ledger no
longer carries. These cases are about that seam: the revision a thing was committed against is
recorded, a moved trait leaves it for review, and each reader does its own one thing about it —
a wish stops being ready, a step asks for exactly one plan review, an intent falls back.

Synthetic replays only: an injected clock, sources built through the engine, and scripted providers
that read the projection the model is really shown. No model call and no network.
"""
import json
from datetime import timedelta

import pytest
from test_appraisal_sections import CONTEXT
from test_next_move import enable_continuity, shown_plan
from test_trait_ledger import World, first_round, second_round

from kin_mind import judgment_cache, trait_refs
from kin_mind import manifest as manifests
from kin_mind.appraisal import (
    Appraisal,
    ExpressionIntent,
    NextMove,
    TraitDecision,
    Wish,
    appraisal_context,
)
from kin_mind.autonomy_models import ActionDecision
from kin_mind.expression_intent import TRAIT
from kin_mind.expression_intent import view as intent_view
from kin_mind.next_move import recent
from kin_mind.plans import REVIEW_REASONS

pytest_plugins = ("test_memory_continuity",)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def established(world, slug="lanterns"):
    """Two evenings apart, so the shared history really carries the trait before anything rests on it."""
    first_round(world, slug=slug)
    second_round(world, slug=slug)
    return world.ledger().read()["traits"][0]


def contact_wish(world, source):
    """A wish this appraisal really commits, so it carries the decision receipt a wish needs to be
    ready at all. The move that says it rests on a trait comes in the round after it."""
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="A real owner message", wishes=[Wish(content="Say what the lanterns turned into",
            topic="lanterns", kind="contact", strength=70, ttl_hours=24, completion="They have heard about it")]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    return next(d["id"] for d in world.mind.read()["desires"] if d["kind"] == "contact")


def ready(world, identifier):
    with world.mind.engine.db.connect() as conn:
        state = world.mind._load(conn)
        return world.mind._desire_ready(conn, state["desires"][identifier], world.mind.clock(), state=state)


def desire(world, identifier):
    return next(d for d in world.mind.read()["desires"] if d["id"] == identifier)


def dependents(world):
    return world.rows("SELECT kind,dependent_id,trait_id,trait_revision,state FROM mind_trait_dependents"
                      " ORDER BY kind,dependent_id")


def revoke(world, trait, *, text="That is not you at all; drop the lantern thing."):
    """An owner correction through the host's own route: it does not wait for an appraisal."""
    ended = world.owner("owner-correction", text)
    return world.ledger().revoke({"trait_id": trait["id"], "expected_revision": trait["revision"],
                                  "source_id": ended, "reason": "The owner asked for it to stop"}), ended


def commits_on(world, trait_id, source, *, plan=None, wish_ref=None, intent=False):
    """One appraisal that does something and says which trait it is doing it on."""
    def script(state, shown):
        decisions = [ActionDecision(plan_id=plan["id"], step_id="make", action="execute",
                                    expected_revision=shown_plan(shown, plan["id"])["revision"],
                                    evidence_ids=[source], reason="Everything this step needs is here")] if plan else []
        return Appraisal(reason="A real owner message", action_decisions=decisions,
            expression_intent=ExpressionIntent(stance="Stay with what the evenings were about",
                valid_minutes=720, evidence_ids=[source], trait_refs=[trait_id]) if intent else None,
            next_move=NextMove(move="reply", wish_ref=wish_ref, step_ref="make" if plan else None,
                grounds=[trait_id], alternative="Letting the evening pass", reason="What this rests on"))
    return world.appraise([source], script)


# --- a choice committed with its grounds ----------------------------------------------------------

def test_a_wish_committed_on_a_trait_keeps_the_revision_it_rested_on(world):
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    wish_id = contact_wish(world, world.owner("owner-asks", "Anything you want to say?"))
    assert ready(world, wish_id) and "trait_revisions" not in desire(world, wish_id)

    world.clock[0] += timedelta(hours=1)
    result, data, _ = commits_on(world, trait["id"], world.owner("owner-again", "Still thinking about it?"),
                                 wish_ref=wish_id)
    assert result["state"] == "complete" and "rejected_sections" not in data
    # The wish keeps the map itself, and the ledger keeps the line back to it.
    assert desire(world, wish_id)["trait_revisions"] == {trait["id"]: trait["revision"]}
    assert desire(world, wish_id)["trait_needs_review"] is False and ready(world, wish_id)
    assert dependents(world) == [{"kind": "desire", "dependent_id": wish_id, "trait_id": trait["id"],
                                 "trait_revision": trait["revision"], "state": "valid"}]
    # And the move that said so is the row that holds the grounds, with the revision it read.
    move = recent(world.mind)["moves"][0]
    assert move["binding"]["wish"] == {"id": wish_id, "kind": "contact", "status": "wanted", "committed": False}
    assert move["grounds"] == [{"kind": "trait", "ref": trait["id"], "revision": trait["revision"],
                                "status": "established"}]


def test_a_move_that_rests_on_no_trait_writes_no_dependent_at_all(world):
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    asked = world.owner("owner-asks", "Anything you want to say?")
    wish_id = contact_wish(world, asked)
    world.clock[0] += timedelta(hours=1)
    again = world.owner("owner-again", "And now?")
    result, data, _ = world.appraise([again], lambda state, shown: Appraisal(reason="A real owner message",
        next_move=NextMove(move="reply", wish_ref=wish_id, grounds=[again],
                           alternative="Saying nothing", reason="Only what was just said")))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert dependents(world) == [] and "trait_revisions" not in desire(world, wish_id)
    # Nothing about the trait changed, so nothing about that wish did either.
    assert world.ledger().read(trait["id"])["traits"][0]["revision"] == trait["revision"]


# --- the owner corrects it ------------------------------------------------------------------------

def test_an_owner_correction_ends_the_trait_and_everything_standing_on_it(world):
    """The whole seam in one replay: the revoke is immediate, the intent falls back to the lookup,
    the linked wish becomes not ready, and exactly one plan review is woken."""
    enable_continuity(world)
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    wish_id = contact_wish(world, world.owner("owner-asks", "Anything you want to say?"))
    plan = world.create(key="lantern-piece")
    world.clock[0] += timedelta(hours=1)
    result, data, _ = commits_on(world, trait["id"], world.owner("owner-again", "Still on it?"),
                                 plan=plan, wish_ref=wish_id, intent=True)
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert ready(world, wish_id) and world.step(plan)["waiting_reason"] is None
    with world.mind.engine.db.connect() as conn:
        assert intent_view(conn, world.mind, world.mind.clock())["use"]["trait_refs"] == \
            [{"trait_id": trait["id"], "revision": trait["revision"]}]
    # Nothing has moved, so nothing is asking for a review.
    assert world.ticks() == 0

    revoked, ended = revoke(world, trait)
    assert revoked["state"] == "revoked" and revoked["trait"]["tombstone"]["source_ids"] == [ended]
    assert world.ledger().read(trait["id"])["traits"][0]["status"] == "revoked"
    assert {(row["kind"], row["state"]) for row in dependents(world)} == {
        ("decision", "needs_review"), ("desire", "needs_review"), ("intent", "needs_review")}

    # The wording falls back to the table, with the static reason for it.
    with world.mind.engine.db.connect() as conn:
        fallen = intent_view(conn, world.mind, world.mind.clock())
    assert fallen["use"] is None and fallen["status"]["stale_reason"] == TRAIT
    assert world.mind.read()["continuity"]["expression_intent"]["stale_reason"] == TRAIT
    # The wish is neither still ready nor quietly gone: it says why.
    assert ready(world, wish_id) is False and desire(world, wish_id)["trait_needs_review"] is True
    assert desire(world, wish_id)["status"] == "wanted"
    # And the step asks for one review, which consumes the reason that woke it.
    assert world.step(plan)["waiting_reason"] == "trait-needs-review"
    assert world.ticks() == 1
    assert world.ticks() == 1


def test_the_reason_a_moved_trait_gives_a_step_asks_for_a_review_and_not_a_retry():
    assert "trait-needs-review" in REVIEW_REASONS


def test_a_decision_taken_again_is_not_held_by_the_one_before_it(world):
    trait = established(world)
    plan = world.create(key="lantern-piece")
    world.clock[0] += timedelta(hours=3)
    commits_on(world, trait["id"], world.owner("owner-asks", "Ready to start?"), plan=plan)
    revoke(world, trait)
    assert world.step(plan)["waiting_reason"] == "trait-needs-review"
    assert world.ticks() == 1

    # A new decision about the same step cites what it cites now; the old row names the old decision.
    world.clock[0] += timedelta(hours=1)
    later = world.owner("owner-later", "Carry on then.")
    world.appraise([later], lambda state, shown: Appraisal(reason="A real owner message",
        action_decisions=[ActionDecision(plan_id=plan["id"], step_id="make", action="execute",
            expected_revision=shown_plan(shown, plan["id"])["revision"], evidence_ids=[later],
            reason="Everything this step needs is here")]))
    assert world.step(plan)["waiting_reason"] is None
    assert [row["state"] for row in dependents(world) if row["kind"] == "decision"] == ["needs_review"]


# --- what the host does with a wish a moved trait left behind --------------------------------------

def test_the_host_route_moves_a_wish_a_moved_trait_left_behind_into_waiting(world):
    from kin_mind.host import dispatch

    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    wish_id = contact_wish(world, world.owner("owner-asks", "Anything you want to say?"))
    world.clock[0] += timedelta(hours=1)
    commits_on(world, trait["id"], world.owner("owner-again", "Still on it?"), wish_ref=wish_id)
    config = {"root": str(world.mind.engine.db.root), "scope": world.mind.scope.model_dump(),
              "agent_version": world.version, "session_id": "synthetic-session"}
    assert dispatch(config, "trait-wish-review", {}) == {"wishes": [], "dependents": [], "settled": [],
                                                         "state": "settled", "scope": world.mind.scope.key()}
    revoke(world, trait)

    seen = dispatch(config, "trait-wish-review", {})
    assert seen["state"] == "dry-run" and [w["id"] for w in seen["wishes"]] == [wish_id]
    assert seen["settled"] == [] and desire(world, wish_id)["status"] == "wanted"
    assert [row["kind"] for row in seen["dependents"]] == ["desire"]

    applied = dispatch(config, "trait-wish-review", {"apply": True})
    assert applied["state"] == "settled" and applied["settled"] == [wish_id]
    moved = desire(world, wish_id)
    assert moved["status"] == "waiting" and moved["reason"] == trait_refs.WAITING_REASON
    assert moved["contact_wait"]["condition"] == "new_evidence"
    # Idempotent: a second run has nothing left to move.
    assert dispatch(config, "trait-wish-review", {"apply": True})["settled"] == []
    assert dispatch(config, "trait-wish-review", {"apply": True})["state"] == "settled"


# --- the input manifest --------------------------------------------------------------------------

def stored_manifest(world, data):
    return manifests.load(world.mind.engine, world.mind.scope.key(), data["manifest"])


def test_the_manifest_carries_what_the_ledger_showed_and_a_moved_trait_changes_it(world):
    enable_continuity(world)
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    commits_on(world, trait["id"], world.owner("owner-asks", "Anything on your mind?"), intent=True)
    # The manifest of the attempt after it: what that attempt was shown is what the one before it
    # committed, which is exactly the question a stored proposal has to answer later.
    world.clock[0] += timedelta(minutes=30)
    _, before, _ = world.appraise([world.owner("owner-quiet", "No rush.")],
                                  lambda state, shown: Appraisal(reason="A quiet answer"))
    old = stored_manifest(world, before)
    assert old["classes"]["traits"][trait["id"]]["revision"] == trait["revision"]
    assert old["classes"]["traits"][trait["id"]]["status"] == "established"
    assert list(old["classes"]["intent"].values())[0]["trait_refs"] == [trait["id"]]
    assert "corrections" not in old["classes"]

    revoke(world, trait)
    world.clock[0] += timedelta(hours=1)
    _, after, _ = world.appraise([world.owner("owner-later", "Anything now?")],
                                 lambda state, shown: Appraisal(reason="A quiet answer"))
    new = stored_manifest(world, after)
    assert trait["id"] not in new["classes"].get("traits", {})
    assert new["classes"]["corrections"][trait["id"]]["revision"] == trait["revision"] + 1
    moved = {(c["class"], c["object"]) for c in manifests.compare(old, new) if c["relevant"]}
    assert ("traits", trait["id"]) in moved and ("corrections", trait["id"]) in moved
    assert ("intent", list(old["classes"]["intent"])[0]) in moved


def test_every_new_class_is_registered_and_history_rests_on_none_of_them():
    for name in ("traits", "corrections", "predictions", "intent"):
        assert name in manifests.CLASSES
        assert manifests.relevant("interaction", name) and not manifests.relevant(manifests.HISTORY, name)
        # Session maintenance rests on the session alone, so these are irrelevant there already.
        assert not manifests.relevant(manifests.MAINTENANCE, name)
    # An older release knows none of them, and an unknown class is relevant: Tier B, never Tier A.
    assert manifests.relevant("interaction", "a-class-from-a-later-release")


def test_a_manifest_built_with_the_ledger_off_carries_none_of_the_new_classes(world):
    world.memory.configure({"trait_ledger": False, "behavior_chain": False, "expression_intent": False,
                            "next_move_audit": False})
    _, data, _ = world.appraise([world.owner("owner-asks", "Anything on your mind?")],
                               lambda state, shown: Appraisal(reason="A quiet answer"))
    assert not {"traits", "corrections", "predictions", "intent"} & set(stored_manifest(world, data)["classes"])


# --- what a light question is asked about ---------------------------------------------------------

def test_a_moved_trait_reaches_a_light_question_as_an_object(world):
    from kin_mind.revalidation import RESTING, _fragments

    trait = established(world)
    proposal = Appraisal(reason="A real owner message", trait_decisions=[TraitDecision(action="revise",
        trait_id=trait["id"], expected_revision=trait["revision"], text=trait["text"], basis="inference",
        reason="Saying it more precisely")], next_move=NextMove(move="reply", grounds=[trait["id"]],
        reason="What this rests on")).model_dump()
    # The item that names the trait, down to its own index, and not the whole section.
    assert _fragments(proposal, "traits", trait["id"]) == [["trait_decisions", 0], ["next_move"]]
    assert _fragments(proposal, "traits", "trait_" + "0" * 32) == []
    # Whatever else moved in a class a judgment rests on points at the sections that rest on it.
    assert set(RESTING["traits"]) == {"trait_decisions", "self_hypothesis", "expression_intent", "next_move"}
    assert RESTING["predictions"] == ("prediction_outcomes",) and RESTING["intent"] == ("expression_intent",)
    # A trait decision fences itself, so what it writes is part of the write set of the proposal.
    assert ("traits", trait["id"]) in manifests.write_set(proposal)


def test_a_moved_trait_reaches_the_question_as_an_object_and_never_as_free_text(world):
    """What a light question is given about a trait that moved: what it was, what it is now, and the
    evidence it may still cite — the same shape every other conflict entry has."""
    from kin_mind.appraisal import Appraisals
    from kin_mind.revalidation import Stored, conflict_list

    enable_continuity(world)
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    commits_on(world, trait["id"], world.owner("owner-asks", "Anything on your mind?"), intent=True)
    world.clock[0] += timedelta(minutes=30)
    _, before, _ = world.appraise([world.owner("owner-quiet", "No rush.")],
                                  lambda state, shown: Appraisal(reason="A quiet answer"))
    old = stored_manifest(world, before)
    correction = revoke(world, trait)[1]
    world.clock[0] += timedelta(hours=1)
    _, after, _ = world.appraise([world.owner("owner-later", "Anything now?")],
                                 lambda state, shown: Appraisal(reason="A quiet answer"))
    new = stored_manifest(world, after)

    proposal = Appraisal(reason="A stored proposal", next_move=NextMove(move="reply", grounds=[trait["id"]],
                         reason="What this rests on")).model_dump()
    stored = Stored("reuse", proposal, {}, [], [], before["manifest"], {}, 0, None, False)
    entries = conflict_list(Appraisals(world.mind), stored, proposal, old, new,
                            changes=[c for c in manifests.compare(old, new) if c["relevant"]],
                            stale=[], owner_moved=False, expired=False,
                            shown=appraisal_context({"state": world.mind.read(), "stimulus": None}),
                            citable={correction})
    listed = {entry["public"]["object"]: entry for entry in entries}
    moved = listed["traits:" + trait["id"]]["public"]
    # What it was: the ledger's own sentence at the revision the stored proposal was shown.
    assert moved["kind"] == "traits" and moved["before"]["content"]["text"].startswith("Kin keeps coming back")
    assert moved["before"]["content"]["status"] == "established" and moved["after"] is None
    # The section of the stored proposal that named it, which is the only thing a patch may touch.
    assert moved["relevant_fragment"] == [{"path": ["next_move"]}]
    # What it is now: the correction that ended it, as its own object, with the words that did it.
    ended = listed["corrections:" + trait["id"]]["public"]
    assert ended["after"]["content"]["basis"] == "owner_correction"
    assert ended["after"]["content"]["owner_statement"] is True and ended["after"]["content"]["actor"] == "host"
    assert ended["after"]["content"]["source_ids"] == [correction] and ended["valid_evidence"] == [correction]
    # The intent rested on it too. This stored proposal states none of its own, so it is not an
    # object the question is about: it is one line of what else moved in a class this rests on.
    identifier = list(old["classes"]["intent"])[0]
    grouped = listed["intent:*"]["public"]
    assert grouped["after"][identifier]["stale_reason"] == TRAIT
    assert grouped["before"][identifier]["needs_review"] is False


# --- the caches ------------------------------------------------------------------------------------

def test_a_cached_judgment_that_rested_on_a_trait_does_not_answer_once_it_moves(world):
    trait = established(world)
    verdict = {"result": {"complete": True}, "receipt": {"model": "deepseek-flash"}}
    judgment = {"scope": world.mind.scope.key(), "type": "step-complete", "goal": "a synthetic goal",
                "completion": "a synthetic completion", "obligation_version": "1"}
    token = judgment_cache.put(world.mind.engine, "review_step", "request-digest", judgment, verdict,
                               now=1.0, depends_on=[trait["id"]])
    judgment_cache.accept(world.mind.engine, token)
    with world.mind.engine.db.connect() as conn:
        assert judgment_cache.get(world.mind.engine, conn, "review_step", "request-digest", judgment, now=2.0)

    revoke(world, trait)
    with world.mind.engine.db.connect() as conn:
        assert judgment_cache.get(world.mind.engine, conn, "review_step", "request-digest", judgment, now=3.0) is None


def test_the_traits_a_request_was_shown_are_what_it_depends_on(world):
    established(world)
    world.clock[0] += timedelta(hours=3)
    seen = {}
    world.appraise([world.owner("owner-asks", "Anything on your mind?")],
                   lambda state, shown: seen.update(shown) and None or Appraisal(reason="A quiet answer"))
    shown = seen["state"]["traits"]["established"][0]["id"]
    assert shown in judgment_cache.dependencies(seen)
    # And with no ledger in the projection there is nothing of the kind to depend on.
    assert judgment_cache.dependencies(appraisal_context(CONTEXT)) == [CONTEXT["new_evidence"][0]["id"]]


# --- what an older release sees ---------------------------------------------------------------------

def stored_wish(world, identifier):
    row = world.rows("SELECT data FROM mind_state WHERE scope=?", world.mind.scope.key())[0]
    return json.loads(row["data"])["desires"][identifier]


def test_the_new_keys_are_only_ever_there_where_something_really_rests_on_a_trait(world):
    trait = established(world)
    world.clock[0] += timedelta(hours=3)
    wish_id = contact_wish(world, world.owner("owner-asks", "Anything you want to say?"))
    plain, was = desire(world, wish_id), stored_wish(world, wish_id)
    assert "trait_revisions" not in plain and "trait_needs_review" not in plain
    projected = next(d for d in appraisal_context({"state": world.mind.read(), "stimulus": None})["state"]["desires"]
                     if d["id"] == wish_id)
    assert "trait_needs_review" not in projected

    world.clock[0] += timedelta(hours=1)
    commits_on(world, trait["id"], world.owner("owner-again", "Still on it?"), wish_ref=wish_id)
    revoke(world, trait)
    # Now it is there, and the model is shown it for what it is: a wish resting on a moved trait.
    projected = next(d for d in appraisal_context({"state": world.mind.read(), "stimulus": None})["state"]["desires"]
                     if d["id"] == wish_id)
    assert projected["trait_needs_review"] is True
    # In storage the wish gained one key and lost none: an older release reads it as it always did,
    # which is why the host route above has to run before a rollback.
    kept = stored_wish(world, wish_id)
    assert set(kept) - set(was) == {"trait_revisions"} and not set(was) - set(kept)
    assert kept["status"] == "wanted" and kept["trait_revisions"] == {trait["id"]: trait["revision"]}
