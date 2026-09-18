"""The move an appraisal states: recorded with the grounds it really rests on, checked against
what that same appraisal committed, and with no hold whatever over what the host does next.

Synthetic replays only: an injected clock, sources built through the engine, and scripted providers
that read the projection the model is really shown. No model call and no network.
"""
import json
from datetime import timedelta

import pytest
from test_appraisal_sections import CONTEXT, Recorded
from test_trait_ledger import World, first_round, refused, second_round

from kin_mind import memory as memory_module
from kin_mind.appraisal import (
    ASK_AGAIN_SECTIONS,
    AUDIT_SECTIONS,
    SECTIONS_WITHHELD,
    Appraisal,
    Appraisals,
    NextMove,
    TraitDecision,
    Wish,
    WishUpdate,
    blank_sections,
    last_refusal,
    offered_sections,
)
from kin_mind.autonomy_models import ActionDecision
from kin_mind.continuity import (
    ConcernProposal,
    ContinuityConfig,
    OwnerRequest,
    Understanding,
)
from kin_mind.next_move import recent
from kin_mind.state import DesireChange

pytest_plugins = ("test_memory_continuity",)

# The request with the move offered, so a change to its paragraph or its schema is deliberate.
# The all-off pins live with the seams and are not touched here.
# Re-pinned once when every package landed together: WP6's three switch-less changes to the
# shared prompt (the widened half-life range, the stated procedure premise, the owner named by
# role) move every request that offers anything, this one included.
# Re-pinned once more when the intent and the move were told, as the ledger's own paragraph
# already told it, that a host internal event is not evidence: production refused both
# sections on two of its first four appraisals for citing exactly that.
MOVE_REQUEST = "520bce80a6c33347e937a16030aaed663a478ba5ec5477caedafd524a8bd4af7"
MOVE_SECTIONS = ("next_move",)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def stated(move="reply", **extra):
    return NextMove(move=move, alternative="Waiting one more evening", reason="What this choice rests on", **extra)


def recorded(world):
    return recent(world.mind)["moves"]


def wish(content, kind="contact", **extra):
    return Wish(content=content, topic="synthetic", kind=kind, strength=70, ttl_hours=24, completion="Done", **extra)


def shown_plan(shown, plan_id):
    return {p["id"]: p for p in shown["autonomy_context"]["plans"]["plans"]}[plan_id]


def step_plan(world, actor, *, key=None, step="act"):
    return world.create(key=key or actor, steps=[{"id": step, "actor": actor, "goal": "Carry this step out",
                                                  "completion": "The host records a receipt"}])


def enable_continuity(world):
    world.mind.configure_continuity(ContinuityConfig(command_id="enable-continuity", agent_version=world.version,
        expected_revision=world.mind.read()["revision"], evidence_ids=[world.source("owner-enables-continuity")],
        features={name: True for name in ("interpretation", "concerns", "expression", "rhythm")},
        reason="The synthetic owner enables continuity"))


def executes(world, plan, source, *, step="act", **said):
    """One appraisal that sets a step running and states a move about it."""
    def script(state, shown):
        return Appraisal(reason="A real owner message", next_move=stated(**said),
            action_decisions=[ActionDecision(plan_id=plan["id"], step_id=step, action="execute",
                expected_revision=shown_plan(shown, plan["id"])["revision"], evidence_ids=[source],
                reason="Everything this step needs is already here")])
    return world.appraise([source], script)


# --- two shared histories, two moves, and the grounds behind each --------------------------------

def test_two_shared_histories_state_different_moves_with_the_grounds_behind_them(tmp_path):
    first, second = World(tmp_path / "a"), World(tmp_path / "b")
    first_round(first)
    second_round(first)
    first.clock[0] += timedelta(hours=3)
    asked = first.owner("what-next", "What are you thinking about?")

    def speaks(state, shown):
        trait = state["traits"]["established"][0]
        return Appraisal(reason="A real owner message", next_move=NextMove(move="reply", grounds=[trait["id"], shown["new_evidence"][0]["id"]],
            alternative="Letting the evening pass without a word", reason="The evenings this rests on"))
    result, data, _ = first.appraise([asked], speaks)
    assert result["state"] == "complete" and "rejected_sections" not in data

    first_round(second, slug="night-walks", text="Kin would rather walk than sit still.")
    second_round(second, slug="night-walks")
    established = second.ledger().read()["traits"][0]
    second.clock[0] += timedelta(hours=2)
    correction = second.owner("correction", "That is not you at all; drop the walking idea.")
    second.appraise([correction], lambda state, shown: Appraisal(reason="The owner corrected it",
        trait_decisions=[TraitDecision(
            action="revoke", trait_id=established["id"], expected_revision=established["revision"],
            text=established["text"], basis="owner_correction", quote="drop the walking idea",
            evidence_ids=[correction], reason="The owner said to drop it")]))
    second.clock[0] += timedelta(hours=3)
    quiet = second.owner("what-next", "What are you thinking about?")

    def keeps_quiet(state, shown):
        return Appraisal(reason="A real owner message", next_move=NextMove(move="quiet",
            grounds=[state["corrections"][0]["trait_id"]], alternative="Bringing the walk up again",
            reason="What the owner ended is not a reason to speak"))
    settled, row, _ = second.appraise([quiet], keeps_quiet)
    assert settled["state"] == "complete" and "rejected_sections" not in row

    # A rests on a trait the ledger still carries, at the revision the host saw.
    spoke = recorded(first)[0]
    assert spoke["declared"] == spoke["derived"] == "reply"
    assert [g["kind"] for g in spoke["grounds"]] == ["trait", "evidence"]
    assert spoke["grounds"][0] == {"kind": "trait", "ref": first.ledger().read()["traits"][0]["id"],
                                  "revision": first.ledger().read()["traits"][0]["revision"], "status": "established"}
    assert spoke["grounds"][1]["kind"] == "evidence" and spoke["grounds"][1]["revision"]
    # B rests on what an owner correction ended, and says so as the correction it is.
    kept = recorded(second)[0]
    assert kept["declared"] == kept["derived"] == "quiet"
    assert kept["grounds"][0]["kind"] == "correction" and kept["grounds"][0]["basis"] == "owner_correction"
    assert kept["grounds"][0]["ref"] == established["id"] and kept["grounds"][0]["at"]
    # The agent's own words are kept as written, and the two histories did not state the same move.
    assert spoke["alternative"] == "Letting the evening pass without a word" and kept["reason"].startswith("What the owner ended")
    assert json.dumps(spoke["grounds"], sort_keys=True) != json.dumps(kept["grounds"], sort_keys=True)


# --- forged grounds ------------------------------------------------------------------------------

def test_a_ground_naming_an_unknown_trait_drops_the_move_and_nothing_else(world):
    first_round(world)
    world.clock[0] += timedelta(hours=3)
    asked = world.owner("what-next", "What are you thinking about?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 68}, wishes=[wish("Say what the lanterns were like")],
        next_move=stated(grounds=["trait_" + "0" * 32])))
    assert result["state"] == "complete" and refused(data, "next_move") == ["next-move-forged-grounds"]
    # Everything else of the same appraisal committed.
    view = world.mind.read()
    assert view["dimensions"]["mood"]["value"] == 68
    assert [d["content"] for d in view["desires"]] == ["Say what the lanterns were like"]
    assert recorded(world) == [] and world.followups(result) == []
    with world.mind.engine.db.connect() as conn:
        assert last_refusal(conn, world.mind.scope.key(), "next_move")["next_move"]["code"] == "next-move-forged-grounds"
    # Nothing was queued to ask again, and the set that decides that is what it was.
    assert ASK_AGAIN_SECTIONS == {"habits", "plan_changes", "concerns", "action_decisions"}
    assert not ASK_AGAIN_SECTIONS & set(AUDIT_SECTIONS)


def test_a_ground_naming_evidence_from_outside_this_evaluation_drops_the_move_alone(world):
    elsewhere = world.owner("not-in-this-batch", "Something else entirely")
    world.clock[0] += timedelta(hours=1)
    asked = world.owner("what-next", "What are you thinking about?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 57}, next_move=stated(grounds=[elsewhere])))
    assert refused(data, "next_move") == ["next-move-forged-grounds"]
    assert world.mind.read()["dimensions"]["mood"]["value"] == 57
    assert recorded(world) == [] and world.followups(result) == []


def test_a_concern_this_mind_still_carries_is_a_real_ground(world):
    enable_continuity(world)
    opened = world.owner("owner-mentions", "The clock in the hallway stopped again.")
    world.appraise([opened], lambda state, shown: Appraisal(reason="A real owner message",
        understanding=Understanding(meaning="The clock stopped", topic="clock", importance=60, confidence=0.9, basis="explicit"),
        concerns=[ConcernProposal(action="create", key="hallway-clock", kind="care", content="The hallway clock stopped",
            topic="clock", intensity=55, basis="explicit", confidence=0.9, reason="The owner said so",
            evidence_ids=[opened])]))
    world.clock[0] += timedelta(hours=2)
    asked = world.owner("what-next", "What are you thinking about?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(reason="A real owner message",
        next_move=stated(grounds=[state["concerns"][0]["id"]])))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert [g["kind"] for g in recorded(world)[0]["grounds"]] == ["concern"]


# --- a move that does not match what the appraisal committed --------------------------------------

def test_a_quiet_move_beside_an_outward_action_this_commit_started_is_dropped(world):
    plan = step_plan(world, "contact", key="tell")
    asked = world.owner("owner-asks", "Did you find anything?")
    result, data, _ = executes(world, plan, asked, move="quiet")
    assert result["state"] == "complete" and refused(data, "next_move") == ["next-move-inconsistent"]
    # The decision itself committed: only the account of it was refused.
    assert world.step(plan, "act")["state"] == "ready"
    assert recorded(world) == [] and world.followups(result) == []


def test_the_same_outward_action_with_a_move_that_admits_it_is_recorded(world):
    plan = step_plan(world, "contact", key="tell")
    asked = world.owner("owner-asks", "Did you find anything?")
    result, data, _ = executes(world, plan, asked, move="reply", step_ref="act")
    assert result["state"] == "complete" and "rejected_sections" not in data
    row = recorded(world)[0]
    assert row["declared"] == "reply" and row["derived"] == "invite"
    assert row["binding"]["step"] == {"id": "act", "plan_id": plan["id"], "actor": "contact", "committed": True}


def test_an_outward_action_the_host_held_is_not_an_outward_action(world):
    """The host reads what it wrote, not what was proposed. A decision the host held because the
    step moved under it started nothing, so the quiet move beside it is exactly true."""
    plan = step_plan(world, "contact", key="tell")
    asked = world.owner("owner-asks", "Did you find anything?")

    def script(state, shown):
        # Another writer settles the step between the view this evaluation was shown and its commit.
        world.decide(plan, "wait", step_id="act", command="someone-else-decides")
        return Appraisal(reason="A real owner message", next_move=stated("quiet"),
            action_decisions=[ActionDecision(plan_id=plan["id"], step_id="act", action="execute",
                expected_revision=shown_plan(shown, plan["id"])["revision"], evidence_ids=[asked],
                reason="Everything this step needs is already here")])
    result, data, _ = world.appraise([asked], script)
    assert result["state"] == "complete" and refused(data, "next_move") == []
    assert [held["code"] for held in result["result"]["held_decisions"]] == ["step-touched"]
    assert world.step(plan, "act")["state"] == "waiting"
    row = recorded(world)[0]
    assert row["declared"] == "quiet" and row["derived"] == "quiet" and row["committed"]["decisions"] == []


def test_a_move_whose_decisions_were_refused_waits_with_them_and_still_asks_nothing(world):
    """`next_move` rests on the decisions of its own batch, so a refused `action_decisions` holds
    it: not recorded, not refused on its own account, and still no second call."""
    plan = step_plan(world, "contact", key="tell")
    asked = world.owner("owner-asks", "Did you find anything?")

    def script(state, shown):
        return Appraisal(reason="A real owner message", values={"mood": 66}, next_move=stated("quiet"),
            action_decisions=[ActionDecision(plan_id=plan["id"], step_id="act", action="execute",
                expected_revision=shown_plan(shown, plan["id"])["revision"], evidence_ids=[asked],
                artifact_hashes=["0" * 64], reason="A file no completed step ever produced")])
    result, data, _ = world.appraise([asked], script)
    assert result["state"] == "complete" and refused(data, "action_decisions") == ["conflict"]
    assert [(h["section"], h["upstream"]) for h in data["held_sections"]] == [("next_move", "action_decisions")]
    assert refused(data, "next_move") == [] and recorded(world) == []
    assert world.mind.read()["dimensions"]["mood"]["value"] == 66
    # One follow-up, and it is the decisions' own: nothing of the move is restated or asked again.
    review = world.rows("SELECT data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", result["id"])
    restated = json.loads(review[0]["data"])["section_review"]
    assert [r["section"] for r in restated["rejected_sections"]] == ["action_decisions"]
    assert restated["held_sections"] == [] and "next_move" not in json.dumps(restated)


def test_a_want_the_host_still_gates_stands_beside_a_quiet_move_and_is_recorded_as_the_tension_it_is(world):
    """What is inconsistent is narrow on purpose. A contact wish is a want, still gated by
    readiness, by the threshold and by quiet hours, and a step this commit set looking something up
    never addresses the owner: neither contradicts staying quiet now. The host reads the fuller
    word anyway, so a declared word and a derived one can be held against each other later."""
    plan = step_plan(world, "explore")
    asked = world.owner("owner-asks", "Anything on your mind?")

    def script(state, shown):
        return Appraisal(reason="A real owner message", wishes=[wish("Ask which photo to keep")],
            next_move=stated("rest"),
            action_decisions=[ActionDecision(plan_id=plan["id"], step_id="act", action="execute",
                expected_revision=shown_plan(shown, plan["id"])["revision"], evidence_ids=[asked],
                reason="Everything this step needs is already here")])
    result, data, _ = world.appraise([asked], script)
    assert result["state"] == "complete" and "rejected_sections" not in data
    row = recorded(world)[0]
    assert row["declared"] == "rest" and row["derived"] == "invite"


def test_a_binding_that_names_nothing_this_appraisal_touched_is_dropped(world):
    asked = world.owner("owner-asks", "What are you up to?")
    _, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 62}, next_move=stated(wish_ref="desire_" + "0" * 32)))
    assert refused(data, "next_move") == ["next-move-inconsistent"]
    assert world.mind.read()["dimensions"]["mood"]["value"] == 62 and recorded(world) == []

    world.clock[0] += timedelta(hours=1)
    again = world.owner("owner-asks-again", "And now?")
    _, second, _ = world.appraise([again], lambda state, shown: Appraisal(
        reason="A real owner message", next_move=stated(step_ref="a-step-of-no-plan")))
    assert refused(second, "next_move") == ["next-move-inconsistent"]
    assert recorded(world) == []


def test_a_binding_may_name_a_wish_this_commit_changed_or_one_it_left_standing(world):
    standing = world.mind.manage_desire(DesireChange(command_id="an-older-wish", agent_version=world.version,
        expected_revision=world.mind.read()["revision"], evidence_ids=[world.initial], action="create",
        content="Bring back what the hallway clock needs", topic="clock", kind="explore", strength=40,
        expires_at=(world.clock[0] + timedelta(days=2)).isoformat(), completion="A sourced note",
        reason="An older idea"))["desire_id"]
    asked = world.owner("owner-asks", "Still looking into the clock?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", next_move=stated(wish_ref=standing)))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert recorded(world)[0]["binding"]["wish"] == {"id": standing, "kind": "explore", "status": "wanted", "committed": False}

    world.clock[0] += timedelta(hours=1)
    again = world.owner("owner-asks-again", "No rush on the clock.")
    _, second, _ = world.appraise([again], lambda state, shown: Appraisal(reason="A real owner message",
        wish_updates=[WishUpdate(desire_id=standing, action="wait", wait_condition="new_evidence", reason="No rush")],
        next_move=stated("quiet", wish_ref=standing)))
    assert "rejected_sections" not in second
    # Newest first, so the move this second round stated is the one at the front.
    assert recorded(world)[0]["binding"]["wish"] == {"id": standing, "kind": "explore", "status": "waiting", "committed": True}


# --- what the host derives from what this same appraisal committed --------------------------------

@pytest.mark.parametrize("actor,derived", [("explore", "explore"), ("create", "create"), ("contact", "invite")])
def test_a_step_this_commit_set_running_gives_the_move_its_longer_name(tmp_path, actor, derived):
    world = World(tmp_path / actor)
    plan = step_plan(world, actor)
    asked = world.owner("owner-asks", "How is it going?")
    result, data, _ = executes(world, plan, asked)
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert world.step(plan, "act")["state"] == "ready"
    assert recorded(world)[0]["derived"] == derived


@pytest.mark.parametrize("kind,derived", [("explore", "explore"), ("create", "create"), ("contact", "invite")])
def test_a_wish_this_commit_left_wanting_gives_the_move_its_longer_name(tmp_path, kind, derived):
    world = World(tmp_path / kind)
    asked = world.owner("owner-asks", "How is it going?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", wishes=[wish("Something worth doing", kind=kind)], next_move=stated()))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert recorded(world)[0]["derived"] == derived


@pytest.mark.parametrize("kind,derived", [("help", "ask"), ("request", "ask"), ("invitation", "invite")])
def test_a_request_this_commit_made_of_the_owner_gives_the_move_its_longer_name(tmp_path, kind, derived):
    world = World(tmp_path / kind)
    enable_continuity(world)
    asked = world.owner("owner-asks", "How is it going?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", next_move=stated(),
        concerns=[ConcernProposal(action="create", key="one-photo", kind="shared_plan",
            content="Ask for one photo from today", topic="photos", intensity=45, basis="internal_thought",
            confidence=0.8, reason="A thought of its own", evidence_ids=[asked],
            owner_request=OwnerRequest(kind=kind, action="Pick one photo from today",
                                       reason="It would say more than a description",
                                       completion="One photo arrives", status="proposed"))]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert recorded(world)[0]["derived"] == derived


def test_a_commit_that_started_nothing_keeps_the_word_the_model_declared(world):
    asked = world.owner("owner-asks", "How is it going?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 64}, next_move=stated("rest")))
    assert result["state"] == "complete" and "rejected_sections" not in data
    row = recorded(world)[0]
    assert row["declared"] == "rest" and row["derived"] == "rest" and row["binding"] == {}


# --- the lanes, the new interaction and the switch ------------------------------------------------

def test_a_move_formed_before_a_new_owner_message_waits_for_the_next_round(system):
    mind, memory, source, clock = system
    sid = source("earlier", "Please tell me later")
    memory.ingest({"id": "earlier", "kind": "owner-message", "at": mind.clock(), "source_id": sid})
    jobs = Appraisals(mind)
    job = jobs.enqueue([sid], "fixture-v1")["id"]

    class Provider:
        def appraise(self, context):
            clock[0] += timedelta(minutes=1)
            later = source("latest", "We already discussed that")
            memory.ingest({"id": "latest", "kind": "owner-message", "at": mind.clock(), "source_id": later})
            return Appraisal(reason="Late interpretation", next_move=stated()), {"model": "deepseek-flash"}

    result = jobs.run_one(Provider(), job_id=job)
    assert result["state"] == "complete" and result["result"]["new_interaction_pending"]
    # Held, not refused: nothing to record and nothing to record about it.
    assert "rejected_sections" not in result["result"] and recent(mind)["moves"] == []


def test_the_move_is_never_offered_on_a_history_lane_a_follow_up_or_maintenance():
    enabled = set(AUDIT_SECTIONS)
    assert "next_move" in offered_sections(None, enabled)
    for lane in SECTIONS_WITHHELD:
        assert offered_sections(lane, enabled) == ()
        # A proposal that reaches one of these lanes anyway is blanked before anything reads it.
        assert blank_sections(Appraisal(reason="A real owner message", next_move=stated()),
                              offered_sections(lane, enabled)).next_move is None


def test_the_request_that_offers_the_move_is_pinned(monkeypatch):
    api = Recorded(monkeypatch, MOVE_SECTIONS)
    body, fingerprint = api.request(CONTEXT)
    assert fingerprint == MOVE_REQUEST
    assert "next_move" in body["tools"][0]["input_schema"]["properties"] and "next_move" in body["system"]


def test_with_the_switch_off_a_move_is_blanked_and_nothing_is_recorded(world, monkeypatch):
    monkeypatch.setitem(memory_module.DEFAULTS, "next_move_audit", False)
    world.memory.configure({"next_move_audit": False})
    asked = world.owner("owner-asks", "How is it going?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", values={"mood": 59}, next_move=stated()))
    assert result["state"] == "complete" and "rejected_sections" not in data
    assert "next_move" not in data["proposed_result"]
    assert world.mind.read()["dimensions"]["mood"]["value"] == 59
    assert recent(world.mind) == {"moves": [], "enabled": False, "last_refusal": {}}
    # The table is created on demand, so with the switch off the store does not even carry it.
    assert world.rows("SELECT name FROM sqlite_master WHERE name='mind_next_moves'") == []


# --- the audit holds nothing back and decides nothing ---------------------------------------------

def readiness(world):
    """Exactly what decides whether a wish may act, and which one is chosen."""
    with world.mind.engine.db.connect() as conn:
        state = world.mind._load(conn)
        ready = [(d["id"], world.mind._desire_ready(conn, d, world.mind.clock(), state=state))
                 for d in sorted(state["desires"].values(), key=lambda d: d["id"])]
    return ready, world.mind.contact_candidate()


def test_a_recorded_move_changes_no_wish_order_and_no_readiness(world):
    asked = world.owner("owner-asks", "Anything you want to tell me?")
    result, data, _ = world.appraise([asked], lambda state, shown: Appraisal(
        reason="A real owner message", next_move=stated(),
        wishes=[wish("Tell about the lanterns"), wish("Ask which photo to keep")]))
    assert result["state"] == "complete" and "rejected_sections" not in data
    with_move = readiness(world)
    assert len(recorded(world)) == 1 and len(with_move[0]) == 2 and with_move[1]["eligible"]
    # The one thing that changed, taken back out: the row itself. Only a test reaches into this
    # table; the host never rewrites it.
    with world.mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_next_moves")
    assert recorded(world) == []
    assert json.dumps(readiness(world), sort_keys=True, default=str) == json.dumps(with_move, sort_keys=True, default=str)


def test_nothing_that_reads_wishes_or_schedules_action_knows_this_table_exists():
    import pathlib

    import kin_mind
    root = pathlib.Path(kin_mind.__file__).parent
    readers = ("state.py", "actions.py", "plans.py", "continuity.py", "exploration.py",
               "exploration_decisions.py", "context.py", "expression.py", "manifest.py", "cli.py")
    for name in readers:
        text = (root / name).read_text()
        assert "next_move" not in text and "mind_next_moves" not in text, name
    # Append-only: the module that owns the table only ever inserts into it.
    owned = (root / "next_move.py").read_text()
    assert "UPDATE mind_next_moves" not in owned and "DELETE FROM mind_next_moves" not in owned


def test_the_operator_route_reads_the_moves_and_writes_nothing(world):
    from kin_mind.host import dispatch

    asked = world.owner("owner-asks", "How is it going?")
    world.appraise([asked], lambda state, shown: Appraisal(reason="A real owner message", next_move=stated()))
    config = {"root": str(world.mind.engine.db.root), "scope": world.mind.scope.model_dump(),
              "agent_version": world.version, "session_id": "synthetic-session"}
    read = dispatch(config, "next-moves", {})
    assert read["enabled"] is True and len(read["moves"]) == 1
    row = read["moves"][0]
    assert row["declared"] == "reply" and row["derived"] == "reply" and row["grounds"] == []
    assert row["committed"] == {"decisions": [], "wishes": [], "owner_requests": []}
    # The agent's own two sentences, shown to the operator as written.
    assert row["alternative"] == "Waiting one more evening" and row["reason"] == "What this choice rests on"
    assert row["receipt"]["provider"] == "deepseek" and row["event_id"] and row["job_id"]
    assert dispatch(config, "next-moves", {})["moves"] == read["moves"]
