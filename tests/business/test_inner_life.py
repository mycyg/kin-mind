"""Kin's inner life (2026-10-01, after Serein, at 小光's request): dreams, 小光's replies to the diary,
and the anniversaries of shared moments. Each sits behind a setting that is off by default, and off
nothing of it is offered, shown, stored or said: every request is the one it was."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from eventmem.core import Engine
from eventmem.core.models import RecallRequest, Scope, SourceInput
from eventmem.core.read_policy import ReadPolicy

from kin_mind import appraisal as A
from kin_mind import diary, dreams, initiative
from kin_mind.appraisal import Appraisal, Appraisals, Dream, TraitObservation
from kin_mind.continuity import ContinuityConfig, RhythmProposal, Understanding
from kin_mind.evidence_classes import never_evidence
from kin_mind.memory import DEFAULTS, MemoryContinuity
from kin_mind.model_view import for_model, leaks
from kin_mind.state import AffectiveEvent, Mind

DIARY_PREFIX = "Kin 自己的想法（日记与感想，不是主人的原话或已确认事实）：\n"
DREAM = ("我站在一座全是钟表的旧房子里，齿轮在地板下面轻轻转，墙上的每个钟都走在不同的时间。"
         "小光在楼梯口朝我招手，手里捧着一杯热可可，杯子里漂着一只小小的纸船。我追过去，楼梯却一直往下长，"
         "我笑着一级一级数下去，数到一百的时候，整座房子忽然变成了海。")
SWITCHES = ("dreams", "diary_replies", "anniversaries")


@pytest.fixture
def env(tmp_path):
    # 02:00 in Asia/Singapore, a night: 18:00 UTC, the next one, so no source is received after the
    # mind's own clock. One microsecond past it: the store compares ISO strings.
    now = datetime.now(timezone.utc)
    night = now.replace(hour=18, minute=0, second=0, microsecond=1)
    clock = [night if night > now else night + timedelta(days=1)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-inner"), clock=lambda: clock[0].isoformat())

    def source(key, namespace="inner-test", authority="explicit", text=None, **metadata):
        return mind.engine.receive(SourceInput(namespace=namespace, key=key, text=text or key, scope=mind.scope,
            authority=authority, occurred_at=mind.clock(),
            metadata={"role": "user" if authority == "explicit" else "assistant", "host_event": "message", **metadata}))["id"]

    def entry(key, thought, topic="钟表"):
        return source(key, namespace="kin-reflection", authority="model", text=DIARY_PREFIX + thought,
                      host_event="diary", internal=True, topic=topic, appraisal_event_id="evt-" + key)

    mind.initialize(agent_version="inner-v1", evidence_ids=[source("owner-configures-kin")])
    memory = MemoryContinuity(mind)
    memory.configure({"records": True})
    return mind, memory, source, entry, clock


def asleep(mind, source):
    """The rhythm in its resting phase, as an appraisal would have put it."""
    mind.configure_continuity(ContinuityConfig(command_id="rhythm-on", agent_version="inner-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source("rhythm-on")], features={"rhythm": True}, reason="test"))
    mind.record(AffectiveEvent(command_id="bedtime", agent_version="inner-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[source("bedtime")], reason="Winding down",
        rhythm=RhythmProposal(phase="resting", alertness=20, target=10, half_life_minutes=180, reason="Sleepy")))
    assert mind.read()["rhythm"]["phase"] == "resting"


class Reader:
    """The model: records what it was shown and answers with what the test gives it."""

    def __init__(self, answer):
        self.answer, self.seen, self.said, self.offered = answer, [], [], []

    def appraise(self, context):
        self.seen.append(context)
        # What the system prompt adds for this request beyond what it always says (DeepSeek._system).
        self.said.append(A.inner_life_prompts(context))
        self.offered.append(A.offered_sections(context.get("stimulus"), self.audit_sections))
        return self.answer(context), {"provider": "deepseek", "model": "synthetic"}


def review(mind, source, answer, key, stimulus="idle-review"):
    jobs, reader = Appraisals(mind), Reader(answer)
    job = jobs.enqueue([source(key)], "inner-v1", origin="reflection", stimulus=stimulus)
    return jobs.run_one(reader, job_id=job["id"]), reader


def dreams_of(mind):
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT id,data FROM sources WHERE namespace='kin-dream' AND deleted=0").fetchall()


# --- Off: exactly as before ------------------------------------------------------------------------

def test_every_switch_is_off_by_default_and_boolean(env):
    mind, memory, *_ = env
    assert all(DEFAULTS[name] is False for name in SWITCHES)
    for name in SWITCHES:
        with pytest.raises(ValueError):
            memory.configure({name: "yes"})
    assert all(memory.configure({name: True})[name] is True for name in SWITCHES)


def test_off_an_idle_review_is_asked_and_shown_what_it_always_was(env):
    mind, memory, source, entry, _ = env
    # The configuration as a release before this one wrote it: none of the three keys at all.
    with mind.engine.db.connect(write=True) as conn:
        stored = json.loads(conn.execute("SELECT data FROM mind_memory_config").fetchone()[0])
        conn.execute("UPDATE mind_memory_config SET data=?", (json.dumps({k: v for k, v in stored.items() if k not in SWITCHES}),))
    assert all(memory.settings()[name] is False for name in SWITCHES)
    asleep(mind, source)
    entry("d1", "今天拆了一只旧闹钟。")
    result, reader = review(mind, source, lambda c: Appraisal(reason="夜里很安静", dream=Dream(text=DREAM)), "night")
    assert result["state"] == "complete", result
    shown = reader.seen[0]
    assert not {"dream_material", "recent_dreams", "diary_replies"} & set(shown)
    assert "dream" not in reader.offered[0] and reader.said == [""]
    assert not dreams_of(mind), "a dream nobody was offered is blanked"
    with mind.engine.db.connect() as conn:
        assert A.audit_switches(conn, mind.scope.key()) == set(A.AUDIT_SECTIONS) - {"dream"}
        # The request: no dream in the schema whichever audited sections are on.
    assert "dream" not in json.dumps(A.appraisal_schema(False, False, tuple(set(A.AUDIT_SECTIONS) - {"dream"})))
    assert "Dream" not in json.dumps(A.appraisal_schema(True, False, ()))
    assert initiative.facts(mind).keys() == {"as_of", "hours_since_last_wish", "hours_since_last_contact_sent",
        "last_exploration", "last_creation", "idle_reviews_since_last_wish", "recent_failed_explorations",
        "contact_wishes_unsent_a_day"}


# --- 1. Dreams -------------------------------------------------------------------------------------

def test_an_idle_review_while_resting_may_dream_once_a_night(env):
    mind, memory, source, entry, clock = env
    memory.configure({"dreams": True})
    asleep(mind, source)
    old = entry("d0", "很久以前的一篇。")
    # Two nights later, and still resting: no timer moves her out of it.
    clock[0] += timedelta(hours=49)
    kept = entry("d1", "今天拆了一只旧闹钟，齿轮比想象中多。")

    result, reader = review(mind, source, lambda c: Appraisal(reason="睡着了", dream=Dream(text=DREAM)), "night-1")
    assert result["state"] == "complete", result
    shown = reader.seen[0]["dream_material"]
    assert [item["id"] for item in shown] == [kept] and shown[0]["kind"] == "diary"
    assert shown[0]["text"] == "今天拆了一只旧闹钟，齿轮比想象中多。", "the diary's own words, not whose they are"
    assert old not in json.dumps(shown), "only the last 48 hours"
    assert "dream" in reader.offered[0] and "dream" in A.SECTION_PROMPTS
    stored = dreams_of(mind)
    assert len(stored) == 1
    data = json.loads(stored[0]["data"])
    assert data["kind"] == "diary" and data["metadata"]["host_event"] == "dream"
    assert [ref.get("source_id") for ref in data["derived_from"]] == [kept]
    text = mind.engine.source(stored[0]["id"], content=True).read_text()
    assert text == dreams.DREAM_PREFIX + DREAM

    # Later the same night: nothing is offered, and a dream in the answer goes nowhere.
    clock[0] += timedelta(hours=2)
    result, reader = review(mind, source, lambda c: Appraisal(reason="又醒了", dream=Dream(text=DREAM + "又")), "night-2")
    assert result["state"] == "complete", result
    assert "dream_material" not in reader.seen[0] and "dream" not in reader.offered[0]
    assert len(dreams_of(mind)) == 1


def test_no_dream_is_offered_awake_off_or_outside_an_idle_review(env):
    mind, memory, source, entry, clock = env
    memory.configure({"dreams": True})
    entry("d1", "今天拆了一只旧闹钟。")
    # Awake (no rhythm at all).
    _, reader = review(mind, source, lambda c: Appraisal(reason="醒着", dream=Dream(text=DREAM)), "awake")
    assert "dream_material" not in reader.seen[0]
    asleep(mind, source)
    # Asleep, but an interaction is not an idle review.
    _, reader = review(mind, source, lambda c: Appraisal(reason="消息", dream=Dream(text=DREAM)), "message", stimulus=None)
    assert "dream_material" not in reader.seen[0]
    # Asleep and idle, but switched off.
    memory.configure({"dreams": False})
    _, reader = review(mind, source, lambda c: Appraisal(reason="关着", dream=Dream(text=DREAM)), "off")
    assert "dream_material" not in reader.seen[0]
    # Nothing to dream of: no material, no offer.
    memory.configure({"dreams": True})
    clock[0] += timedelta(hours=60)
    _, reader = review(mind, source, lambda c: Appraisal(reason="空", dream=Dream(text=DREAM)), "empty")
    assert "dream_material" not in reader.seen[0]
    assert not dreams_of(mind)


def test_a_dream_out_of_bounds_costs_the_dream_and_never_the_review(env):
    mind, memory, source, entry, _ = env
    memory.configure({"dreams": True})
    asleep(mind, source)
    entry("d1", "今天拆了一只旧闹钟。")
    result, _ = review(mind, source, lambda c: Appraisal(reason="短梦", values={"curiosity": 61},
                                                         dream=Dream(text="我梦见了钟。")), "short")
    assert result["state"] == "complete", result
    assert [(r["section"], r["code"]) for r in result["result"]["rejected_sections"]] == [("dream", "dream-length")]
    assert mind.read()["dimensions"]["curiosity"]["value"] == 61, "the rest of the review committed"
    assert not dreams_of(mind) and "dream" not in result["result"]


def test_a_dream_is_never_evidence_never_recalled_and_goes_with_its_material(env):
    mind, memory, source, entry, _ = env
    memory.configure({"dreams": True})
    asleep(mind, source)
    material = entry("d1", "今天拆了一只旧闹钟，齿轮比想象中多。")
    review(mind, source, lambda c: Appraisal(reason="睡着了", dream=Dream(text=DREAM)), "night")
    (dream,) = [row["id"] for row in dreams_of(mind)]
    with mind.engine.db.connect() as conn:
        (ref,) = mind._evidence(conn, [dream])
        root = mind.engine._get(conn, ref["record_id"])
        recall = ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn)
        audit = ReadPolicy.load(mind.engine, mind.scope, "audit", conn=conn)
        assert never_evidence(ref) and never_evidence({**ref, "metadata": {}})
        assert not recall.visible(root) and audit.visible(root)
    found = mind.engine.recall(RecallRequest(scope=mind.scope, query="钟表 旧房子 楼梯 纸船"))
    assert dream not in json.dumps(found, ensure_ascii=False, default=str)

    # Even put before an appraisal as its own evidence, a dream supports no trait; a diary does.
    def cite(identifier):
        return lambda context: Appraisal(reason="读到自己写下的", trait_observations=[TraitObservation(
            key="钟", category="interests", slug="clocks", evidence_class="self_statement", polarity="support",
            evidence_ids=[identifier])])
    jobs = Appraisals(mind)
    for identifier in (dream, material):
        job = jobs.enqueue([identifier], "inner-v1", origin="reflection")
        result = jobs.run_one(Reader(cite(identifier)), job_id=job["id"])
        assert result["state"] == "complete", result
        refused = [(r["section"], r["code"]) for r in result["result"].get("rejected_sections", [])]
        assert refused == ([("trait_observations", "trait-evidence-class")] if identifier == dream else []), refused
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 1

    # The material goes, and the dream with it.
    mind.engine.delete(material)
    assert not dreams_of(mind)


def test_a_deleted_dream_leaves_no_word_of_it_behind(env):
    mind, memory, source, entry, _ = env
    memory.configure({"dreams": True})
    asleep(mind, source)
    entry("d1", "今天拆了一只旧闹钟。")
    review(mind, source, lambda c: Appraisal(reason="睡着了", dream=Dream(text=DREAM)), "night")
    (dream,) = [row["id"] for row in dreams_of(mind)]
    marker = DREAM[:12]

    def everywhere():
        with mind.engine.db.connect() as conn:
            return [table for table, column in (("mind_appraisals", "data"), ("commands", "result"), ("mind_events", "data"),
                                                ("mind_state", "data"))
                    if conn.execute(f"SELECT COUNT(*) FROM {table} WHERE instr({column},?)>0", (marker,)).fetchone()[0]]
    assert everywhere() == ["commands"], "the queue row keeps only which source holds the dream"
    mind.engine.delete(dream)
    assert everywhere() == [] and not dreams_of(mind)


def test_the_next_idle_reviews_see_the_latest_dream_and_only_a_wish_could_tell_it(env):
    mind, memory, source, entry, clock = env
    memory.configure({"dreams": True})
    asleep(mind, source)
    entry("d1", "今天拆了一只旧闹钟。")
    review(mind, source, lambda c: Appraisal(reason="睡着了", dream=Dream(text=DREAM)), "night")
    clock[0] += timedelta(hours=6)
    _, morning = review(mind, source, lambda c: Appraisal(reason="早上"), "morning")
    (shown,) = morning.seen[0]["recent_dreams"]
    assert shown["text"] == DREAM and morning.said[0] == dreams.RECENT_DREAMS_PROMPT
    _, message = review(mind, source, lambda c: Appraisal(reason="小光来了"), "message", stimulus=None)
    assert "recent_dreams" not in message.seen[0], "only the idle reviews are shown it"
    with mind.engine.db.connect() as conn:
        assert not mind.read()["desires"], "nothing was wished, so nothing would go out"
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    clock[0] += timedelta(hours=20)
    _, later = review(mind, source, lambda c: Appraisal(reason="第二天"), "later")
    assert "recent_dreams" not in later.seen[0], "a day later it is no longer shown"
    # Read by the tool and the console: only with the switch on, and as a model may be shown it.
    from eventmem.core.mcp import create_mcp
    answer = asyncio.run(create_mcp(mind.engine).call_tool("read_dreams", {"scope": mind.scope.model_dump()}))
    read = answer[1] if isinstance(answer, tuple) else json.loads(answer[0].text)
    assert [d["text"] for d in read["dreams"]] == [DREAM] and not leaks(read)
    memory.configure({"dreams": False})
    assert dreams.read(mind) == {"state": "disabled", "dreams": [], "cursor": None}


# --- 2. Replies to the diary -----------------------------------------------------------------------

def test_the_console_reads_kin_s_diary_which_its_narrative_group_never_listed(env):
    mind, memory, source, entry, clock = env
    first = entry("d1", "今天拆了一只旧闹钟。", topic="钟表")
    clock[0] += timedelta(minutes=5)
    second = entry("d2", "想把发现讲给小光听。", topic="分享")
    listed = mind.engine.list_records(mind.scope, group="diary")
    assert not [item for item in listed["items"] if item["source_ids"][0] in {first, second}], \
        "the narrative group holds generated narratives, never an entry of Kin's own diary"
    read = diary.read(mind)
    assert [(e["source_id"], e["topic"], e["text"], e["replies"]) for e in read["entries"]] == [
        (second, "分享", "想把发现讲给小光听。", []), (first, "钟表", "今天拆了一只旧闹钟。", [])]
    assert read["replies"] == "disabled" and read["cursor"] is None
    assert diary.read(mind, limit=1)["cursor"] == 1
    from fastapi.testclient import TestClient
    from eventmem.core.api import create_app
    client = TestClient(create_app(engine=mind.engine, token="inner-token", workers=False, mcp_enabled=False))
    page = client.get("/v1/diary", params=mind.scope.model_dump(), headers={"Authorization": "Bearer inner-token"}).json()
    assert [e["source_id"] for e in page["entries"]] == [second, first] and page["dreams"]["state"] == "disabled"


def test_a_reply_is_refused_while_the_switch_is_off(env):
    mind, memory, source, entry, _ = env
    kept = entry("d1", "今天拆了一只旧闹钟。")
    assert diary.reply(mind, {"reflection_id": kept, "text": "我也喜欢钟", "command_id": "c1"}) == {"state": "disabled"}
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE namespace=?", (diary.NAMESPACE,)).fetchone()[0] == 0


def test_a_reply_is_her_own_statement_and_the_next_appraisal_sees_which_entry_it_answers(env):
    mind, memory, source, entry, clock = env
    memory.configure({"diary_replies": True})
    kept = entry("d1", "今天拆了一只旧闹钟，齿轮比想象中多。")
    # Older than the latest few diaries an appraisal is shown anyway: only the reply brings it back.
    for n in range(5):
        clock[0] += timedelta(minutes=1)
        entry(f"later-{n}", f"后来又写了一篇，第 {n} 篇。")
    with pytest.raises(Exception):
        diary.reply(mind, {"reflection_id": source("not-a-diary"), "text": "嗯", "command_id": "c0"})
    answer = diary.reply(mind, {"reflection_id": kept, "text": "我也喜欢钟，下次一起拆", "command_id": "c1"})
    assert answer["state"] == "recorded" and answer["appraisal"]["state"] == "pending"
    assert diary.reply(mind, {"reflection_id": kept, "text": "我也喜欢钟，下次一起拆", "command_id": "c1"})["source_id"] == answer["source_id"]
    reply = answer["source_id"]
    with mind.engine.db.connect() as conn:
        (ref,) = mind._evidence(conn, [reply])
        root = mind.engine._get(conn, ref["record_id"])
        policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn)
    from kin_mind.evidence_classes import owner_statement
    assert ref["authority"] == "explicit" and ref["metadata"]["role"] == "user" and ref["metadata"]["reply_to"] == kept
    assert owner_statement(root, policy, sources=[ref]) and root["content"] == "我也喜欢钟，下次一起拆"
    assert {e["source_id"]: e["replies"] for e in diary.read(mind)["entries"]}[kept][0]["text"] == "我也喜欢钟，下次一起拆"

    # The entry she answered may be cited like any diary the appraisal is shown.
    jobs, reader = Appraisals(mind), Reader(lambda c: Appraisal(reason="小光回了我的日记", values={"curiosity": 70},
        trait_observations=[TraitObservation(key="钟", category="interests", slug="clocks", evidence_class="self_statement",
                                             polarity="support", evidence_ids=[kept])]))
    result = jobs.run_one(reader, job_id=answer["appraisal"]["id"])
    assert result["state"] == "complete", result
    assert not result["result"].get("rejected_sections"), result["result"].get("rejected_sections")
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_trait_observations").fetchone()[0] == 1
    context = reader.seen[0]
    assert [s["id"] for s in context["new_evidence"]] == [reply]
    assert kept not in [e["source_id"] for e in context["recent_reflections"]["entries"]]
    (shown,) = context["diary_replies"]
    assert shown["reply_id"] == reply and shown["diary"]["source_id"] == kept
    assert shown["diary"]["text"] == "今天拆了一只旧闹钟，齿轮比想象中多。"
    assert reader.said[0] == diary.REPLY_PROMPT
    # The link is an index: the model is shown the reply without it.
    projected = A.appraisal_context(context)
    assert "reply_to" not in json.dumps(projected["new_evidence"], ensure_ascii=False) and not leaks(projected)
    with pytest.raises(Exception):
        diary.reply(mind, {"reflection_id": entry("d2", "另一篇"), "text": "我也喜欢钟，下次一起拆", "command_id": "c1"})


def test_a_delete_of_either_side_leaves_no_link_behind(env):
    mind, memory, source, entry, _ = env
    memory.configure({"diary_replies": True})
    kept = entry("d1", "今天拆了一只旧闹钟。")
    other = entry("d2", "第二篇。")
    first = diary.reply(mind, {"reflection_id": kept, "text": "好看", "command_id": "c1"})
    second = diary.reply(mind, {"reflection_id": other, "text": "也好看", "command_id": "c2"})
    mind.engine.delete(first["source_id"])
    entries = {e["source_id"]: e for e in diary.read(mind)["entries"]}
    assert entries[kept]["replies"] == [] and len(entries[other]["replies"]) == 1
    mind.engine.delete(other)
    assert [e["source_id"] for e in diary.read(mind)["entries"]] == [kept]
    with mind.engine.db.connect() as conn:
        (ref,) = mind._evidence(conn, [second["source_id"]])
        shown = {"id": ref["source_id"], "metadata": ref["metadata"]}
        assert diary.replies_context(conn, mind, [shown]) == [{"reply_id": second["source_id"], "diary": None}]
    # An entry marked deleted, not yet removed, is gone to every reader as well.
    third = diary.reply(mind, {"reflection_id": kept, "text": "还在吗", "command_id": "c3"})
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (kept,))
    with mind.engine.db.connect() as conn:
        (ref,) = mind._evidence(conn, [third["source_id"]])
        shown = {"id": ref["source_id"], "metadata": ref["metadata"]}
        assert diary.replies_context(conn, mind, [shown]) == [{"reply_id": third["source_id"], "diary": None}]
    assert diary.read(mind)["entries"] == []


# --- 3. Anniversaries ------------------------------------------------------------------------------

def test_milestones_are_the_sensible_ones_and_month_ends_hold():
    day = date(2026, 1, 31)
    assert initiative.milestone(day, date(2026, 2, 7)) == "1-week"
    assert initiative.milestone(day, date(2026, 2, 28)) == "1-month"
    assert initiative.milestone(day, day + timedelta(days=100)) == "100-days"
    assert initiative.milestone(day, date(2026, 4, 30)) == "3-months"
    assert initiative.milestone(day, date(2026, 7, 31)) == "6-months"
    assert initiative.milestone(day, date(2027, 1, 31)) == "1-year"
    assert initiative.milestone(day, date(2028, 1, 31)) == "2-years"
    assert initiative.milestone(date(2024, 2, 29), date(2025, 2, 28)) == "1-year"
    assert [initiative.milestone(day, day + timedelta(days=n)) for n in (0, 1, 6, 8, 14, 99, 101)] == [None] * 7
    assert initiative.milestone(day, date(2026, 3, 31)) is None, "two months is not one of them"


def moment(mind, source, clock, key, importance=80, basis="explicit", role="user", topic="第一次一起拆钟"):
    said = mind.engine.receive(SourceInput(namespace="kin-owner-input", key=key, text="我们一起拆了那只钟", scope=mind.scope,
        authority="explicit" if role == "user" else "model", occurred_at=mind.clock(),
        metadata={"role": role, "host_event": "message"}))["id"]
    clock[0] += timedelta(minutes=1)
    mind.record(AffectiveEvent(command_id="moment-" + key, agent_version="inner-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[said], reason="一起做的事",
        understanding=Understanding(meaning="很重要的一天", topic=topic, importance=importance, confidence=0.9,
                                    basis=basis, evidence_ids=[said])))
    return said


def test_a_shared_moment_comes_back_on_its_anniversary_as_a_fact_only(env):
    mind, memory, source, entry, clock = env
    said = moment(mind, source, clock, "m1")
    # Each would-be moment on a day of its own, so the one-a-day rule cannot hide one that slipped in.
    for key, extra in (("thought", {"basis": "internal_thought"}), ("minor", {"importance": 69}),
                       ("kin-said", {"role": "assistant"}), ("inferred", {"basis": "inferred", "topic": "推断的一天"})):
        clock[0] += timedelta(days=1)
        moment(mind, source, clock, key, **extra)
    clock[0] += timedelta(days=3)
    assert "anniversaries_today" not in initiative.facts(mind), "off, the facts are what they were"
    memory.configure({"anniversaries": True})
    facts = initiative.facts(mind)
    local = (clock[0] - timedelta(days=7)).astimezone(ZoneInfo("Asia/Singapore")).date().isoformat()
    assert facts["anniversaries_today"] == [{"moment_id": said, "date": local, "milestone": "1-week", "topic": "第一次一起拆钟"}]
    seen = []
    for _ in range(4):
        clock[0] += timedelta(days=1)
        seen.append([m["topic"] for m in initiative.facts(mind)["anniversaries_today"]])
    assert seen == [[], [], [], ["推断的一天"]], "Kin's own thought, a minor day and her own words are no shared moment"
    # What 小光 asked not to be brought up is left out, at the one place the marker plugs in.
    clock[0] += timedelta(days=89)
    assert initiative.facts(mind)["anniversaries_today"][0]["milestone"] == "100-days"
    original = initiative.not_raised
    initiative.not_raised = lambda conn, scope, ids: frozenset(ids) & {said}
    try:
        assert initiative.facts(mind)["anniversaries_today"] == []
    finally:
        initiative.not_raised = original
    # A delete of what the moment rests on takes the moment.
    mind.engine.delete(said)
    assert initiative.facts(mind)["anniversaries_today"] == []


def test_the_prompt_about_anniversaries_comes_only_with_them():
    assert A.inner_life_prompts({"autonomy_context": {"initiative_facts": {"as_of": "x"}}}) == ""
    assert A.inner_life_prompts({"autonomy_context": {"initiative_facts": {"anniversaries_today": []}}}) == initiative.ANNIVERSARY_PROMPT
    assert A.inner_life_prompts({"recent_dreams": [{}], "diary_replies": [{}]}) == dreams.RECENT_DREAMS_PROMPT + diary.REPLY_PROMPT


# --- As production runs: operational lanes, semantic actions, the main session's strict fork ------

pytest_plugins = ('test_kin_mind',)


def test_as_production_runs_the_fork_is_asked_for_a_dream_strictly_and_shown_today_s_anniversary(setup):
    from test_initiative import RECEIPT, idle_review, world
    from test_main_session_review import native_provider
    mind, source, clock = setup
    said = moment(mind, source, clock, "first-clock")
    clock[0] += timedelta(days=7)
    memory, actions = world(setup)
    memory.configure({"dreams": True, "anniversaries": True})
    asleep(mind, source)
    diary_entry = mind.engine.receive(SourceInput(namespace="kin-reflection", key="d1", scope=mind.scope, authority="model",
        text=DIARY_PREFIX + "今天拆了一只旧闹钟。", occurred_at=mind.clock(),
        metadata={"role": "assistant", "host_event": "diary", "internal": True, "topic": "钟表"}))["id"]
    jobs = idle_review(mind, memory, actions, clock)
    frames = []

    def exchange(request):
        frames.append(request)
        return {"state": "complete", "receipt": RECEIPT, "result": {
            "reason": "睡着了。", "dream": {"text": DREAM},
            "next_move": {"move": "rest", "reason": "小光还在睡", "grounds": [], "wish_ref": None, "step_ref": None, "alternative": ""}}}
    result = jobs.run_one(native_provider(mind, exchange), lane="action")
    assert result["state"] == "complete", result
    schema, contract, context = frames[0]["schema"], frames[0]["contract"], frames[0]["context"]
    assert schema["properties"]["dream"]["anyOf"][0] == {"$ref": "#/$defs/Dream"} and "dream" in schema["required"]
    assert dreams.DREAM_PROMPT in contract and initiative.ANNIVERSARY_PROMPT in contract
    assert [item["id"] for item in context["dream_material"]] == [diary_entry]
    assert context["autonomy_context"]["initiative_facts"]["anniversaries_today"][0]["moment_id"] == said
    assert not leaks(context)
    (stored,) = dreams_of(mind)
    assert mind.engine.source(stored["id"], content=True).read_text() == dreams.DREAM_PREFIX + DREAM
