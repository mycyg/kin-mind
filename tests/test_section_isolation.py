"""Dependency-aware section isolation (A1), unknown fields, the bounded advice repair and the woken plan's view.

Synthetic replays only: an injected clock, scripted providers or httpx.MockTransport. No model or network call.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_plan_review_loop import RECEIPT, Env, Reviewer, later

from eventmem.core.db import Conflict, Missing
from kin_mind.appraisal import (
    FOLLOW_UP,
    ISOLATED_SECTIONS,
    SECTION_ISOLATION,
    SECTION_UPSTREAM,
    UPSTREAM_SECTIONS,
    Appraisal,
    Appraisals,
    DeepSeek,
    Wish,
    WishUpdate,
    refusal,
    static_message,
)
from kin_mind.autonomy_models import ActionDecision, PlanChange, PlanStep
from kin_mind.continuity import ConcernProposal, ContinuityConfig, Understanding
from kin_mind.habits import HabitProposal
from kin_mind.session_advice import SessionAdvice, SessionFinding
from kin_mind.state import DesireChange, Motivation

SESSION = {"id": "snapshot-1", "binding": {"generation": 1}, "lastCompaction": {"id": "compact-1", "completedAt": 100},
           "evidence": [{"id": "observed-1", "at": 200, "text": "PRIVATE OBSERVATION TEXT"}],
           "recent": [{"id": "turn-1", "role": "user", "text": "PRIVATE OWNER WORDS about the clock"}]}
UNKNOWN_ADVICE = {"section": "session_advice", "code": "unknown-observation", "message": "Session advice cites unknown observations"}
STALE_HABITS = {"section": "habits", "code": "conflict", "message": "Conversation preferences changed"}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run(env, script, *, job_id=None, session=None, provider=None):
    """One appraisal as the host runs it; returns (status, provider)."""
    jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version}, session_context=session)
    provider = provider or Reviewer(script)
    env.actions.drain(jobs)
    result = jobs.run_one(provider, job_id=job_id)
    env.actions.drain(jobs)
    return result, provider


def children(env, job_id):
    return [{"id": r["id"], "state": r["state"], **json.loads(r["data"])} for r in
            env.rows("SELECT id,state,data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=? ORDER BY id", job_id)]


def enable_continuity(env):
    env.mind.configure_continuity(ContinuityConfig(command_id="enable-continuity", agent_version=env.version, expected_revision=env.mind.read()["revision"],
        evidence_ids=[env.source("owner-enables-continuity")], features={k: True for k in ("interpretation", "concerns", "expression", "rhythm")},
        reason="The synthetic owner enables continuity"))


def saved_state(env):
    """The row _mutate() wrote, not a projection of it."""
    return json.loads(env.rows("SELECT data FROM mind_state")[0]["data"])


def wish(content, kind="contact", **extra):
    return Wish(content=content, topic="synthetic", kind=kind, strength=70, ttl_hours=24, completion="Done", **extra)


def test_every_appraisal_field_is_registered_with_its_upstream_sections():
    assert set(SECTION_UPSTREAM) == set(Appraisal.model_fields)
    assert UPSTREAM_SECTIONS == {"habits", "plan_changes", "concerns"} and UPSTREAM_SECTIONS <= set(ISOLATED_SECTIONS)
    # Freely isolatable: nothing rests on them, and they rest on nothing.
    for free in ("session_advice", "procedure_candidates"):
        assert free in ISOLATED_SECTIONS and free not in UPSTREAM_SECTIONS and SECTION_UPSTREAM[free] == {}
    # What the owner's preferences are upstream of.
    assert {name for name, rests in SECTION_UPSTREAM.items() if "habits" in rests} == {"plan_changes", "action_decisions", "wishes", "wish_updates", "values", "motivations"}
    assert SECTION_UPSTREAM["plan_changes"]["habits"] is None


def test_invalid_session_advice_no_longer_blocks_mood_understanding_or_plan_decisions(env):
    enable_continuity(env)
    plan = env.create()
    job = env.enqueue("owner-chat")
    def script(shown, context):
        return Appraisal(reason="A real owner message", values={"mood": 72},
            understanding=Understanding(meaning="The owner asked about the clock", topic="clock", importance=60, confidence=0.9, basis="explicit"),
            action_decisions=[env.decision(shown[plan["id"]], "wait", strength=35, next_review_at=later(env, hours=3))],
            session_advice=SessionAdvice(action="recall", reason="Context looks thin", evidenceIds=["invented-observation"]))
    result, provider = run(env, script, job_id=job, session=SESSION)
    assert result["state"] == "complete" and provider.calls == 1
    view = env.mind.read()
    assert view["dimensions"]["mood"]["value"] == 72 and view["appraisal_summary"]["understanding"]["topic"] == "clock"
    step = env.step(plan)
    assert step["state"] == "waiting" and step["decision"]["action"] == "wait" and step["strength"] == 35
    assert view["session_advice"] is None
    data = env.job(job)
    assert data["rejected_sections"] == [UNKNOWN_ADVICE] == result["result"]["rejected_sections"]
    # A freely isolatable section is only dropped: nothing is held and nothing is asked again.
    assert "held_sections" not in data and "error" not in data and children(env, job) == []
    assert "PRIVATE" not in json.dumps(data["rejected_sections"])


def habits_case(env):
    """An owner message, a habits proposal on a stale revision, and everything the spec lists as resting on it."""
    research = env.create(key="research", steps=[{"id": "look", "actor": "explore", "goal": "Read about clock escapements", "completion": "A sourced note"}])
    clock = env.create(key="clock")
    wanted = env.mind.manage_desire(DesireChange(command_id="older-explore-wish", agent_version=env.version,
        expected_revision=env.mind.read()["revision"], evidence_ids=[env.initial], action="create", content="Look into sundials", topic="sundials",
        kind="explore", strength=40, expires_at=later(env, days=2), completion="A note", reason="An older idea"))
    env.mind.manage_desire(DesireChange(command_id="older-explore-wish-waits", agent_version=env.version,
        expected_revision=env.mind.read()["revision"], evidence_ids=[env.initial], action="wait", desire_id=wanted["desire_id"], reason="Later"))
    owner = env.source("owner-asks-to-pause-exploring")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    def proposal(shown, context, *, revision):
        return Appraisal(reason="The owner wants fewer explorations", values={"mood": 70, "curiosity": 88},
            motivations={"curiosity": Motivation(target=90, half_life_minutes=60, reason="A new question"),
                         "initiative": Motivation(target=55, half_life_minutes=60, reason="Wants to talk")},
            habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked to pause", expected_revision=revision),
            wishes=[wish("Find out how escapements work", kind="explore"), wish("Tell the owner about the clock")],
            wish_updates=[WishUpdate(desire_id=wanted["desire_id"], action="resume", reason="Curious again")],
            plan_changes=[PlanChange(action="create", key="sundial", goal="Draw a sundial", motivation="A whim", reason="Own idea", evidence_ids=[owner],
                steps=[PlanStep(id="draw", actor="create", goal="Draw it", completion="A saved drawing")])],
            action_decisions=[env.decision(shown[research["id"]], "execute", step_id="look"), env.decision(shown[clock["id"]], "execute"),
                ActionDecision(plan_id="sundial", step_id="draw", expected_revision=1, action="wait", reason="Tomorrow", evidence_ids=[owner])])
    return SimpleNamespace(research=research, clock=clock, older=wanted["desire_id"], owner=owner, job=job, proposal=proposal)


def test_refused_habits_hold_what_rests_on_them_and_one_follow_up_restates_it(env):
    case = habits_case(env)
    research, clock, older, owner, job, proposal = case.research, case.clock, case.older, case.owner, case.job, case.proposal
    before = env.mind.read()["dimensions"]["curiosity"]
    result, provider = run(env, lambda shown, context: proposal(shown, context, revision=7), job_id=job)
    assert result["state"] == "complete" and provider.calls == 1
    view, data = env.mind.read(), env.job(job)
    # Independent sections committed.
    assert view["dimensions"]["mood"]["value"] == 70 and view["dimensions"]["initiative"]["motivation"]["target"] == 55
    assert [d["content"] for d in view["desires"] if d["kind"] == "contact"] == ["Tell the owner about the clock"]
    assert env.step(clock)["state"] == "ready"
    # Nothing that rests on the refused preference was applied.
    assert env.memory.habits.read()["revision"] == 0 and env.memory.habits.read()["preferences"]["exploration_paused"] is False
    assert view["dimensions"]["curiosity"] == before and before["motivation"] is None
    assert [d["content"] for d in view["desires"] if d["kind"] == "explore"] == ["Look into sundials"]
    assert next(d for d in view["desires"] if d["id"] == older)["status"] == "waiting"
    assert env.step(research, "look")["state"] == "pending" and all(p["key"] != "sundial" for p in env.plans.read()["plans"])
    assert env.plans.claim("explore", "worker")["state"] == "waiting"
    assert data["rejected_sections"] == [STALE_HABITS]
    held = {(h["section"], h.get("part")): h for h in data["held_sections"]}
    assert set(held) == {("plan_changes", None), ("action_decisions", "decisions on a plan this proposal changes"),
        ("action_decisions", "execute on an explore or contact step"), ("values", "curiosity"), ("motivations", "curiosity"),
        ("wishes", "explore wishes"), ("wish_updates", "resume of an explore wish")}
    assert all(h["rejected"] == "habits" and h["code"] == "conflict" and h["message"] == "Conversation preferences changed" and h["items"] == 1 for h in held.values())
    # Held through the held plan change, not directly: the cause is still the refused preference.
    assert held[("action_decisions", "decisions on a plan this proposal changes")]["upstream"] == "plan_changes"
    assert all(h["upstream"] == "habits" for key, h in held.items() if key != ("action_decisions", "decisions on a plan this proposal changes"))
    # Exactly one durable follow-up, queued with the commit itself.
    [follow] = children(env, job)
    assert follow["state"] == "pending" and follow["stimulus"] == FOLLOW_UP and follow["id"] == result["result"]["follow_up_id"]
    assert follow["section_review"] == {"rejected_sections": data["rejected_sections"], "held_sections": data["held_sections"]}
    # It already carries evidence of its own, so whichever host version picks it up can commit it: on the
    # parent's evidence alone the state refuses a second appraisal of what was already appraised.
    assert follow["evidence_ids"] == [follow["review_source_id"], owner]

    seen = {}
    def restated(shown, context):
        seen.update(stimulus=context["stimulus"], notes=[json.loads(e["text"]) for e in context["new_evidence"] if e["metadata"].get("host_event") == "internal-" + FOLLOW_UP],
                    evidence=[e["id"] for e in context["new_evidence"]])
        return proposal(shown, context, revision=0).model_copy(update={"values": {"curiosity": 88}, "wishes": [wish("Find out how escapements work", kind="explore")]})
    result, provider = run(env, restated, job_id=follow["id"])
    assert result["state"] == "complete" and provider.calls == 1
    # The model was told why, as evidence of this appraisal, together with the owner's message it must cite.
    assert seen["stimulus"] == FOLLOW_UP and owner in seen["evidence"] and len(seen["notes"]) == 1
    assert seen["notes"][0]["rejected_sections"] == [STALE_HABITS] and seen["notes"][0]["appraisal_id"] == job
    view = env.mind.read()
    assert env.memory.habits.read()["preferences"]["exploration_paused"] is True
    assert view["dimensions"]["curiosity"]["value"] == 88 and view["dimensions"]["curiosity"]["motivation"]["target"] == 90
    assert sorted(d["content"] for d in view["desires"] if d["kind"] == "explore") == ["Find out how escapements work", "Look into sundials"]
    assert next(d for d in view["desires"] if d["id"] == older)["status"] == "wanted"
    assert env.step(research, "look")["state"] == "ready" and any(p["key"] == "sundial" for p in env.plans.read()["plans"])
    after = env.job(follow["id"])
    assert "rejected_sections" not in after and "held_sections" not in after and children(env, follow["id"]) == []
    assert len(env.rows("SELECT id FROM mind_appraisals WHERE json_extract(data,'$.stimulus')=?", FOLLOW_UP)) == 1


def test_follow_up_that_leaves_the_preference_out_frees_nothing_and_asks_no_third_time(env):
    case = habits_case(env)
    research, job = case.research, case.job
    run(env, lambda shown, context: case.proposal(shown, context, revision=7), job_id=job)
    [follow] = children(env, job)
    def without_habits(shown, context):
        return Appraisal(reason="Only the wish again", wishes=[wish("Find out how escapements work", kind="explore")],
            action_decisions=[env.decision(shown[research["id"]], "execute", step_id="look")])
    result, _ = run(env, without_habits, job_id=follow["id"])
    assert result["state"] == "complete"
    data = env.job(follow["id"])
    assert data["rejected_sections"] == [{"section": "habits", "code": "not-restated", "message": "A refused owner preference was not restated"}]
    assert {(h["section"], h["code"]) for h in data["held_sections"]} == {("wishes", "not-restated"), ("action_decisions", "not-restated")}
    assert [d["content"] for d in env.mind.read()["desires"] if d["kind"] == "explore"] == ["Look into sundials"]
    assert env.step(research, "look")["state"] == "pending"
    # Bounded: a follow-up never queues another one.
    assert children(env, follow["id"]) == [] and len(env.rows("SELECT id FROM mind_appraisals WHERE json_extract(data,'$.stimulus')=?", FOLLOW_UP)) == 1
    assert run(env, without_habits)[0]["state"] == "idle"


def test_refused_preference_is_asked_again_even_when_nothing_of_the_proposal_rested_on_it(env):
    """Beyond the letter of the spec: without this the owner's requirement would be lost with no second chance."""
    owner = env.source("owner-asks-to-pause-exploring")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    result, _ = run(env, lambda shown, context: Appraisal(reason="The owner asked to pause", values={"mood": 60},
        habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=7)), job_id=job)
    assert result["state"] == "complete" and env.mind.read()["dimensions"]["mood"]["value"] == 60
    data = env.job(job)
    assert data["rejected_sections"] == [STALE_HABITS] and "held_sections" not in data
    [follow] = children(env, job)
    assert follow["stimulus"] == FOLLOW_UP and follow["section_review"] == {"rejected_sections": [STALE_HABITS], "held_sections": []}
    result, _ = run(env, lambda shown, context: Appraisal(reason="Restated",
        habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=0)), job_id=follow["id"])
    assert result["state"] == "complete" and env.memory.habits.read()["preferences"]["exploration_paused"] is True


def test_follow_up_makes_its_own_evidence_when_the_host_died_right_after_the_commit(env):
    owner = env.source("owner-asks-to-pause-exploring")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    habit = lambda revision: HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=revision)
    run(env, lambda shown, context: Appraisal(reason="The owner asked to pause", values={"mood": 60}, habits=habit(7)), job_id=job)
    [follow] = children(env, job)
    with env.mind.engine.db.connect(write=True) as conn:
        # As the commit itself left it: the parent's evidence only.
        bare = {k: v for k, v in follow.items() if k not in {"id", "state", "review_source_id"}} | {"evidence_ids": [owner]}
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (json.dumps(bare), follow["id"]))
    result, _ = run(env, lambda shown, context: Appraisal(reason="Restated", habits=habit(0)), job_id=follow["id"])
    assert result["state"] == "complete" and env.memory.habits.read()["preferences"]["exploration_paused"] is True
    assert env.job(follow["id"])["evidence_ids"] == [follow["review_source_id"], owner]


def test_refused_procedure_candidate_is_dropped_alone(env):
    from kin_mind.autonomy_models import ProcedureCandidate
    env.memory.configure({"procedure_learning": True})
    owner = env.source("owner-chat")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    candidate = ProcedureCandidate(key="draw-clock", title="Draw a clock", applicable_when="Asked for a clock", steps=["Draw"], success_criteria="It shows the time",
        evidence_ids=[owner], result_ids=["a-result-that-never-happened"], reason="It worked once")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A method idea", values={"mood": 63}, procedure_candidates=[candidate]), job_id=job)
    assert result["state"] == "complete" and env.mind.read()["dimensions"]["mood"]["value"] == 63
    assert env.job(job)["rejected_sections"] == [{"section": "procedure_candidates", "code": "conflict", "message": "Method learning requires an actual verified result"}]
    assert env.rows("SELECT * FROM mind_procedures") == [] and children(env, job) == []


def test_follow_up_runs_on_the_action_lane_beside_the_enrichment_job(tmp_path):
    """The production configuration: semantic memory and operational lanes."""
    from test_memory_continuity import system
    mind, memory, source, _ = system.__wrapped__(tmp_path)
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    owner = source("owner-asks-to-pause-exploring")
    job = jobs.enqueue([owner], "fixture-v1")["id"]
    class Scripted:
        def __init__(self, revision):
            self.revision, self.contexts = revision, []
        def appraise(self, context):
            self.contexts.append(context)
            return Appraisal(reason="The owner asked to pause", values={"mood": 70} if self.revision else {},
                habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=self.revision),
                wishes=[wish("Find out how escapements work", kind="explore")]), dict(RECEIPT)
    assert jobs.run_one(Scripted(7), lane="action")["state"] == "complete"
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT id,state,data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", (job,)).fetchall()
    made = {json.loads(r["data"])["stimulus"]: r for r in rows}
    assert set(made) == {"memory-enrichment", FOLLOW_UP} and all(r["state"] == "pending" for r in rows)
    assert mind.read()["dimensions"]["mood"]["value"] == 70 and mind.read()["desires"] == [] and memory.habits.read()["revision"] == 0
    restating = Scripted(0)
    result = jobs.run_one(restating, lane="action")
    assert result["id"] == made[FOLLOW_UP]["id"] and result["state"] == "complete", result
    assert restating.contexts[0]["stimulus"] == FOLLOW_UP and restating.contexts[0]["operational_only"] is True
    assert memory.habits.read()["preferences"]["exploration_paused"] is True
    assert [d["content"] for d in mind.read()["desires"]] == ["Find out how escapements work"]
    # The enrichment lane is untouched by it.
    assert jobs.status(made["memory-enrichment"]["id"])["state"] == "pending"


def test_follow_up_takes_in_no_pending_event_and_never_applies_the_event_itself_again(tmp_path):
    """Semantic memory without lanes: an ordinary appraisal takes in pending events and moves the source cursor."""
    from test_memory_continuity import system
    mind, memory, _, clock = system.__wrapped__(tmp_path)
    jobs = Appraisals(mind)
    first = memory.ingest({"id": "owner-1", "kind": "owner-message", "at": mind.clock(), "text": "Please pause exploring for now"})
    jobs.enqueue([first["source_id"]], "fixture-v1")
    class Scripted:
        def __init__(self, **fields):
            self.fields, self.contexts = fields, []
        def appraise(self, context):
            self.contexts.append(context)
            return Appraisal(reason="The owner asked to pause", wishes=[wish("Find out how escapements work", kind="explore")], **self.fields), dict(RECEIPT)
    pause = lambda revision: HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[first["source_id"]], reason="The owner asked", expected_revision=revision)
    assert jobs.run_one(Scripted(values={"mood": 70}, habits=pause(7)))["state"] == "complete"
    cursor = memory.semantic_context()["cursor"]
    # The owner writes again before the follow-up runs.
    clock[0] += timedelta(minutes=1)
    second = memory.ingest({"id": "owner-2", "kind": "owner-message", "at": mind.clock(), "text": "And tell me about your day"})
    restating = Scripted(values={"mood": 5, "curiosity": 80}, habits=pause(0),
        understanding=Understanding(meaning="Scored twice", topic="pause", importance=50, confidence=0.9, basis="explicit"))
    result = jobs.run_one(restating)
    assert result["state"] == "complete" and restating.contexts[0]["stimulus"] == FOLLOW_UP, result
    # The second message is neither evidence of the follow-up nor marked as processed by it.
    assert second["source_id"] not in [e["id"] for e in restating.contexts[0]["new_evidence"]]
    assert restating.contexts[0]["memory_context"]["pending_events"] == []
    after = memory.semantic_context()
    assert after["cursor"] == cursor and [e["id"] for e in after["pending_events"]] == ["owner-2"]
    # Only what a commit can refuse or hold is restated: the mood and the understanding belong to the event the
    # parent already committed, and are not applied a second time.
    view = mind.read()
    assert view["dimensions"]["mood"]["value"] == 70 and view["dimensions"]["curiosity"]["value"] == 80
    assert result["result"]["proposal"]["values"] == {"curiosity": 80} and result["result"]["proposal"]["understanding"] is None
    assert memory.habits.read()["preferences"]["exploration_paused"] is True
    assert [d["content"] for d in view["desires"]] == ["Find out how escapements work"]


def test_section_failing_after_it_changed_state_leaves_no_half_applied_wish(env, monkeypatch):
    job = env.enqueue("owner-chat")
    sizes, original = [], env.mind._apply_desire
    def spy(conn, state, request, event_id):
        outcome = original(conn, state, request, event_id)
        sizes.append(len(state["desires"]))
        return outcome
    monkeypatch.setattr(env.mind, "_apply_desire", spy)
    # The second wish links a result that has no sharing decision: refused only after the first was applied.
    result, _ = run(env, lambda shown, context: Appraisal(reason="Two wishes", values={"mood": 64},
        wishes=[wish("Make the owner a paper clock", kind="create"), wish("Share the finding", exploration_id="explore_without_a_decision")]), job_id=job)
    assert result["state"] == "complete"
    # The first wish really was in the in-memory state when the second one failed.
    assert sizes == [1]
    assert env.job(job)["rejected_sections"] == [{"section": "wishes", "code": "conflict", "message": "This result has no current decision to communicate"}]
    state = saved_state(env)
    assert state["desires"] == {} and env.mind.read()["desires"] == [] and state["dimensions"]["mood"]["score"] == 64
    snapshot = json.loads(env.rows("SELECT data FROM mind_events ORDER BY revision DESC LIMIT 1")[0]["data"])["snapshot"]
    assert snapshot["desires"] == {} and snapshot["revision"] == state["revision"]
    # Nothing rests on wishes, so nothing is asked again.
    assert children(env, job) == []


def test_refused_section_undoes_its_sql_and_its_state_together(env):
    enable_continuity(env)
    older = env.mind.manage_desire(DesireChange(command_id="older-wish", agent_version=env.version, expected_revision=env.mind.read()["revision"],
        evidence_ids=[env.initial], action="create", content="Send the owner the drawing", topic="drawing", kind="contact", strength=50,
        expires_at=later(env, days=2), completion="Accepted", reason="An older idea"))["desire_id"]
    job = env.enqueue("owner-chat")
    concern = lambda **values: ConcernProposal(reason="A thought", **values)
    result, _ = run(env, lambda shown, context: Appraisal(reason="Two concerns", values={"mood": 61},
        concerns=[concern(action="create", key="interview", kind="care", content="An interview is coming", topic="interview", intensity=70, basis="inferred", confidence=0.9),
                  concern(action="update", concern_id="concern_that_does_not_exist", intensity=30)],
        wishes=[wish("Ask how it went", concern_ids=["interview"]), wish("Make a paper clock", kind="create")],
        wish_updates=[WishUpdate(desire_id=older, action="link", concern_ids=["interview"], reason="Belongs to the interview"),
                      WishUpdate(desire_id=older, action="wait", reason="Not now", wait_condition="owner_reply")]), job_id=job)
    assert result["state"] == "complete"
    data, state = env.job(job), saved_state(env)
    assert data["rejected_sections"] == [{"section": "concerns", "code": "missing-reference", "message": "Concern belongs to another scope or is missing"}]
    # The first concern had written its evidence row and its state entry before the second one failed.
    assert env.rows("SELECT * FROM mind_concern_evidence") == [] and state.get("concerns", {}) == {}
    # What cites a concern is held, what does not is applied, and the refused upstream section is asked again.
    assert [(h["section"], h["part"], h["items"]) for h in data["held_sections"]] == [
        ("wishes", "wishes citing concern_ids", 1), ("wish_updates", "updates citing concern_ids", 1)]
    assert sorted(d["content"] for d in state["desires"].values()) == ["Make a paper clock", "Send the owner the drawing"]
    assert state["desires"][older]["status"] == "waiting" and not state["desires"][older].get("concern_ids")
    assert state["dimensions"]["mood"]["score"] == 61 and [c["stimulus"] for c in children(env, job)] == [FOLLOW_UP]


def test_refused_plan_changes_leave_no_plan_row_and_hold_only_decisions_on_those_plans(env):
    plan = env.create()
    job = env.enqueue("owner-chat")
    def script(shown, context):
        good = PlanChange(action="create", key="poem", goal="Write a poem", motivation="A whim", reason="Own idea", evidence_ids=[env.initial],
            steps=[PlanStep(id="write", actor="create", goal="Write it", completion="A saved text")])
        bad = PlanChange(action="create", key="window", goal="Send it", motivation="A whim", reason="Own idea", evidence_ids=[env.initial],
            steps=[PlanStep(id="send", actor="contact", goal="Send it", completion="Accepted", not_before=later(env, days=2), not_after=later(env, days=1))])
        return Appraisal(reason="Arrange", plan_changes=[good, bad], action_decisions=[
            ActionDecision(plan_id="poem", step_id="write", expected_revision=1, action="wait", reason="Tonight", evidence_ids=[env.initial]),
            env.decision(shown[plan["id"]], "execute")])
    result, _ = run(env, script, job_id=job)
    assert result["state"] == "complete"
    data = env.job(job)
    assert data["rejected_sections"] == [{"section": "plan_changes", "code": "invalid-value", "message": "Time window must end after it begins"}]
    # The first change had inserted its plan and history rows; both are gone, and the recorded view never learned of it.
    assert [p["key"] for p in env.plans.read()["plans"]] == ["idea"] and len(env.rows("SELECT * FROM mind_plan_history")) == 2
    assert [(h["section"], h["part"], h["items"]) for h in data["held_sections"]] == [("action_decisions", "decisions on a plan this proposal changes", 1)]
    assert env.step(plan)["state"] == "ready" and [c["stimulus"] for c in children(env, job)] == [FOLLOW_UP]


def test_refusal_text_is_a_literal_of_the_raising_code_never_a_value(env):
    def raised(call):
        try:
            call()
        except Exception as error:  # noqa: BLE001
            return error
    def cited(identifier):
        raise Missing(identifier)
    def formatted(identifier):
        raise Conflict(f"Source {identifier}")
    def literal():
        raise Conflict("Plan changed during evaluation")
    secret = "PRIVATE words the owner said"
    assert refusal("wishes", raised(lambda: cited(secret))) == {"section": "wishes", "code": "missing-reference", "message": ""}
    assert refusal("wishes", raised(lambda: formatted(secret)))["message"] == ""
    assert refusal("plan_changes", raised(lambda: datetime.fromisoformat(secret))) == {"section": "plan_changes", "code": "invalid-value", "message": ""}
    assert refusal("habits", raised(lambda: {}[secret])) == {"section": "habits", "code": "KeyError", "message": ""}
    assert refusal("plan_changes", raised(literal)) == {"section": "plan_changes", "code": "conflict", "message": "Plan changed during evaluation"}
    assert static_message(Conflict("Never raised")) == ""
    invalid = raised(lambda: Wish.model_validate({"content": secret, "topic": "t", "kind": "sing", "strength": 5, "ttl_hours": 3, "completion": "c"}))
    assert isinstance(invalid, ValidationError)
    assert refusal("wishes", invalid) == {"section": "wishes", "code": "schema-invalid", "message": "kind=literal_error"}
    # End to end: a time the model wrote as prose is parsed inside the section; the queue row's refusal never repeats it.
    plan = env.create()
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="Prose where a time belongs", values={"mood": 58},
        action_decisions=[env.decision(shown[plan["id"]], "wait", next_review_at=secret)]), job_id=job)
    assert result["state"] == "complete" and env.mind.read()["dimensions"]["mood"]["value"] == 58
    assert env.job(job)["rejected_sections"] == [{"section": "action_decisions", "code": "invalid-value", "message": ""}]
    assert env.step(plan)["state"] == "pending"


def test_commit_that_fails_as_a_whole_keeps_its_refusals_as_feedback_and_commits_nothing(env):
    from kin_mind.exploration import Explorations
    from kin_mind.exploration_decisions import SharingDecision
    Explorations(env.mind)
    owner = env.source("owner-chat")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    before = env.mind.read()
    def script(shown, context):
        return Appraisal(reason="Refused preference, then a fault in the event itself", values={"mood": 99},
            habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=7),
            wishes=[wish("Look it up", kind="explore")], sharing=[SharingDecision(exploration_id="explore_missing", decision="keep", reason="Keep")])
    result, _ = run(env, script, job_id=job)
    # The sharing decision belongs to the event itself: as before, its failure fails the appraisal.
    assert result["state"] == "pending" and result["error"] == "Conflict"
    after, data = env.mind.read(), env.job(job)
    assert after["revision"] == before["revision"] and after["dimensions"] == before["dimensions"] and after["desires"] == []
    assert data["rejected_sections"] == [STALE_HABITS] and "result" not in data
    # The follow-up belonged to the rolled-back transaction.
    assert children(env, job) == []


def test_database_error_inside_a_section_is_never_mistaken_for_a_refusal(env, monkeypatch):
    import sqlite3

    from kin_mind.habits import ConversationHabits
    owner = env.source("owner-chat")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    before = env.mind.read()
    def broken(self, conn, proposal, command_id, allowed=None, **_options):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(ConversationHabits, "apply", broken)
    result, _ = run(env, lambda shown, context: Appraisal(reason="The owner asked", values={"mood": 99},
        habits=HabitProposal(preferences={"exploration_paused": True}, evidence_ids=[owner], reason="The owner asked", expected_revision=0)), job_id=job)
    assert result["state"] == "pending" and result["error"] == "OperationalError"
    assert env.mind.read()["dimensions"] == before["dimensions"] and "rejected_sections" not in env.job(job)


def test_refusals_survive_a_replayed_commit_receipt(env):
    case = habits_case(env)
    job = case.job
    run(env, lambda shown, context: case.proposal(shown, context, revision=7), job_id=job)
    committed = env.job(job)
    # The process died after the commit and before the queue acknowledgement.
    with env.mind.engine.db.connect(write=True) as conn:
        data = {k: v for k, v in committed.items() if k not in {"rejected_sections", "held_sections", "result"}}
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0,data=? WHERE id=?", (json.dumps(data), job))
    result, provider = run(env, lambda shown, context: pytest.fail("A committed evaluation must not call the model again"), job_id=job)
    assert result["state"] == "complete" and provider.calls == 0
    replayed = env.job(job)
    assert replayed["rejected_sections"] == committed["rejected_sections"] and replayed["held_sections"] == committed["held_sections"]
    assert len(children(env, job)) == 1


def test_switch_off_restores_all_or_nothing(env):
    env.memory.configure({SECTION_ISOLATION: False})
    job = env.enqueue("owner-chat")
    before = env.mind.read()
    result, provider = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 72},
        session_advice=SessionAdvice(action="recall", reason="Context looks thin", evidenceIds=["invented-observation"])), job_id=job, session=SESSION)
    assert result["state"] == "pending" and result["error"] == "AdviceRejected" and provider.calls == 1
    data = env.job(job)
    assert "rejected_sections" not in data and "held_sections" not in data
    assert env.mind.read()["dimensions"] == before["dimensions"] and env.mind.read()["revision"] == before["revision"]
    # The same holds for a refused preference: nothing is held, everything fails.
    case = habits_case(env)
    result, _ = run(env, lambda shown, context: case.proposal(shown, context, revision=7), job_id=case.job)
    assert result["state"] == "pending" and result["error"] == "Conflict"
    assert env.step(case.clock)["state"] == "pending" and children(env, case.job) == [] and "held_sections" not in env.job(case.job)
    assert [d["content"] for d in env.mind.read()["desires"]] == ["Look into sundials"]


class Api:
    """The provider's HTTP endpoint, scripted: one response per tool call, every request kept."""

    def __init__(self, monkeypatch, responses):
        monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
        self.requests, self.responses = [], list(responses)
        self.provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_KEY", transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        body = json.loads(request.content)
        name = body["tools"][0]["name"]
        self.requests.append({"tool": name, "content": json.loads(body["messages"][0]["content"])})
        answer = self.responses.pop(0)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json={"model": "deepseek-flash", "id": name + "-" + str(len(self.requests)), "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": len(self.requests)}, "content": [{"type": "tool_use", "name": name, "input": answer}]})

    @property
    def tools(self):
        return [r["tool"] for r in self.requests]


UNKNOWN_ONLY = {"reason": "A real owner message", "values": {"mood": 66}, "procedures": [{"title": "not a field"}],
                "session_advice": {"action": "keep", "reason": "Healthy", "recheckCondition_note": "later"}}


def test_output_whose_only_fault_is_unknown_fields_commits_without_a_model_call(env, monkeypatch):
    api = Api(monkeypatch, [UNKNOWN_ONLY])
    job = env.enqueue("owner-chat")
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "complete" and api.tools == ["submit_appraisal"]
    data = env.job(job)
    assert sorted(data["dropped_fields"]) == [["procedures"], ["session_advice", "recheckCondition_note"]] and result["attempts"] == 1
    assert data["receipt"]["dropped_fields"] == data["dropped_fields"] and data["receipt"]["schema_repair"] is None
    view = env.mind.read()
    assert view["dimensions"]["mood"]["value"] == 66 and view["session_advice"]["decision"]["action"] == "keep"
    assert "rejected_sections" not in data


def test_unknown_fields_are_not_dropped_with_the_switch_off(env, monkeypatch):
    env.memory.configure({SECTION_ISOLATION: False})
    api = Api(monkeypatch, [UNKNOWN_ONLY])
    job = env.enqueue("owner-chat")
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "pending" and result["error"].startswith("deepseek-invalid-result:") and "extra_forbidden" in result["error"]
    assert api.tools == ["submit_appraisal"] and "dropped_fields" not in env.job(job)


@pytest.mark.parametrize("repaired", [True, False])
def test_other_schema_faults_get_one_bounded_repair_on_the_action_lane(monkeypatch, repaired):
    faulty = {"reason": "Mixed faults", "values": {"mood": "high"}, "procedures": []}
    api = Api(monkeypatch, [faulty, {"reason": "Mixed faults", "values": {"mood": 70}} if repaired else faulty])
    if repaired:
        proposal, receipt = api.provider.appraise({"stimulus": None})
        assert proposal.values == {"mood": 70} and receipt["schema_repair"]["usage"]["output_tokens"] == 2 and receipt["usage"]["output_tokens"] == 1
        assert "dropped_fields" not in receipt
    else:
        with pytest.raises(RuntimeError, match="deepseek-invalid-result"):
            api.provider.appraise({"stimulus": None})
        assert api.provider.failure_receipt["usage"]["output_tokens"] == 1
        assert api.provider.failure_receipt["schema_repair"]["outcome"] == "schema-invalid"
    assert api.tools == ["submit_appraisal", "repair_appraisal"]
    # The repair is told where and what kind, never the offending value.
    assert api.requests[1]["content"]["errors"] == [{"loc": ["values", "mood"], "type": "int_type"}, {"loc": ["procedures"], "type": "extra_forbidden"}]


def test_fault_that_only_shows_once_unknown_fields_are_gone_is_repaired_not_retried(monkeypatch):
    link = {"desire_id": "desire_1", "action": "link", "reason": "Belongs together"}
    api = Api(monkeypatch, [{"reason": "Link", "wish_updates": [{**link, "note": "not a field"}]}, {"reason": "Link", "wish_updates": [{**link, "concern_ids": []}]}])
    proposal, receipt = api.provider.appraise({"stimulus": None})
    assert api.tools == ["submit_appraisal", "repair_appraisal"] and receipt["dropped_fields"] == [["wish_updates", 0, "note"]]
    assert proposal.wish_updates[0].concern_ids == [] and "note" not in json.dumps(api.requests[1]["content"]["proposal"])
    assert api.requests[1]["content"]["errors"] == [{"loc": ["wish_updates", 0], "type": "value_error"}]


def test_action_lane_without_the_switch_keeps_failing_without_a_repair(monkeypatch):
    api = Api(monkeypatch, [{"reason": "Fault", "values": {"mood": "high"}}])
    api.provider.section_isolation = False
    with pytest.raises(RuntimeError, match="deepseek-invalid-result:values.mood=int_type"):
        api.provider.appraise({"stimulus": None})
    assert api.tools == ["submit_appraisal"]


def maintenance(env):
    source = env.source("host-asks-for-a-session-review")
    return Appraisals(env.mind).enqueue([source], env.version, origin="reflection", stimulus="session-maintenance")["id"]


INVENTED = {"reason": "Rotate", "session_advice": {"action": "recall", "reason": "Thin context", "evidenceIds": ["invented-observation"],
            "findings": [{"sourceId": "turn-1", "kind": "task-omission", "quote": "about the clock", "reason": "The owner repeated it"}]}}


def test_session_maintenance_with_refused_advice_is_repaired_once_and_committed(env, monkeypatch):
    fixed = {"action": "recall", "reason": "Thin context", "evidenceIds": ["observed-1"]}
    api = Api(monkeypatch, [INVENTED, fixed])
    job = maintenance(env)
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "complete" and api.tools == ["submit_appraisal", "repair_session_advice"]
    advice = env.mind.read()["session_advice"]
    assert advice["decision"]["evidenceIds"] == ["observed-1"] and advice["snapshotId"] == "snapshot-1"
    data = env.job(job)
    assert "rejected_sections" not in data and data["advice_repair_attempted"] is True
    # Both calls keep their usage, the way a schema repair does.
    assert data["receipt"]["usage"]["output_tokens"] == 1 and data["receipt"]["advice_repair"]["usage"]["output_tokens"] == 2
    assert advice["receipt"]["advice_repair"]["request_id"] == "repair_session_advice-2"
    # The repair sees the static refusal, what may be cited and the advice itself; no observed text.
    sent = api.requests[1]["content"]
    assert sent["problem"] == {"code": "unknown-observation", "message": "Session advice cites unknown observations"}
    assert sent["allowed_evidence"] == [{"id": "observed-1", "at": 200}] and sent["last_compaction"] == {"id": "compact-1", "completedAt": 100}
    assert sent["advice"]["evidenceIds"] == ["invented-observation"] and set(sent) == {"problem", "allowed_evidence", "last_compaction", "advice"}
    assert "PRIVATE OBSERVATION TEXT" not in json.dumps(sent) and "PRIVATE OWNER WORDS" not in json.dumps(sent)


@pytest.mark.parametrize("second", ["still-invalid", "http-500", "schema-invalid"])
def test_session_maintenance_whose_repair_fails_completes_with_the_refusal_after_two_calls(env, monkeypatch, second):
    answer = {"still-invalid": INVENTED["session_advice"], "http-500": 500, "schema-invalid": {"action": "force", "reason": "Bypass"}}[second]
    api = Api(monkeypatch, [INVENTED, answer])
    job = maintenance(env)
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "complete" and api.tools == ["submit_appraisal", "repair_session_advice"]
    data = env.job(job)
    assert data["rejected_sections"] == [UNKNOWN_ADVICE] == result["result"]["rejected_sections"]
    assert env.mind.read()["session_advice"] is None and result["result"]["session_advice"] is None and "error" not in data
    repair = data["receipt"]["advice_repair"]
    assert repair["usage"]["output_tokens"] == 2 if second != "http-500" else repair["usage_status"] == "unknown"
    # Complete means complete: the host finds nothing to run, and no third call is ever made.
    assert run(env, None, session=SESSION, provider=api.provider)[0]["state"] == "idle" and len(api.requests) == 2


def test_one_repair_per_job_even_when_a_later_attempt_is_refused_again(env, monkeypatch):
    api = Api(monkeypatch, [INVENTED, 500, INVENTED])
    job = maintenance(env)
    jobs = Appraisals(env.mind, session_context=SESSION)
    # The first attempt loses its lease between the repair and the commit, so the job is attempted again.
    original = api.provider.repair_session_advice
    def repair_then_lose_the_lease(*args):
        with env.mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET lease=0 WHERE id=?", (job,))
        return original(*args)
    monkeypatch.setattr(api.provider, "repair_session_advice", repair_then_lose_the_lease)
    assert jobs.run_one(api.provider, job_id=job)["state"] == "pending"
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job,))
    assert jobs.run_one(api.provider, job_id=job)["state"] == "complete"
    assert api.tools == ["submit_appraisal", "repair_session_advice", "submit_appraisal"] and env.job(job)["rejected_sections"] == [UNKNOWN_ADVICE]


def test_session_maintenance_is_not_repaired_with_the_switch_off(env, monkeypatch):
    env.memory.configure({SECTION_ISOLATION: False})
    api = Api(monkeypatch, [INVENTED])
    job = maintenance(env)
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "pending" and result["error"] == "AdviceRejected" and api.tools == ["submit_appraisal"]


def test_refused_finding_names_no_owner_words(env):
    job = maintenance(env)
    advice = SessionAdvice(action="keep", reason="Fine", findings=[SessionFinding(sourceId="turn-1", kind="repeat-share", quote="words the owner never said", reason="Guess")])
    result, _ = run(env, None, job_id=job, session=SESSION, provider=type("Scripted", (), {"appraise": lambda self, context: (Appraisal(reason="Keep", session_advice=advice), dict(RECEIPT))})())
    assert result["state"] == "complete"
    assert env.job(job)["rejected_sections"] == [{"section": "session_advice", "code": "finding-source-mismatch", "message": "Session finding needs an exact owner source"}]


def crowd(env, count=41):
    """Plans that sort before any later one in the model's window and that never wake: paused, with an early review time."""
    for index in range(count):
        plan = env.create(key="paused-" + str(index))
        env.plans.manage({"command_id": "pause-" + str(index), "action": "pause", "id": plan["id"], "expected_revision": plan["revision"],
                          "reason": "Set aside", "evidence_ids": [env.initial]})
    env.clock[0] += timedelta(minutes=5)


def test_woken_plan_leads_its_own_review_view_however_many_plans_sort_before_it(env):
    crowd(env)
    plan = env.decide(env.create(key="the-one-under-review"), "wait", strength=40, next_review_at=later(env, hours=1))
    env.clock[0] += timedelta(hours=1, seconds=1)
    assert env.ticks() == 1 and [r[0] for r, _ in env.wakeups(plan)] == ["due"]
    # By its usual order it would be the 42nd of a window of 40.
    assert plan["id"] not in [p["id"] for p in env.plans.read(limit=40)["plans"]]
    # The window keeps its size; a reader that follows the cursor loses none of the plans it displaced.
    led = env.plans.read(limit=40, first=plan["id"])
    assert led["plans"][0]["id"] == plan["id"] and len(led["plans"]) == 40 and led["next_cursor"] == 39
    assert env.plans.read(limit=40, first="plan_that_does_not_exist") == env.plans.read(limit=40)
    seen = {}
    def script(shown, context):
        seen.update(order=list(shown))
        return Appraisal(reason="Reviewed", action_decisions=[env.decision(shown[plan["id"]], "wait", strength=45, next_review_at=later(env, days=3))])
    result, _ = env.evaluate(script)
    assert seen["order"][0] == plan["id"] and len(seen["order"]) == 40
    data = env.job(result["id"])
    assert data["plan_review_target"] == {"event_id": env.events()[0]["id"], "plan_id": plan["id"], "shown": True} and plan["id"] in data["plan_view"]["plans"]
    assert env.step(plan)["strength"] == 45 and env.ticks(6) == 1
    # Any other appraisal keeps the usual window.
    ordinary = {}
    env.evaluate(lambda shown, context: ordinary.update(order=list(shown)) or Appraisal(reason="Chat"), job_id=env.enqueue("owner-chat"))
    assert plan["id"] not in ordinary["order"] and "plan_review_target" not in env.job(env.rows("SELECT id FROM mind_appraisals ORDER BY rowid DESC LIMIT 1")[0]["id"])


def test_review_that_cannot_show_its_plan_registers_no_version_and_reopens_its_reasons(env):
    plan = env.decide(env.create(), "wait", strength=40, next_review_at=later(env, days=30))
    env.upgrade("planning-v2")
    [event] = env.plans.tick(env.actions)
    assert [(r[0], e) for r, e in env.wakeups(plan)] == [("agent_version", event)]
    # It stops being active before its review starts.
    paused = env.plans.manage({"command_id": "pause", "action": "pause", "id": plan["id"], "expected_revision": plan["revision"],
                               "reason": "Owner needs the computer", "evidence_ids": [env.initial], "next_review_at": later(env, days=30)})
    assert paused["agent_version"] == "planning-v2"
    with env.mind.engine.db.connect(write=True) as conn:
        # As if an older host had paused it: the version still differs, so a registration would be visible.
        stale = {**paused, "agent_version": "planning-v1"}
        conn.execute("UPDATE mind_plans SET data=? WHERE id=?", (json.dumps(stale), plan["id"]))
    def script(shown, context):
        # It is listed, as any plan is, but not as the active plan under review; it becomes active again while the model thinks.
        assert shown[plan["id"]]["status"] == "paused"
        env.plans.manage({"command_id": "resume", "action": "resume", "id": plan["id"], "expected_revision": paused["revision"],
                          "reason": "The owner is done", "evidence_ids": [env.initial], "next_review_at": later(env, days=30)})
        with env.mind.engine.db.connect(write=True) as conn:
            resumed = {**env.plans.get(conn, plan["id"]), "agent_version": "planning-v1"}
            conn.execute("UPDATE mind_plans SET data=? WHERE id=?", (json.dumps(resumed), plan["id"]))
        return Appraisal(reason="Nothing to arrange")
    result, _ = env.evaluate(script)
    assert env.job(result["id"])["plan_review_target"] == {"event_id": event, "plan_id": plan["id"], "shown": False}
    # No version was registered for the plan this review never showed as active, and its reasons are open again.
    assert env.current(plan)["agent_version"] == "planning-v1" and env.reviews("version-seen") == []
    assert [(r[0], e) for r, e in env.wakeups(plan)] == [("agent_version", "unshown:" + event)]
    # So the next tick wakes exactly one review: an event of its own, although the reason is the very same.
    [again] = env.plans.tick(env.actions)
    assert again != event and env.ticks(6) == 2 and [(r[0], e) for r, e in env.wakeups(plan)] == [("agent_version", again)]
    shown_again = {}
    env.evaluate(lambda shown, context: shown_again.update(first=next(iter(shown))) or Appraisal(reason="Seen now"))
    assert shown_again["first"] == plan["id"] and env.current(plan)["agent_version"] == "planning-v2" and env.ticks(6) == 2


def test_review_of_a_plan_that_no_longer_exists_reopens_nothing_it_can_use_and_completes(env):
    plan = env.create()
    [event] = env.plans.tick(env.actions)
    env.actions.drain(Appraisals(env.mind))
    assert [e for _, e in env.wakeups(plan)] == [event]
    with env.mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_plans WHERE id=?", (plan["id"],))
    jobs = Appraisals(env.mind, exploration_capabilities={"version": env.version})
    result = jobs.run_one(Reviewer(lambda shown, context: Appraisal(reason="Nothing to see")))
    assert result["state"] == "complete" and env.job(result["id"])["plan_review_target"] == {"event_id": event, "plan_id": plan["id"], "shown": False}
    assert env.rows("SELECT * FROM mind_plan_wakeups") == []
