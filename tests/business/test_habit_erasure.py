"""A conversation habit goes with the message it was set from, its words and all (CL8-MM-01).

小光 names the directions Kin should explore and how often, and the habit keeps her words as its
value, beside the message she said them in. Deleting the message used to take only the habit's
reason: the value stayed in the habits, their revisions and their commands, a read still carried it
in its entries, and a queue row that had frozen its memory context kept the habits read with it,
which the row's next attempt handed to the model. Now the value goes with the reason wherever a
habit is kept, a read shows the entry without one, and no queue row freezes the habits: every
attempt reads them afresh. Said again, a preference is set again as before."""
import json

from fastapi.testclient import TestClient

from eventmem.core.api import create_app

from kin_mind import erasure
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.erasure import ERASED
from kin_mind.habits import DEFAULTS
from kin_mind.memory import MemoryContinuity
from test_derived_erasure import answered, paid, queue_row
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

DIRECTIONS = ["天文台的夜观", "老木匠的手艺"]
FREQUENCY = "每周两三次就好"
REASON = "她说想让我多去看看天文台的夜观和老木匠的手艺"
WORDS = (*DIRECTIONS, FREQUENCY, REASON)
TABLES = ("mind_conversation_habits", "mind_habit_revisions", "mind_habit_commands")
TOKEN = "synthetic-test-credential"


def said(memory, clock, key, text):
    return memory.ingest({"id": key, "kind": "owner-message", "at": clock[0].isoformat(), "text": text})["source_id"]


def habits_from(memory, clock):
    """Her directions and frequency from one message, her reply choice from another. Returns both messages."""
    directions = said(memory, clock, "said-directions", "以后多去看看天文台的夜观和老木匠的手艺，每周两三次就好")
    reply = said(memory, clock, "said-reply", "闲聊的时候你可以自己决定回不回")
    memory.habits.update({"command_id": "directions", "expected_revision": 0, "evidence_ids": [directions], "reason": REASON,
                          "preferences": {"exploration_directions": DIRECTIONS, "exploration_frequency": FREQUENCY}})
    memory.habits.update({"command_id": "reply", "expected_revision": 1, "evidence_ids": [reply], "reason": "她说闲聊可以自己决定",
                          "preferences": {"reply_choice": "autonomous"}})
    return directions, reply


def words_in(value):
    text = json.dumps(value, ensure_ascii=False)
    return [word for word in WORDS if word in text]


def rows(mind, table):
    with mind.engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(f"SELECT data FROM {table}")]


def test_a_deleted_message_takes_the_habit_it_set_out_of_every_table_every_read_and_every_later_write(setup):
    """The message goes, and the three tables keep the entries it set with their values emptied and
    their reasons gone; the reply choice from another message stands. A read and the API show the
    entries without a value and the preferences at their defaults, a later write carries nothing
    back, a second run of the same erase finds nothing, and no table of the store holds the words."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True})
    directions, reply = habits_from(memory, clock)
    record = mind.engine.source(directions)["record_ids"][0]
    assert all(words_in(rows(mind, table)) for table in TABLES), "set: all three tables hold her words"

    mind.engine.delete(directions)
    for table in TABLES:
        for row in rows(mind, table):
            assert not words_in(row), table
            entries = row["entries"]
            assert entries["exploration_directions"]["value"] == [] and entries["exploration_frequency"]["value"] == ERASED, table
            assert entries["exploration_directions"]["reason"] == ERASED, table
            if "reply_choice" in entries:
                assert entries["reply_choice"]["value"] == "autonomous" and entries["reply_choice"]["reason"] == "她说闲聊可以自己决定"
    with mind.engine.db.connect() as conn:
        assert erasure.erase(conn, [record], [directions], clock[0].isoformat(), write=False, again=True) == {}, "nothing left to take"

    read = memory.habits.read()
    assert read["preferences"]["exploration_directions"] == DEFAULTS["exploration_directions"]
    assert read["preferences"]["exploration_frequency"] == DEFAULTS["exploration_frequency"]
    assert read["preferences"]["reply_choice"] == "autonomous", "what another message set stands"
    for key in ("exploration_directions", "exploration_frequency"):
        assert "value" not in read["entries"][key] and read["entries"][key]["source_deleted"] is True
    client = TestClient(create_app(engine=mind.engine, token=TOKEN, workers=False, mcp_enabled=False))
    answer = client.get("/v1/conversation/habits", params={"persona": "synthetic"}, headers={"Authorization": "Bearer " + TOKEN})
    assert answer.status_code == 200 and answer.json()["preferences"] == read["preferences"]
    assert not words_in(answer.json()) and "value" not in answer.json()["entries"]["exploration_directions"]

    # A later write is built on a read: it carries nothing of what the delete took back.
    paused = said(memory, clock, "said-pause", "这几天先别出去探索了")
    memory.habits.update({"command_id": "pause", "expected_revision": 2, "evidence_ids": [paused], "reason": "她说这几天先别探索",
                          "preferences": {"exploration_paused": True}})
    assert memory.habits.read()["preferences"]["exploration_paused"] is True
    settle(mind.engine)
    for word in WORDS:
        assert texts_everywhere(mind.engine, word) == set(), word

    # Said again, a preference is set again as before.
    again = said(memory, clock, "said-again", "还是多去看看天文台的夜观吧")
    memory.habits.update({"command_id": "again", "expected_revision": 3, "evidence_ids": [again], "reason": "她又说了一遍",
                          "preferences": {"exploration_directions": DIRECTIONS[:1]}})
    assert memory.habits.read()["preferences"]["exploration_directions"] == DIRECTIONS[:1]


def test_a_copy_an_earlier_build_froze_and_a_proposal_lose_the_habit_words_at_the_delete(setup):
    """A queue row an earlier build of this release wrote: it names what its model was shown, not the
    message, and keeps the memory context it froze -- the habits read with it -- and the proposal that
    set them. A row a release before that wrote names nothing, and keeps them too. At the delete, both
    lose the words of the message: the entries' values, the preferences they gave the read, the
    proposal's preferences; the reply choice from another message stays in the copy of the named row.
    The named row's next attempt is shown the habits read afresh, and keeps none of them after."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True})
    directions, reply = habits_from(memory, clock)
    shown = source("shown", "今天天气不错")
    jobs = Appraisals(mind)
    named, legacy = jobs.enqueue([shown], "synthetic-v1"), jobs.enqueue([source("older", "昨天也不错")], "synthetic-v1")
    proposal = {"reason": "她说了想让我多看看的地方", "habits": {"preferences": {"exploration_directions": DIRECTIONS, "exploration_frequency": FREQUENCY},
                                                            "evidence_ids": [directions], "reason": REASON, "expected_revision": 0}}
    frozen = memory.semantic_context()
    assert frozen["conversation_habits"]["preferences"]["exploration_directions"] == DIRECTIONS, "as an earlier build froze it"
    with mind.engine.db.connect(write=True) as conn:
        for job, data in ((named, {"evaluated_ids": [shown], "tombstone_mark": erasure.tombstone_mark(conn)}), (legacy, {})):
            row = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])
            row.update(data, proposed_result=proposal, frozen_memory_context=frozen)
            conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (json.dumps(row, ensure_ascii=False), job["id"]))
        conn.execute("UPDATE mind_appraisals SET state='complete' WHERE id=?", (legacy["id"],))
    assert words_in(queue_row(mind, named["id"])[2]) and words_in(queue_row(mind, legacy["id"])[2])

    mind.engine.delete(directions)
    for job in (named, legacy):
        assert not words_in(queue_row(mind, job["id"])[2]), job["id"]
    _, _, row = queue_row(mind, named["id"])
    habits = row["frozen_memory_context"]["conversation_habits"]
    assert habits["preferences"]["exploration_directions"] == [] and habits["preferences"]["exploration_frequency"] == ERASED
    assert habits["entries"]["exploration_directions"]["value"] == [] and habits["entries"]["exploration_frequency"]["value"] == ERASED
    assert habits["preferences"]["reply_choice"] == "autonomous" and habits["entries"]["reply_choice"]["value"] == "autonomous", \
        "what another message set stays in a row that did not name this one"
    assert row["proposed_result"]["habits"]["preferences"]["exploration_directions"] == []
    assert row["proposed_result"]["habits"]["expected_revision"] == 0, "numbers and ids stay"
    with mind.engine.db.connect() as conn:
        assert erasure.erase(conn, [], [directions], clock[0].isoformat(), write=False, again=True) == {}, "nothing left to take"

    seen = []

    class Again:
        def appraise(self, context):
            seen.append(context["memory_context"]["conversation_habits"])
            paid(self)
            return answered(Appraisal(reason="看了看今天"))

    jobs.run_one(Again(), job_id=named["id"])
    assert len(seen) == 1 and seen[0]["preferences"] == memory.habits.read()["preferences"], "read afresh, not the copy"
    assert seen[0]["preferences"]["exploration_frequency"] == DEFAULTS["exploration_frequency"]
    assert "conversation_habits" not in queue_row(mind, named["id"])[2]["frozen_memory_context"], "and the row keeps it no more"


def in_the_graph(memory, record_id):
    with memory.engine.db.connect(write=True) as conn:
        memory.graph.ensure(conn, record_id)


def test_a_queue_row_freezes_no_habits_and_its_next_attempt_reads_them_afresh(setup):
    """A backfill's first attempt is answered and refused -- something it was shown was deleted
    meanwhile -- so the row names what that call was shown: her habits among it, read as they were.
    Its second attempt freezes its context and waits for its evidence to be compressed: the row keeps
    no habits. The message that set them is deleted. The next attempt is shown the habits as the
    store has them now -- the defaults, not the words, not a blank -- beside the context it froze."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "graph": True, "operational_lanes": True})
    directions, _ = habits_from(memory, clock)
    doomed = source("doomed", "她说今天去了图书馆")
    in_the_graph(memory, mind.engine.source(doomed)["record_ids"][0])
    primary = source("today", "今天天气不错")
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1", origin="reflection", stimulus="memory-backfill")
    shown = []

    class Refused:
        def appraise(self, context):
            shown.append(context["memory_context"]["conversation_habits"])
            mind.engine.delete(doomed)
            paid(self)
            return answered(Appraisal(reason="翻了翻以前的事"))

    class Compressing:
        def appraise(self, context):
            raise RuntimeError("deepseek-evidence-compression-pending:budget")

    class Again:
        def appraise(self, context):
            shown.append(context["memory_context"]["conversation_habits"])
            paid(self)
            return answered(Appraisal(reason="翻了翻以前的事"))

    def due():
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))

    jobs.run_one(Refused())
    assert shown[0]["preferences"]["exploration_directions"] == DIRECTIONS, "while the message stands, the habits are shown"
    state, _, data = queue_row(mind, job["id"])
    assert state == "pending" and data["evaluated_ids"], data.get("error")
    due()
    jobs.run_one(Compressing())
    state, _, data = queue_row(mind, job["id"])
    assert state == "pending" and data["error"].startswith("deepseek-evidence-compression-pending"), data.get("error")
    assert data["frozen_memory_context"]["recent_interaction"] and "conversation_habits" not in data["frozen_memory_context"], \
        "the row keeps the dialogue it froze -- her message among it, by its id -- and no habits"

    mind.engine.delete(directions)
    assert not words_in(queue_row(mind, job["id"])[2]), "her message goes from the row with its id"
    due()
    jobs.run_one(Again())
    assert len(shown) == 2 and not words_in(shown[1]), "the next attempt is shown none of her words"
    assert shown[1]["preferences"]["exploration_directions"] == DEFAULTS["exploration_directions"]
    assert shown[1]["preferences"]["exploration_frequency"] == DEFAULTS["exploration_frequency"], "the store's habits now, not a blank"
    assert shown[1]["preferences"]["reply_choice"] == "autonomous"
    settle(mind.engine)
    for word in WORDS:
        assert texts_everywhere(mind.engine, word) == set(), word


def test_the_release_reerase_takes_the_habit_words_a_delete_before_it_left(setup, monkeypatch):
    """A delete as a release before this one made it took a habit's reason and left its value. The
    release's reerase replays every delete there was: it takes the values too, in all three tables,
    and a second run finds nothing more to take."""
    from eventmem.core import repair

    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True})
    directions, _ = habits_from(memory, clock)
    monkeypatch.setattr(erasure, "HABIT_ENTRY", frozenset({"not-a-habit"}))
    monkeypatch.setattr(erasure, "HABIT_PREFERENCES", "not-a-habit")
    mind.engine.delete(directions)
    monkeypatch.undo()
    settle(mind.engine)
    left = {table for word in DIRECTIONS for table, _ in texts_everywhere(mind.engine, word)}
    assert set(TABLES) <= left and REASON not in json.dumps(rows(mind, TABLES[0]), ensure_ascii=False), "the value was left, the reason taken"

    plan = repair.run(mind.engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert all(plan["layers"].get(table) for table in TABLES), plan["layers"]
    repair.run(mind.engine.db.root, apply=True, steps=("reerase",))
    settle(mind.engine)
    for word in WORDS:
        assert texts_everywhere(mind.engine, word) == set(), word
    again = repair.run(mind.engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert not any(again["layers"].get(table) for table in TABLES), again["layers"]


def test_only_a_habit_loses_its_value_and_a_number_or_a_switch_keeps_it():
    """A `value` goes where a habit keeps one -- beside the evidence, the reason and the time it was
    said -- and nowhere else: elsewhere a value is a number or an enum, and a dict that cites what was
    erased keeps it. Of a habit's values a string goes and a list of them is emptied; a number and a
    switch stay. A proposal's preferences go with the evidence it cites, a read's with its entries'."""
    gone = "src_" + "1" * 32
    ref = {"source_id": gone, "record_id": "mem_" + "1" * 32, "revision": 1}

    def entry(value):
        return {"value": value, "evidence": [ref], "reason": REASON, "at": "2026-09-26T00:00:00+00:00", "revision": 1}

    value = {"entries": {"exploration_directions": entry(DIRECTIONS), "exploration_frequency": entry(FREQUENCY),
                         "exploration_paused": entry(True), "exploration_min_interval_minutes": entry(30)},
             "preferences": {"exploration_directions": DIRECTIONS, "exploration_paused": True, "reply_choice": "always"},
             "proposal": {"preferences": {"exploration_frequency": FREQUENCY, "exploration_min_interval_minutes": 30},
                          "evidence_ids": [gone], "expected_revision": 1},
             "setting": {"value": "fork", "evidence": [ref], "kind": "channel"}}
    out = erasure.scrub(value, frozenset({gone}))
    entries = out["entries"]
    assert entries["exploration_directions"]["value"] == [] and entries["exploration_frequency"]["value"] == ERASED
    assert entries["exploration_paused"]["value"] is True and entries["exploration_min_interval_minutes"]["value"] == 30
    assert out["preferences"] == {"exploration_directions": [], "exploration_paused": True, "reply_choice": "always"}, \
        "a read loses what its erased entries gave it; the default beside them stays"
    assert out["proposal"]["preferences"] == {"exploration_frequency": ERASED, "exploration_min_interval_minutes": 30}
    assert out["setting"]["value"] == "fork", "not a habit: its value stays"
    assert erasure.scrub(out, frozenset({gone})) is out, "a second run changes nothing"
