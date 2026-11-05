"""A topic candidate takes its title only from a member it lists (CL7B-MM-03).

Organize titles a family by one member -- the first by id: its topic, else its title -- and the store
deletes the family with any member. The memory context lists a family's members that are current and
visible to the read, and a queue row keeps that context frozen for its retry. A title taken from a
member left out would outlive it there: the delete finds no id of it in the copy, and the retry's
check of the listed members finds nothing changed. So the candidate is titled by the family's title
only where a listed member has it, and otherwise by the first listed member's."""
import json
from pathlib import Path

from eventmem.core.models import RecordInput
from eventmem.core.organize import prepare_communities

from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.memory import MemoryContinuity
from test_derived_erasure import answered, paid, queue_row
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

MARKER = "xyzzyquux"
# Organize titles a family by the member with the lowest id.
FIRST, SECOND, THIRD = ("mem_" + digit * 32 for digit in "123")
FIRST_TOPIC, SECOND_TOPIC = f"{MARKER} 的旧事", "周末去公园"


def configured(mind):
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "graph": True, "auto_volumes": True, "operational_lanes": True})
    return memory


def family(mind, source, clock):
    """Three messages, one record each, related to one another: one family, titled by the first's topic.
    Returns the first message, and the family as organize stored it."""
    engine, scope = mind.engine, mind.scope
    sources = {}
    for rid, key, text, topic, title in ((FIRST, "first", f"她说起 {MARKER} 的旧事", FIRST_TOPIC, f"{MARKER} 那天"),
                                         (SECOND, "second", "她说周末想去公园走走", SECOND_TOPIC, "周末"),
                                         (THIRD, "third", "她说公园的银杏黄了", "公园的银杏", "银杏")):
        sources[rid] = source(key, text)
        engine.add_record(RecordInput(id=rid, kind="episode", title=title, content=text, scope=scope,
                                      source_ids=[sources[rid]], valid_from=clock[0].isoformat(),
                                      attributes={"topic": topic}), "record-" + rid)
    for subject, object_ in ((FIRST, SECOND), (FIRST, THIRD), (SECOND, THIRD)):
        engine.relate(subject, "related", object_)
    apply = prepare_communities(engine, scope)
    with engine.db.connect(write=True) as conn:
        apply(conn)
        [stored] = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM families WHERE scope=? AND kind='family'", (scope.key(),))]
    assert stored["members"] == [FIRST, SECOND, THIRD] and stored["title"] == FIRST_TOPIC, "organize titles it by the first"
    return sources[FIRST], stored


def in_the_graph(memory, record_id):
    """A graph node for the record: the memory context offers the families of what its graph shows."""
    with memory.engine.db.connect(write=True) as conn:
        memory.graph.ensure(conn, record_id)


def test_a_topic_candidate_takes_its_title_only_from_a_member_it_lists(setup):
    """While the member organize titled the family by is listed, the candidate carries the family's
    title as organize gave it -- its topic, not its title. A newer version of that member's message
    comes, so the member is not current and is left out: the candidate lists the other two and is
    titled by the first of them, with nothing of the one it left out."""
    mind, source, clock = setup
    memory = configured(mind)
    _, stored = family(mind, source, clock)
    in_the_graph(memory, SECOND)
    [candidate] = memory.semantic_context()["topic_candidates"]
    assert [m["id"] for m in candidate["members"]] == [FIRST, SECOND, THIRD]
    assert candidate["id"] == stored["id"] and candidate["title"] == FIRST_TOPIC, "the family's own title"
    source("first", "她改口说那是周末的事", version="2")
    [candidate] = memory.semantic_context()["topic_candidates"]
    assert [m["id"] for m in candidate["members"]] == [SECOND, THIRD] and candidate["omitted_count"] == 1
    assert candidate["title"] == SECOND_TOPIC, "the first listed member's topic"
    assert MARKER not in json.dumps(candidate, ensure_ascii=False)
    guide = " ".join((Path(__file__).resolve().parents[2] / "docs" / "operations.md").read_text(encoding="utf-8").split())
    assert "A topic candidate is titled only by a member it shows" in guide


def test_a_title_from_a_member_a_frozen_candidate_left_out_is_in_neither_the_row_nor_the_next_attempt(setup):
    """A backfill's first attempt is answered and refused -- something it was shown was deleted
    meanwhile -- so the row names what that call was shown. Its second attempt builds the context
    again, freezes it and waits for its evidence to be compressed. The member the family is titled
    by was not current then and is not listed. It is deleted, and the store deletes the family with
    it. Neither the queue row nor the next attempt, which is shown the frozen context, has its words;
    the candidate stays, titled by a member it lists."""
    mind, source, clock = setup
    memory = configured(mind)
    first, stored = family(mind, source, clock)
    in_the_graph(memory, SECOND)
    source("first", "她改口说那是周末的事", version="2")
    doomed = source("doomed", "她说今天去了图书馆")
    in_the_graph(memory, mind.engine.source(doomed)["record_ids"][0])
    primary = source("today", "今天天气不错")
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1", origin="reflection", stimulus="memory-backfill")

    class Refused:
        def appraise(self, context):
            mind.engine.delete(doomed)
            paid(self)
            return answered(Appraisal(reason="翻了翻以前的事"))

    class Compressing:
        def appraise(self, context):
            raise RuntimeError("deepseek-evidence-compression-pending:budget")

    shown = []

    class Again:
        def appraise(self, context):
            shown.append(json.dumps(context, ensure_ascii=False))
            paid(self)
            return answered(Appraisal(reason="翻了翻以前的事"))

    def due():
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))

    jobs.run_one(Refused())
    state, _, data = queue_row(mind, job["id"])
    assert state == "pending" and data["evaluated_ids"] and "frozen_memory_context" not in data, data.get("error")
    due()
    jobs.run_one(Compressing())
    state, _, data = queue_row(mind, job["id"])
    assert state == "pending" and data["error"].startswith("deepseek-evidence-compression-pending"), data.get("error")
    [candidate] = data["frozen_memory_context"]["topic_candidates"]
    assert candidate["id"] == stored["id"] and [m["id"] for m in candidate["members"]] == [SECOND, THIRD]
    assert not {first, FIRST} & set(data["evaluated_ids"]), "the row does not name the member left out"
    assert candidate["title"] == SECOND_TOPIC and MARKER not in json.dumps(data, ensure_ascii=False)

    mind.engine.delete(first)
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM families WHERE id=?", (stored["id"],)).fetchone(), "the store deletes the family"
    state, _, data = queue_row(mind, job["id"])
    assert state == "pending" and MARKER not in json.dumps(data, ensure_ascii=False), "the queue row"
    assert data["frozen_memory_context"]["topic_candidates"][0]["title"] == SECOND_TOPIC, "the copy it keeps stands"
    due()
    jobs.run_one(Again())
    assert len(shown) == 1 and stored["id"] in shown[0], "the next attempt is shown the frozen copy"
    assert MARKER not in shown[0], "and nothing of the member it left out"
    assert SECOND_TOPIC in shown[0], "titled by a member it lists"
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
