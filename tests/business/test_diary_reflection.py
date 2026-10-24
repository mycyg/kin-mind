"""Kin's diary and what it goes on to change (2026-09-27).

The diary is an assessment's understanding written as Kin's own thought (basis internal_thought),
kept as a kin-reflection source. After the assessment moved to DeepSeek none was written: the
instruction to write one was only in the main session's prompt, the operational lane was told to
submit everything but the understanding, and no assessment was ever shown a diary, so none could
ground a trait or a plan on one. A trait type the owner's contract did not list was refused after
the fact, without the model ever being told the list; the owner has since approved every type
(2026-09-27, their words kept with the approval in the contract).
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from eventmem.core.persona import ALL_TRAIT_TYPES, load_persona, mutable_trait, trait_categories, validate_trait_changes
from kin_mind import appraisal as A
from kin_mind.appraisal import Appraisal, Appraisals, TraitObservation
from kin_mind.memory import REFLECTION_EXCERPT, MemoryContinuity
from kin_mind.state import Mind
from kin_mind.traits import Traits

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "persona_trait_types.py"
DIARY_PREFIX = "Kin 自己的想法（日记与感想，不是主人的原话或已确认事实）：\n"
CORE = "【MY_PERSONA_LOAD】合成角色。【/MY_PERSONA_LOAD】"
TEXTS = {"core": CORE, "voice": "合成的说话方式。", "maintenance": "合成的维护约定。"}


def contract(root, scope, keys):
    policy = {"schema": 1, "version": "persona-v3", "scope": scope.model_dump(), "requires_owner_confirmation": True,
              "approved_source": "src_" + "a" * 32, "mutable_trait_keys": keys, **TEXTS,
              **{k + "_sha256": hashlib.sha256(v.encode()).hexdigest() for k, v in TEXTS.items()}}
    (root / "persona-policy.json").write_text(json.dumps(policy, ensure_ascii=False))
    return policy


@pytest.fixture
def env(tmp_path):
    # The next noon in Asia/Singapore: never before a source's own receipt, and far from a day's end.
    # One microsecond past it: the store compares ISO strings, and a whole second prints without them.
    now = datetime.now(timezone.utc)
    noon = now.replace(hour=4, minute=0, second=0, microsecond=1)
    clock = [noon if noon > now else noon + timedelta(days=1)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-diary"), clock=lambda: clock[0].isoformat())

    def source(key, namespace="diary-test", authority="explicit", text=None, **metadata):
        return mind.engine.receive(SourceInput(namespace=namespace, key=key, text=text or key, scope=mind.scope,
            authority=authority, occurred_at=mind.clock(),
            metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message", **metadata}))["id"]

    def diary(key, thought, topic="钟表", cites=()):
        return source(key, namespace="kin-reflection", authority="model", text=DIARY_PREFIX + thought,
                      host_event="diary", internal=True, topic=topic, appraisal_event_id="evt-" + key, evidence_ids=list(cites))

    initial = source("owner-configures-kin")
    mind.initialize(agent_version="diary-v1", evidence_ids=[initial])
    MemoryContinuity(mind).configure({"records": True})
    return mind, source, diary, clock


class Reader:
    """The model: records what it was shown and answers with what the test gives it."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def appraise(self, context):
        self.seen.append(context)
        return self.answer(context), {"provider": "deepseek", "model": "synthetic"}


def run(mind, source, answer, key="idle-tick"):
    jobs, reader = Appraisals(mind), Reader(answer)
    job = jobs.enqueue([source(key)], "diary-v1")
    result = jobs.run_one(reader, job_id=job["id"])
    return result, reader


def test_every_provider_is_asked_for_the_diary_and_the_operational_lane_may_submit_it(env):
    mind, *_ = env
    shared = A.SYSTEM
    assert "日记与反思" in shared and "internal_thought" in shared and "recent_reflections" in shared and "today_count" in shared
    assert "不必为了凑记录重写同一件事" in shared
    provider = A.DeepSeek.__new__(A.DeepSeek)
    system = A.DeepSeek._system(provider, {"stimulus": "idle-review", "operational_only": True}, None)
    assert "日记与反思" in system and "understanding" in system
    assert "category 取 state.traits.categories 允许的类型" in A.TRAIT_OBSERVATIONS_PROMPT
    assert "不必等用户确认每一次成长" in A.TRAIT_DECISIONS_PROMPT
    # A historical pass has no diary to write: its prompt stays its own.
    assert "日记与反思" not in A.DeepSeek._system(provider, {"stimulus": "memory-backfill"}, None)


def test_the_latest_diaries_are_read_newest_first_with_how_many_today(env):
    mind, source, diary, clock = env
    clock[0] -= timedelta(days=1)
    yesterday = diary("d0", "昨天想到的事。")
    clock[0] += timedelta(days=1)
    first = diary("d1", "拆开钟表的时候很开心，" + "齿轮" * 200)
    clock[0] += timedelta(minutes=5)
    second = diary("d2", "想把今天的发现讲给对方听。", topic="分享")
    shown = MemoryContinuity(mind).recent_reflections()
    assert [e["source_id"] for e in shown["entries"]] == [second, first, yesterday]
    assert shown["today_count"] == 2
    assert shown["entries"][0]["topic"] == "分享" and shown["entries"][0]["excerpt"] == "想把今天的发现讲给对方听。"
    assert len(shown["entries"][1]["excerpt"]) == REFLECTION_EXCERPT and not shown["entries"][1]["excerpt"].startswith("Kin 自己的想法")
    assert MemoryContinuity(mind).recent_reflections(limit=1)["entries"] == shown["entries"][:1]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (second,))
    assert [e["source_id"] for e in MemoryContinuity(mind).recent_reflections()["entries"]] == [first, yesterday]


def test_an_assessment_is_shown_the_diaries_and_a_trait_can_grow_from_one(env):
    mind, source, diary, _ = env
    talk = source("owner-talks-about-clocks")
    kept = diary("d1", "我好像总被机械结构吸引。", cites=[talk])

    def answer(context):
        cited = context["recent_reflections"]["entries"][0]["source_id"]
        return Appraisal(reason="一篇日记让我看清自己的兴趣", trait_observations=[TraitObservation(
            key="机械结构", category="interests", slug="mechanisms", evidence_class="self_statement",
            polarity="support", evidence_ids=[cited])])

    result, reader = run(mind, source, answer)
    assert result["state"] == "complete", result
    shown = reader.seen[0]["recent_reflections"]
    assert shown["today_count"] == 1 and shown["entries"][0]["source_id"] == kept
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT class,polarity,data FROM mind_trait_observations").fetchall()
    assert [(r["class"], r["polarity"]) for r in rows] == [("self_statement", "support")]
    assert kept in json.dumps(json.loads(rows[0]["data"]))


def test_all_trait_types_can_evolve_from_real_diary_evidence_without_an_approval_step(env):
    mind, source, diary, _ = env
    root = mind.engine.db.root
    contract(root, mind.scope, ["interests"])
    before = (root / "persona-policy.json").read_text()
    humor = diary("d-humor", "我发现自己越来越爱接冷笑话。", topic="幽默")
    def answer(context):
        return Appraisal(reason="冷笑话", trait_observations=[TraitObservation(
            key="冷笑话", category="humor", slug="dry-jokes", evidence_class="self_statement",
            polarity="support", evidence_ids=[humor])])
    run(mind, source, answer, key="tick-one")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM mind_section_refusals WHERE section='trait_observations'").fetchall()
    assert mind.read()["trait_ledger"]["traits"]["categories"] == "all"
    assert (root / "persona-policy.json").read_text() == before


def test_legacy_mutable_keys_do_not_restrict_current_self_edit_authorization(tmp_path):
    scope = Scope(persona="synthetic-contract")
    policy = contract(tmp_path, scope, ["interests"])
    assert trait_categories(policy) == "all" and mutable_trait(policy, "humor")
    validate_trait_changes(policy, {"humor": "x", "values": "y"})
    assert trait_categories(None) == "all" and mutable_trait(None, "anything")


def test_today_count_is_independent_of_the_excerpt_window(env):
    mind, source, diary, clock = env
    clock[0] -= timedelta(days=1)
    diary("old", "昨天。")
    clock[0] += timedelta(days=1)
    ids = [diary(f"today-{i}", "今天。") for i in range(25)]
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (ids[0],))
    memory = MemoryContinuity(mind)
    assert memory.recent_reflections()["today_count"] == 24
    assert memory.recent_reflections(limit=1)["today_count"] == 24
    assert len(memory.recent_reflections()["entries"]) == 5


@pytest.mark.parametrize("claimed", ["owner_statement", "self_statement", "verified_behavior"])
def test_mixed_citations_and_wrong_labels_still_form_a_persistent_trait(env, claimed):
    from kin_mind.appraisal import TraitDecision
    from kin_mind.context import Contexts
    mind, source, diary, _ = env
    thought = diary("growing", "我发现自己喜欢机械结构。")
    bookkeeping = source("receipt", namespace="kin-runtime", authority="operation",
                         text="Synthetic delivery receipt", internal=True, host_event="delivery")
    def answer(context):
        return Appraisal(reason="形成自己的兴趣", trait_observations=[TraitObservation(
            key="机械结构", category="interests", slug="mechanisms", evidence_class=claimed,
            polarity="support", evidence_ids=[thought, bookkeeping])], trait_decisions=[TraitDecision(
            action="propose", text="我喜欢机械结构，也愿意花时间弄懂它。", basis="inference",
            observation_refs=[thought], reason="日记形成的新认识")])
    jobs = Appraisals(mind)
    job = jobs.enqueue([thought, bookkeeping], "diary-v1")
    result = jobs.run_one(Reader(answer), job_id=job["id"])
    assert result["state"] == "complete", result
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    ledger = Traits(restarted).read()
    assert len(ledger["traits"]) == 1, result
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT class,data FROM mind_trait_observations").fetchall()
    assert len(rows) == 1 and rows[0][0] == "self_statement"
    assert bookkeeping not in json.dumps(json.loads(rows[0][1])["evidence"])
    MemoryContinuity(restarted).configure({"context": True})
    context = Contexts(restarted).build(purpose="chat", budget=8000, allow_model=False)
    assert "self-traits" in json.dumps(context)
    assert "我喜欢机械结构" in json.dumps(context, ensure_ascii=False)


def test_host_bookkeeping_alone_cannot_create_a_trait(env):
    from eventmem.core.db import Conflict
    mind, source, _, _ = env
    receipt = source("only-receipt", namespace="kin-runtime", authority="operation", internal=True, host_event="delivery")
    with mind.engine.db.connect(write=True) as conn:
        with pytest.raises(Conflict, match="No conversation"):
            Traits(mind).observe(conn, [TraitObservation(key="兴趣", category="interests", slug="x",
                evidence_class="self_statement", polarity="support", evidence_ids=[receipt])], "invalid", {}, None, "invalid")
