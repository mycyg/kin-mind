"""The trait ledger: what the host will vouch for about a slow-changing trait, and what it refuses.

Synthetic replays only: an injected clock, sources built through the engine, and scripted providers
that read the projection the model is really shown. No model call and no network.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from test_appraisal_sections import CONTEXT, Recorded
from test_plan_review_loop import RECEIPT, Env

from eventmem.core.db import digest, dumps
from eventmem.core.models import SourceInput
from eventmem.core.read_policy import configure_registry
from eventmem.core.self_knowledge import ClaimInput, SelfKnowledge
from kin_mind.appraisal import (
    ASK_AGAIN_SECTIONS,
    Appraisal,
    Appraisals,
    TraitDecision,
    TraitEpisode,
    TraitObservation,
    appraisal_context,
    last_refusal,
)
from kin_mind.traits import Traits

# The request with the ledger offered, so a change to its paragraph or its schema is deliberate.
# The all-off pins live with the seams and are not touched here.
# Re-pinned once when every package landed together: WP6's three switch-less changes to the
# shared prompt (the widened half-life range, the stated procedure premise, the owner named by
# role) move every request that offers anything, this one included.
LEDGER_REQUEST = "21996e6ec09bb3da03f59d86b9f0f0c946c696c76bffd6eab92a28feb9874c87"
LEDGER_SECTIONS = ("trait_observations", "trait_decisions")


class World(Env):
    """The plan-review world plus real owner turns, which are what draws an interaction window."""

    def owner(self, key, text, *, namespace="kin-owner-input", minutes=0):
        self.clock[0] += timedelta(minutes=minutes)
        return self.mind.engine.receive(SourceInput(namespace=namespace, key=key, text=text, extract=False,
            scope=self.mind.scope, authority="explicit", occurred_at=self.mind.clock(),
            metadata={"role": "user", "host_event": "message"}))["id"]

    def kin(self, key, text, *, namespace="kin-exploration", minutes=0, authority="model"):
        """Kin's own words, written back as a source of their own."""
        self.clock[0] += timedelta(minutes=minutes)
        return self.mind.engine.receive(SourceInput(namespace=namespace, key=key, text=text, extract=False,
            scope=self.mind.scope, authority=authority, kind="observation", occurred_at=self.mind.clock(),
            metadata={"role": "assistant", "host_event": "exploration-result"}))["id"]

    def exploration(self, key, text):
        """One exploration the host resolved: its report is Kin's own words, its receipt is not."""
        source = self.kin(key, text)
        with self.mind.engine.db.connect(write=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,"
                         "state TEXT NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)")
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                         (key, self.mind.scope.key(), "complete", self.mind.clock(), dumps({"source_id": source})))
        return key, source

    def appraise(self, sources, script):
        jobs = Appraisals(self.mind, exploration_capabilities={"version": self.version})
        job = jobs.enqueue(sources, self.version)["id"]
        provider = Watcher(script)
        result = jobs.run_one(provider, job_id=job)
        return result, self.job(job), provider

    def ledger(self):
        return Traits(self.mind)

    def rows(self, sql, *args):
        with self.mind.engine.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def observations(self, trait_id=None):
        from kin_mind.traits import installed
        with self.mind.engine.db.connect() as conn:
            return self.ledger().observations(conn, trait_id) if installed(conn) else []

    def followups(self, result):
        """What one refusal queued for a second, paid call. An audited section queues nothing."""
        return self.rows("SELECT id FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", result["id"])


class Watcher:
    """The provider as the request really reaches it: it is handed the projection, not the view."""

    def __init__(self, script):
        self.script, self.seen, self.calls = script, [], 0

    def appraise(self, context):
        self.calls += 1
        shown = appraisal_context(context)
        self.seen.append(shown)
        return self.script(shown.get("state", {}), shown), dict(RECEIPT)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def notice(source, slug, *, category="interests", evidence_class="owner_statement", polarity="support", **extra):
    return TraitObservation(key=slug, category=category, slug=slug, evidence_class=evidence_class,
                            polarity=polarity, evidence_ids=[source] if source else [], **extra)


def refused(data, section):
    return [r["code"] for r in data.get("rejected_sections", []) if r["section"] == section]


def first_round(world, slug="lanterns", text="Kin keeps coming back to lantern light."):
    """One owner evening, one observation, and the candidate that already acts."""
    source = world.owner(slug + "-praise", "I loved the lanterns you drew tonight.")
    result, data, _ = world.appraise([source], lambda state, shown: Appraisal(
        reason="The owner said so", values={"mood": 70}, trait_observations=[notice(source, slug)],
        trait_decisions=[TraitDecision(action="propose", text=text, basis="inference", reason="A first sign",
                                       observation_refs=[source], evidence_ids=[source])]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    return source


def second_round(world, slug="lanterns", *, hours=30, action="establish"):
    """Another evening, a day later: its own interaction window and its own episode."""
    world.clock[0] += timedelta(hours=hours)
    later = world.owner(slug + "-again", "You did it again with the lanterns.")

    def script(state, shown):
        trait = state["traits"]["candidate"][0]
        return Appraisal(reason="Again", trait_observations=[notice(later, slug)],
            trait_decisions=[TraitDecision(action=action, trait_id=trait["id"], expected_revision=trait["revision"],
                text=trait["text"], basis="inference", reason="Two separate evenings", evidence_ids=[later],
                observation_refs=[trait["observations"][0]["id"]],
                episodes=[TraitEpisode(ref=trait["observations"][0]["id"], why_distinct="An earlier evening"),
                          TraitEpisode(ref=later, why_distinct="Days later, unprompted")])])
    return later, world.appraise([later], script)


# --- two shared histories, two different contexts ---------------------------------------------------

def test_two_histories_show_the_model_different_traits_and_corrections(tmp_path):
    first, second = World(tmp_path / "a"), World(tmp_path / "b")
    first_round(first)
    _, (result, data, _) = second_round(first)
    assert result["state"] == "complete" and "rejected_sections" not in data

    first_round(second, slug="night-walks", text="Kin would rather walk than sit still.")
    _, (settled, _, _) = second_round(second, slug="night-walks")
    assert settled["state"] == "complete"
    established = second.ledger().read()["traits"][0]
    second.clock[0] += timedelta(hours=2)
    other = second.owner("tea-praise", "You always make the tea sound like an occasion.")
    second.appraise([other], lambda state, shown: Appraisal(reason="A second thread",
        trait_observations=[notice(other, "tea")],
        trait_decisions=[TraitDecision(action="propose", text="Kin makes small rituals of things.",
            basis="inference", reason="Another sign", observation_refs=[other], evidence_ids=[other])]))
    second.clock[0] += timedelta(hours=2)
    correction = second.owner("correction", "That is not you at all; drop the walking idea.")
    second.appraise([correction], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(action="revoke", trait_id=established["id"], expected_revision=established["revision"],
            text=established["text"], basis="owner_correction", quote="drop the walking idea",
            evidence_ids=[correction], reason="The owner said to drop it")]))

    seen = {}
    for name, world in (("a", first), ("b", second)):
        world.clock[0] += timedelta(hours=3)
        source = world.owner("what-next", "What are you thinking about?")
        world.appraise([source], lambda state, shown, name=name: seen.setdefault(name, state) and None
                       or Appraisal(reason="A quiet answer"))
    # A lists the trait its history established, with the counts behind it.
    trait = seen["a"]["traits"]["established"][0]
    assert trait["text"].startswith("Kin keeps coming back") and trait["status"] == "established"
    assert trait["facts"]["support"] == {"owner_statement": 2} and trait["facts"]["distinct_episodes"] == 2
    assert trait["facts"]["single_window"] is False and trait["facts"]["first_day"] < trait["facts"]["last_day"]
    assert seen["a"]["corrections"] == [] and seen["a"]["open_predictions"] == []
    # B lists another trait and the correction that ended the first, with the source that ended it.
    assert [t["slug"] for t in seen["b"]["traits"]["candidate"]] == ["tea"]
    assert seen["b"]["traits"]["established"] == [] and seen["a"]["traits"]["candidate"] == []
    assert seen["b"]["corrections"][0]["text"].startswith("Kin would rather walk")
    assert seen["b"]["corrections"][0]["source_ids"] == [correction]
    assert seen["b"]["corrections"][0]["owner_statement"] is True
    assert seen["b"]["corrections"][0]["quote"] == "drop the walking idea"
    assert json.dumps(seen["a"]["traits"], sort_keys=True) != json.dumps(seen["b"]["traits"], sort_keys=True)


# --- one time something happened -----------------------------------------------------------------

def test_ten_strong_messages_in_one_window_are_one_episode_and_cannot_establish(world):
    said = [world.owner("evening-" + str(n), "I really love how you think about lanterns.", minutes=2)
            for n in range(10)]
    world.appraise(said[:1], lambda state, shown: Appraisal(reason="A strong evening",
        trait_observations=[notice(source, "lanterns") for source in said[:1]],
        trait_decisions=[TraitDecision(action="propose", text="Kin lights up about lanterns.", basis="inference",
                                       reason="Tonight", observation_refs=[said[0]], evidence_ids=[said[0]])]))
    # Four more citations from the same window: one time something happened, so one observation.
    world.appraise(said[1:5], lambda state, shown: Appraisal(reason="Still that evening",
        trait_observations=[notice(source, "lanterns") for source in said[1:5]]))
    assert len(world.observations()) == 1

    def script(state, shown):
        trait = state["traits"]["candidate"][0]
        return Appraisal(reason="The owner has said it again and again",
            trait_decisions=[TraitDecision(action="establish", trait_id=trait["id"], expected_revision=trait["revision"],
                text=trait["text"], basis="inference", reason="Ten separate times", evidence_ids=[said[9]],
                observation_refs=[said[1], said[3]],
                episodes=[TraitEpisode(ref=said[1], why_distinct="Said on its own"),
                          TraitEpisode(ref=said[3], why_distinct="And again, independently")])])
    result, data, _ = world.appraise(said[5:], script)
    assert result["state"] == "complete"
    assert refused(data, "trait_decisions") == ["trait-single-episode"]
    kept = world.ledger().read()["traits"][0]
    assert kept["status"] == "candidate" and kept["revision"] == 1
    assert kept["facts"]["distinct_episodes"] == 1 and kept["facts"]["single_window"] is True
    # Recorded and never asked again: no follow-up, and the derivation is where it was.
    assert world.followups(result) == [] and "held_sections" not in data
    assert ASK_AGAIN_SECTIONS == {"habits", "plan_changes", "concerns", "action_decisions"}


def test_the_same_self_statement_in_ten_windows_still_has_no_support_outside_kin(world):
    said = [world.kin("thought-" + str(n), "I notice I keep returning to lantern light.", minutes=90)
            for n in range(10)]
    world.appraise(said[:4], lambda state, shown: Appraisal(reason="A thought of its own",
        trait_observations=[notice(s, "lanterns", evidence_class="self_statement") for s in said[:4]],
        trait_decisions=[TraitDecision(action="propose", text="Kin keeps returning to lantern light.",
            basis="inference", reason="It keeps recurring", observation_refs=[said[0]], evidence_ids=[said[0]])]))
    for batch in (said[4:8], said[8:]):
        world.appraise(batch, lambda state, shown, batch=batch: Appraisal(reason="Again, on its own",
            trait_observations=[notice(s, "lanterns", evidence_class="self_statement") for s in batch]))
    trait = world.ledger().read()["traits"][0]
    assert trait["facts"]["distinct_episodes"] == 10 and trait["facts"]["episodes"]["non_self_support"] == 0

    def script(state, shown):
        candidate = state["traits"]["candidate"][0]
        return Appraisal(reason="Ten times over", trait_decisions=[TraitDecision(action="establish",
            trait_id=candidate["id"], expected_revision=candidate["revision"], text=candidate["text"],
            basis="inference", reason="Ten separate windows", evidence_ids=[],
            episodes=[TraitEpisode(ref=said[0], why_distinct="One evening"),
                      TraitEpisode(ref=said[7], why_distinct="Another, days later")])])
    world.clock[0] += timedelta(hours=2)
    result, data, _ = world.appraise([world.owner("later", "Anything new?")], script)
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-single-episode"]
    assert world.ledger().read()["traits"][0]["status"] == "candidate"


def test_one_utterance_under_two_namespaces_is_one_root_and_one_episode(world):
    phone = world.owner("phone", "Same words about lanterns")
    desktop = world.owner("transcript", "same  words   about lanterns", namespace="host:codex", minutes=1)
    world.appraise([phone, desktop], lambda state, shown: Appraisal(reason="One sentence, twice ingested",
        trait_observations=[notice(phone, "lanterns"), notice(desktop, "lanterns")],
        trait_decisions=[TraitDecision(action="propose", text="Kin reads lantern light as warmth.",
            basis="inference", reason="The owner said it once", observation_refs=[phone], evidence_ids=[phone])]))
    rows = world.observations()
    assert len(rows) == 1 and len({r["root_key"] for r in rows}) == 1
    assert world.ledger().read()["traits"][0]["facts"]["distinct_episodes"] == 1


# --- what may stand for what -------------------------------------------------------------------------

def test_a_configuration_source_is_not_her_saying_what_kin_has_become(world):
    configure_registry(world.mind.engine, {"synthetic-persona-store": "role_configuration"})
    setup = world.mind.engine.receive(SourceInput(namespace="synthetic-persona-store", key="configure",
        text="Please speak warmly about lantern evenings.", scope=world.mind.scope, authority="explicit",
        occurred_at=world.mind.clock(), extract=False, metadata={"role": "user", "host_event": "message"}))["id"]
    result, data, _ = world.appraise([setup], lambda state, shown: Appraisal(
        reason="A configuration turn", trait_observations=[notice(setup, "lanterns")]))
    assert result["state"] == "complete" and refused(data, "trait_observations") == ["trait-evidence-class"]
    assert world.observations() == []


def test_a_role_claim_about_kin_is_not_evidence_of_what_kin_became(world):
    owner = world.owner("owner-mentions", "I like hearing what you explored.")
    claim = SelfKnowledge(world.mind.engine, world.mind.scope).claim(ClaimInput(command_id="role-claim",
        aspect="voice", context="chat", agent_version=world.version, basis="role",
        claim="I explore rarely.", evidence_ids=["mem_" + digest([owner, "root"])[:32]]))["id"]
    result, data, _ = world.appraise([owner], lambda state, shown: Appraisal(reason="A claim of its own",
        trait_observations=[notice(claim, "lanterns")]))
    assert result["state"] == "complete" and data["rejected_sections"][0]["section"] == "trait_observations"
    assert world.observations() == []


def test_an_explorations_report_is_kins_own_words_and_its_receipt_is_behaviour(world):
    run_id, report = world.exploration("exploration-1", "I found three lantern makers.")
    result, data, _ = world.appraise([report], lambda state, shown: Appraisal(reason="A finished exploration",
        trait_observations=[notice(report, "lanterns", evidence_class="verified_behavior")]))
    assert result["state"] == "complete" and refused(data, "trait_observations") == ["trait-evidence-class"]
    world.clock[0] += timedelta(hours=2)
    owner = world.owner("owner-asks", "Did you find anything?")
    result, data, _ = world.appraise([owner], lambda state, shown: Appraisal(reason="The receipt, not the report",
        trait_observations=[notice(None, "lanterns", evidence_class="verified_behavior", result_ids=[run_id])]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert [r["class"] for r in world.observations()] == ["verified_behavior"]


@pytest.mark.parametrize("evidence_class", ["owner_statement", "self_statement", "verified_behavior"])
def test_an_internal_event_is_evidence_of_nothing_in_any_class(world, evidence_class):
    owner = world.owner("owner-says", "Tell me what you decided.")
    internal = world.mind.engine.receive(SourceInput(namespace="mind-internal-event", key="decided-1",
        text=dumps({"kind": "internal", "decision": "explore"}), scope=world.mind.scope, authority="model",
        occurred_at=world.mind.clock(), extract=False, kind="observation",
        metadata={"role": "assistant", "host_event": "internal-event"}))["id"]
    proposed = notice(internal, "lanterns", evidence_class=evidence_class,
                      **({"result_ids": [internal]} if evidence_class == "verified_behavior" else {}))
    result, data, _ = world.appraise([owner, internal], lambda state, shown: Appraisal(
        reason="An internal note", trait_observations=[proposed]))
    assert result["state"] == "complete" and refused(data, "trait_observations") == ["trait-evidence-class"]
    assert world.observations() == []


# --- revoking, and coming back ---------------------------------------------------------------------

def test_a_revoke_keeps_its_history_leaves_other_traits_alone_and_needs_newer_words_to_return(world):
    first_round(world)
    second_round(world)
    world.clock[0] += timedelta(hours=3)
    other = world.owner("tea-praise", "You always make the tea sound like an occasion.")
    world.appraise([other], lambda state, shown: Appraisal(reason="Another sign",
        trait_observations=[notice(other, "tea")],
        trait_decisions=[TraitDecision(action="propose", text="Kin makes small rituals of things.",
            basis="inference", reason="A second thread", observation_refs=[other], evidence_ids=[other])]))
    lanterns = next(t for t in world.ledger().read()["traits"] if t["slug"] == "lanterns")
    # An owner statement made before the tombstone, kept back for a later evaluation.
    stale = world.owner("earlier-praise", "The lanterns really are you.")

    world.clock[0] += timedelta(hours=1)
    ended = world.owner("owner-corrects", "Honestly, drop the lantern thing; it is not you.")
    result, data, _ = world.appraise([ended], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(action="revoke", trait_id=lanterns["id"], expected_revision=lanterns["revision"],
            text=lanterns["text"], basis="owner_correction", quote="drop the lantern thing",
            evidence_ids=[ended], reason="The owner asked for it to go")]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    read = world.ledger().read(lanterns["id"], history=True)["traits"][0]
    assert read["status"] == "revoked" and read["tombstone"]["source_ids"] == [ended]
    assert [h["status"] for h in read["history"]] == ["candidate", "established", "revoked"]
    assert [t["status"] for t in world.ledger().read()["traits"] if t["slug"] == "tea"] == ["candidate"]

    world.clock[0] += timedelta(hours=1)
    result, data, _ = world.appraise([stale], lambda state, shown: Appraisal(reason="Trying again",
        trait_decisions=[TraitDecision(action="propose", trait_id=lanterns["id"], text=lanterns["text"],
            basis="owner_instruction", quote="The lanterns really are you", evidence_ids=[stale],
            reason="The owner said it before")]))
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-revoked"]
    assert world.ledger().read(lanterns["id"])["traits"][0]["status"] == "revoked"

    world.clock[0] += timedelta(hours=2)
    renewed = world.owner("owner-returns", "I was wrong: the lanterns really are you.")
    result, data, _ = world.appraise([renewed], lambda state, shown: Appraisal(reason="The owner took it back",
        trait_decisions=[TraitDecision(action="propose", trait_id=lanterns["id"], text=lanterns["text"],
            basis="owner_instruction", quote="the lanterns really are you", evidence_ids=[renewed],
            reason="The owner's own words, later")]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    back = world.ledger().read(lanterns["id"], history=True)["traits"][0]
    assert back["status"] == "candidate" and back["tombstone"] is None
    assert [h["status"] for h in back["history"]][-1] == "candidate"


def test_a_correction_without_her_own_words_changes_nothing(world):
    first_round(world)
    trait = world.ledger().read()["traits"][0]
    world.clock[0] += timedelta(hours=1)
    guessed = world.kin("kin-decides", "I think the lantern trait is wrong.")
    result, data, _ = world.appraise([guessed], lambda state, shown: Appraisal(reason="A second thought",
        trait_decisions=[TraitDecision(action="revoke", trait_id=trait["id"], expected_revision=trait["revision"],
            text=trait["text"], basis="owner_correction", quote="the lantern trait is wrong",
            evidence_ids=[guessed], reason="It no longer fits")]))
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-quote-unverified"]
    assert world.ledger().read()["traits"][0]["status"] == "candidate"


def test_a_stale_expected_revision_refuses_the_decision_and_leaves_the_trait(world):
    first_round(world)
    trait = world.ledger().read()["traits"][0]
    world.clock[0] += timedelta(hours=3)
    later = world.owner("later-praise", "The lanterns again, honestly.")
    result, data, _ = world.appraise([later], lambda state, shown: Appraisal(reason="A stale revision",
        trait_observations=[notice(later, "lanterns")],
        trait_decisions=[TraitDecision(action="revise", trait_id=trait["id"], expected_revision=trait["revision"] + 5,
            text="Something else entirely", basis="inference", reason="Rewriting it", evidence_ids=[later])]))
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-revision-changed"]
    assert world.ledger().read()["traits"][0]["text"].startswith("Kin keeps coming back")


# --- what a refusal costs, and what the model sees next ------------------------------------------------

def test_a_refusal_is_recorded_asks_nothing_again_and_reaches_the_next_projection(world):
    first_round(world)
    world.clock[0] += timedelta(hours=3)
    later = world.owner("second-evening", "Lanterns once more.")

    def script(state, shown):
        candidate = state["traits"]["candidate"][0]
        return Appraisal(reason="Too soon to establish", trait_decisions=[TraitDecision(action="establish",
            trait_id=candidate["id"], expected_revision=candidate["revision"], text=candidate["text"],
            basis="inference", reason="One evening is enough", evidence_ids=[later],
            episodes=[TraitEpisode(ref=candidate["observations"][0]["id"], why_distinct="It felt like its own moment")])])
    result, data, _ = world.appraise([later], script)
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-single-episode"]
    assert "held_sections" not in data and world.followups(result) == []
    with world.mind.engine.db.connect() as conn:
        assert last_refusal(conn, world.mind.scope.key())["trait_decisions"]["code"] == "trait-single-episode"

    world.clock[0] += timedelta(hours=3)
    seen = {}
    world.appraise([world.owner("third", "Anything new?")],
                   lambda state, shown: seen.update(state) and None or Appraisal(reason="Nothing new"))
    assert seen["traits"]["last_refusal"]["trait_decisions"]["code"] == "trait-single-episode"
    assert seen["traits"]["candidate"][0]["facts"]["episodes"]["support"] == 1


# --- the host's own routes ------------------------------------------------------------------------------

def test_the_host_reads_revokes_and_migrates_without_the_model(world, tmp_path):
    from kin_mind.host import dispatch

    first_round(world)
    config = {"root": str(world.mind.engine.db.root), "scope": world.mind.scope.model_dump(),
              "agent_version": world.version, "session_id": "synthetic-session"}
    read = dispatch(config, "traits", {})
    assert [t["slug"] for t in read["traits"]] == ["lanterns"] and read["enabled"] is True
    assert set(read["traits"][0]["facts"]) >= {"support", "counter", "episodes", "distinct_episodes"}
    ended = world.owner("operator-correction", "Drop the lantern thing, please.")
    revoked = dispatch(config, "trait-revoke", {"trait_id": read["traits"][0]["id"],
        "expected_revision": read["traits"][0]["revision"], "source_id": ended, "reason": "The owner asked"})
    assert revoked["state"] == "revoked" and revoked["trait"]["tombstone"]["actor"] == "host"
    assert dispatch(config, "traits", {})["corrections"][0]["source_ids"] == [ended]


def test_the_older_trait_record_is_carried_in_once_and_only_once(world):
    from kin_mind.host import dispatch

    with world.mind.engine.db.connect(write=True) as conn:
        state = world.mind._load(conn)
        state["traits"] = {"interests": {"text": "Kin likes long walks.", "basis": "hypothesis_trial",
                                         "event_id": "mind_synthetic", "evidence": []}}
        world.mind._save(conn, state)
    config = {"root": str(world.mind.engine.db.root), "scope": world.mind.scope.model_dump(),
              "agent_version": world.version, "session_id": "synthetic-session"}
    dry = dispatch(config, "traits-migrate", {})
    assert dry["state"] == "dry-run" and len(dry["carried"]) == 1 and world.ledger().read()["traits"] == []
    applied = dispatch(config, "traits-migrate", {"apply": True})
    assert applied["state"] == "applied" and applied["carried"] == dry["carried"]
    again = dispatch(config, "traits-migrate", {"apply": True})
    assert again["carried"] == [] and again["already_present"] == dry["carried"]
    carried = world.ledger().read()["traits"][0]
    assert carried["text"] == "Kin likes long walks." and carried["basis"] == "migrated"
    # The older record stays where it is, and the ledger answers in its shape as well.
    assert world.mind.read()["traits"]["interests"]["trait_id"] == carried["id"]


# --- with the switch off ---------------------------------------------------------------------------------

def test_with_the_ledger_off_the_view_and_the_projection_are_what_they_were(world):
    first_round(world)
    assert "trait_ledger" in world.mind.read()
    # The behaviour chain projects its own open predictions beside the ledger, on its own switch:
    # both off is what "what they were" means.
    world.memory.configure({"trait_ledger": False, "behavior_chain": False})
    view = world.mind.read()
    assert "trait_ledger" not in view and view["traits"] == {}
    projected = appraisal_context({"state": view, "stimulus": None})
    assert not {"traits", "corrections", "open_predictions"} & set(projected["state"])


def test_the_projection_is_added_only_where_the_view_carries_the_ledger():
    without = appraisal_context(CONTEXT)
    assert not {"traits", "corrections", "open_predictions"} & set(without["state"])
    ledger = {"traits": {"established": [], "candidate": [], "last_refusal": {}}, "corrections": [],
              "open_predictions": []}
    with_ledger = appraisal_context({**CONTEXT, "state": {**CONTEXT["state"], "trait_ledger": ledger}})
    assert with_ledger["state"]["traits"] == ledger["traits"] and with_ledger["state"]["corrections"] == []
    assert with_ledger["state"]["open_predictions"] == []
    # Nothing else about the projection moved.
    assert json.dumps({k: v for k, v in with_ledger["state"].items() if k not in
                       {"traits", "corrections", "open_predictions"}}, sort_keys=True) == \
        json.dumps(without["state"], sort_keys=True)


def test_the_request_that_offers_the_ledger_is_pinned(monkeypatch):
    api = Recorded(monkeypatch, LEDGER_SECTIONS)
    body, fingerprint = api.request(CONTEXT)
    assert fingerprint == LEDGER_REQUEST
    assert set(body["tools"][0]["input_schema"]["properties"]) >= set(LEDGER_SECTIONS)
    assert "trait_observations" in body["system"] and "trait_decisions" in body["system"]


def test_an_observation_the_evaluation_never_saw_is_refused_before_anything_else(world):
    owner = world.owner("owner-says", "Lanterns, again.")
    elsewhere = world.owner("not-in-this-batch", "Something else entirely")
    result, data, _ = world.appraise([owner], lambda state, shown: Appraisal(reason="Evidence from outside",
        trait_observations=[notice(elsewhere, "lanterns")]))
    assert result["state"] == "complete" and data["rejected_sections"][0]["section"] == "trait_observations"
    assert world.observations() == []


# --- what time does to a trait, and what a moved source does -------------------------------------------

def test_support_that_decayed_away_reads_and_then_records_as_fading(world):
    first_round(world)
    assert world.ledger().read()["traits"][0]["status"] == "candidate"
    world.clock[0] += timedelta(days=45)
    # Read first: the projection derives it from the same curve before anything is written.
    assert world.ledger().read()["traits"][0]["status"] == "fading"
    assert world.rows("SELECT status FROM mind_traits")[0]["status"] == "candidate"
    fresh = world.owner("much-later", "Still the lanterns.")
    world.appraise([fresh], lambda state, shown: Appraisal(reason="A late evening",
        trait_observations=[notice(fresh, "tea")]))
    assert world.rows("SELECT status FROM mind_traits WHERE json_extract(data,'$.slug')='lanterns'")[0]["status"] == "fading"
    history = world.ledger().read(history=True)["traits"]
    assert [h["status"] for h in next(t for t in history if t["slug"] == "lanterns")["history"]] == ["candidate", "fading"]


def test_a_faded_trait_cannot_be_restored_without_new_support(world):
    first_round(world)
    trait = world.ledger().read()["traits"][0]
    world.clock[0] += timedelta(days=45)
    stale = world.owner("late-question", "What are you thinking?")

    def script(state, shown):
        return Appraisal(reason="Trying to bring it back", trait_decisions=[TraitDecision(action="restore",
            trait_id=trait["id"], expected_revision=state["traits"]["candidate"][0]["revision"],
            text=trait["text"], basis="inference", reason="It still feels true", evidence_ids=[stale])])
    result, data, _ = world.appraise([stale], script)
    assert result["state"] == "complete" and refused(data, "trait_decisions") == ["trait-needs-support"]


def test_a_source_that_moved_leaves_the_trait_and_its_observation_for_review(world):
    source = first_round(world)
    world.revise(source, "retract", "owner-retracted-own-words")
    assert world.observations() == []
    with world.mind.engine.db.connect() as conn:
        assert [o["state"] for o in world.ledger().observations(conn, state="needs_review")] == ["needs_review"]
    trait = world.ledger().read()["traits"][0]
    assert trait["status"] == "needs_review" and trait["needs_review"] is True
    # A trait under review is neither established nor offered as a candidate the model may build on.
    assert world.mind.read()["trait_ledger"]["traits"]["established"] == []


def test_the_dependents_table_is_there_for_whatever_cites_a_trait(world):
    first_round(world)
    assert world.rows("SELECT name FROM sqlite_master WHERE name='mind_trait_dependents'")
    assert world.rows("SELECT * FROM mind_trait_dependents") == []


def test_the_chat_model_may_read_the_ledger_and_has_no_way_to_write_to_it(world):
    from eventmem.core.mcp import create_mcp

    first_round(world)
    tools = {t.name for t in create_mcp(world.mind.engine)._tool_manager.list_tools()}
    assert "read_trait_ledger" in tools
    assert {name for name in tools if "trait" in name} == {"read_trait_ledger"}


def test_a_proposal_neither_demotes_an_established_trait_nor_ignores_its_revision(world):
    first_round(world)
    second_round(world)
    assert world.ledger().read()["traits"][0]["status"] == "established"
    world.clock[0] += timedelta(hours=30)
    again = world.owner("third-evening", "The lanterns, still.")

    def script(state, shown):
        found = state["traits"]["established"][0]
        return Appraisal(reason="Saying it again", trait_observations=[notice(again, "lanterns")],
            trait_decisions=[TraitDecision(action="propose", trait_id=found["id"], expected_revision=found["revision"],
                text=found["text"], basis="inference", reason="Still true", evidence_ids=[again],
                observation_refs=[again])])
    result, data, _ = world.appraise([again], script)
    assert result["state"] == "complete" and "rejected_sections" not in data
    established = world.ledger().read()["traits"][0]
    assert established["status"] == "established" and established["facts"]["distinct_episodes"] == 3

    world.clock[0] += timedelta(hours=30)
    stale = world.owner("fourth-evening", "Lanterns, again.")
    _, data, _ = world.appraise([stale], lambda state, shown: Appraisal(reason="A stale proposal",
        trait_decisions=[TraitDecision(action="propose", trait_id=established["id"], expected_revision=1,
            text=established["text"], basis="inference", reason="Out of date", evidence_ids=[stale])]))
    assert refused(data, "trait_decisions") == ["trait-revision-changed"]


def persona(world, keys):
    """The owner-approved contract as the host installs it: it alone says which categories may grow."""
    policy = {"schema": 1, "version": "test-v1", "scope": world.mind.scope.model_dump(),
              "approved_source": "owner-request", "requires_owner_confirmation": True,
              "core": "【MY_PERSONA_LOAD】 SYNTHETIC\n【/MY_PERSONA_LOAD】", "voice": "Use complete sentences.",
              "maintenance": "Preserve source quotes.", "mutable_trait_keys": list(keys)}
    for key in ("core", "voice", "maintenance"):
        policy[key + "_sha256"] = hashlib.sha256(policy[key].encode()).hexdigest()
    (world.mind.engine.db.root / "persona-policy.json").write_text(json.dumps(policy))


def test_a_category_the_contract_has_not_approved_never_reaches_the_ledger(world):
    persona(world, ["interests"])
    unapproved = world.owner("humour", "You are funnier than you think.")
    result, data, _ = world.appraise([unapproved], lambda state, shown: Appraisal(reason="A new category",
        trait_observations=[notice(unapproved, "dry-jokes", category="humor")]))
    assert result["state"] == "complete" and refused(data, "trait_observations") == ["invalid-value"]
    assert world.observations() == []
    # A Chinese name for an approved category reaches the contract as that one key.
    world.clock[0] += timedelta(hours=2)
    approved = world.owner("lanterns", "You always come back to the lanterns.")
    result, data, _ = world.appraise([approved], lambda state, shown: Appraisal(reason="An approved category",
        trait_observations=[notice(approved, "lanterns", category="兴趣")]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert [o["category"] for o in world.observations()] == ["interests"]
