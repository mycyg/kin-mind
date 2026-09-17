"""Input manifest, Tier A (reuse without a model call) and Tier B (light revalidation), stage 2 WP4.

Synthetic replays only: an injected clock, scripted providers or httpx.MockTransport. No model or
network call. "Another writer" is a side effect of the scripted model call, which is exactly where a
concurrent commit lands in production: after the context was built and before the commit.
"""
import hashlib
import json
import time
from datetime import timedelta

import pytest
from test_attempt_ledger import Api, metrics, purposes, rows, saved
from test_plan_review_loop import RECEIPT, Env, later
from test_section_isolation import enable_continuity

from eventmem.core.db import Conflict, dumps
from eventmem.core.models import SourceInput
from kin_mind import manifest as manifests
from kin_mind import revalidation
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.host import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.recovery import recover_quarantined
from kin_mind.state import AffectiveEvent, DesireChange

pytest_plugins = ("test_memory_continuity",)

SWITCHES = ("manifest_rebase", "appraisal_reuse", "appraisal_revalidation")
MOOD = {"reason": "A synthetic current judgment", "values": {"mood": 61}, "next_review_minutes": 25}
NEW_WISH = {"content": "Tell her what the sundial showed", "topic": "sundial", "kind": "contact",
            "strength": 60, "ttl_hours": 24, "completion": "She has heard it"}
OTHER_WISH = {"content": "Ask her which clock face she prefers", "topic": "clock", "kind": "contact",
              "strength": 55, "ttl_hours": 24, "completion": "She has answered"}


def keep_all(request):
    return {"items": [{"conflict_id": c["conflict_id"], "verdict": "keep", "reason": "The judgment still holds"}
                      for c in request["conflicts"]]}


def answer(verdicts, default="keep"):
    """A revalidation answer by conflict object: {object prefix: verdict or (verdict, patch)}."""
    def build(request):
        items = []
        for conflict in request["conflicts"]:
            chosen = next((v for prefix, v in verdicts.items() if conflict["object"].startswith(prefix)), default)
            verdict, patch = chosen if isinstance(chosen, tuple) else (chosen, None)
            items.append({"conflict_id": conflict["conflict_id"], "verdict": verdict, "reason": "A synthetic verdict",
                          **({"patch": patch} if patch else {})})
        return {"items": items}
    return build


class Model(Api):
    """The scripted endpoint of test_attempt_ledger, with two additions: an answer may be computed
    from the request, and `during[tool]` runs once while that tool's call is in flight."""

    def __init__(self, monkeypatch, responses, engine, during=None):
        super().__init__(monkeypatch, responses, engine)
        self.during = {tool: list(hooks) for tool, hooks in (during or {}).items()}

    def respond(self, request):
        body = json.loads(request.content)
        tool = body["tools"][0]["name"]
        if self.during.get(tool):
            self.during[tool].pop(0)()
        if self.responses and callable(self.responses[0]):
            self.responses[0] = self.responses[0](json.loads(body["messages"][0]["content"]))
        return super().respond(request)

    def sent(self, tool):
        return [r["content"] for r in self.requests if r["tool"] == tool]


class Scripted:
    """A provider without `structured`: it can appraise, and nothing else."""

    def __init__(self, proposal, during=None):
        self.proposal, self.during, self.calls = proposal, list(during or []), []

    def appraise(self, context):
        self.calls.append(context)
        if self.during:
            self.during.pop(0)()
        return Appraisal.model_validate(self.proposal), dict(RECEIPT)


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    # Noon in the contact timezone, a day ahead of every source: no quiet-hour edge is ever near.
    # (A whole second would print without its fraction, and the engine compares validity as text.)
    env.clock[0] = (env.clock[0] + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=1)
    return env


def jobs_of(mind, **extra):
    return Appraisals(mind, **extra)


def run(mind, provider, job, **extra):
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job,))
    return jobs_of(mind, **extra).run_one(provider, job_id=job)


def history_commit(mind, version, evidence, key="history-commit"):
    """What a history-lane commit does to the mind state: the revision moves and nothing the model is shown does."""
    return mind._mutate({"command_id": key, "agent_version": version, "expected_revision": mind.read()["revision"],
                         "evidence_ids": [evidence]}, "memory-history", lambda conn, state, event_id: {})


def tiers(mind):
    return [(m["tier"], m["reasons"]) for m in metrics(mind, "appraisal_tier")]


def manifest_rows(mind):
    with mind.engine.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_appraisal_manifests'").fetchone():
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM mind_appraisal_manifests").fetchall()]


def no_manifest_fault(mind):
    return metrics(mind, "appraisal_manifest_failed") == []


# --- The dependency table ------------------------------------------------------------------------


def test_relevance_follows_the_judgment_type_and_unknowns_are_relevant():
    history, maintenance = manifests.judgment_type("memory-enrichment"), manifests.judgment_type("session-maintenance")
    assert history == manifests.judgment_type("memory-backfill") == manifests.HISTORY
    # History is shown no mood, wishes, plans, methods, timing or session.
    for name in ("dimensions", "desires", "concerns", "rhythm", "plans", "procedures", "time", "session"):
        assert not manifests.relevant(history, name)
    for name in ("graph", "topics", "works", "shares", "dialogue", "habits"):
        assert manifests.relevant(history, name)
    # A session judgment rests on the session (and its roots, which are always compared).
    assert [name for name in manifests.CLASSES if manifests.relevant(maintenance, name)] == ["session"]
    assert not manifests.relevant("delivery", "concerns") and not manifests.relevant("delivery", "rhythm")
    assert manifests.relevant("delivery", "desires") and manifests.relevant("delivery", "dimensions")
    # Everything else, a judgment type nobody registered and a class nobody registered: relevant.
    for judgment in ("interaction", "idle-review", "plan-review", "a-judgment-type-added-later"):
        assert all(manifests.relevant(judgment, name) for name in manifests.CLASSES)
    for judgment in (history, maintenance, "delivery", "interaction"):
        assert manifests.relevant(judgment, "a-class-added-later")


def test_the_write_set_names_what_a_proposal_writes_and_never_what_it_only_reads():
    proposal = Appraisal.model_validate({
        "reason": "r", "values": {"mood": 60}, "motivations": {"curiosity": {"target": 70, "half_life_minutes": 60, "reason": "r"}},
        "wish_updates": [{"desire_id": "desire_a", "action": "wait", "reason": "r", "concern_ids": ["concern_read"]}],
        "plan_changes": [{"action": "create", "key": "new", "goal": "g", "motivation": "m", "reason": "r", "evidence_ids": ["src_a"],
                          "steps": [{"id": "s", "actor": "create", "goal": "g", "completion": "c"}]},
                         {"action": "pause", "id": "plan_b", "expected_revision": 3, "reason": "r", "evidence_ids": ["src_a"]}],
        "action_decisions": [{"plan_id": "new", "step_id": "s", "expected_revision": 1, "action": "wait", "reason": "r", "evidence_ids": ["src_a"]},
                             {"plan_id": "plan_c", "step_id": "make", "expected_revision": 2, "action": "wait", "reason": "r", "evidence_ids": ["src_a"]}],
    }).model_dump()
    assert manifests.write_set(proposal) == {("dimensions", "mood"), ("dimensions", "curiosity"), ("desires", "desire_a"),
                                             ("plans", "plan_b"), ("plans", "plan_c/make")}


# --- What a manifest holds -----------------------------------------------------------------------


def test_the_manifest_is_content_addressed_and_holds_no_text(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    memory.ingest({"id": "owner-says", "kind": "owner-message", "at": mind.clock(), "text": "PRIVATE_FIXTURE owner words"})
    mind.manage_desire(DesireChange(command_id="wish", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("wish-evidence")], action="create", content="PRIVATE_FIXTURE wish text", topic="PRIVATE_FIXTURE topic",
        kind="contact", strength=40, expires_at=later_of(mind, hours=30), completion="PRIVATE_FIXTURE done",
        reason="PRIVATE_FIXTURE reason"))
    seen = mind.read()["dimensions"]["mood"]["projected_value"]
    api = Model(monkeypatch, [MOOD], mind.engine)
    job = jobs_of(mind).enqueue([source("evidence", "PRIVATE_FIXTURE evidence text")], "fixture-v1")["id"]
    assert run(mind, api.provider, job)["state"] == "complete" and no_manifest_fault(mind)
    stored = saved(mind, job)
    # The queue row keeps the digest; `evaluated_sources` is still written for recover_batched.
    assert len(stored["manifest"]) == 64 and stored["proposal_manifest"] == stored["manifest"] and stored["evaluated_sources"]
    assert "reuse" not in stored and "tier" not in stored
    manifest = manifests.load(mind.engine, mind.scope.key(), stored["manifest"])
    assert manifest["version"] == manifests.MANIFEST_VERSION
    assert manifest["judgment"] == {"type": "interaction-batch", "lane": "action", "stimulus": "interaction-batch"}
    assert manifest["built_at"] == manifest["time"]["clock"] and manifest["valid_until"] > manifest["built_at"]
    # What frames the request: the prompt, the schema, the model and its parameters.
    assert all(len(manifest["request"][k]) == 64 for k in ("system", "schema", "parameters", "context"))
    assert manifest["request"]["model"] == "deepseek-flash" and manifest["policy"]["profile_version"] == mind.read()["profile_version"]
    assert manifest["owner"]["latest_owner_seq"] == manifest["owner"]["owner_epoch"] >= 1
    # Roots and sources as [source, hash, revision] by record; per dimension who wrote it and its curve.
    assert list(manifest["roots"]) == [r["record_id"] for r in stored["evaluated_sources"] if r["source_id"] in stored["evidence_ids"]]
    assert set(manifest["roots"]) <= set(manifest["sources"])
    mood = manifest["classes"]["dimensions"]["mood"]
    assert set(mood) == {"event_id", "target", "baseline", "half_life_hours", "needs_review", "motivation", "at"}
    # The values the model saw are kept beside the clock it saw them at, not among the class facts.
    assert manifest["time"]["values"]["dimensions"]["mood"] == seen != mind.read()["dimensions"]["mood"]["projected_value"]
    desire, = [v for k, v in manifest["classes"]["desires"].items() if k != "#window"]
    assert (desire["status"], desire["revision"], desire["expired"]) == ("wanted", 1, False)
    assert ["desire-expiry"] == [b[0] for b in manifest["time"]["boundaries"] if b[0].startswith("desire")]
    assert manifest["classes"]["dialogue"] and manifest["classes"]["habits"]["habits"]["revision"] == 0
    # Identifiers, revisions, digests, states and numbers: never a message body or a wish's words.
    assert "PRIVATE_FIXTURE" not in dumps(manifest_rows(mind))
    # The same manifest is the same row, and a class that did not move is shared between manifests.
    assert manifests.store(mind.engine, mind.scope.key(), manifest) == stored["manifest"]
    second = jobs_of(mind).enqueue([source("later-evidence")], "fixture-v1")["id"]
    assert run(mind, Model(monkeypatch, [dict(MOOD, values={})], mind.engine).provider, second)["state"] == "complete"
    parts = [r for r in manifest_rows(mind) if r["lane"] == manifests.PART]
    assert len([r for r in manifest_rows(mind) if r["lane"] != manifests.PART]) == 2
    assert len([r for r in parts if r["judgment"] == "classes/habits"]) == 1 and len([r for r in parts if r["judgment"] == "sources"]) == 2


def later_of(mind, **delta):
    from kin_mind.state import timestamp
    return (timestamp(mind.clock()) + timedelta(**delta)).isoformat()


# --- Tier B: the read set moved, the write set is clean -------------------------------------------


def delivering(mind, source):
    """A contact wish the model is shown as `wanted`, already handed to the transport."""
    wish = mind.manage_desire(DesireChange(command_id="older-wish", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("older-wish-evidence")], action="create", content="Tell her what the sundial showed", topic="sundial",
        kind="contact", strength=80, expires_at=later_of(mind, days=2), completion="The platform accepts it", reason="A finding to share"))
    mind.record(AffectiveEvent(command_id="drive", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
                               evidence_ids=[source("drive-evidence")], values={"initiative": 90}, reason="A synthetic drive"))
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=attempt["id"], state="pending")
    return wish["desire_id"], lambda: mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="synthetic-platform-id")


def test_a_changed_read_set_with_a_clean_write_set_is_revalidated_not_reused(system, monkeypatch):
    """The model saw a wish as `wanted`; the transport delivered it while the model was thinking. The
    proposal only adds a wish, so nothing it writes was touched, and the previous rebase committed it."""
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, values={}, wishes=[OTHER_WISH]), keep_all], mind.engine, during={"submit_appraisal": [delivered]})
    first = run(mind, api.provider, job)
    assert (first["state"], first["attempts"]) == ("pending", 1) and first["error_detail"]["code"] == "mind-revision-changed"
    kept = saved(mind, job)["reuse"]
    assert kept["n"] == 0 and kept["manifest_digest"] == saved(mind, job)["manifest"] and kept["proposal"]["wishes"][0]["topic"] == "clock"
    assert kept["conflict"] == {k: first["error_detail"][k] for k in ("class", "kind", "code", "target", "expected", "actual")}
    with mind.engine.db.connect() as conn:
        available = conn.execute("SELECT available FROM mind_appraisals WHERE id=?", (job,)).fetchone()[0]
    assert time.time() < available <= time.time() + revalidation.LIGHT_RETRY_SECONDS

    second = run(mind, api.provider, job)
    assert second["state"] == "complete" and second["tier"] == "B" and no_manifest_fault(mind)
    # One full call and one light one; the light attempt is not a charged attempt.
    assert api.tools == ["submit_appraisal", "revalidate_appraisal"] and second["attempts"] == 1
    assert tiers(mind) == [("B", ["read-set-changed"])]
    request, = api.sent("revalidate_appraisal")
    assert request["stored_proposal"]["wishes"][0]["topic"] == "clock" and request["commit_conflict"]["code"] == "mind-revision-changed"
    assert len(request["recent_dialogue"]) <= 8 and request["clock"]["authority"] == "host-clock"
    moved = {c["object"]: c for c in request["conflicts"]}
    # Nothing of the proposal names that wish: it is part of what moved among the wishes it was shown,
    # and the sections resting on wishes are what a patch may touch.
    desires = moved["desires:*"]
    assert (desires["before"][wish]["status"], desires["after"][wish]["status"]) == ("wanted", "completed")
    assert desires["after"][wish]["revision"] == desires["before"][wish]["revision"] + 1
    assert desires["after"][wish]["content"]["topic"] == "sundial" and [f["path"] for f in desires["relevant_fragment"]] == [["wishes"]]
    assert "timing:*" in moved and [c["conflict_id"] for c in request["conflicts"]] == ["c" + str(i + 1) for i in range(len(moved))]
    # The same apply(): the wish exists, its receipt is the original one, marked as revalidated.
    view = mind.read()
    assert [d["status"] for d in view["desires"] if d["topic"] == "clock"] == ["wanted"]
    receipt = saved(mind, job)["receipt"]
    assert receipt["request_id"] == "submit_appraisal-1" and receipt["reuse"]["tier"] == "B"
    assert [v["verdict"] for v in receipt["reuse"]["verdicts"]] == ["keep"] * len(request["conflicts"])
    assert "reuse" not in saved(mind, job)
    failed, revalidated = sorted(rows(mind, job), key=lambda r: r["ordinal"])
    assert (failed["outcome"], failed["charged"], purposes(failed)) == ("failed", True, ["appraise"])
    assert (revalidated["outcome"], revalidated["charged"], revalidated["tier"]) == ("revalidated", False, "B")
    assert purposes(revalidated) == ["revalidate"] and revalidated["calls"][0]["tool"] == "revalidate_appraisal"
    assert [v["verdict"] for v in revalidated["revalidation"]] == ["keep"] * len(request["conflicts"])
    # Verdicts and digests reach the ledger; a reason or a wish's words never do.
    assert "synthetic" not in dumps(revalidated["revalidation"]).lower() and "clock face" not in dumps([failed, revalidated])


def test_the_previous_rebase_would_have_committed_that_proposal_at_once(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True, "manifest_rebase": False})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, values={}, wishes=[OTHER_WISH])], mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "complete" and api.tools == ["submit_appraisal"]


def test_adjust_patches_one_fragment_and_leaves_every_other_section_byte_identical(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    proposal = dict(MOOD, values={"mood": 58, "sharing": 66}, wishes=[NEW_WISH, OTHER_WISH])
    # The delivered wish already said what the first new wish wanted to say: only that one is withdrawn.
    patch = {"path": ["wishes"], "value": [OTHER_WISH]}
    api = Model(monkeypatch, [proposal, answer({"desires:": ("adjust", patch)})], mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    stored = saved(mind, job)["reuse"]["proposal"]
    assert run(mind, api.provider, job)["state"] == "complete"
    committed = saved(mind, job)["result"]["proposal"]
    assert [w["topic"] for w in committed["wishes"]] == ["clock"]
    for section in stored:
        if section != "wishes":
            assert dumps(committed[section]) == dumps(stored[section]), section
    view = mind.read()
    assert view["dimensions"]["mood"]["value"] == 58 and [d["topic"] for d in view["desires"] if d["status"] == "wanted"] == ["clock"]
    assert [i["patched"] for i in saved(mind, job)["revalidation"]["items"] if i["verdict"] == "adjust"] == [True]


@pytest.mark.parametrize("reply,code", [
    (lambda request: {"items": []}, "conflict-unanswered"),
    (lambda request: {"items": [dict(i, conflict_id="c99") for i in keep_all(request)["items"]]}, "unknown-conflict"),
    (answer({"desires:": "append"}), "route-verdict-outside-event-route"),
    (answer({"desires:": ("keep", {"path": ["wishes"], "value": []})}), "patch-without-adjust"),
    (answer({"desires:": "adjust"}), "adjust-without-patch"),
    (answer({"desires:": ("adjust", {"path": ["values"], "value": {"mood": 1}})}), "patch-out-of-scope"),
    (answer({"desires:": ("adjust", {"path": ["wishes"], "value": [{"content": "No kind, no expiry"}]})}), "patched-proposal-invalid"),
])
def test_an_answer_the_host_refuses_means_a_full_rerun_and_no_repair_call(system, monkeypatch, reply, code):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, wishes=[OTHER_WISH]), reply, dict(MOOD, values={"mood": 64})], mind.engine,
                during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    refused = run(mind, api.provider, job)
    assert (refused["state"], refused["attempts"]) == ("pending", 1)
    assert refused["error"] == "deepseek-revalidation-rejected:" + code and "reuse" not in saved(mind, job)
    assert saved(mind, job)["revalidation"]["refused"] == code
    assert run(mind, api.provider, job)["state"] == "complete"
    # No repair of the refused answer: the third call is a whole appraisal, told what the host refused.
    assert api.tools == ["submit_appraisal", "revalidate_appraisal", "submit_appraisal"]
    assert api.sent("submit_appraisal")[1]["previous_attempt"]["code"] == refused["error"]
    assert "previous_attempt" not in api.sent("submit_appraisal")[0] and mind.read()["dimensions"]["mood"]["value"] == 64
    assert [(r["outcome"], r.get("tier"), r["charged"]) for r in sorted(rows(mind, job), key=lambda r: r["ordinal"])] == [
        ("failed", None, True), ("failed", "B", False), ("committed", None, True)]


def test_replan_means_a_full_rerun(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, wishes=[NEW_WISH]), answer({"desires:": "replan"}), dict(MOOD, values={"mood": 52})],
                mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    replanned = run(mind, api.provider, job)
    assert (replanned["state"], replanned["error"], replanned["attempts"]) == ("pending", "deepseek-revalidation-replan", 1)
    assert "reuse" not in saved(mind, job)
    final = run(mind, api.provider, job)
    assert (final["state"], final["attempts"]) == ("complete", 2)
    assert api.tools == ["submit_appraisal", "revalidate_appraisal", "submit_appraisal"]
    # The judgment that committed is the new one: the withdrawn wish was never created.
    view = mind.read()
    assert view["dimensions"]["mood"]["value"] == 52 and not [d for d in view["desires"] if d["status"] == "wanted"]


# --- Tier A: nothing but bookkeeping moved --------------------------------------------------------


def bookkeeping(env, plan):
    """Review time, lease and a receipt nobody is shown: three writes, none of them an input."""
    def write():
        with env.mind.engine.db.connect(write=True) as conn:
            refs = env.mind._evidence(conn, [env.initial])
            # An unchanged wait is recorded without a plan revision: only next_review_at moves.
            env.plans.decide(conn, {"plan_id": plan["id"], "step_id": "make", "expected_revision": plan["revision"], "action": "wait",
                                    "reason": "Nothing new", "evidence_ids": [env.initial], "next_review_at": later(env, days=9)},
                             "bookkeeping-review", {**RECEIPT, "agent_version": env.version}, refs, unchanged_view=True)
            conn.execute("INSERT INTO mind_model_leases VALUES('synthetic-lease','background',?,'{}')", (time.time() + 90,))
        # A history-lane commit: the mind revision moves, and nothing the model is shown does.
        history_commit(env.mind, env.version, env.initial)
    return write


def waiting_plan(env, **step):
    plan = env.create(steps=[{"id": "make", "actor": "create", "goal": "Make the clock", "completion": "A verified SVG is saved", **step}])
    return env.decide(plan, "wait", next_review_at=later(env, days=30))


def test_bookkeeping_only_changes_reuse_the_stored_proposal_without_a_model_call(env):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    provider = Scripted(MOOD, during=[bookkeeping(env, plan)])
    first = run(env.mind, provider, job)
    assert (first["state"], first["attempts"]) == ("pending", 1) and first["error_detail"]["code"] == "mind-revision-changed"
    assert env.current(plan)["revision"] == plan["revision"] and env.current(plan)["next_review_at"] != plan["next_review_at"]
    second = run(env.mind, provider, job)
    assert (second["state"], second["tier"], second["attempts"]) == ("complete", "A", 1) and no_manifest_fault(env.mind)
    # Zero model calls for the second attempt, and the same apply() committed the stored proposal.
    assert len(provider.calls) == 1 and tiers(env.mind) == [("A", [])]
    assert env.mind.read()["dimensions"]["mood"]["value"] == 61
    failed, reused = sorted(rows(env.mind, job), key=lambda r: r["ordinal"])
    assert (failed["outcome"], failed["charged"]) == ("failed", True)
    assert (reused["outcome"], reused["charged"], reused["calls"], reused["tier"]) == ("reused", False, [], "A")
    assert reused["manifest_digest"] != failed["manifest_digest"]
    stored = env.job(job)
    assert stored["receipt"]["reuse"]["tier"] == "A" and "reuse" not in stored and "previous_attempt" not in provider.calls[0]


def test_a_decision_that_names_its_plan_by_key_is_still_reused(env):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    decision = {"plan_id": "idea", "step_id": "make", "expected_revision": plan["revision"], "action": "wait", "reason": "Still waiting",
                "evidence_ids": [env.initial], "next_review_at": later(env, days=20), "strength": 35}
    provider = Scripted(dict(MOOD, action_decisions=[decision]), during=[bookkeeping(env, plan)])
    assert run(env.mind, provider, job)["state"] == "pending"
    second = run(env.mind, provider, job)
    assert (second["state"], second["tier"]) == ("complete", "A") and len(provider.calls) == 1
    assert env.step(plan)["strength"] == 35 and "held_decisions" not in env.job(job)


def test_a_proposal_that_writes_what_it_was_never_shown_is_not_reused(env):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    update = {"desire_id": "desire_" + "0" * 32, "action": "wait", "reason": "A wish the model was never shown"}
    provider = Scripted(dict(MOOD, wish_updates=[update]), during=[bookkeeping(env, plan)])
    assert run(env.mind, provider, job)["state"] == "pending"
    assert run(env.mind, provider, job)["state"] == "complete" and len(provider.calls) == 2
    # Nothing moved, yet nobody can prove that wish untouched: no reuse, and nothing to ask about either.
    assert tiers(env.mind) == [("full", ["write-set-unverifiable", "revalidation-unavailable"])]


def test_new_owner_input_is_never_reused_without_a_question(env):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    def owner_writes():
        bookkeeping(env, plan)()
        env.owner_message("owner-says-something-new")
    provider = Scripted(MOOD, during=[owner_writes])
    assert run(env.mind, provider, job)["state"] == "pending"
    assert run(env.mind, provider, job)["state"] == "complete"
    (tier, reasons), = tiers(env.mind)
    # This provider cannot revalidate, so the owner's words are judged by a whole new appraisal.
    assert tier == "full" and "owner-input" in reasons and "revalidation-unavailable" in reasons and len(provider.calls) == 2
    assert provider.calls[1]["previous_attempt"]["code"] == "mind-revision-changed"


def test_new_owner_input_goes_to_revalidation_when_the_provider_can(env, monkeypatch):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    def owner_writes():
        bookkeeping(env, plan)()
        env.owner_message("owner-says-something-new")
    api = Model(monkeypatch, [MOOD, keep_all], env.mind.engine, during={"submit_appraisal": [owner_writes]})
    assert run(env.mind, api.provider, job)["state"] == "pending"
    assert run(env.mind, api.provider, job)["state"] == "complete" and api.tools == ["submit_appraisal", "revalidate_appraisal"]
    request, = api.sent("revalidate_appraisal")
    owner = next(c for c in request["conflicts"] if c["object"] == "owner:input")
    assert owner["after"]["owner_epoch"] > owner["before"]["owner_epoch"]
    assert any(turn["text"] == "owner-says-something-new" for turn in request["recent_dialogue"])


def persona(env):
    policy = {"schema": 1, "version": "synthetic-v2", "scope": env.mind.scope.model_dump(), "approved_source": "owner-request",
              "requires_owner_confirmation": True, "core": "【MY_PERSONA_LOAD】 SYNTHETIC\n【/MY_PERSONA_LOAD】",
              "voice": "Use complete sentences.", "maintenance": "Preserve source quotes.", "mutable_trait_keys": ["interests"]}
    for key in ("core", "voice", "maintenance"):
        policy[key + "_sha256"] = hashlib.sha256(policy[key].encode()).hexdigest()
    (env.mind.engine.db.root / "persona-policy.json").write_text(json.dumps(policy))


@pytest.mark.parametrize("change", ["persona", "configuration", "setting"])
def test_a_persona_policy_or_configuration_change_is_a_full_rerun(env, monkeypatch, change):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    def changed():
        bookkeeping(env, plan)()
        if change == "persona":
            persona(env)
        elif change == "configuration":
            env.upgrade("planning-v2")
        else:
            env.memory.configure({"procedure_learning": True})
    # The provider could revalidate: a changed system prompt is still never a light question.
    api = Model(monkeypatch, [MOOD, dict(MOOD, values={"mood": 47})], env.mind.engine, during={"submit_appraisal": [changed]})
    assert run(env.mind, api.provider, job)["state"] == "pending"
    assert run(env.mind, api.provider, job, exploration_capabilities={})["state"] == "complete"
    assert api.tools == ["submit_appraisal", "submit_appraisal"] and tiers(env.mind) == [("full", ["policy-changed"])]
    assert env.mind.read()["dimensions"]["mood"]["value"] == 47


# --- Validity -------------------------------------------------------------------------------------


def test_validity_expires_by_the_injected_clock(env):
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    provider = Scripted(MOOD, during=[bookkeeping(env, plan)])
    assert run(env.mind, provider, job)["state"] == "pending"
    manifest = manifests.load(env.mind.engine, env.mind.scope.key(), env.job(job)["manifest"])
    # One ordinary attempt's bound: what a single attempt already accepts, and not a second more.
    assert manifest["time"]["bound_seconds"] == manifests.attempt_bound(provider) == 180
    assert manifest["valid_until"] == later(env, seconds=180)
    env.clock[0] += timedelta(seconds=181)
    assert run(env.mind, provider, job)["state"] == "complete"
    assert tiers(env.mind) == [("full", ["validity-expired", "revalidation-unavailable"])] and len(provider.calls) == 2


def test_a_shown_boundary_nearer_than_the_bound_ends_validity_first(system):
    mind, memory, source, clock = system
    memory.configure({"semantic": False})
    mind.manage_desire(DesireChange(command_id="expiring", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("expiring-evidence")], action="create", content="A wish about to expire", topic="soon", kind="explore",
        strength=30, expires_at=later_of(mind, seconds=60), completion="Looked into", reason="An older idea"))
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    provider = Scripted(MOOD, during=[lambda: history_commit(mind, "fixture-v1", source("history-evidence"))])
    assert run(mind, provider, job)["state"] == "pending"
    manifest = manifests.load(mind.engine, mind.scope.key(), saved(mind, job)["manifest"])
    assert manifest["valid_until"] == later_of(mind, seconds=60) and manifest["time"]["boundaries"][0][0] == "desire-expiry"
    clock[0] += timedelta(seconds=61)
    assert run(mind, provider, job)["state"] == "complete" and len(provider.calls) == 2
    (tier, reasons), = tiers(mind)
    assert tier == "full" and "validity-expired" in reasons


def test_a_window_still_open_with_two_minutes_left_instead_of_two_hours_is_not_reused(env, monkeypatch):
    plan = waiting_plan(env, not_after=later(env, hours=2))
    job = env.enqueue("owner-chat")
    api = Model(monkeypatch, [MOOD, keep_all], env.mind.engine, during={"submit_appraisal": [bookkeeping(env, plan)]})
    assert run(env.mind, api.provider, job)["state"] == "pending"
    env.clock[0] += timedelta(minutes=118)
    # No predicate flipped: the window is as open as it was, and no curve parameter changed.
    assert env.step(plan)["waiting_reason"] == "step-waiting"
    assert run(env.mind, api.provider, job)["state"] == "complete"
    assert api.tools == ["submit_appraisal", "revalidate_appraisal"] and tiers(env.mind) == [("B", ["validity-expired"])]
    request, = api.sent("revalidate_appraisal")
    moved, = request["conflicts"]
    assert moved["object"] == "time:clock" and moved["kind"] == "time"
    window = lambda side: next(b for b in moved[side]["boundaries"] if b["kind"] == "step-window-closes")
    # The values the model saw are what the question is about: two hours then, two minutes now.
    assert (window("before")["remaining_seconds"], window("after")["remaining_seconds"]) == (7200, 120)
    assert window("before")["object"] == plan["id"] + "/make" and moved["after"]["clock"] > moved["before"]["clock"]


# --- keep: only the conflicting expectation is reset, and the same apply() decides -----------------


def test_keep_resets_only_the_conflicting_expectation_and_commits_through_the_same_apply(env, monkeypatch):
    env.memory.configure({"appraisal_section_isolation": False})
    paused, untouched = env.create(key="paused"), env.create(key="untouched")
    owner = env.source("owner-asks-to-pause-exploring")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    change = lambda plan: {"action": "pause", "id": plan["id"], "expected_revision": plan["revision"], "reason": "Pause it", "evidence_ids": [owner]}
    proposal = dict(MOOD, habits={"preferences": {"exploration_paused": True}, "evidence_ids": [owner], "reason": "She asked", "expected_revision": 0},
                    plan_changes=[change(paused), change(untouched)])
    def others_write():
        # Someone else moves both compare-and-swap targets of this proposal while the model thinks.
        env.memory.habits.update({"command_id": "another-preference", "preferences": {"reply_choice": "autonomous"},
                                  "evidence_ids": [env.source("owner-allows-silence")], "reason": "She said so", "expected_revision": 0})
        env.decide(paused, "wait", next_review_at=later(env, days=3))
    api = Model(monkeypatch, [proposal, answer({"habits:": "keep", "plans:": "wait"})], env.mind.engine, during={"submit_appraisal": [others_write]})
    first = run(env.mind, api.provider, job)
    assert first["state"] == "pending" and first["error_detail"]["code"] in {"plan-revision-changed", "habits-revision-changed"}
    assert run(env.mind, api.provider, job)["state"] == "complete" and api.tools == ["submit_appraisal", "revalidate_appraisal"]
    request, = api.sent("revalidate_appraisal")
    habits = next(c for c in request["conflicts"] if c["object"] == "habits:habits")
    assert (habits["before"]["revision"], habits["after"]["revision"]) == (0, 1) and habits["before"]["content"] == {}
    assert habits["after"]["content"]["reply_choice"] == "autonomous" and [f["path"] for f in habits["relevant_fragment"]] == [["habits"]]
    assert {c["object"] for c in request["conflicts"] if c["object"].startswith("plans:")} == {"plans:" + paused["id"], "plans:" + paused["id"] + "/make"}
    committed = env.job(job)["result"]["proposal"]
    # Kept: its expectation is the current revision now. Waiting: withdrawn, and nothing of it was reset.
    # What nobody touched is what the model wrote, to the byte: the host resets nothing it was not answered for.
    assert committed["habits"]["expected_revision"] == 1 and committed["plan_changes"] == [Appraisal.model_validate(
        {"reason": "r", "plan_changes": [change(untouched)]}).model_dump()["plan_changes"][0]]
    preferences = env.memory.habits.read()
    assert preferences["revision"] == 2 and preferences["preferences"]["exploration_paused"] is True
    assert env.current(paused)["status"] == "active" and env.current(untouched)["status"] == "paused"
    assert env.mind.read()["dimensions"]["mood"]["value"] == 61


def test_stale_evidence_inside_a_patch_is_refused_by_the_same_apply(env, monkeypatch):
    enable_continuity(env)
    plan = waiting_plan(env)
    corrected = env.source("an-older-remark")
    job = env.enqueue("owner-chat")
    understanding = {"meaning": "She asked about the clock", "topic": "clock", "importance": 50, "confidence": 0.9, "basis": "inferred"}
    def others_write():
        bookkeeping(env, plan)()
        env.owner_message("owner-says-something-new")
        # A correction of that remark: citing its old version is citing evidence that is no longer current.
        env.mind.engine.receive(SourceInput(namespace="plan-review-test", key="an-older-remark", version="2", text="A corrected remark",
            scope=env.mind.scope, authority="explicit", occurred_at=env.mind.clock(), metadata={"role": "user", "host_event": "message"}))
    patch = {"path": ["understanding"], "value": {**understanding, "evidence_ids": [corrected]}}
    api = Model(monkeypatch, [dict(MOOD, understanding=understanding), answer({"owner:": ("adjust", patch)})], env.mind.engine,
                during={"submit_appraisal": [others_write]})
    assert run(env.mind, api.provider, job)["state"] == "pending"
    before = env.mind.read()["revision"]
    refused = run(env.mind, api.provider, job)
    # DeepSeek cannot declare old evidence valid: the host's own check refuses the patched proposal.
    assert (refused["state"], refused["attempts"]) == ("pending", 1) and refused["error"] in {"Conflict", "Missing"}
    assert refused["error_detail"]["kind"] in {"runtime", "semantic"} and env.mind.read()["revision"] == before
    assert env.mind.read()["dimensions"]["mood"]["value"] != 61


# --- What never reaches a revalidation ------------------------------------------------------------


def unregistered(*_args, **_kwargs):
    raise Conflict("A synthetic conflict nobody registered")


@pytest.mark.parametrize("fault,code", [("out-of-bounds", "evidence-out-of-bounds"), ("authority", "insufficient-authority"),
                                        ("lease", "lease-lost"), ("unknown", None)])
def test_blocked_and_unknown_conflicts_never_reach_revalidation(env, monkeypatch, fault, code):
    env.memory.configure({"appraisal_section_isolation": False})
    owner, elsewhere = env.source("owner-chat"), env.source("never-supplied-to-this-evaluation")
    job = Appraisals(env.mind).enqueue([owner], env.version)["id"]
    proposal = dict(MOOD)
    if fault == "out-of-bounds":
        proposal["habits"] = {"preferences": {"exploration_paused": True}, "evidence_ids": [elsewhere], "reason": "r", "expected_revision": 0}
    elif fault == "authority":
        inferred = env.source("an-assistant-remark", authority="model")
        job = Appraisals(env.mind).enqueue([owner, inferred], env.version)["id"]
        proposal["habits"] = {"preferences": {"exploration_paused": True}, "evidence_ids": [inferred], "reason": "r", "expected_revision": 0}
    elif fault == "unknown":
        monkeypatch.setattr(MemoryContinuity, "commit_action", unregistered, raising=True)
        monkeypatch.setattr("kin_mind.state.Mind._apply_event", unregistered)
    def lease_expires():
        with env.mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET lease=1 WHERE id=?", (job,))
    api = Model(monkeypatch, [proposal, MOOD], env.mind.engine, during={"submit_appraisal": [lease_expires] if fault == "lease" else []})
    first = run(env.mind, api.provider, job)
    assert first["state"] == "pending" and first["error_detail"].get("code") == code
    assert first["error_detail"]["kind"] == ("unknown" if fault == "unknown" else first["error_detail"]["kind"])
    # The host decides these alone: nothing is kept for reuse, and the next attempt is a whole appraisal.
    assert "reuse" not in env.job(job)
    monkeypatch.undo()
    monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
    assert run(env.mind, api.provider, job)["state"] == "complete"
    assert api.tools == ["submit_appraisal", "submit_appraisal"] and tiers(env.mind) == []


def test_a_lease_lost_before_the_light_call_ends_as_lease_lost_and_nothing_is_paid_for(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, wishes=[OTHER_WISH])], mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    listed = revalidation.conflict_list

    def taken_over(*args, **kwargs):
        # Another worker claims the row while this attempt is still comparing manifests.
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET data=json_set(data,'$.attempt_token','someone-else') WHERE id=?", (job,))
        return listed(*args, **kwargs)
    monkeypatch.setattr(revalidation, "conflict_list", taken_over)
    run(mind, api.provider, job)
    # The host's own block, before any light call: the question is never sent, and this worker's attempt is discarded.
    assert api.tools == ["submit_appraisal"]
    discarded = max(rows(mind, job), key=lambda r: r["ordinal"])
    assert (discarded["outcome"], discarded["calls"], discarded["error_detail"]["code"]) == ("discarded", [], "lease-lost")
    assert saved(mind, job)["attempt_token"] == "someone-else" and "reuse" in saved(mind, job)


# --- Charging and the light budget ----------------------------------------------------------------


def test_a_spent_light_budget_means_a_full_rerun_and_the_stage_one_caps_still_quarantine(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True, "max_charged_attempts": 2})
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    writes = iter(range(10))
    def another_judgment():
        n = next(writes)
        mind.record(AffectiveEvent(command_id="parallel-" + str(n), agent_version="fixture-v1", expected_revision=mind.read()["revision"],
                                   evidence_ids=[source("parallel-" + str(n))], values={"focus": 40 + n}, reason="A synthetic parallel judgment"))
    api = Model(monkeypatch, [MOOD, keep_all, keep_all, MOOD], mind.engine,
                during={"submit_appraisal": [another_judgment, another_judgment], "revalidate_appraisal": [another_judgment, another_judgment]})
    expected = [("pending", 1, 0), ("pending", 1, 1), ("pending", 1, 2), ("needs-repair", 2, None)]
    for state, attempts, spent in expected:
        result = run(mind, api.provider, job)
        assert (result["state"], result["attempts"]) == (state, attempts), result
        assert (saved(mind, job).get("reuse") or {}).get("n") == spent
    # One full call, two light ones, one full call: then the cap of charged attempts, as in stage 1.
    assert api.tools == ["submit_appraisal", "revalidate_appraisal", "revalidate_appraisal", "submit_appraisal"]
    assert [t for t, _ in tiers(mind)] == ["B", "B", "full"] and tiers(mind)[-1][1] == ["light-budget-exhausted"]
    stored = saved(mind, job)
    assert stored["repair_reason"] == "charged-attempts-exhausted:2" and stored["light_attempts"] == 2
    assert [(r["outcome"], r["charged"]) for r in sorted(rows(mind, job), key=lambda r: r["ordinal"])] == [
        ("failed", True), ("failed", False), ("failed", False), ("quarantined", True)]
    assert metrics(mind, "appraisal_quarantined")[0]["charged_attempts"] == 2
    # A light failure is a wait of fifteen seconds, not an exponential backoff.
    assert run(mind, api.provider, job) == {"state": "idle"}


def test_a_provider_outage_during_the_light_call_keeps_the_stored_proposal(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, wishes=[OTHER_WISH]), 503, keep_all], mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    outage = run(mind, api.provider, job)
    assert (outage["state"], outage["attempts"], outage["transient_failures"]) == ("pending", 1, 1)
    assert saved(mind, job)["reuse"]["n"] == 0
    assert run(mind, api.provider, job)["state"] == "complete"
    assert api.tools == ["submit_appraisal", "revalidate_appraisal", "revalidate_appraisal"]


# --- Recovery, and the switches -------------------------------------------------------------------


def test_recover_appraisals_drops_what_was_kept_for_reuse(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    _wish, delivered = delivering(mind, source)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [dict(MOOD, wishes=[OTHER_WISH]), dict(MOOD, values={"mood": 49})], mind.engine, during={"submit_appraisal": [delivered]})
    assert run(mind, api.provider, job)["state"] == "pending"
    with mind.engine.db.connect(write=True) as conn:
        # An operator set it aside before any retry ran: the proposal kept for reuse is still on the row.
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (job,))
    assert saved(mind, job)["reuse"]["proposal"]
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "fixture-v1", "session_id": "synthetic"}
    assert dispatch(config, "recover-appraisals", {"job_ids": [job], "command_id": "wp4-resume", "source": "owner approval"})["resumed"] == [job]
    resumed = saved(mind, job)
    assert not {"reuse", "tier", "revalidation", "light_attempts"} & set(resumed)
    assert resumed["recovery_history"][0]["proposed_result"]["wishes"]
    assert run(mind, api.provider, job)["state"] == "complete"
    # Judged afresh: a whole appraisal, never the proposal the row was quarantined with.
    assert api.tools == ["submit_appraisal", "submit_appraisal"] and tiers(mind) == []
    assert mind.read()["dimensions"]["mood"]["value"] == 49 and recover_quarantined


def test_with_all_three_switches_off_a_conflict_is_retried_exactly_as_in_stage_one(env):
    env.memory.configure({name: False for name in SWITCHES})
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    provider = Scripted(MOOD, during=[bookkeeping(env, plan)])
    first = run(env.mind, provider, job)
    assert (first["state"], first["attempts"]) == ("pending", 1)
    stored = env.job(job)
    assert not {"manifest", "proposal_manifest", "evaluated_continuity", "reuse", "tier"} & set(stored) and manifest_rows(env.mind) == []
    with env.mind.engine.db.connect() as conn:
        available = conn.execute("SELECT available FROM mind_appraisals WHERE id=?", (job,)).fetchone()[0]
    assert time.time() + 50 < available <= time.time() + 60
    second = run(env.mind, provider, job)
    assert (second["state"], second["attempts"]) == ("complete", 2) and len(provider.calls) == 2
    assert provider.calls[1]["previous_attempt"]["code"] == "mind-revision-changed" and tiers(env.mind) == []
    assert "reuse" not in env.job(job)["receipt"] and [r["outcome"] for r in rows(env.mind, job)] == ["committed", "failed"]


@pytest.mark.parametrize("off,calls,tier", [("appraisal_reuse", 2, "full"), ("appraisal_revalidation", 1, "A")])
def test_each_switch_turns_off_its_own_tier(env, off, calls, tier):
    env.memory.configure({off: False})
    plan = waiting_plan(env)
    job = env.enqueue("owner-chat")
    provider = Scripted(MOOD, during=[bookkeeping(env, plan)])
    assert run(env.mind, provider, job)["state"] == "pending"
    assert run(env.mind, provider, job)["state"] == "complete"
    assert len(provider.calls) == calls and [t for t, _ in tiers(env.mind)] == [tier]


def test_a_bookkeeping_commit_is_rebased_inside_the_transaction_and_a_real_one_is_not(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    quiet = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [MOOD], mind.engine, during={"submit_appraisal": [lambda: history_commit(mind, "fixture-v1", source("history-evidence"))]})
    done = run(mind, api.provider, quiet)
    # A6, narrowed: the revision moved and nothing this judgment wrote or was shown did.
    assert (done["state"], done["attempts"]) == ("complete", 1) and api.tools == ["submit_appraisal"]
    loud = jobs_of(mind).enqueue([source("owner-chat-again")], "fixture-v1")["id"]
    def scored_elsewhere():
        mind.record(AffectiveEvent(command_id="parallel", agent_version="fixture-v1", expected_revision=mind.read()["revision"],
                                   evidence_ids=[source("parallel")], values={"focus": 12}, reason="A synthetic parallel judgment"))
    # `focus` is not written by this proposal, but the model was shown it: the read set moved.
    api = Model(monkeypatch, [dict(MOOD, values={"mood": 44})], mind.engine, during={"submit_appraisal": [scored_elsewhere]})
    refused = run(mind, api.provider, loud)
    assert refused["state"] == "pending" and refused["error_detail"]["code"] == "mind-revision-changed"
    assert saved(mind, loud)["reuse"]["conflict"]["code"] == "mind-revision-changed"


# --- The historical lane, the seed it always had, and what was recalled ----------------------------


def graph_moves(*_args, **_kwargs):
    # A registered, reusable commit conflict; `actual` moves each time, as a real race would.
    graph_moves.actual += 1
    raise Conflict("Graph revision changed", target="graph_event_synthetic", expected=1, actual=graph_moves.actual)


def test_the_historical_lane_makes_one_full_call_two_light_attempts_and_one_full_call(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    graph_moves.actual = 1
    monkeypatch.setattr(MemoryContinuity, "apply_assessment", graph_moves)
    job = jobs_of(mind).enqueue([source("an-older-day")], "fixture-v1", stimulus="memory-enrichment")["id"]
    said = lambda: memory.ingest({"id": "a-later-reply", "kind": "assistant-message", "at": mind.clock(), "text": "A later public reply"})
    api = Model(monkeypatch, [{"reason": "An older day, organized"}, keep_all, {"reason": "An older day, organized again"}], mind.engine)
    expected = [("pending", 1, None), ("pending", 1, "A"), ("pending", 1, "B"), ("needs-repair", 2, None)]
    for index, (state, attempts, tier) in enumerate(expected):
        if index == 2:
            said()  # The dialogue a history judgment is shown moved: the second light attempt asks.
        result = jobs_of(mind).run_one(api.provider, lane="enrichment", job_id=requeued(mind, job))
        assert (result["state"], result["attempts"], result.get("tier")) == (state, attempts, tier), result
    assert api.tools == ["submit_appraisal", "revalidate_appraisal", "submit_appraisal"]
    assert [t for t, _ in tiers(mind)] == ["A", "B", "full"] and tiers(mind)[1][1] == ["read-set-changed"]
    # Stage 1's cap for this lane still ends it: the second charged failure is quarantined.
    assert saved(mind, job)["repair_reason"] == "Conflict" and "reuse" not in saved(mind, job)
    assert [(r["outcome"], r["charged"]) for r in sorted(rows(mind, job), key=lambda r: r["ordinal"])] == [
        ("failed", True), ("failed", False), ("failed", False), ("quarantined", True)]


def requeued(mind, job):
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job,))
    return job


def test_the_same_unexplained_conflict_twice_is_not_reused_a_third_time(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True, "max_charged_attempts": 3})

    def stuck(*_args, **_kwargs):
        raise Conflict("Graph revision changed", target="graph_event_synthetic", expected=1, actual=2)
    monkeypatch.setattr(MemoryContinuity, "commit_action", stuck)
    job = jobs_of(mind).enqueue([source("owner-chat")], "fixture-v1")["id"]
    api = Model(monkeypatch, [MOOD, MOOD], mind.engine)
    assert run(mind, api.provider, job)["state"] == "pending"
    assert run(mind, api.provider, job)["tier"] == "A" and saved(mind, job)["reuse"]["repeated"] is True
    # Nothing moved and the proposal met the very same conflict again: it is judged afresh instead of
    # being committed a third time, and stage 1's rule for a repeated charged failure ends the row.
    third = run(mind, api.provider, job)
    assert api.tools == ["submit_appraisal", "submit_appraisal"] and tiers(mind)[-1] == ("full", ["conflict-repeats"])
    assert (third["state"], third["repair_reason"]) == ("needs-repair", "repeated-failure:graph-revision-changed")


def test_a_seed_without_a_manifest_commits_as_before_and_is_ledgered_as_reused(system):
    from kin_mind.recovery import recover_history
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    job = jobs_of(mind).enqueue([source("quarantined")], "fixture-v1", stimulus="memory-backfill")["id"]
    with mind.engine.db.connect(write=True) as conn:
        data = saved(mind, job) | {"error": "Conflict", "proposed_result": Appraisal(reason="Original result").model_dump(),
                                   "receipt": {"model": "deepseek-flash"}}
        conn.execute("UPDATE mind_appraisals SET state='needs-repair',attempts=7,data=? WHERE id=?", (dumps(data), job))
    recover_history(mind, job_ids=[job], command_id="owner-approved", source="An approved historical repair", workers_stopped=True)
    assert "seed_manifest" not in saved(mind, job)

    class Unused:
        def appraise(self, _context):
            raise AssertionError("A valid saved result needs no model request")
    assert jobs_of(mind).run_one(Unused(), lane="enrichment", job_id=job)["state"] == "complete"
    attempt, = rows(mind, job)
    assert (attempt["outcome"], attempt["calls"], attempt["charged"]) == ("reused", [], True) and tiers(mind) == []


def test_a_recovered_seed_with_its_own_manifest_is_compared_like_any_reuse(system, monkeypatch):
    from kin_mind.recovery import recover_history
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    graph_moves.actual = 1
    monkeypatch.setattr(MemoryContinuity, "apply_assessment", graph_moves)
    job = jobs_of(mind).enqueue([source("an-older-day")], "fixture-v1", stimulus="memory-enrichment")["id"]
    api = Model(monkeypatch, [{"reason": "An older day, organized"}, keep_all], mind.engine)
    assert run(mind, api.provider, job)["state"] == "pending"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='needs-repair' WHERE id=?", (job,))
    monkeypatch.undo()
    monkeypatch.setenv("SYNTHETIC_KEY", "test-only-key")
    recover_history(mind, job_ids=[job], command_id="owner-approved", source="An approved historical repair", workers_stopped=True)
    resumed = saved(mind, job)
    # The conflict's own record is gone; the approved seed carries the manifest its judgment rests on.
    assert "reuse" not in resumed and resumed["seed_manifest"] == resumed["proposal_manifest"]
    memory.ingest({"id": "a-later-reply", "kind": "assistant-message", "at": mind.clock(), "text": "A later public reply"})
    assert run(mind, api.provider, job)["state"] == "complete"
    # What a history judgment is shown has moved since, so the seed is no longer taken on trust.
    assert api.tools == ["submit_appraisal", "revalidate_appraisal"] and tiers(mind) == [("B", ["read-set-changed"])]
    assert [r["outcome"] for r in sorted(rows(mind, job), key=lambda r: r["ordinal"])] == ["failed", "revalidated"]


def last_week(env):
    """An explicit owner remark old enough for recall, whose validity follows the real clock."""
    return env.mind.engine.receive(SourceInput(namespace="plan-review-test", key="owner-asked-to-pause-exploring-last-week",
        text="owner-asked-to-pause-exploring-last-week", scope=env.mind.scope, authority="explicit",
        occurred_at=(env.clock[0] - timedelta(days=8)).isoformat(), metadata={"role": "user", "host_event": "message"}))["id"]


def test_evidence_the_first_attempt_recalled_stays_in_bounds_when_its_proposal_is_reused(env, monkeypatch):
    env.memory.configure({"appraisal_section_isolation": False})
    plan = waiting_plan(env)
    older = last_week(env)
    job = env.enqueue("owner-chat")
    recall = {"query": "what she said about exploring", "reason": "Needed", "mode": "light", "identifiers": [older]}
    habits = {"preferences": {"exploration_paused": True}, "evidence_ids": [older], "reason": "She asked last week", "expected_revision": 0}
    api = Model(monkeypatch, [dict(MOOD, recall_needs=[recall]), dict(MOOD, habits=habits)], env.mind.engine,
                during={"submit_appraisal": [lambda: None, bookkeeping(env, plan)]})
    first = run(env.mind, api.provider, job)
    assert first["state"] == "pending" and first["error_detail"]["code"] == "mind-revision-changed"
    kept = env.job(job)["reuse"]
    assert older in {ref["source_id"] for ref in kept["sources"]}
    second = run(env.mind, api.provider, job)
    # The rebuilt context never recalled it. Merged back from the stored sources, it is still in bounds.
    assert (second["state"], second["tier"]) == ("complete", "A") and api.tools == ["submit_appraisal", "submit_appraisal"]
    assert env.memory.habits.read()["preferences"]["exploration_paused"] is True
    assert older in {ref["source_id"] for ref in env.job(job)["evaluated_sources"]}


def test_recalled_evidence_that_has_moved_since_is_not_merged_back(env, monkeypatch):
    env.memory.configure({"appraisal_section_isolation": False})
    plan = waiting_plan(env)
    older = last_week(env)
    job = env.enqueue("owner-chat")
    recall = {"query": "what she said about exploring", "reason": "Needed", "mode": "light", "identifiers": [older]}
    habits = {"preferences": {"exploration_paused": True}, "evidence_ids": [older], "reason": "She asked last week", "expected_revision": 0}
    def corrected():
        bookkeeping(env, plan)()
        env.mind.engine.receive(SourceInput(namespace="plan-review-test", key="owner-asked-to-pause-exploring-last-week", version="2",
            text="She corrected it", scope=env.mind.scope, authority="explicit", occurred_at=env.mind.clock(),
            metadata={"role": "user", "host_event": "message"}))
    api = Model(monkeypatch, [dict(MOOD, recall_needs=[recall]), dict(MOOD, habits=habits), keep_all, dict(MOOD, values={"mood": 50})],
                env.mind.engine, during={"submit_appraisal": [lambda: None, corrected]})
    assert run(env.mind, api.provider, job)["state"] == "pending"
    second = run(env.mind, api.provider, job)
    # DeepSeek kept it, and may: what it cannot do is make the superseded source valid. The host refuses it.
    assert (second["state"], second["tier"], second["attempts"]) == ("pending", "B", 1) and tiers(env.mind) == [("B", ["cited-evidence-changed"])]
    request, = api.sent("revalidate_appraisal")
    cited, = request["conflicts"]
    assert cited["object"].startswith("sources:mem_") and [f["path"] for f in cited["relevant_fragment"]] == [["habits"]]
    assert (cited["before"]["content"], cited["after"]["content"]) == ("owner-asked-to-pause-exploring-last-week", "She corrected it")
    assert second["error_detail"]["code"] == "insufficient-authority" and "reuse" not in env.job(job)
    assert env.memory.habits.read()["revision"] == 0
    assert run(env.mind, api.provider, job)["state"] == "complete" and api.tools[-1] == "submit_appraisal"


def test_memory_set_aside_for_enrichment_survives_a_reused_action_proposal(system, monkeypatch):
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True})
    evidence = source("owner-chat")
    job = jobs_of(mind).enqueue([evidence], "fixture-v1")["id"]
    note = {"key": "note", "title": "A synthetic note", "content": "What the day was about", "evidence_ids": [evidence]}
    committed, calls = MemoryContinuity.commit_action, []

    def once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Conflict("Graph revision changed", target="graph_event_synthetic", expected=1, actual=2)
        return committed(self, *args, **kwargs)
    monkeypatch.setattr(MemoryContinuity, "commit_action", once)
    # A scripted provider may return memory on the action lane; the host sets it aside for enrichment.
    provider = Scripted(dict(MOOD, memory={"notes": [note]}))
    assert run(mind, provider, job)["state"] == "pending"
    kept = saved(mind, job)["reuse"]
    assert kept["proposal"]["memory"]["notes"] == [] and kept["deferred_memory"]["notes"][0]["key"] == "note"
    second = run(mind, provider, job)
    assert (second["state"], second["tier"]) == ("complete", "A") and len(provider.calls) == 1
    with mind.engine.db.connect() as conn:
        enrichment, = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_appraisals WHERE json_extract(data,'$.parent_id')=?", (job,))]
    assert enrichment["stimulus"] == "memory-enrichment" and enrichment["seed_memory"]["notes"][0]["key"] == "note"


# --- Judgment types that rest on less --------------------------------------------------------------


SESSION = {"id": "snapshot-1", "binding": {"generation": 1}, "lastCompaction": {"id": "compact-1", "completedAt": 100},
           "evidence": [{"id": "observed-1", "at": 200, "text": "SYNTHETIC OBSERVATION TEXT"}],
           "recent": [{"id": "turn-1", "role": "user", "text": "SYNTHETIC OWNER WORDS about the clock"}]}
ADVICE = {"reason": "Review", "session_advice": {"action": "keep", "reason": "Nothing degraded"}}


def maintenance_job(env):
    return Appraisals(env.mind, session_context=SESSION).enqueue([env.source("host-asks-for-a-session-review")], env.version,
                                                                 origin="reflection", stimulus="session-maintenance")["id"]


def test_a_session_judgment_rests_on_the_session_alone(env):
    job = maintenance_job(env)
    def the_mind_moves():
        # A mood scored elsewhere and an owner message: neither is anything a session judgment is shown.
        env.mind.record(AffectiveEvent(command_id="parallel", agent_version=env.version, expected_revision=env.mind.read()["revision"],
                                       evidence_ids=[env.source("parallel")], values={"mood": 12}, reason="A synthetic parallel judgment"))
    provider = Scripted(ADVICE, during=[the_mind_moves])
    assert run(env.mind, provider, job, session_context=SESSION)["state"] == "pending"
    second = run(env.mind, provider, job, session_context=SESSION)
    assert (second["state"], second["tier"]) == ("complete", "A") and len(provider.calls) == 1
    manifest = manifests.load(env.mind.engine, env.mind.scope.key(), env.job(job)["manifest"])
    assert manifest["judgment"]["type"] == "session-maintenance" and manifest["classes"]["session"]["session"]["id"] == "snapshot-1"
    assert env.mind.read()["session_advice"]["decision"]["action"] == "keep"


def test_a_session_judgment_is_not_reused_once_the_session_itself_moved(env):
    job = maintenance_job(env)
    provider = Scripted(ADVICE, during=[lambda: history_commit(env.mind, env.version, env.initial)])
    assert run(env.mind, provider, job, session_context=SESSION)["state"] == "pending"
    later_session = {**SESSION, "evidence": [*SESSION["evidence"], {"id": "observed-2", "at": 300, "text": "A NEWER OBSERVATION"}]}
    assert run(env.mind, provider, job, session_context=later_session)["state"] == "complete"
    assert tiers(env.mind) == [("full", ["read-set-changed", "revalidation-unavailable"])] and len(provider.calls) == 2


def test_a_receipt_settlement_does_not_rest_on_concerns_or_rhythm(env):
    enable_continuity(env)
    from kin_mind.continuity import ConcernChange
    job = Appraisals(env.mind).enqueue([env.source("a-delivery-receipt")], env.version, origin="reflection", stimulus="delivery")["id"]
    def a_concern_appears():
        env.mind.manage_concern(ConcernChange(command_id="a-new-concern", agent_version=env.version, expected_revision=env.mind.read()["revision"],
            evidence_ids=[env.source("owner-mentions-an-interview")], action="create", key="interview", kind="care", content="An interview tomorrow",
            topic="interview", intensity=60, basis="explicit", confidence=0.9, reason="She mentioned it"))
    provider = Scripted(dict(MOOD, values={"sharing": 30}), during=[a_concern_appears])
    assert run(env.mind, provider, job)["state"] == "pending"
    second = run(env.mind, provider, job)
    assert (second["state"], second["tier"]) == ("complete", "A") and len(provider.calls) == 1
    # The same change is a changed read set for a judgment that is shown concerns.
    other = env.enqueue("owner-chat")
    def another_concern():
        env.mind.manage_concern(ConcernChange(command_id="another-concern", agent_version=env.version, expected_revision=env.mind.read()["revision"],
            evidence_ids=[env.source("owner-mentions-a-trip")], action="create", key="trip", kind="anticipation", content="A trip next week",
            topic="trip", intensity=40, basis="explicit", confidence=0.9, reason="She mentioned it"))
    provider = Scripted(MOOD, during=[another_concern])
    assert run(env.mind, provider, other)["state"] == "pending"
    assert run(env.mind, provider, other)["state"] == "complete" and len(provider.calls) == 2
    assert tiers(env.mind)[-1][0] == "full" and "read-set-changed" in tiers(env.mind)[-1][1]


# --- The in-transaction rebase: one predicate ------------------------------------------------------


def test_the_rebase_compares_what_was_written_and_what_was_shown_and_nothing_else(env):
    from copy import deepcopy
    job = env.enqueue("owner-chat")
    assert run(env.mind, Scripted(MOOD), job)["state"] == "complete"
    manifest = manifests.load(env.mind.engine, env.mind.scope.key(), env.job(job)["manifest"])
    with env.mind.engine.db.connect() as conn:
        before = env.mind._load(conn)
    # The manifest above was built on the state before that commit: rebuild the pair it belongs to.
    before["revision"] = manifest["state_revision"]
    for name, entry in manifest["classes"]["dimensions"].items():
        before["dimensions"][name].update(event_id=entry["event_id"], target=entry["target"])
    proposal = Appraisal.model_validate(MOOD)
    verdict = lambda state, **extra: manifests.rebase(env.mind, manifest, before, state, proposal, historical=False, **extra)
    moved = deepcopy(before)
    moved.update(revision=before["revision"] + 1, session_advice={"decision": "keep"}, updated_at="later")
    # Bookkeeping: the revision, a field nobody is shown, and a curve re-anchored where it already was.
    moved["dimensions"]["focus"].update(score=59.5, at="2026-09-18T04:00:30.000001+00:00")
    assert verdict(moved)
    # A dimension the proposal does not write, scored by someone else: the read set moved.
    scored = deepcopy(moved)
    scored["dimensions"]["focus"]["event_id"] = "mind_someone_else"
    assert not verdict(scored)
    # A wish nobody showed the model, a changed policy and a changed persona are never rebased over.
    wished = deepcopy(moved)
    wished["desires"]["desire_new"] = {"id": "desire_new", "revision": 1, "status": "wanted"}
    assert not verdict(wished)
    assert not verdict({**moved, "profile_version": "another"})
    persona(env)
    assert not verdict(moved)
    # A session judgment is shown none of the mind state, so none of it stands in its way.
    (env.mind.engine.db.root / "persona-policy.json").unlink()
    session = {**manifest, "judgment": {**manifest["judgment"], "type": "session-maintenance"}}
    assert manifests.rebase(env.mind, session, before, scored, Appraisal.model_validate(ADVICE), historical=False)


def test_a_fault_while_recording_the_manifest_never_fails_the_appraisal(env, monkeypatch):
    def broken(*_args, **_kwargs):
        raise KeyError("a synthetic fault in an optimization")
    monkeypatch.setattr(manifests, "build", broken)
    job = env.enqueue("owner-chat")
    assert run(env.mind, Scripted(MOOD), job)["state"] == "complete"
    assert "manifest" not in env.job(job) and [m["error"] for m in metrics(env.mind, "appraisal_manifest_failed")] == ["KeyError"]


# --- The answer, checked without a queue -----------------------------------------------------------


def route_entry(kind="event-route"):
    path = ["memory", "event_routes", 0]
    return {"public": {"conflict_id": "c1", "kind": kind, "object": "graph:graph_event_a"}, "paths": [path],
            "resets": [([*path, "expected_revision"], 7)]}


ROUTED = Appraisal.model_validate({"reason": "r", "values": {"mood": 55}, "memory": {"event_routes": [
    {"key": "follow", "action": "append", "event_id": "graph_event_a", "expected_revision": 3, "evidence_ids": ["src_a"], "reason": "It continues"}]}}).model_dump()


@pytest.mark.parametrize("verdict,action,expected", [("append", "append", 7), ("correct", "correct", 7), ("link", "link", 7),
                                                     ("keep", "append", 7), ("wait", "defer", 3)])
def test_route_verdicts_restate_an_event_route_and_only_a_standing_verdict_resets_it(verdict, action, expected):
    answered = revalidation.Revalidation.model_validate({"items": [{"conflict_id": "c1", "verdict": verdict, "reason": "r"}]})
    proposal, summary = revalidation.accept(answered, [route_entry()], ROUTED)
    route, = proposal.memory.event_routes
    assert (route.action, route.expected_revision) == (action, expected)
    assert summary == [{"conflict_id": "c1", "object": "graph:graph_event_a", "verdict": verdict}]
    # Everything else is the stored proposal, byte for byte.
    untouched = proposal.model_dump()
    untouched["memory"]["event_routes"][0].update(action="append", expected_revision=3)
    assert dumps(untouched) == dumps(ROUTED)


def test_a_route_verdict_on_anything_but_an_event_route_is_refused():
    answered = revalidation.Revalidation.model_validate({"items": [{"conflict_id": "c1", "verdict": "link", "reason": "r"}]})
    with pytest.raises(revalidation.Refused) as refused:
        revalidation.accept(answered, [route_entry(kind="graph")], ROUTED)
    assert refused.value.code == "route-verdict-outside-event-route"


# --- An event route whose target moved, end to end -------------------------------------------------


def test_a_moved_event_is_a_route_question_and_the_verdict_restates_the_route(system, monkeypatch):
    from kin_mind.lifecycle import EventLifecycle, EventRoute
    mind, memory, source, _clock = system
    memory.configure({"operational_lanes": True, "graph": True, "event_lifecycle": True})
    began = source("the-report-begins")
    with mind.engine.db.connect(write=True) as conn:
        event_id = EventLifecycle(mind, memory.graph).apply_routes(conn, [EventRoute(
            key="begins", action="create", title="A synthetic report", evidence_ids=[began], reason="A sourced synthetic event")],
            memory.graph.proof(conn, [began]), "appraisal-begins")[0]["event_id"]
    follow = source("the-report-continues")
    job = jobs_of(mind).enqueue([follow], "fixture-v1", stimulus="memory-enrichment")["id"]

    def proposal(request):
        shown = next(n for n in request["memory_context"]["graph_candidates"] if n["id"] == event_id)
        return {"reason": "It continues the report", "memory": {"event_routes": [{
            "key": "follow", "action": "append", "event_id": event_id, "expected_revision": shown["revision"],
            "evidence_ids": [follow], "reason": "The same report, continued"}]}}

    def corrected_elsewhere():
        # Someone else's write to the event: its title is corrected, so what the model was shown moved.
        with mind.engine.db.connect(write=True) as conn:
            memory.graph._put(conn, {**memory.graph.get(conn, event_id), "title": "A synthetic report, corrected"})
    api = Model(monkeypatch, [proposal, answer({"graph:" + event_id: "link"})], mind.engine, during={"submit_appraisal": [corrected_elsewhere]})
    first = run(mind, api.provider, job)
    assert first["state"] == "pending" and first["error_detail"]["code"] == "graph-node-changed"
    assert first["error_detail"]["target"] == event_id
    second = run(mind, api.provider, job)
    assert (second["state"], second["tier"]) == ("complete", "B")
    request, = api.sent("revalidate_appraisal")
    moved = next(c for c in request["conflicts"] if c["object"] == "graph:" + event_id)
    assert moved["kind"] == "event-route" and [f["path"] for f in moved["relevant_fragment"]] == [["memory", "event_routes", 0]]
    assert moved["after"]["revision"] == moved["before"]["revision"] + 1
    # "Before" comes from the graph's own revision history, not from anything the manifest copied.
    assert (moved["before"]["content"]["title"], moved["after"]["content"]["title"]) == ("A synthetic report", "A synthetic report, corrected")
    committed, = saved(mind, job)["result"]["proposal"]["memory"]["event_routes"]
    # Restated as a link against the event as it is now; everything else of the route is untouched.
    assert (committed["action"], committed["expected_revision"]) == ("link", moved["after"]["revision"])
    assert committed["reason"] == "The same report, continued"
    with mind.engine.db.connect() as conn:
        stored, = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_event_routes WHERE scope=? AND json_extract(data,'$.appraisal_id') LIKE 'mind_%'",
                                                          (mind.scope.key(),))]
    assert (stored["state"], stored["event_id"]) == ("link", event_id)
