"""Kin's diary and what it goes on to change (2026-09-27).

The diary is an assessment's understanding written as Kin's own thought (basis internal_thought),
kept as a kin-reflection source. After the assessment moved to DeepSeek none was written: the
instruction to write one was only in the main session's prompt, the operational lane was told to
submit everything but the understanding, and no assessment was ever shown a diary, so none could
ground a trait or a plan on one. A trait type the owner's contract did not list was refused after
the fact, without the model ever being told the list; the owner has since approved every type
(小光: "人格契约所有类型都应该能改变哦").
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
    assert "日记与反思" in shared and "basis=internal_thought" in shared and "recent_reflections" in shared and "today_count" in shared
    assert "不重复增加成长依据" in shared
    provider = A.DeepSeek.__new__(A.DeepSeek)
    system = A.DeepSeek._system(provider, {"stimulus": "idle-review", "operational_only": True}, None)
    assert "日记与反思" in system and "感想与日记（understanding）" in system
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


def test_a_trait_type_the_contract_does_not_list_is_named_to_the_model_and_all_types_once_the_owner_approves(env, tmp_path):
    mind, source, diary, _ = env
    root = mind.engine.db.root
    with mind.engine.db.connect(write=True) as conn:
        Traits(mind).ensure(conn)  # the ledger's tables, as a store that has run it has them
    contract(root, mind.scope, ["interests", "兴趣"])
    assert mind.read()["trait_ledger"]["traits"]["categories"] == ["interests", "兴趣"]
    humor = diary("d-humor", "我发现自己越来越爱接冷笑话。", topic="幽默")

    def answer(context):
        return Appraisal(reason="冷笑话", trait_observations=[TraitObservation(
            key="冷笑话", category="humor", slug="dry-jokes", evidence_class="self_statement",
            polarity="support", evidence_ids=[humor])])

    run(mind, source, answer, key="tick-one")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 0
        refused = [r[0] for r in conn.execute("SELECT section FROM mind_section_refusals")]
    assert "trait_observations" in refused

    # The owner's word, as the release records it: every type, the canon itself untouched.
    before = json.loads((root / "persona-policy.json").read_text())
    done = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--quote", "人格契约所有类型都应该能改变哦",
                           "--at", "2026-09-27T09:00:00+08:00"], capture_output=True, text=True, check=True)
    assert json.loads(done.stdout)["state"] == "widened"
    after = json.loads((root / "persona-policy.json").read_text())
    assert after["mutable_trait_keys"] == ["interests", "兴趣", ALL_TRAIT_TYPES]
    assert after["mutable_trait_keys_approval"]["quote"] == "人格契约所有类型都应该能改变哦"
    assert {k: v for k, v in after.items() if not k.startswith("mutable_trait_keys")} == \
        {k: v for k, v in before.items() if not k.startswith("mutable_trait_keys")}
    again = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--quote", "x", "--at", "y"],
                           capture_output=True, text=True, check=True)
    assert json.loads(again.stdout)["state"] == "unchanged"

    policy = load_persona(mind.engine, mind.scope)
    assert trait_categories(policy) == "all" and mutable_trait(policy, "humor")
    validate_trait_changes(policy, {"humor": "x", "values": "y"})
    assert mind.read()["trait_ledger"]["traits"]["categories"] == "all"
    run(mind, source, answer, key="tick-two")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 1


def test_without_the_owners_word_a_listed_contract_still_refuses_other_types(tmp_path):
    scope = Scope(persona="synthetic-contract")
    policy = contract(tmp_path, scope, ["interests"])
    assert trait_categories(policy) == ["interests"] and not mutable_trait(policy, "humor")
    with pytest.raises(ValueError, match="explicit owner approval"):
        validate_trait_changes(policy, {"humor": "x"})
    assert trait_categories(None) == "all" and mutable_trait(None, "anything")
    # The script refuses to run without the owner's words, and a dry run changes nothing.
    missing = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--quote", " ", "--at", "t"], capture_output=True, text=True)
    assert missing.returncode != 0
    planned = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--quote", "都可以", "--at", "t", "--dry-run"],
                             capture_output=True, text=True, check=True)
    assert json.loads(planned.stdout)["state"] == "planned"
    assert json.loads((tmp_path / "persona-policy.json").read_text())["mutable_trait_keys"] == ["interests"]
