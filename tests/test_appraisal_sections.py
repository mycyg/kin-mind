"""The seams the audited sections plug into: the switches, what a request offers, what each lane
blanks, where a section commits, and what the queue row stores.

Synthetic replays only: an injected clock, scripted providers or httpx.MockTransport. No model or
network call. Nothing here implements a section; every one of them is still a no-op handler.
"""
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import Field, ValidationError
from test_section_isolation import (  # noqa: F401 - the fixture and its helpers
    SESSION,
    Api,
    children,
    env,
    run,
)

from eventmem.core.db import Conflict
from eventmem.core.models import Model
from kin_mind import memory as memory_module
from kin_mind.appraisal import (
    ASK_AGAIN_SECTIONS,
    AUDIT_HANDLERS,
    AUDIT_SECTION_SWITCH,
    AUDIT_SECTIONS,
    ISOLATED_SECTIONS,
    NEW_INTERACTION_COMMITS,
    SECTION_PROMPTS,
    SECTION_UPSTREAM,
    SECTIONS_WITHHELD,
    UPSTREAM_SECTIONS,
    Appraisal,
    Appraisals,
    DeepSeek,
    ExpressionIntent,
    NextMove,
    SelfHypothesis,
    TraitObservation,
    appraisal_schema,
    last_refusal,
    offered_sections,
    proposal_record,
    register_audit_section,
)

pytest_plugins = ("test_memory_continuity",)

# The request as it was before any of this existed, for one fixed context. A section that is not
# offered leaves no trace: no property, no definition and no paragraph. Re-pin only together with a
# deliberate change to the shared prompt, the shared schema or the context projection.
UNCHANGED_REQUEST = {
    "interaction": "349ab11490e2c2eae3e3d1d8b2efa86867fafd0db499bb92840d3d54db356e44",
    "action": "ae08dcffa488a80c6b4a21bc8031888dc9e738173279678c118b6254da2c79a4",
    "history": "b374eb359aee4f49e3a38990e5454c3e6667c76dab6b5f793dbbe677d80783a3",
}

CONTEXT = {
    "stimulus": None,
    "operational_only": False,
    "state": {
        "scope": {"project": "default", "persona": "synthetic", "collection": "default", "world": "real"},
        "agent_version": "synthetic-v1",
        "revision": 4,
        "as_of": "2026-01-01T00:00:00+00:00",
        "dimensions": {"mood": {"label": "mood", "value": 60, "baseline": 50, "reason": "A shared afternoon"}},
        "desires": [],
        "concerns": [],
    },
    "new_evidence": [{"id": "src_" + "a" * 32, "authority": "explicit", "revision": 1,
                      "occurred_at": "2026-01-01T00:00:00+00:00", "received_at": "2026-01-01T00:00:00+00:00",
                      "metadata": {"role": "user", "host_event": "message"}, "text": "a synthetic owner turn"}],
    "recent_dialogue": [],
    "exploration_targets": [],
}

SWITCHES = ("trait_ledger", "behavior_chain", "expression_intent", "next_move_audit",
            "wish_version_review", "rest_review_window")


class Recorded:
    """The provider's endpoint, scripted: the whole request body of every call is kept."""

    def __init__(self, monkeypatch, sections=()):
        monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
        self.bodies = []
        self.provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_KEY",
                                 transport=httpx.MockTransport(self.respond))
        self.provider.audit_sections = set(sections)

    def respond(self, request):
        self.bodies.append(request.content)
        return httpx.Response(200, json={"model": "deepseek-flash", "id": "synthetic-1", "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": 1},
            "content": [{"type": "tool_use", "name": "submit_appraisal", "input": {"reason": "A synthetic proposal"}}]})

    def request(self, context):
        self.provider.appraise(context)
        return json.loads(self.bodies[-1]), hashlib.sha256(self.bodies[-1]).hexdigest()


def observation(**extra):
    return TraitObservation(key="patience", category="habits", slug="patience", evidence_class="owner_statement",
                            polarity="support", evidence_ids=["src_synthetic"], **extra)


def move():
    return NextMove(move="reply", reason="A synthetic audit of this move")


# --- the switches ------------------------------------------------------------------------------

def test_every_switch_is_registered_default_on_and_boolean(env):
    for switch in SWITCHES:
        assert memory_module.DEFAULTS[switch] is True
        assert env.memory.settings()[switch] is True
    env.memory.configure({switch: False for switch in SWITCHES})
    assert all(env.memory.settings()[switch] is False for switch in SWITCHES)
    with pytest.raises(ValueError, match="Feature flags are boolean"):
        env.memory.configure({"trait_ledger": "off"})
    with pytest.raises(ValueError, match="Unknown memory setting"):
        env.memory.configure({"trait_ledgers": True})
    # Every switch that carries sections is one of these, and every section names one.
    assert set(AUDIT_SECTION_SWITCH.values()) <= set(SWITCHES)
    assert set(AUDIT_SECTION_SWITCH) == set(AUDIT_SECTIONS)


# --- registration without a paid follow-up -------------------------------------------------------

def test_an_audited_section_is_isolated_and_never_asked_again():
    assert set(SECTION_UPSTREAM) == set(Appraisal.model_fields)
    # Exactly what it was: nothing an audited section rests on, and no audited section, joins these.
    assert ASK_AGAIN_SECTIONS == {"habits", "plan_changes", "concerns", "action_decisions"}
    assert UPSTREAM_SECTIONS == {"habits", "plan_changes", "concerns"}
    assert set(AUDIT_SECTIONS) <= set(ISOLATED_SECTIONS) and not set(AUDIT_SECTIONS) & ASK_AGAIN_SECTIONS
    # What rests on what is still registered, so a refusal can hold what depended on it.
    assert SECTION_UPSTREAM["trait_decisions"] == {"trait_observations": None}
    assert set(SECTION_UPSTREAM["next_move"]) == {"trait_decisions", "wishes", "action_decisions"}
    assert NEW_INTERACTION_COMMITS == {name: name == "trait_observations" for name in AUDIT_SECTIONS}


# --- what a request offers ------------------------------------------------------------------------

def test_a_request_offering_nothing_is_the_request_as_it_was(monkeypatch):
    api = Recorded(monkeypatch)
    body, fingerprint = api.request(CONTEXT)
    assert fingerprint == UNCHANGED_REQUEST["interaction"]
    assert not set(body["tools"][0]["input_schema"]["properties"]) & set(AUDIT_SECTIONS)
    assert not any(paragraph in body["system"] for paragraph in SECTION_PROMPTS.values())
    assert api.request({**CONTEXT, "operational_only": True})[1] == UNCHANGED_REQUEST["action"]
    assert api.request({**CONTEXT, "stimulus": "memory-backfill"})[1] == UNCHANGED_REQUEST["history"]


@pytest.mark.parametrize("off", [(), *[(switch,) for switch in SWITCHES], SWITCHES])
def test_each_switch_takes_its_own_properties_and_paragraphs_out(monkeypatch, off):
    enabled = {name for name, switch in AUDIT_SECTION_SWITCH.items() if switch not in off}
    api = Recorded(monkeypatch, enabled)
    body, _ = api.request(CONTEXT)
    schema = body["tools"][0]["input_schema"]
    base = set(appraisal_schema()["properties"])
    assert set(schema["properties"]) == base | enabled
    for name, paragraph in SECTION_PROMPTS.items():
        assert (paragraph in body["system"]) is (name in enabled)
    # A property that is offered brings its definitions with it, and no others.
    assert ("TraitObservation" in schema.get("$defs", {})) is ("trait_observations" in enabled)
    assert ("NextMove" in schema.get("$defs", {})) is ("next_move" in enabled)


@pytest.mark.parametrize("stimulus", sorted(SECTIONS_WITHHELD))
def test_the_lanes_that_blank_them_offer_nothing(monkeypatch, stimulus):
    assert offered_sections(stimulus, set(AUDIT_SECTIONS)) == ()
    api = Recorded(monkeypatch, AUDIT_SECTIONS)
    body, _ = api.request({**CONTEXT, "stimulus": stimulus})
    schema = body["tools"][0]["input_schema"]
    assert not set(schema["properties"]) & set(AUDIT_SECTIONS)
    assert not any(paragraph in body["system"] for paragraph in SECTION_PROMPTS.values())


def test_a_lane_that_offers_nothing_blanks_what_a_proposal_carries(env, monkeypatch):
    """Maintenance answers with audited sections anyway: they never reach the commit."""
    api = Api(monkeypatch, [{"reason": "Rotate", "session_advice": {"action": "keep", "reason": "Healthy"},
                             "next_move": {"move": "reply", "reason": "A synthetic audit"},
                             "trait_observations": [observation().model_dump()]}])
    source = env.source("host-asks-for-a-session-review")
    job = Appraisals(env.mind).enqueue([source], env.version, origin="reflection", stimulus="session-maintenance")["id"]
    result, _ = run(env, None, job_id=job, session=SESSION, provider=api.provider)
    assert result["state"] == "complete" and api.tools == ["submit_appraisal"]
    data = env.job(job)
    assert "rejected_sections" not in data and children(env, job) == []
    assert not set(data["proposed_result"]) & set(AUDIT_SECTIONS)
    assert env.mind.read()["session_advice"]["decision"]["action"] == "keep"


# --- the commit seam ------------------------------------------------------------------------------

def test_a_section_no_module_claims_is_refused_alone_and_recorded(env):
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 72},
                                                          next_move=move()), job_id=job)
    assert result["state"] == "complete"
    data = env.job(job)
    assert data["rejected_sections"] == [{"section": "next_move", "code": "section-unavailable",
                                          "message": "No module has claimed this section yet"}]
    # The rest of the appraisal committed, and nothing was asked again.
    assert env.mind.read()["dimensions"]["mood"]["value"] == 72
    assert "held_sections" not in data and children(env, job) == []
    with env.mind.engine.db.connect() as conn:
        refused = last_refusal(conn, env.mind.scope.key())
    assert set(refused) == {"next_move"} and refused["next_move"]["code"] == "section-unavailable"
    assert last_refusal_at(env, "trait_decisions") == {}


def last_refusal_at(env, section):
    with env.mind.engine.db.connect() as conn:
        return last_refusal(conn, env.mind.scope.key(), section)


def test_what_rests_on_a_refused_section_is_held_and_still_asks_nothing(env):
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 64},
        trait_observations=[observation()], self_hypothesis=SelfHypothesis(statement="A synthetic hypothesis",
        reason="A synthetic reason")), job_id=job)
    assert result["state"] == "complete"
    data = env.job(job)
    assert [r["section"] for r in data["rejected_sections"]] == ["trait_observations", "self_hypothesis"]
    # trait_decisions was empty, so nothing of it was held; either way no follow-up is queued.
    assert "held_sections" not in data and children(env, job) == []
    assert env.mind.read()["dimensions"]["mood"]["value"] == 64


def test_a_module_claims_its_section_by_registering_one_handler(env, monkeypatch):
    """What a module writes to plug in: a handler, one registration, and no edit to apply()."""
    seen = {}

    def commit_next_move(commit):
        seen.update(section=commit.section, move=commit.value.move, event=commit.event_id,
                    stimulus=commit.stimulus, version=commit.version)
        raise Conflict("Next move is inconsistent with this batch", code="next-move-inconsistent")

    # setitem first, so the registration this test makes is undone when it ends.
    monkeypatch.setitem(AUDIT_HANDLERS, "next_move", AUDIT_HANDLERS["next_move"])
    monkeypatch.setitem(NEW_INTERACTION_COMMITS, "next_move", NEW_INTERACTION_COMMITS["next_move"])
    register_audit_section("next_move", commit_next_move)
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 58},
                                                          next_move=move()), job_id=job)
    assert result["state"] == "complete" and seen["section"] == "next_move" and seen["move"] == "reply"
    assert seen["stimulus"] is None and seen["version"] == env.version and seen["event"]
    data = env.job(job)
    # The owning module's own static code, recorded and projected, and still nothing asked again.
    assert [(r["section"], r["code"]) for r in data["rejected_sections"]] == [("next_move", "next-move-inconsistent")]
    assert children(env, job) == [] and last_refusal_at(env, "next_move")["next_move"]["code"] == "next-move-inconsistent"


def test_registration_refuses_a_section_that_does_not_exist():
    with pytest.raises(RuntimeError, match="Unknown audited section"):
        register_audit_section("trait_inventions", lambda commit: None)


def test_what_was_observed_commits_while_a_decision_waits_for_the_next_round(system):
    """The owner writes again while the appraisal runs: only the observation handler runs at all."""
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
            return Appraisal(reason="Late interpretation", trait_observations=[observation()],
                             next_move=move(), expression_intent=ExpressionIntent(stance="A synthetic stance")), {"model": "deepseek-flash"}

    result = jobs.run_one(Provider(), job_id=job)
    assert result["state"] == "complete" and result["result"]["new_interaction_pending"]
    refused = [r["section"] for r in result["result"]["rejected_sections"]]
    # The observation was offered to its handler; the intent and the move were held back entirely.
    assert refused == ["trait_observations"]


# --- what the queue row stores --------------------------------------------------------------------

class OlderAppraisal(Model):
    """The proposal model as it was before the audited sections, which forbids what it does not
    know. A row this code writes with every audited section empty still validates against it."""

    values: dict = Field(default_factory=dict)
    motivations: dict = Field(default_factory=dict)
    reason: str
    wishes: list = Field(default_factory=list)
    wish_updates: list = Field(default_factory=list)
    evolution: dict | None = None
    understanding: dict | None = None
    concerns: list = Field(default_factory=list)
    rhythm: dict | None = None
    sharing: list = Field(default_factory=list)
    memory: dict = Field(default_factory=dict)
    next_review_minutes: int = 20
    habits: dict | None = None
    session_advice: dict | None = None
    recall_needs: list = Field(default_factory=list)
    plan_changes: list = Field(default_factory=list)
    action_decisions: list = Field(default_factory=list)
    procedure_candidates: list = Field(default_factory=list)


def test_the_older_proposal_model_is_the_current_one_without_the_audited_sections():
    assert set(OlderAppraisal.model_fields) == set(Appraisal.model_fields) - set(AUDIT_SECTIONS)


def test_an_empty_section_is_left_out_of_the_stored_proposal(env):
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 51}), job_id=job)
    assert result["state"] == "complete"
    stored = env.job(job)["proposed_result"]
    assert not set(stored) & set(AUDIT_SECTIONS)
    # Byte for byte the row the previous release wrote, and a reader that forbids unknown fields
    # accepts it as its own.
    assert stored == result["result"]["proposal"] == OlderAppraisal.model_validate(stored).model_dump()
    assert json.dumps(stored, sort_keys=True) == json.dumps(
        Appraisal.model_validate(stored).model_dump(exclude=set(AUDIT_SECTIONS)), sort_keys=True)


def test_a_section_that_carries_something_is_stored_and_the_older_reader_refuses_it(env):
    job = env.enqueue("owner-chat")
    run(env, lambda shown, context: Appraisal(reason="A real owner message", next_move=move()), job_id=job)
    stored = env.job(job)["proposed_result"]
    assert stored["next_move"]["move"] == "reply" and set(stored) & set(AUDIT_SECTIONS) == {"next_move"}
    with pytest.raises(ValidationError):
        OlderAppraisal.model_validate(stored)


def test_the_record_helper_leaves_out_exactly_the_empty_sections():
    proposal = Appraisal(reason="A synthetic proposal", trait_observations=[observation()])
    assert set(proposal_record(proposal)) & set(AUDIT_SECTIONS) == {"trait_observations"}
    assert set(proposal_record(Appraisal(reason="Nothing"))) & set(AUDIT_SECTIONS) == set()


# --- the stub models validate shape, and nothing else ---------------------------------------------

def test_the_stubs_bound_what_they_accept_and_forbid_what_they_do_not_know():
    with pytest.raises(ValidationError):
        TraitObservation(key="k", category="c", slug="s", evidence_class="hearsay", polarity="support")
    with pytest.raises(ValidationError):
        NextMove(move="explore", reason="The host derives the rest")
    with pytest.raises(ValidationError):
        NextMove(move="reply", reason="A reason", grounds=["g"] * 7)
    with pytest.raises(ValidationError):
        ExpressionIntent(stance="A stance", valid_minutes=9)
    with pytest.raises(ValidationError):
        observation(note="x" * 301)
    with pytest.raises(ValidationError):
        Appraisal(reason="r", next_move={"move": "reply", "reason": "r", "mood": "invented"})
