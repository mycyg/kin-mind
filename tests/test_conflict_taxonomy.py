"""Conflict taxonomy (stage 2 WP1): every static host conflict on the commit path is
registered, the two conflated sites are split, a version that moved counts as progress,
and a conflict raised before any model call is neither charged nor retried for ever."""

import ast
import importlib
import io
import json
import pathlib
import sys
import time

import pytest
from test_appraisal_retry_policy import Failing, requeue, saved
from test_operational_recovery import Provider

from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.models import SourceInput
from kin_mind.appraisal import MAX_PREPARATION_CONFLICTS, Appraisals, error_detail
from kin_mind.autonomy_schema import optimized
from kin_mind.conflicts import COMMIT_PATH_MODULES, HANDLINGS, KINDS, REGISTRY, classify, reusable
from kin_mind.memory import MemoryAssessment, MemoryNote

pytest_plugins = ("test_memory_continuity",)

STAGE_TWO_FLAGS = ("attempt_ledger", "idempotency_fingerprint", "manifest_rebase", "appraisal_reuse",
                   "appraisal_revalidation", "model_lanes", "semantic_cache_v2", "memory_item_isolation")


def raised_conflicts(module):
    """(line, message) of every Conflict/Missing raised with a static literal."""
    path = pathlib.Path(importlib.import_module(module).__file__)
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        called = node.exc.func
        called = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
        if called not in {"Conflict", "Missing", "Deleted"} or not node.exc.args:
            continue
        literal = node.exc.args[0]
        # A formatted message carries no static text, so only its keywords classify it.
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
            found.append((node.lineno, literal.value))
    return found


def test_every_conflict_literal_on_the_commit_path_is_registered():
    """A new raise in a commit-path module has to say what kind of conflict it is."""
    unregistered, registered, sites = [], set(), 0
    for module in COMMIT_PATH_MODULES:
        for line, message in raised_conflicts(module):
            sites += 1
            registered.add(message)
            if message not in REGISTRY:
                unregistered.append(module + ":" + str(line))
    assert unregistered == []
    # The design brief counted 97 sites reachable from run_one; the modules that contain
    # them hold these, and the registry covers all of them, not only the reachable ones.
    assert sites >= 170
    # No stale rows either: a message that no longer exists must leave the table.
    assert set(REGISTRY) - registered == set()


def test_the_registry_keeps_one_kind_per_code_and_blocks_are_never_reusable():
    for message, (kind, code, handling) in REGISTRY.items():
        assert kind in KINDS and handling in HANDLINGS and code == code.lower(), message
        if handling in {"block", "terminal", "section"}:
            assert not reusable(Conflict(message, kind=kind, code=code))
    kinds = {}
    for _message, (kind, code, handling) in REGISTRY.items():
        assert kinds.setdefault(code, kind) == kind
    # A host-blocked fault never reaches a reuse path, whatever its kind.
    assert REGISTRY["Semantic evidence is outside the evaluated source set"][2] == "block"
    assert REGISTRY["Coverage mapping is outside evaluated delivery evidence"][2] == "block"
    # An unclassified failure is treated conservatively, never as reusable.
    assert not reusable(RuntimeError("deepseek-timeout"))
    assert classify(RuntimeError("deepseek-timeout")).kind == "unknown"


def test_a_stale_citation_is_not_an_out_of_bounds_one_in_plan_evidence(system):
    """plans._refs used to raise one message for two faults with opposite handling."""
    mind, _memory, source, _clock = system
    from kin_mind.plans import AutonomousPlans

    plans = AutonomousPlans(mind)
    cited = source("plan-evidence")
    with mind.engine.db.connect() as conn:
        supplied = mind._evidence(conn, [cited])
        shown = [{**ref, "revision": ref["revision"] + 1} for ref in supplied]
        with pytest.raises(Conflict) as outside:
            plans._refs(conn, [cited], allowed=[])
        with pytest.raises(Conflict) as stale:
            plans._refs(conn, [cited], allowed=shown)
    out, moved = classify(outside.value), classify(stale.value)
    assert (out.kind, out.code, out.handling) == ("semantic", "evidence-out-of-bounds", "block")
    assert (moved.kind, moved.code, moved.handling) == ("runtime", "cited-evidence-changed", "reuse")
    assert str(outside.value) != str(stale.value)
    detail = error_detail(stale.value, "Conflict")
    assert (detail["expected"], detail["actual"]) == (supplied[0]["revision"] + 1, supplied[0]["revision"])
    assert not reusable(outside.value) and reusable(stale.value)


def test_a_stale_citation_is_not_an_out_of_bounds_one_in_memory_evidence(system):
    """The same split at memory.apply_assessment, the other conflated site."""
    mind, memory, source, _clock = system
    cited = source("memory-evidence")
    with mind.engine.db.connect() as conn:
        supplied = mind._evidence(conn, [cited])
    assessment = MemoryAssessment(notes=[MemoryNote(key="note", title="A synthetic note",
                                                    content="A synthetic note body", evidence_ids=[supplied[0]["record_id"]])])

    def commit(allowed):
        with mind.engine.db.connect(write=True) as conn:
            memory.apply_assessment(conn, assessment, allowed, "mind_taxonomy_fixture", 0, 20, {"model": "fixture"})

    with pytest.raises(Conflict) as outside:
        commit([])
    # A correction of the same source: the citation is still in bounds, but out of date.
    mind.engine.receive(SourceInput(namespace="synthetic", key="memory-evidence", version="2",
                                    text="A corrected synthetic source", scope=mind.scope, occurred_at=mind.clock(),
                                    metadata={"role": "user", "host_event": "message"}))
    with pytest.raises(Conflict) as stale:
        commit(supplied)
    out, moved = classify(outside.value), classify(stale.value)
    assert (out.kind, out.code, out.handling) == ("semantic", "evidence-out-of-bounds", "block")
    assert (moved.kind, moved.code, moved.handling) == ("runtime", "cited-evidence-changed", "reuse")
    assert str(outside.value) != str(stale.value)


def conflicting(actual):
    """One mind-revision conflict; only `actual` separates two of them."""
    try:
        raise Conflict("Mind revision changed; read current state before updating",
                       code="mind-revision-changed", target="synthetic-scope", expected=1, actual=actual)
    except Conflict as error:
        return error


def test_a_runtime_conflict_whose_actual_moved_is_progress_not_a_repeat(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([conflicting(2), conflicting(3)])
    job = jobs.enqueue([source("moving")], "fixture-v1")
    for attempt in (1, 2):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        assert (result["state"], result["attempts"]) == ("pending", attempt)
    stored = saved(mind, job["id"])
    # Two competing revisions are two different failures, so neither quarantines the row.
    assert stored["error_repeats"] == 1 and stored["error_detail"]["actual"] == 3
    assert stored["error_detail"]["kind"] == "runtime"


def test_the_same_runtime_conflict_twice_still_quarantines(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([conflicting(2), conflicting(2)])
    job = jobs.enqueue([source("stuck")], "fixture-v1")
    for expected in ("pending", "needs-repair"):
        requeue(mind, job["id"])
        assert Appraisals(mind).run_one(provider, lane="action")["state"] == expected
    assert saved(mind, job["id"])["repair_reason"] == "repeated-failure:mind-revision-changed"


def test_missing_root_evidence_supersedes_the_appraisal_without_a_model_call(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Failing([RuntimeError("deepseek-http-500")])
    root = source("erased", "A synthetic private note PRIVATE_FIXTURE_DO_NOT_COPY")
    child = jobs.enqueue([source("absorbed")], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    parent = jobs.enqueue([root], "fixture-v1", origin="reflection", stimulus="delivery")["id"]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (0, parent))
        conn.execute("UPDATE mind_appraisals SET available=? WHERE id=?", (1, child))
    # One pass absorbs the child; the outage is uncharged and leaves the batch in place.
    assert jobs.run_one(provider, lane="action")["state"] == "pending"
    calls = len(provider.calls)
    mind.engine.delete(root)
    requeue(mind, parent)
    result = Appraisals(mind).run_one(provider, lane="action")
    assert result["state"] == "superseded"
    assert len(provider.calls) == calls
    assert result["error_detail"]["code"] == "root-evidence-unavailable"
    assert result["error_detail"]["kind"] == "runtime"
    assert "PRIVATE_FIXTURE" not in dumps(result) and root not in dumps(result["error_detail"])
    with mind.engine.db.connect() as conn:
        released = conn.execute("SELECT state,available FROM mind_appraisals WHERE id=?", (child,)).fetchone()
    assert released["state"] == "pending" and released["available"] <= time.time()
    # A terminal row is never claimed again, so nothing more is paid for.
    assert Appraisals(mind).run_one(provider, lane="action", job_id=parent) == {"state": "idle"}


def test_a_preparation_conflict_is_uncharged_and_bounded_by_its_own_counter(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    provider = Provider()
    job = jobs.enqueue([source("prepared")], "fixture-v1", stimulus="idle-review")
    # A correction of the evidence this row was enqueued for: preparation cannot finish.
    mind.engine.receive(SourceInput(namespace="synthetic", key="prepared", version="2",
                                    text="A corrected synthetic source", scope=mind.scope, occurred_at=mind.clock(),
                                    metadata={"role": "user", "host_event": "message"}))
    for wait in range(1, MAX_PREPARATION_CONFLICTS + 1):
        requeue(mind, job["id"])
        result = Appraisals(mind).run_one(provider, lane="action")
        assert (result["state"], result["attempts"]) == ("pending", 0)
        assert result["preparation_conflicts"] == wait
    requeue(mind, job["id"])
    result = Appraisals(mind).run_one(provider, lane="action")
    assert (result["state"], result["attempts"]) == ("needs-repair", 0)
    assert result["repair_reason"] == "preparation-conflicts-exhausted:" + str(MAX_PREPARATION_CONFLICTS + 1)
    assert result["error_detail"]["code"] == "source-needs-review"
    assert provider.calls == []


def test_a_commit_by_another_attempt_completes_this_one_from_its_receipt(system):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("raced")], "fixture-v1")
    committed = {"event_id": "mind_fixture_committed_elsewhere", "revision": 9}

    class Racing(Provider):
        def appraise(self, context):
            result = super().appraise(context)
            # What a parallel worker leaves behind when its commit lands first: the
            # durable receipt of this very appraisal, written under another payload.
            with mind.engine.db.connect(write=True) as conn:
                conn.execute("INSERT OR IGNORE INTO commands VALUES(?,?,?)",
                             (mind._key(job["id"]), "another-attempt-digest", dumps(committed)))
            return result

    provider = Racing()
    revision = mind.read()["revision"]
    result = jobs.run_one(provider, lane="action")
    assert result["state"] == "complete" and result["result"] == committed
    assert result["completed_from"] == "already-committed"
    # No second call, and the judgment that committed is not scored again.
    assert len(provider.calls) == 1 and mind.read()["revision"] == revision
    assert "error" not in result and "error_detail" not in result


def test_conflict_and_missing_take_the_same_optional_facts(system):
    _mind, *_ = system
    plain, detailed = Conflict("Evidence is not current"), Missing(
        "Evidence source is unavailable", kind="runtime", code="root-evidence-unavailable", target="src_x")
    assert str(plain) == "Evidence is not current" and plain.args == ("Evidence is not current",)
    assert (plain.kind, plain.code, plain.target, plain.expected, plain.actual) == (None, None, None, None, None)
    assert str(detailed) == "Evidence source is unavailable" and detailed.target == "src_x"
    assert classify(detailed) == ("runtime", "root-evidence-unavailable", "terminal")
    assert not isinstance(detailed, Conflict)


def test_the_host_error_output_carries_the_code_and_kind(monkeypatch, capsys, tmp_path):
    from kin_mind import host

    def refuse(*_args, **_kwargs):
        raise Conflict("Coverage mapping is outside evaluated delivery evidence")

    monkeypatch.setattr(host, "load_config", lambda path: {})
    monkeypatch.setattr(host, "dispatch", refuse)
    monkeypatch.setattr(sys, "argv", ["host", "--config", str(tmp_path / "config.json"), "review"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    host.main()
    # Additive: the class stays what every caller reads today.
    assert json.loads(capsys.readouterr().out) == {
        "error": "Conflict", "kind": "semantic", "code": "evidence-out-of-bounds"}


def test_every_stage_two_flag_is_registered_and_defaults_to_on(system):
    mind, memory, *_ = system
    settings = memory.settings()
    assert all(settings[flag] is True for flag in STAGE_TWO_FLAGS)
    with mind.engine.db.connect() as conn:
        assert all(optimized(conn, mind.scope.key(), flag) for flag in STAGE_TWO_FLAGS)
    memory.configure({flag: False for flag in STAGE_TWO_FLAGS})
    with mind.engine.db.connect() as conn:
        assert not any(optimized(conn, mind.scope.key(), flag) for flag in STAGE_TWO_FLAGS)
    with pytest.raises(ValueError):
        memory.configure({"attempt_ledger": "on"})
