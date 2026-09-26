"""The conversation habits in a context name the messages their standing entries were set from, so what
rendered them -- a prepared delivery, a window receipt, a compression -- is found and loses her words
when such a message is deleted, even where nothing else in it names the message. They name them as
`source_ids` and `record_ids`, never as dependencies: a revised message keeps its habit, marked for
review, and the context keeps showing it (K1-18, CL8-MM-01 follow-up). What a release before this one
rendered from the habits names nothing: it goes, in the scope, when a message that set a habit is
deleted, or by the release's reerase for one deleted before; and nothing this release keeps is taken
for it, so a reerase with nothing left to erase plans nothing. The state read shows the habits with
the same names (CL9-MM-02)."""
import json

import pytest

from eventmem.core.db import dumps, named_in
from eventmem.core.models import RevisionInput

from kin_mind import erasure
from kin_mind.context import CACHE_RESTS_ON, HABITS_ITEM, RESTS_ON, Contexts, unnamed_compression
from kin_mind.erasure import ERASED
from kin_mind.memory import MemoryContinuity

from test_erasure import LateModel, settle, stored_words, system, texts_everywhere  # noqa: F401  (the fixture)
from test_fork_reads import contact_row
from test_habit_erasure import DIRECTIONS, FREQUENCY, habits_from
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)

WORDS = (*DIRECTIONS, FREQUENCY)
FILLER = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
# What a tool's result says it left out, which the owned ACP does not read (codex-runtime-patch.mjs KIN_LEFT_OUT).
ACP_LEFT_OUT = frozenset({"trace", "omitted_ids", "needs_review_ids", "deleted_ids"})


def her_words(engine):
    """Where the store still holds any of what she said the habits should be."""
    return {word: found for word in WORDS if (found := texts_everywhere(engine, word))}


def acp_read(value):
    """The store ids the owned ACP reads out of a tool's result (`kinToolResultIds`): every one it
    names, keys too, outside what it says it left out."""
    if isinstance(value, dict):
        return {found for key, item in value.items() if key not in ACP_LEFT_OUT for found in (*named_in(key), *acp_read(item))}
    if isinstance(value, list):
        return {found for item in value for found in acp_read(item)}
    return set(named_in(value))


def rows_in(engine, table):
    with engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(f"SELECT data FROM {table}")]


def summarising(engine, monkeypatch, words=True):
    """A model that summarises each item apart; the habits' summary repeats her words, when `words`."""
    return LateModel(engine, monkeypatch, lambda: None, lambda asked: {
        "entries": [{"item_ids": [identifier], "summary": "她想让我去看" + "、".join(DIRECTIONS) + "，" + FREQUENCY
                     if words and identifier == HABITS_ITEM else "Harbour walk."} for identifier in asked["allowed_item_ids"]],
        "omitted_ids": []})


def as_a_release_before_kept_them(engine, showing=None):
    """Every context and compression kept so far, as a release before this one kept it: the habits named
    nothing they rest on, and no compression named `rests_on`. `showing`: an event whose context also
    showed an item, by its id alone."""
    def bare(entries):
        return [{key: value for key, value in entry.items() if key not in RESTS_ON} for entry in entries or []]

    showing = showing or {}
    with engine.db.connect(write=True) as conn:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for key, data in conn.execute("SELECT rowid,data FROM mind_context_deliveries").fetchall() if "mind_context_deliveries" in present else ():
            value = json.loads(data)
            also = [{"id": showing[value["event_id"]], "revision": 1, "depth": "index"}] if value["event_id"] in showing else []
            conn.execute("UPDATE mind_context_deliveries SET data=? WHERE rowid=?",
                         (dumps({**value, "items": [*bare(value["items"]), *also]}), key))
        for key, data in conn.execute("SELECT rowid,data FROM mind_context_windows").fetchall() if "mind_context_windows" in present else ():
            value = json.loads(data)
            receipts = {event: {**receipt, "index": [*bare(receipt["index"]), *([{"id": showing[event], "revision": 1, "depth": "index"}]
                                                                                 if event in showing else [])]}
                        for event, receipt in value["receipts"].items()}
            conn.execute("UPDATE mind_context_windows SET data=? WHERE rowid=?", (dumps({**value, "receipts": receipts}), key))
        for key, data in conn.execute("SELECT rowid,data FROM mind_context_cache").fetchall():
            value = json.loads(data)
            value.pop(CACHE_RESTS_ON)  # every compression this release keeps names it
            conn.execute("UPDATE mind_context_cache SET data=? WHERE rowid=?", (dumps(value), key))


def notes(engine, contexts, source):
    return [contexts.record_item(engine.get(engine.source(source(f"walk-{index}", f"Harbour walk note {index}. " + FILLER))["record_ids"][0]))
            for index in range(2)]


@pytest.mark.parametrize("receipt_mode", [True, False])
def test_a_context_that_rendered_only_the_habits_keeps_none_of_her_words_once_the_message_is_deleted(system, receipt_mode):
    """A background context with no query renders the habits and nothing that names the messages that
    set them; its prepared delivery, or its window receipt, now names them through the habits alone.
    The message is deleted: the delivery or the receipt loses its words, and no table holds them."""
    mind, memory, source, clock = system
    directions, reply = habits_from(memory, clock)
    record = mind.engine.source(directions)["record_ids"][0]
    Contexts(mind).build("", purpose="chat", session="thread-1", event_id="turn-1", receipt_mode=receipt_mode)
    deliveries, windows = stored_words(mind.engine, "thread-1")
    [kept] = deliveries if receipt_mode else windows
    assert all(word in kept for word in WORDS), "her words were rendered"
    stored = json.loads(kept)
    named = stored["items"] if receipt_mode else stored["receipts"]["turn-1"]["index"]
    assert [entry["id"] for entry in named if directions in json.dumps(entry)] == ["conversation-habits"]
    habits = next(entry for entry in named if entry["id"] == "conversation-habits")
    assert (habits["source_ids"], habits["record_ids"]) == (sorted([directions, reply]),
                                                             sorted([record, mind.engine.source(reply)["record_ids"][0]]))

    mind.engine.delete(directions)
    settle(mind.engine)
    assert her_words(mind.engine) == {}
    deliveries, windows = stored_words(mind.engine, "thread-1")
    if receipt_mode:
        assert json.loads(deliveries[0])["text"] == ERASED
    else:
        receipt = json.loads(windows[0])["receipts"]["turn-1"]
        assert receipt["erased_at"] and receipt["rendered_text"] == ERASED


def test_a_compression_that_summarised_the_habits_is_dropped_with_the_message(system, monkeypatch):
    """The habits are compressed with two notes; the model's summary repeats her words, and both the
    batch and the whole result are cached, as is the model's answer among the cached semantic
    answers. The message is deleted: the two rows go, since they name what the habits were set from,
    and so does every semantic answer cached before the delete, none of which is served again; no
    table holds her words."""
    mind, memory, source, clock = system
    engine = mind.engine
    directions, reply = habits_from(memory, clock)
    contexts = Contexts(mind)
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
    notes = [source(f"walk-{index}", f"Harbour walk note {index}. " + filler) for index in range(2)]
    items = [contexts.habits_item(memory.habits.read()),
             *(contexts.record_item(engine.get(engine.source(sid)["record_ids"][0])) for sid in notes)]
    model = LateModel(engine, monkeypatch, lambda: None, lambda asked: {
        "entries": [{"item_ids": [identifier], "summary": ("她想让我去看" + "、".join(DIRECTIONS) + "，" + FREQUENCY)
                     if identifier == "conversation-habits" else "Harbour walk."} for identifier in asked["allowed_item_ids"]],
        "omitted_ids": []})
    packed = contexts.pack(items, "harbour walk", 300, provider=model.provider, allow_model=True, persist=True)
    assert packed["state"] == "compressed" and "rests_on" not in packed
    with engine.db.connect() as conn:
        cached = [row[0] for row in conn.execute("SELECT data FROM mind_context_cache")]
    assert len(cached) == 2 and all(DIRECTIONS[0] in row and directions in row for row in cached)
    with engine.db.connect() as conn:
        answers = [row[0] for row in conn.execute("SELECT data FROM mind_semantic_cache")]
    assert answers and any(DIRECTIONS[0] in row for row in answers) and not any(directions in row for row in answers)
    again = contexts.pack(items, "harbour walk", 300, provider=model.provider, allow_model=True, persist=True)
    assert again["cache_hit"] and "rests_on" not in again

    engine.delete(directions)
    settle(engine)
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mind_semantic_cache").fetchone()[0] == 0
    assert her_words(engine) == {}


def test_a_revised_message_keeps_its_habit_in_the_context(system):
    """The message that set the directions is corrected. The habit keeps its value, marked for review
    (K1-18), and the context still shows it: the habits name the message, and never depend on it."""
    mind, memory, source, clock = system
    directions, reply = habits_from(memory, clock)
    record = mind.engine.source(directions)["record_ids"][0]
    revision = mind.engine.get(record)["revision"]
    mind.engine.revise(record, RevisionInput(expected_revision=revision, command_id="correct-directions", action="correct",
                                             content="以后多去看看天文台的夜观", reason="她改了一下说法"))
    contexts = Contexts(mind)
    habits = memory.habits.read()
    assert habits["entries"]["exploration_directions"]["needs_review"]
    assert habits["preferences"]["exploration_directions"] == DIRECTIONS
    item = contexts.habits_item(habits)
    assert "dependencies" not in item and directions in item["source_ids"] and record in item["record_ids"]
    packed = contexts.build("", purpose="chat", session="thread-2", event_id="turn-1")
    assert "conversation-habits" in packed["covered_ids"] and "conversation-habits" not in packed["omitted_ids"]
    assert all(word in packed["text"] for word in WORDS)
    # A message that is gone is named no more: the habit it set shows no value.
    mind.engine.delete(reply)
    assert contexts.habits_item(memory.habits.read())["source_ids"] == [directions]


@pytest.mark.parametrize("receipt_mode", [True, False])
def test_what_a_release_before_rendered_from_the_habits_goes_with_the_message_that_set_one(system, monkeypatch, receipt_mode):
    """A release before this one rendered the habits twice and compressed them twice, naming nothing they
    rest on: one context also showed the message itself, and one compression covered it, by its record's
    id. Deleting a message that set no habit leaves all of them. Deleting the one that set the directions
    takes every one -- by its record's id, or as rendered from the habits -- each counted once, by a dry
    run as by the delete itself, and no table holds her words."""
    mind, memory, source, clock = system
    engine = mind.engine
    directions, reply = habits_from(memory, clock)
    [record] = engine.source(directions)["record_ids"]
    contexts = Contexts(mind)
    for turn in ("turn-1", "turn-2"):
        contexts.build("", purpose="chat", session="thread-1", event_id=turn, receipt_mode=receipt_mode)
    walks, model = notes(engine, contexts, source), summarising(engine, monkeypatch)
    habits = contexts.habits_item(memory.habits.read())
    for items in ([habits, contexts.record_item(engine.get(record)), walks[0]], [habits, *walks]):
        assert contexts.pack(items, "harbour walk", 300, provider=model.provider, allow_model=True, persist=True)["state"] == "compressed"
    as_a_release_before_kept_them(engine, showing={"turn-2": record})
    table = "mind_context_deliveries" if receipt_mode else "mind_context_windows"
    before = (rows_in(engine, table), rows_in(engine, "mind_context_cache"))
    assert len(before[0]) == (2 if receipt_mode else 1) and len(before[1]) == 4
    assert all(DIRECTIONS[0] in json.dumps(value, ensure_ascii=False) and directions not in json.dumps(value)
               for value in [*before[0], *before[1]]), "her words, and nothing names the message"

    unrelated = source("unrelated", "An unrelated note about the weather.")
    mind.engine.delete(unrelated)
    assert (rows_in(engine, table), rows_in(engine, "mind_context_cache")) == before

    with engine.db.connect() as conn:
        planned = erasure.erase(conn, [record], [directions], mind.clock(), write=False)
    engine.delete(directions)
    with engine.db.connect() as conn:
        took = json.loads(conn.execute("SELECT data FROM metrics WHERE name='memory_erased' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert ((planned.get("context_receipts"), planned.get("mind_context_cache"))
            == (took.get("context_receipts"), took.get("mind_context_cache")) == (2, 4)), (planned, took)
    settle(engine)
    assert her_words(engine) == {}
    assert rows_in(engine, "mind_context_cache") == []
    if receipt_mode:
        assert [value["text"] for value in rows_in(engine, table)] == [ERASED, ERASED]
    else:
        [window] = rows_in(engine, table)
        assert all(receipt["erased_at"] and receipt["rendered_text"] == ERASED for receipt in window["receipts"].values())


def test_the_release_reerase_takes_what_a_release_before_rendered_from_the_habits_and_then_plans_nothing(system, monkeypatch):
    """A delete made before this release took the message that set the directions, and left what that
    release had rendered and compressed from the habits, naming nothing. This release's reerase takes
    them, and a second run plans nothing. Nor does a run plan anything for what this release renders,
    compresses and asks the model after a delete -- a delivery, a window receipt, a compression of habits
    that rest on nothing now, a cached semantic answer: those name what they rest on, even nothing, and
    an answer asked since the delete is served in its generation and holds nothing deleted."""
    from eventmem.core import repair

    mind, memory, source, clock = system
    engine = mind.engine
    directions, reply = habits_from(memory, clock)
    contexts = Contexts(mind)
    contexts.build("", purpose="chat", session="thread-1", event_id="turn-1", receipt_mode=True)
    walks = notes(engine, contexts, source)
    model = summarising(engine, monkeypatch)
    assert contexts.pack([contexts.habits_item(memory.habits.read()), *walks], "harbour walk", 300, provider=model.provider,
                         allow_model=True, persist=True)["state"] == "compressed"
    as_a_release_before_kept_them(engine)
    with monkeypatch.context() as before:
        before.setattr(erasure, "_unnamed_habits", lambda *args, **options: (0, 0))
        engine.delete(directions)
    settle(engine)
    left = {table for word in DIRECTIONS for table, _ in texts_everywhere(engine, word)}
    assert {"mind_context_deliveries", "mind_context_cache"} <= left, left

    plan = repair.run(engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert plan["layers"] == {"context_receipts": 1, "mind_context_cache": 2}, plan["layers"]
    repair.run(engine.db.root, apply=True, steps=("reerase",))
    settle(engine)
    assert her_words(engine) == {}
    assert repair.run(engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]["derived_rows"] == 0

    engine.delete(reply)
    habits = contexts.habits_item(memory.habits.read())
    assert habits["revision"] and habits["source_ids"] == habits["record_ids"] == [], "the habits rest on nothing now"
    contexts.build("", purpose="chat", session="thread-1", event_id="turn-2", receipt_mode=True)
    contexts.build("", purpose="chat", session="thread-2", event_id="turn-1", receipt_mode=False)
    quiet = summarising(engine, monkeypatch, words=False)
    assert contexts.pack([habits, *walks], "tide table", 300, provider=quiet.provider, allow_model=True, persist=True)["state"] == "compressed"
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_semantic_cache").fetchone()[0]
    [window] = [value for value in rows_in(engine, "mind_context_windows") if "turn-1" in value["receipts"] and not value["receipts"]["turn-1"].get("erased_at")]
    assert [entry for entry in window["receipts"]["turn-1"]["index"] if entry["id"] == HABITS_ITEM][0]["source_ids"] == []
    again = repair.run(engine.db.root, steps=("reerase",))["steps"]["reerase"]["plan"]
    assert again["derived_rows"] == 0 and not again["layers"], again["layers"]


def test_a_compression_kept_before_that_may_hold_the_habits_is_told_apart():
    """Which cached compressions a release before this one kept may hold the conversation habits: a batch
    or a whole pack that covered them, and a reduction, whose groups name no item. Not one that covered
    other items only, not an overview, and nothing this release keeps, which names `rests_on`."""
    batch = {"value": {"entries": [{"item_ids": [HABITS_ITEM], "summary": "…"}], "omitted_ids": []}, "receipt": {}}
    whole = {"text": "…", "covered_ids": ["affect", HABITS_ITEM], "omitted_ids": []}
    reduction = {"value": {"entries": [{"item_ids": ["group:0", "group:1"], "summary": "…"}], "omitted_ids": []}, "receipt": {}}
    others = {"value": {"entries": [{"item_ids": ["mem_" + "0" * 32], "summary": "…"}], "omitted_ids": [HABITS_ITEM]}, "receipt": {}}
    overview = {"text": "…", "source": {"id": "mem_" + "0" * 32}, "receipt": None, "coverage": "overview"}
    assert [unnamed_compression(value) for value in (batch, whole, reduction, others, overview)] == [True, True, True, False, False]
    assert not any(unnamed_compression({**value, CACHE_RESTS_ON: []}) for value in (batch, whole, reduction))


def test_the_state_read_names_the_messages_the_habits_were_set_from(setup):
    """`read_affective_state` shows the habits -- her words -- and names the messages their standing
    entries were set from, as the background item does, so a fork that reads them there names those
    among what it read. A contact draft whose fork read them there and wrote from them, with the
    background item left out of its memory context as already seen in the window, is found and loses
    the words when the message that set them is deleted (CL9-MM-02)."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True})
    directions, reply = habits_from(memory, clock)
    records = {sid: mind.engine.source(sid)["record_ids"][0] for sid in (directions, reply)}
    state = Contexts(mind).affective()
    habits = state["conversation_habits"]
    assert habits["revision"] == memory.habits.read()["revision"], "the revision an update expects"
    assert habits["preferences"]["exploration_directions"] == DIRECTIONS
    assert (habits["source_ids"], habits["record_ids"]) == (sorted(records), sorted(records.values()))
    read = acp_read(state)
    assert {directions, records[directions]} <= read

    wish(mind, source, "habits-wish", content="Suggest an outing she asked for")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    receipt = {"channel": "fork", "tool_calls": [{"name": "memorypalace.read_affective_state", "ok": True,
                                                  "ids": [{"id": identifier} for identifier in sorted(read)]}]}
    pending = mind.settle_contact(attempt_id=attempt["id"], state="pending", text=f"这周要不要去看{DIRECTIONS[0]}？",
                                  shown_ids=[], draft_receipt=receipt)
    assert pending["state"] == "pending" and records[directions] in pending["evaluated_ids"]
    assert DIRECTIONS[0] in contact_row(mind, attempt["id"])[1]["text_excerpt"]

    mind.engine.delete(directions)
    settle(mind.engine)
    state, row = contact_row(mind, attempt["id"])
    assert row["text_excerpt"] == ERASED and row["text_digest"]
    assert texts_everywhere(mind.engine, DIRECTIONS[0]) == set()
    assert Contexts(mind).affective()["conversation_habits"]["source_ids"] == [reply], "a message that is gone is named no more"
