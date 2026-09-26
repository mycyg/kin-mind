"""The conversation habits in a context name the messages their standing entries were set from, so what
rendered them -- a prepared delivery, a window receipt, a compression -- is found and loses her words
when such a message is deleted, even where nothing else in it names the message. They name them as
`source_ids` and `record_ids`, never as dependencies: a revised message keeps its habit, marked for
review, and the context keeps showing it (K1-18, CL8-MM-01 follow-up). What a release before this one
rendered from the habits names nothing: it goes, in the scope, when a message that set a habit is
deleted, or by the release's reerase for one deleted before; and nothing this release keeps is taken
for it, so a reerase with nothing left to erase plans nothing. The state read shows the habits with
the same names (CL9-MM-02). A window that saw the habits gets them again once a delete took a value
from them (CL9-MM-03). Both name each message at the revision its habit was set from as well: a fork
that read only the habits did not read the message as she corrected it since, and may not cite that
(K1-16, CL10-MM-01). An update of the habits says it expects the state read's revision (CL10-MM-03)."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core.db import NAMED, digest, dumps
from eventmem.core.models import RevisionInput

from kin_mind import context, erasure
from kin_mind.appraisal import Appraisals
from kin_mind.context import CACHE_RESTS_ON, HABITS_ITEM, RESTS_ON, SET_FROM, Contexts, unnamed_compression
from kin_mind.context_delivery import ContextDelivery
from kin_mind.erasure import ERASED
from kin_mind.memory import MemoryContinuity

from test_erasure import LateModel, settle, stored_words, system, texts_everywhere  # noqa: F401  (the fixture)
from test_fork_reads import contact_row
from test_habit_erasure import DIRECTIONS, FREQUENCY, habits_from, said
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)

WORDS = (*DIRECTIONS, FREQUENCY)
FILLER = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
# What a tool's result says it left out, which the owned ACP does not read (codex-runtime-patch.mjs KIN_LEFT_OUT).
ACP_LEFT_OUT = frozenset({"trace", "omitted_ids", "needs_review_ids", "deleted_ids"})


def her_words(engine):
    """Where the store still holds any of what she said the habits should be."""
    return {word: found for word in WORDS if (found := texts_everywhere(engine, word))}


def acp_ids(result):
    """What the owned ACP keeps of a tool's result (codex-runtime-patch.mjs `kinToolResultIds`), as
    [{id, revision}]: every store id it names, keys and JSON carried as text too, outside what it says it
    left out and what it lists as deleted. One under `id` or `record_id` beside an integer `revision` is
    at that revision, any other mention bare, and a bare entry stands only until a revision is named."""
    found, deleted = {}, set(result.get("deleted_ids") or ()) if isinstance(result, dict) else set()

    def add(identifier, revision):
        if identifier in deleted:
            return
        revision = revision if type(revision) is int else None
        first = found.get(identifier)
        if first is not None:
            if first["revision"] is None:
                if revision is not None:
                    first["revision"] = revision
                return
            if revision is None or revision == first["revision"] or f"{identifier}@{revision}" in found:
                return
        found[f"{identifier}@{revision}" if first is not None else identifier] = {"id": identifier, "revision": revision}

    def read(text, revision):
        if text.lstrip()[:1] in ("{", "["):
            try:
                whole = json.loads(text)
            except ValueError:
                whole = None
            if isinstance(whole, (dict, list)):
                return walk(whole)
            for line in text.split("\n"):
                try:
                    part = json.loads(line) if line.lstrip()[:1] in ("{", "[") else None
                except ValueError:
                    part = None
                if isinstance(part, (dict, list)):
                    walk(part)
                else:
                    for identifier in NAMED.findall(line):
                        add(identifier, revision)
            return
        for identifier in NAMED.findall(text):
            add(identifier, revision)

    def walk(node):
        if isinstance(node, str):
            read(node, None)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in ACP_LEFT_OUT:
                    continue
                read(str(key), None)
                if isinstance(item, str):
                    read(item, node.get("revision") if key in ("id", "record_id") else None)
                else:
                    walk(item)

    walk(result)
    return list(found.values())


def set_from(engine, *sources):
    """Each message, as a habit set from it now names it: at the revision its record has."""
    named = [{"source_id": sid, "record_id": rid, "revision": engine.get(rid)["revision"]}
             for sid in sources for rid in engine.source(sid)["record_ids"][:1]]
    return sorted(named, key=dumps)


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
        return [{key: value for key, value in entry.items() if key not in (*RESTS_ON, SET_FROM)} for entry in entries or []]

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
    set them; its prepared delivery, or its window receipt, now names them through the habits alone,
    each at the revision its habit was set from as well. The message is deleted: the delivery or the
    receipt loses its words, and no table holds them."""
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
    assert habits[SET_FROM] == set_from(mind.engine, directions, reply)

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
    among what it read, each at the revision its habit was set from (CL10-MM-01). A contact draft whose
    fork read them there and wrote from them, with the background item left out of its memory context
    as already seen in the window, is found and loses the words when the message that set them is
    deleted (CL9-MM-02)."""
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
    assert habits[SET_FROM] == set_from(mind.engine, directions, reply)
    read = acp_ids(state)
    assert {"id": directions, "revision": None} in read
    assert {"id": records[directions], "revision": mind.engine.get(records[directions])["revision"]} in read

    wish(mind, source, "habits-wish", content="Suggest an outing she asked for")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    receipt = {"channel": "fork", "tool_calls": [{"name": "memorypalace.read_affective_state", "ok": True, "ids": read}]}
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


@pytest.mark.parametrize("receipt_mode", [True, False])
def test_a_window_that_saw_the_habits_gets_them_again_once_a_delete_took_a_value(system, monkeypatch, receipt_mode):
    """The habits reach a native window -- a window receipt, or a delivery the host confirms -- and a
    later read there leaves them out as already seen. The message that set the directions is deleted:
    the habits lose the value and keep the table's revision. The next background read in the same
    window sends them again, as they are now: without her directions, with the reply choice the other
    message set (CL9-MM-03). Their revision is a hash of the table's revision and of the keys the delete
    took, never of anything she said: what the window keeps of it before and after names none of it
    (CL10-MM-02)."""
    mind, memory, source, clock = system
    engine = mind.engine
    directions, reply = habits_from(memory, clock)
    contexts = Contexts(mind)

    def read(turn):
        packed = contexts.build("", purpose="chat", session="thread-1", event_id=turn, receipt_mode=receipt_mode)
        if receipt_mode and packed.get("injection", {}).get("id"):
            sent, deliveries = packed["injection"], ContextDelivery(contexts)
            deliveries.begin("thread-1", sent["epoch"], sent["id"])
            deliveries.acknowledge("thread-1", sent["epoch"], sent["id"], actual_session="thread-1", marker=sent["marker"],
                                   text_hash=sent["text_hash"], verified=True)
        return packed

    first = read("turn-1")
    assert HABITS_ITEM in first["covered_ids"] and all(word in first["text"] for word in WORDS)
    seen = contexts.window("thread-1")["seen"][HABITS_ITEM]
    assert re.fullmatch(r"[0-9a-f]{64}", seen), "a hash, never her words"
    assert HABITS_ITEM not in read("turn-2")["covered_ids"], "seen in this window"

    before = memory.habits.read()
    engine.delete(directions)
    settle(engine)
    after = memory.habits.read()
    assert after["revision"] == before["revision"], "a delete leaves the table's revision"
    again = read("turn-3")
    assert HABITS_ITEM in again["covered_ids"], "sent again, as they are now"
    assert not any(word in again["text"] for word in WORDS) and "autonomous" in again["text"]
    assert contexts.window("thread-1")["seen"][HABITS_ITEM] != seen
    assert HABITS_ITEM not in read("turn-4")["covered_ids"], "and seen again"
    assert her_words(engine) == {}

    hashed = []
    monkeypatch.setattr(context, "digest", lambda value: hashed.append(value) or digest(value))
    assert [Contexts.habits_item(habits)["revision"] for habits in (before, after)] == [seen, contexts.window("thread-1")["seen"][HABITS_ITEM]]
    assert hashed == [[before["revision"], []], [before["revision"], ["exploration_directions", "exploration_frequency"]]]
    assert not any(value in json.dumps(hashed, ensure_ascii=False) for value in (*WORDS, "autonomous")), "no preference of hers"


@pytest.mark.parametrize("tool", ["read_affective_state", "read_continuity_context"])
def test_a_fork_that_read_the_habits_may_cite_their_message_only_as_they_were_set_from_it(system, tool):
    """Replay H (CL10-MM-01). A fork reads the habits -- in the state read, or in the index of a
    continuity read -- and cites the message that set the directions, by its record or by its source.
    Uncorrected, the message is what the habits were set from, and K1-16 accepts it. She corrects it:
    the habit keeps its value, marked for review (K1-18), and the read still names the message at the
    revision the habit was set from, so a citation of what she says now is refused. Named bare, as the
    lists alone name it, it was taken as read at the revision it has now."""
    mind, memory, source, clock = system
    directions, reply = habits_from(memory, clock)
    [record] = mind.engine.source(directions)["record_ids"]
    revision = mind.engine.get(record)["revision"]
    contexts, jobs = Contexts(mind), Appraisals(mind)
    started = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    def fork_read():
        result = contexts.affective() if tool == "read_affective_state" else contexts.build("", purpose="read")
        shown = result["conversation_habits"] if tool == "read_affective_state" else next(
            entry for entry in result["index"] if entry["id"] == HABITS_ITEM)
        assert record in shown["record_ids"] and {"source_id": directions, "record_id": record, "revision": revision} in shown[SET_FROM]
        ids = acp_ids(result)
        return ids, {"native_receipt": {"channel": "fork", "tool_calls": [{"name": f"memorypalace.{tool}", "ok": True, "ids": ids}]}}

    def citing(identifier):
        class Proposal:
            def model_dump(self):
                return {"understanding": {"evidence_ids": [identifier]}}
        return Proposal()

    ids, receipt = fork_read()
    assert [entry for entry in ids if entry["id"] == record] == [{"id": record, "revision": revision}]
    for cited in (record, directions):
        assert set(jobs._tool_fetched(citing(cited), receipt, {}, started)) == {record}, cited

    mind.engine.revise(record, RevisionInput(expected_revision=revision, command_id="correct-directions", action="correct",
                                             content="以后多去看看天文台的夜观", reason="她改了一下说法"))
    habits = memory.habits.read()
    assert habits["preferences"]["exploration_directions"] == DIRECTIONS and habits["entries"]["exploration_directions"]["needs_review"]
    ids, receipt = fork_read()
    assert [entry for entry in ids if entry["id"] == record] == [{"id": record, "revision": revision}], "as the habit was set from it"
    for cited in (record, directions):
        assert jobs._tool_fetched(citing(cited), receipt, {}, started) == {}, cited
    bare = [{**entry, "revision": None} if entry["id"] == record else entry for entry in ids]
    bare = {"native_receipt": {"channel": "fork", "tool_calls": [{"name": f"memorypalace.{tool}", "ok": True, "ids": bare}]}}
    assert set(jobs._tool_fetched(citing(record), bare, {}, started)) == {record}


def test_an_update_of_the_habits_expects_the_revision_the_state_read_shows(system):
    """The background item's revision is a hash, which no update takes. The update tool says where its
    `expected_revision` comes from -- the state read's `conversation_habits.revision`, the table's -- and
    an update with that one applies (CL10-MM-03)."""
    import asyncio

    from eventmem.core.mcp import create_mcp

    mind, memory, source, clock = system
    habits_from(memory, clock)
    tools = {tool.name: tool for tool in asyncio.run(create_mcp(mind.engine).list_tools())}
    assert "expected_revision 用 read_affective_state 返回的 conversation_habits.revision" in tools["update_conversation_habits"].description
    shown = Contexts(mind).affective()["conversation_habits"]["revision"]
    again = said(memory, clock, "said-again", "还是每周一次吧")
    assert memory.habits.update({"command_id": "again", "expected_revision": shown, "evidence_ids": [again], "reason": "她改成每周一次",
                                 "preferences": {"exploration_frequency": "每周一次"}})["revision"] == shown + 1
