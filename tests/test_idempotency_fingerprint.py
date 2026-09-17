"""Idempotency three ways (stage 2 WP3): the command id says which command this is, the
payload fingerprint says what it would write, and the precondition says whether it may take
effect now. A retry that only reread its expected revision keeps its receipt; a proposal
whose content changed never passes as the old success, and where the host owns the command
id it becomes an explicit revision that is validated again in full."""

import ast
import importlib
import json
import pathlib

import pytest

from eventmem.core.db import Conflict, digest
from eventmem.core.idempotency import (
    FINGERPRINT_FIELDS,
    FINGERPRINT_VERSION,
    effective,
    revision_id,
    stamp,
)
from eventmem.core.models import SourceInput
from kin_mind.conflicts import classify
from kin_mind.habits import ConversationHabits, HabitProposal
from kin_mind.lifecycle import EventIdentityJudgement, EventRoute
from kin_mind.state import AffectiveEvent
from test_autonomous_plans import create, env  # noqa: F401 - fixture and helper for plans
from test_memory_learning import propose as propose_method
from test_memory_lifecycle import system  # noqa: F401 - mind, memory, lifecycle, source, clock

# The three explicit policy updates hand `_mutate` a plain dict instead of a model; these are
# the keys they read, so the table stays complete without a model to compare them against.
POLICY_KEYS = {"style", "quiet_start_hour", "wait_for_reply"}

QUOTE = "第一版草稿里写的是 x² 的结果"


def route_payload(**changes):
    base = dict(key="first", action="append", event_id=None, expected_revision=None, title="进度记录",
                member_ids=[], evidence_ids=["src_a", "src_b"], binding="explicit_reference",
                quote=QUOTE, identity=None, reason="同一件事的后续", thread_id=None,
                expected_thread_revision=None)
    return {**base, **changes}


def fingerprint_of(payload, family="event-route"):
    return stamp(family, "scope", payload).fingerprint


# --- the table itself -------------------------------------------------------------------

def test_every_model_field_is_declared_as_content_precondition_or_identity():
    """A new field on a command model fails the build until someone says what it is."""
    for name, family in FINGERPRINT_FIELDS.items():
        declared = set(family.fields) | set(family.precondition) | set(family.identity)
        # Content, precondition and identity are three disjoint groups, never overlapping.
        assert len(declared) == (
            len(family.fields) + len(family.precondition) + len(family.identity)), name
        known = set()
        for reference in family.models:
            module, model = reference.split(":")
            fields = set(getattr(importlib.import_module(module), model).model_fields)
            assert fields <= declared, (name, model, sorted(fields - declared))
            known |= fields
        if family.models:
            assert declared - known <= POLICY_KEYS, (name, sorted(declared - known))
    # The precondition is never part of the content of any family.
    assert all("expected_revision" in f.precondition for f in FINGERPRINT_FIELDS.values())
    assert "expected_thread_revision" in FINGERPRINT_FIELDS["event-route"].precondition
    for wanted in ("title", "quote", "reason", "binding", "identity"):
        assert wanted in FINGERPRINT_FIELDS["event-route"].fields


def test_the_graph_revision_family_declares_every_request_key_that_function_reads():
    """That family's payload is a plain dict, so the code itself is the model."""
    family = FINGERPRINT_FIELDS["graph-revision"]
    declared = set(family.fields) | set(family.precondition) | set(family.identity)
    source = pathlib.Path(importlib.import_module("kin_mind.graph").__file__).read_text()
    revise = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "revise")
    read = set()
    for node in ast.walk(revise):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "request" and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "request" and node.args
                and isinstance(node.args[0], ast.Constant)):
            read.add(node.args[0].value)
    assert read and read <= declared, sorted(read - declared)


# --- canonicalization: stable JSON and declared sets, never the business strings ---------

def test_a_superscript_and_doubled_space_are_different_content():
    """Quotes are verified by literal substring match against the source, so the fingerprint
    must not fold them: no NFKC, no whitespace collapsing, no case folding."""
    assert fingerprint_of(route_payload(quote="x²")) != fingerprint_of(route_payload(quote="x2"))
    assert fingerprint_of(route_payload(title="a  b")) != fingerprint_of(route_payload(title="a b"))
    assert fingerprint_of(route_payload(reason="Ready")) != fingerprint_of(route_payload(reason="ready"))
    assert fingerprint_of(route_payload(quote=QUOTE)) == fingerprint_of(route_payload(quote=QUOTE))


def test_only_the_sets_the_host_sorts_itself_ignore_their_order():
    """`Mind._evidence` reduces evidence ids to `sorted(set(ids))`, so their order writes
    nothing. Member order is written down in the receipt, so it is content."""
    assert fingerprint_of(route_payload(evidence_ids=["src_b", "src_a", "src_a"])) == \
        fingerprint_of(route_payload(evidence_ids=["src_a", "src_b"]))
    assert fingerprint_of(route_payload(member_ids=["mem_b", "mem_a"])) != \
        fingerprint_of(route_payload(member_ids=["mem_a", "mem_b"]))
    ordered = {"preferences": {"exploration_paused": True, "reply_choice": "always"},
               "evidence_ids": ["src_a"], "reason": "同一份偏好", "expected_revision": 0}
    other = {"preferences": {"reply_choice": "always", "exploration_paused": True},
             "evidence_ids": ["src_a"], "reason": "同一份偏好", "expected_revision": 0}
    assert fingerprint_of(ordered, "habit") == fingerprint_of(other, "habit")


def test_the_precondition_is_not_part_of_what_a_command_is():
    moved = route_payload(expected_revision=4, expected_thread_revision=2)
    assert fingerprint_of(moved) == fingerprint_of(route_payload())
    assert "expected_revision" not in effective("event-route", moved)
    assert stamp("event-route", "scope", moved).precondition != \
        stamp("event-route", "scope", route_payload()).precondition
    assert stamp("event-route", "scope", moved).version == FINGERPRINT_VERSION


# --- mind state: a retry that reread the revision keeps its receipt ----------------------

def affect(mind, source, command="observation-1", *, revision=None, reason="主人说今天很顺利"):
    return mind.record(AffectiveEvent(command_id=command, agent_version="fixture",
        expected_revision=mind.read()["revision"] if revision is None else revision,
        evidence_ids=[source], values={"mood": 70}, reason=reason))


def test_a_retry_with_only_a_newer_expected_revision_returns_the_original_receipt(system):
    mind, _memory, _lifecycle, source, _clock = system
    evidence = source("owner-note", "主人说今天很顺利")["id"]
    first = affect(mind, evidence)
    after = mind.read()["revision"]
    # The state moved on, so this attempt was built against a newer revision. Same command.
    again = affect(mind, evidence, revision=after)
    assert again == first and mind.read()["revision"] == after


def test_the_same_id_with_other_content_never_returns_the_old_success(system):
    mind, _memory, _lifecycle, source, _clock = system
    evidence = source("owner-note", "主人说今天很顺利")["id"]
    first = affect(mind, evidence)
    with pytest.raises(Conflict) as refused:
        affect(mind, evidence, revision=mind.read()["revision"], reason="完全不同的判断")
    found = classify(refused.value)
    assert (found.kind, found.code) == ("runtime", "payload-changed")
    assert mind.read()["revision"] == first["revision"]


def test_a_command_stored_before_the_side_table_replays_by_its_legacy_digest(system):
    """An old database has no fingerprint row, so the digest of the whole payload decides,
    exactly as it did before this work package."""
    mind, _memory, _lifecycle, source, _clock = system
    evidence = source("owner-note", "主人说今天很顺利")["id"]
    first = affect(mind, evidence)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_command_fingerprints")
    assert affect(mind, evidence, revision=first["revision"] - 1) == first
    with pytest.raises(Conflict):
        affect(mind, evidence, revision=mind.read()["revision"])


def test_a_side_row_from_another_fingerprint_version_falls_back_to_the_legacy_digest(system):
    """The lesson of the recovery-identity commit: a new fingerprint version must not declare
    every committed command changed. An unreadable version means the old comparison decides."""
    mind, _memory, _lifecycle, source, _clock = system
    evidence = source("owner-note", "主人说今天很顺利")["id"]
    first = affect(mind, evidence)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_command_fingerprints SET fingerprint_version='payload-v0',"
                     "fingerprint='not-comparable'")
    assert affect(mind, evidence, revision=first["revision"] - 1) == first
    with pytest.raises(Conflict):
        affect(mind, evidence, revision=mind.read()["revision"])


def test_with_the_flag_off_a_refreshed_expected_revision_is_a_different_command(system):
    mind, memory, _lifecycle, source, _clock = system
    memory.configure({"idempotency_fingerprint": False})
    evidence = source("owner-note", "主人说今天很顺利")["id"]
    first = affect(mind, evidence)
    with pytest.raises(Conflict) as refused:
        affect(mind, evidence, revision=mind.read()["revision"])
    assert str(refused.value) == "Idempotency key reused with different content"
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_command_fingerprints'").fetchone()
    assert affect(mind, evidence, revision=first["revision"] - 1) == first


# --- event routes: refused for a client, an explicit revision for the host ---------------

def sourced_route(system, key, text, **changes):
    """One appraisal-shaped route whose quote really is in its own evidence."""
    mind, memory, lifecycle, source, _clock = system
    src = source(key, text)
    with mind.engine.db.connect(write=True) as conn:
        proof = memory.graph.proof(conn, [src["id"]])
    return EventRoute(key=key, action="create", title="星图报告", evidence_ids=[src["id"]],
                      binding="semantic_candidate", reason="有来源的合成事件", **changes), src, proof


FOLLOW_UP = "星图报告最初版本还未发送。后来补了一段说明。"


def append_route(system, target, src, prior_ids, **changes):
    fields = dict(key="follow-up", action="append", event_id=target["id"],
                  expected_revision=target["revision"], evidence_ids=[src["id"]],
                  binding="explicit_reference", quote=FOLLOW_UP, reason="同一件事的后续",
                  identity=EventIdentityJudgement(decision="same_event", participants_match=True,
                      object_match=True, time_compatible=True, continuation_supported=True,
                      prior_record_ids=prior_ids))
    return EventRoute(**{**fields, **changes})


@pytest.fixture
def appended(system):
    """A committed `append` route, with everything a second attempt would need to repeat it."""
    mind, memory, lifecycle, source, _clock = system
    first, _src, proof = sourced_route(system, "first", "星图报告最初版本还未发送。")
    with mind.engine.db.connect(write=True) as conn:
        created = lifecycle.apply_routes(conn, [first], proof, "appraisal-a")[0]
    later = source("follow-up", FOLLOW_UP)
    with mind.engine.db.connect(write=True) as conn:
        target = memory.graph.get(conn, created["event_id"])
        prior = list(lifecycle.snapshot(conn, target["id"])["records"])[:1]
        proof = memory.graph.proof(conn, [later["id"]]) + memory.graph.proof(conn, prior)
        route = append_route(system, target, later, prior)
        applied = lifecycle.apply_routes(conn, [route], proof, "appraisal-b", revise=True)[0]
    assert applied["state"] == "append"
    return route, later, proof, applied


def test_a_route_retry_that_reread_the_event_keeps_its_receipt(system, appended):
    """Requirement: a retry re-applies the stored proposal, and the revisions it refreshed
    are a precondition, not a new command."""
    mind, memory, lifecycle, _source, _clock = system
    route, _later, proof, applied = appended
    with mind.engine.db.connect() as conn:
        moved = memory.graph.get(conn, applied["event_id"])["revision"]
    assert moved != route.expected_revision
    with mind.engine.db.connect(write=True) as conn:
        again = lifecycle.apply_routes(conn, [route.model_copy(update={"expected_revision": moved})],
                                       proof, "appraisal-b", revise=True)[0]
        assert again == applied
        assert memory.graph.get(conn, applied["event_id"])["revision"] == moved


@pytest.mark.parametrize("changed", [{"title": "另一个标题"}, {"quote": "另一段引文"},
                                     {"reason": "另一个理由"}])
def test_title_quote_and_reason_each_change_the_command_on_their_own(system, appended, changed):
    mind, _memory, lifecycle, _source, _clock = system
    route, _later, proof, applied = appended
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        # A caller that supplies its own command id gets a conflict, never the old receipt.
        lifecycle.apply_routes(conn, [route.model_copy(update=changed)], proof, "appraisal-b")
    found = classify(refused.value)
    assert (found.kind, found.code, found.handling) == ("runtime", "payload-changed", "block")
    assert str(refused.value) == "Event route command changed"
    with mind.engine.db.connect() as conn:
        stored = conn.execute("SELECT COUNT(*) FROM mind_event_routes WHERE scope=?",
                              (mind.scope.key(),)).fetchone()[0]
    assert stored == 2 and applied["state"] == "append"


def test_a_host_revision_supersedes_the_original_and_is_validated_again_in_full(system, appended):
    mind, memory, lifecycle, _source, _clock = system
    route, later, proof, applied = appended
    with mind.engine.db.connect() as conn:
        moved = memory.graph.get(conn, applied["event_id"])["revision"]
    rewritten = route.model_copy(update={"reason": "补充说明属于同一件事", "expected_revision": moved})
    with mind.engine.db.connect(write=True) as conn:
        revised = lifecycle.apply_routes(conn, [rewritten], proof, "appraisal-b", revise=True)[0]
    original = digest(["appraisal-b", route.key])
    expected = revision_id(original, stamp("event-route", mind.scope.key(), rewritten.model_dump()))
    assert revised["state"] == "append" and revised != applied
    assert revised["supersedes"] == original and revised["undo_command_id"] == "route-" + expected
    with mind.engine.db.connect() as conn:
        side = conn.execute("SELECT * FROM mind_command_fingerprints WHERE id=?", (expected,)).fetchone()
        assert (side["family"], side["supersedes"]) == ("event-route", original)
        # Before and after are both kept: the original command's undo record is untouched and
        # the revision has one of its own, taken at the state the revision found.
        before = json.loads(conn.execute("SELECT data FROM mind_graph_commands WHERE id=?",
                                         ("route-" + original,)).fetchone()[0])
        after = json.loads(conn.execute("SELECT data FROM mind_graph_commands WHERE id=?",
                                        (revised["undo_command_id"],)).fetchone()[0])
        assert before["appraisal_id"] == after["appraisal_id"] == "appraisal-b"
        assert after["before"][applied["event_id"]]["revision"] == moved
        assert after["after_revisions"][applied["event_id"]] > moved
        # The original row still holds the original content, and nothing was applied twice.
        assert conn.execute("SELECT COUNT(*) FROM mind_event_routes WHERE scope=?",
                            (mind.scope.key(),)).fetchone()[0] == 3
    # Replaying that same revised content finds the revision instead of making another.
    with mind.engine.db.connect(write=True) as conn:
        assert lifecycle.apply_routes(conn, [rewritten], proof, "appraisal-b", revise=True)[0] == revised
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_event_routes WHERE scope=?",
                            (mind.scope.key(),)).fetchone()[0] == 3


def test_a_revision_runs_the_quote_check_again_and_falls_back_when_it_fails(system, appended):
    """Full validation, not a receipt swap: an explicit-reference binding whose quote is no
    longer in its own evidence cannot claim the same event."""
    mind, memory, lifecycle, _source, _clock = system
    route, _later, proof, applied = appended
    with mind.engine.db.connect() as conn:
        moved = memory.graph.get(conn, applied["event_id"])["revision"]
    invented = route.model_copy(update={"quote": "这段话不在任何来源里", "expected_revision": moved})
    with mind.engine.db.connect(write=True) as conn:
        revised = lifecycle.apply_routes(conn, [invented], proof, "appraisal-b", revise=True)[0]
    assert revised["state"] == "defer" and revised["requested_action"] == "append"


def test_a_revision_built_on_a_revision_that_moved_on_is_refused(system, appended):
    """The precondition is checked at the first effect and again for every revision."""
    mind, _memory, lifecycle, _source, _clock = system
    route, _later, proof, _applied = appended
    stale = route.model_copy(update={"reason": "补充说明属于同一件事"})
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        lifecycle.apply_routes(conn, [stale], proof, "appraisal-b", revise=True)
    assert str(refused.value) == "Event route target changed"
    assert classify(refused.value).code == "event-target-changed"


def test_a_legacy_route_without_a_side_row_still_replays(system, appended):
    mind, _memory, lifecycle, _source, _clock = system
    route, _later, proof, applied = appended
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM mind_command_fingerprints WHERE family='event-route'")
    with mind.engine.db.connect(write=True) as conn:
        assert lifecycle.apply_routes(conn, [route], proof, "appraisal-b", revise=True)[0] == applied


# --- habits: the preference revision gates the first effect and every revision -----------

def habit_case(system, preferences, *, revision=0, reason="主人要求先问再探索"):
    mind, _memory, _lifecycle, _source, _clock = system
    # A conversational preference rests on the owner's own turn, so the source is one.
    owner = mind.engine.receive(SourceInput(namespace="synthetic", key="owner-preference", text="主人要求先问再探索",
        scope=mind.scope, authority="explicit", occurred_at=mind.clock(), extract=False,
        metadata={"role": "user", "host_event": "message"}))
    return HabitProposal(preferences=preferences, evidence_ids=[owner["id"]], reason=reason,
                         expected_revision=revision)


def test_a_habit_revision_is_refused_on_a_stale_revision_and_accepted_on_the_current_one(system):
    mind, _memory, _lifecycle, _source, _clock = system
    habits = ConversationHabits(mind)
    first = habit_case(system, {"exploration_paused": True})
    with mind.engine.db.connect(write=True) as conn:
        applied = habits.apply(conn, first, "appraisal-c:habits", revise=True)
    assert applied["revision"] == 1 and "supersedes" not in applied
    changed = first.model_copy(update={"preferences": {"exploration_paused": False}})
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        habits.apply(conn, changed, "appraisal-c:habits", revise=True)
    assert str(refused.value) == "Conversation preferences changed"
    with mind.engine.db.connect(write=True) as conn:
        revised = habits.apply(conn, changed.model_copy(update={"expected_revision": 1}),
                               "appraisal-c:habits", revise=True)
    assert revised["revision"] == 2 and revised["supersedes"] == "appraisal-c:habits"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_habit_revisions WHERE scope=?",
                            (mind.scope.key(),)).fetchone()[0] == 2
        assert habits.read(conn)["preferences"]["exploration_paused"] is False
        # Each command keeps the precondition it took effect under, beside its fingerprint.
        stored = {row["supersedes"]: json.loads(row["precondition"])
                  for row in conn.execute("SELECT * FROM mind_command_fingerprints WHERE family='habit'")}
        assert stored == {None: {"expected_revision": 0}, "appraisal-c:habits": {"expected_revision": 1}}


def test_a_client_supplied_habit_command_id_is_still_refused_when_its_content_changes(system):
    mind, _memory, _lifecycle, _source, _clock = system
    habits = ConversationHabits(mind)
    first = habit_case(system, {"exploration_paused": True})
    with mind.engine.db.connect(write=True) as conn:
        applied = habits.apply(conn, first, "client-command")
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        habits.apply(conn, first.model_copy(update={"preferences": {"exploration_paused": False},
                                                    "expected_revision": 1}), "client-command")
    assert str(refused.value) == "Habit command changed"
    assert classify(refused.value).code == "payload-changed"
    with mind.engine.db.connect(write=True) as conn:
        # The same content with a revision that moved on is the same command, not a new one.
        assert habits.apply(conn, first.model_copy(update={"expected_revision": 1}), "client-command") == applied


def test_with_the_flag_off_a_changed_route_stays_a_flat_refusal(system, appended):
    mind, memory, lifecycle, _source, _clock = system
    route, _later, proof, applied = appended
    memory.configure({"idempotency_fingerprint": False})
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict) as refused:
        lifecycle.apply_routes(conn, [route.model_copy(update={"reason": "另一个理由"})],
                               proof, "appraisal-b", revise=True)
    assert str(refused.value) == "Event route command changed"
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        # And a refreshed expected revision is a different command again, as before stage 2.
        moved = memory.graph.get(conn, applied["event_id"])["revision"]
        lifecycle.apply_routes(conn, [route.model_copy(update={"expected_revision": moved})],
                               proof, "appraisal-b", revise=True)


# --- plans, graph corrections and methods ------------------------------------------------

def test_a_plan_change_retried_after_rereading_the_plan_keeps_its_receipt(env):
    mind, plans, _source, _clock, initial = env
    plan = create(env)
    change = {"command_id": "refine-goal", "action": "update", "id": plan["id"],
              "expected_revision": plan["revision"], "goal": "Make a small illustrated clock with a label",
              "reason": "A sourced refinement", "evidence_ids": [initial]}
    first = plans.manage(change)
    again = plans.manage({**change, "expected_revision": first["revision"]})
    assert again == first and plans.read(identifier=plan["id"])["plans"][0]["revision"] == first["revision"]
    with pytest.raises(Conflict) as refused:
        plans.manage({**change, "expected_revision": first["revision"], "goal": "An entirely different goal"})
    assert str(refused.value) == "A command ID cannot be reused for a different change"
    assert classify(refused.value).code == "payload-changed"


def test_a_graph_correction_retried_after_rereading_the_node_keeps_its_receipt(system):
    mind, memory, lifecycle, _source, _clock = system
    first, src, proof = sourced_route(system, "first", "星图报告最初版本还未发送。")
    with mind.engine.db.connect(write=True) as conn:
        created = lifecycle.apply_routes(conn, [first], proof, "appraisal-a")[0]
        node = memory.graph.get(conn, created["event_id"])
    correction = {"command_id": "retitle", "action": "correct", "id": node["id"],
                  "expected_revision": node["revision"], "reason": "标题写得不准确",
                  "evidence_ids": [src["id"]], "changes": {"title": "星图报告（初稿）"}}
    applied = memory.graph.revise(correction)
    moved = memory.graph.detail(node["id"])["revision"]
    assert memory.graph.revise({**correction, "expected_revision": moved}) == applied
    with pytest.raises(Conflict) as refused:
        memory.graph.revise({**correction, "expected_revision": moved, "changes": {"title": "别的标题"}})
    assert str(refused.value) == "Graph command ID reused with other data"
    assert classify(refused.value).code == "payload-changed"
    assert memory.graph.detail(node["id"])["title"] == "星图报告（初稿）"


def test_a_method_proposal_with_the_same_id_and_other_content_is_refused(env):
    """This family compared nothing at all: the same command id returned the stored method
    whatever the candidate said."""
    mind, _plans, _source, _clock, initial = env
    methods, first, ids = propose_method(env)
    candidate = {"key": "repeatable-computation", "title": "Compute a total",
                 "applicable_when": "Inputs are finite numeric rows",
                 "steps": ["Validate inputs", "Compute and compare an independent total"],
                 "tools": ["python"], "environment": {"python": "fixture"},
                 "success_criteria": "Computed total matches the independent result",
                 "evidence_ids": [initial], "result_ids": ids,
                 "reason": "Actual outcomes support a candidate"}
    receipt = {"provider": "deepseek", "reasoning": "high"}
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [initial])
        assert methods.propose(conn, candidate, "method-1", receipt, refs) == first
        # Evidence order writes nothing, so re-supplying the same set is the same command.
        assert methods.propose(conn, {**candidate, "result_ids": ids, "evidence_ids": [initial]},
                               "method-1", receipt, refs) == first
        with pytest.raises(Conflict) as refused:
            methods.propose(conn, {**candidate, "steps": ["Skip validation", "Guess a total"]},
                            "method-1", receipt, refs)
    assert str(refused.value) == "Procedure command changed"
    assert classify(refused.value).code == "payload-changed"
    assert methods.read(identifier=first["id"])["procedures"][0]["steps"] == candidate["steps"]
