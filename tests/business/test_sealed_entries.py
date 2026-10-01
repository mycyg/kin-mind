"""Sealed entries, 暗房 and 时光信 (小光, 2026-10-01): a diary Kin seals and a letter the owner writes,
each opened on its day (kin_mind.sealed).

Held here: with `sealed_entries` off nothing changes -- the schema, the prompt, the store's structure,
a diary that carries a date anyway; with it on, until its day a sealed entry's words are in no file of
the store at all, so no model boundary and no read tool can show them -- the core MCP tools, the
host's model-facing actions, an assessment's context, the console's views -- while a placeholder
may show; an erase reaches through the lock, of the entry itself or of what a sealed diary rests on;
on its day the entry is the source it would have been, extracted and indexed then, with a fact for
an assessment and no message; and the review minute opens it whatever else fails.

Synthetic replays only: an injected clock, sources through the engine, scripted providers, no network."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eventmem.core.api import create_app
from eventmem.core.db import Conflict, dumps
from eventmem.core.mcp import create_mcp
from eventmem.core.models import RecallRequest
from kin_mind import appraisal as A
from kin_mind import initiative, sealed
from kin_mind.appraisal import Appraisal, Appraisals, appraisal_context
from kin_mind.continuity import Understanding as Interpretation
from kin_mind.memory import MemoryContinuity
from test_erasure import settle, texts_everywhere
from test_model_view import answer, call, config_for, rich_store, tools_of

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

LETTER = "qxletterwordsqx"
DIARY = "qxdiarywordsqx"
TOKEN = "synthetic-sealed"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def answered(proposal):
    return proposal, {"provider": "deepseek", "model": "synthetic", "request_id": "req-sealed", "usage": {}}


class Thinker:
    """The model: writes a diary, sealed until `day` when one is given, and records what it was shown."""

    def __init__(self, record, day=None, words=DIARY, basis="internal_thought"):
        self.record, self.day, self.words, self.basis, self.seen = record, day, words, basis, []

    def appraise(self, context):
        self.seen.append(context)
        return answered(Appraisal(reason="想了想今天", values={"curiosity": 63}, understanding=A.Understanding(
            meaning=f"今天的 {self.words} 想先收起来", topic=f"{self.words} 的心情", importance=50, confidence=0.7,
            basis=self.basis, evidence_ids=[self.record], unlock_at=self.day)))


def days(clock, n):
    return (sealed.today(clock[0].isoformat()) + timedelta(days=n)).isoformat()


def client_of(mind):
    return TestClient(create_app(engine=mind.engine, token=TOKEN, workers=False, mcp_enabled=False))


def files_holding(engine, needle):
    """Every file under the store's root whose bytes hold `needle`: the database, its log, the blobs."""
    return sorted(str(path) for path in engine.db.root.rglob("*") if path.is_file() and needle.encode() in path.read_bytes())


def write_letter(mind, clock, day_offset=2, words=LETTER, command="letter-1", http=True):
    if not http:
        return sealed.seal_letter(mind, f"给 Kin 的信：{words}", days(clock, day_offset), command)
    response = client_of(mind).post("/v1/sealed", headers=HEADERS, json={
        "scope": mind.scope.model_dump(), "text": f"给 Kin 的信：{words}", "unlock_at": days(clock, day_offset),
        "command_id": command})
    assert response.status_code == 200, response.text
    return response.json()


def seal_a_diary(mind, source, clock, day_offset=3, words=DIARY):
    primary = source(f"evening-{len(words)}", "她说起那个晚上")
    record = mind.engine.source(primary)["record_ids"][0]
    thinker = Thinker(record, days(clock, day_offset), words)
    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(thinker)
    return primary, record, thinker


def on(mind, **more):
    MemoryContinuity(mind).configure({"sealed_entries": True, **more})


# --- off: exactly as before ----------------------------------------------------------------------

def test_off_by_default_the_schema_the_prompt_the_store_and_a_dated_diary_are_as_before(setup):
    mind, source, clock = setup
    assert MemoryContinuity(mind).settings()["sealed_entries"] is False
    schema = A.appraisal_schema()
    assert "unlock_at" not in dumps(schema)
    assert schema["$defs"]["Understanding"] == {k: v for k, v in Interpretation.model_json_schema().items()}
    assert A.appraisal_schema(sealing=True)["$defs"]["Understanding"]["properties"]["unlock_at"]["description"] == A.UNLOCK_AT_DESCRIPTION
    provider = A.DeepSeek.__new__(A.DeepSeek)
    assert A.SEALED_ENTRIES_PROMPT not in A.DeepSeek._system(provider, {"stimulus": "idle-review"}, None)
    # A dump without a date is the one there always was: no key is added anywhere it is stored or shown.
    plain = Appraisal(reason="r", understanding=Interpretation(meaning="m", topic="t", importance=1, confidence=.5,
                                                               basis="internal_thought"))
    assert "unlock_at" not in dumps(plain.model_dump())
    # A diary that carries a date while sealing is not offered is an ordinary diary, and no table is made.
    primary, record, thinker = seal_a_diary(mind, source, clock)
    assert A.SEALED_ENTRIES_PROMPT not in dumps(thinker.seen) and "sealed" not in thinker.seen[0]["recent_reflections"]
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE namespace='kin-reflection'").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (sealed.TABLE,)).fetchone()
    assert "sealed_opened" not in initiative.facts(mind)
    assert sealed.open_due(mind) == {"state": "idle"}
    response = client_of(mind).post("/v1/sealed", headers=HEADERS, json={
        "scope": mind.scope.model_dump(), "text": "x", "unlock_at": days(clock, 2), "command_id": "off"})
    assert response.status_code == 409 and response.json()["code"] == "sealed-entries-off"
    assert client_of(mind).get("/v1/sealed", headers=HEADERS, params=mind.scope.model_dump()).json() == {
        "items": [], "cursor": None, "enabled": False}
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (sealed.TABLE,)).fetchone()


# --- on: the lock -------------------------------------------------------------------------------

def test_until_its_day_a_sealed_entry_is_in_no_file_of_the_store_and_no_model_or_tool_reads_it(setup):
    mind, source, clock = setup
    on(mind)
    letter = write_letter(mind, clock)
    assert letter["state"] == "sealed" and letter["placeholder"] == f"一封 {letter['unlock_at']} 才能打开的信"
    assert LETTER not in dumps(letter) and "source_id" not in letter
    primary, record, thinker = seal_a_diary(mind, source, clock)
    # The model that wrote it was offered the date and told what it means.
    assert thinker.sealing is True
    provider = A.DeepSeek.__new__(A.DeepSeek)
    provider.sealing = True
    assert A.SEALED_ENTRIES_PROMPT in A.DeepSeek._system(provider, thinker.seen[0], None)
    assert "unlock_at" in dumps(A.appraisal_schema(sealing=provider._sealing()))
    # Never on a main-session fork, nor on a lane that keeps no understanding.
    fork = A.NativeReview.__new__(A.NativeReview)
    fork.sealing = True
    assert not fork._sealing() and A.SEALED_ENTRIES_PROMPT not in A.DeepSeek._system(provider, {"stimulus": "memory-backfill"}, None)
    # The next assessment: what it is shown, and its projection; a sealed diary only as a placeholder.
    later = source("next-morning", "早上好")
    reader = Thinker(mind.engine.source(later)["record_ids"][0], words="平常的日记")
    jobs = Appraisals(mind)
    jobs.enqueue([later], "synthetic-v1")
    jobs.run_one(reader)
    [shown] = reader.seen
    assert shown["recent_reflections"]["sealed"] == [{"at": shown["recent_reflections"]["sealed"][0]["at"],
                                                      "unlock_at": days(clock, 3)}]
    assert shown["recent_reflections"]["today_count"] == 1, "the sealed diary was written today"
    # Switched off, the placeholder goes from what an assessment is shown; the count stays a count.
    MemoryContinuity(mind).configure({"sealed_entries": False})
    assert "sealed" not in MemoryContinuity(mind).recent_reflections()
    on(mind)
    # Everything else a model or a tool reads, over a store with a graph, shares, plans and wishes.
    rich_store(mind, clock)
    on(mind)
    settle(mind.engine)
    for needle in (LETTER, DIARY):
        assert files_holding(mind.engine, needle) == [], needle
        assert texts_everywhere(mind.engine, needle) == set(), needle
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace=?", (sealed.LETTER_NAMESPACE,)).fetchone()
        # The one reflection is the diary the next assessment wrote without a seal.
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE namespace='kin-reflection'").fetchone()[0] == 1
        assert {row[0] for row in conn.execute(f"SELECT kind FROM {sealed.TABLE} WHERE state='sealed'")} == {"letter", "diary"}

    def clean(value, label):
        text = dumps(value)
        assert LETTER not in text and DIARY not in text, label

    # Every core MCP read, over every record and source the store has.
    server, scope = create_mcp(mind.engine), mind.scope.model_dump()
    with mind.engine.db.connect() as conn:
        records = [row[0] for row in conn.execute("SELECT id FROM records WHERE deleted=0")]
        sources = [row[0] for row in conn.execute("SELECT id FROM sources WHERE deleted=0")]
    reads = {"recall_memory": {"request": {"query": "那个晚上 信 日记 想先收起来", "scope": scope}},
             "read_graph": {"scope": scope, "query": "晚上"}, "read_affective_state": {"scope": scope, "history": 5},
             "read_continuity_context": {"scope": scope, "query": "晚上 信"},
             "read_autonomous_plans": {"scope": scope, "history": True}, "read_trait_ledger": {"scope": scope},
             "read_share_history": {"scope": scope}, "read_work_history": {"scope": scope},
             "browse_topics": {"scope": scope}, "memory_status": {}}
    assert set(reads) <= set(tools_of(server))
    for name, arguments in reads.items():
        clean(answer(call(server, name, **arguments)), name)
    for identifier in records:
        clean(answer(call(server, "read_memory", record_id=identifier)), identifier)
        clean(answer(call(server, "memory_history", record_id=identifier)), identifier)
    for identifier in sources:
        clean(answer(call(server, "source_evidence", source_id=identifier)), identifier)
    # The host's actions whose answers it puts before a model.
    from kin_mind.host import dispatch
    for action, request in (("memory-context", {"query": "那个晚上 信", "purpose": "chat", "budget": 8000}),
                            ("read", {"history": 5}), ("state-overview", {"query": "晚上"}), ("graph", {"query": "晚上"}),
                            ("autonomous-plans", {"history": True}), ("candidate", {}), ("prepare-exploration", {})):
        clean(dispatch(config_for(mind), action, request), action)
    clean(shown, "assessment context")
    clean(appraisal_context(shown), "assessment projection")
    # The console: its record views, the overview, the graph, a recall, and its own list.
    console = client_of(mind)
    for path in ("/v1/memories", "/v1/overview", "/v1/graph", "/v1/sealed"):
        response = console.get(path, headers=HEADERS, params=scope if path != "/v1/overview" else None)
        assert response.status_code == 200, (path, response.text)
        clean(response.json(), path)
    clean(console.post("/v1/recall", headers=HEADERS, json={"query": "那个晚上的信", "scope": scope}).json(), "recall")
    listed = console.get("/v1/sealed", headers=HEADERS, params=scope).json()
    assert listed["enabled"] and {item["placeholder"] for item in listed["items"]} == {
        f"一封 {days(clock, 2)} 才能打开的信", f"一篇 {days(clock, 3)} 才能打开的日记"}


def test_on_its_day_an_entry_is_the_source_it_would_have_been_and_a_fact_not_a_message(setup):
    mind, source, clock = setup
    on(mind, records=True, semantic=True, semantic_actions=True)
    # Written two days ago, to open yesterday: the host's minute reads its own clock.
    clock[0] -= timedelta(days=2)
    letter = write_letter(mind, clock, day_offset=1, http=False)
    primary, record, _ = seal_a_diary(mind, source, clock, day_offset=1)
    assert sealed.open_due(mind) == {"state": "idle", "opened": [], "dropped": []}
    clock[0] += timedelta(days=2)
    # The review minute opens it, and asks no model to.
    from kin_mind.host import dispatch
    dispatch(config_for(mind), "review-due", {})
    with mind.engine.db.connect() as conn:
        rows = {row["kind"]: row for row in conn.execute(f"SELECT * FROM {sealed.TABLE}")}
        assert {kind: row["state"] for kind, row in rows.items()} == {"letter": "opened", "diary": "opened"}
        assert all(row["data"] == "{}" for row in rows.values())
        own = conn.execute("SELECT * FROM sources WHERE id=?", (rows["letter"]["source_id"],)).fetchone()
        diary = conn.execute("SELECT * FROM sources WHERE id=?", (rows["diary"]["source_id"],)).fetchone()
        extract = conn.execute("SELECT 1 FROM jobs WHERE kind='extract' AND json_extract(payload,'$.source_id')=?",
                               (own["id"],)).fetchone()
        contacts = conn.execute("SELECT COUNT(*) FROM mind_contacts").fetchone()[0]
    # The owner's own explicit words, written when they were written, indexed and extracted now.
    data = json.loads(own["data"])
    assert own["namespace"] == sealed.LETTER_NAMESPACE and data["authority"] == "explicit"
    assert data["metadata"]["role"] == "user" and own["occurred_at"] == letter["created_at"] and extract
    assert LETTER in mind.engine.source(own["id"], content=True).read_text()
    found = mind.engine.recall(RecallRequest(query=LETTER, scope=mind.scope))
    assert LETTER in dumps(found)
    # The diary is the kin-reflection it would have been, resting on what it was written from.
    assert diary["namespace"] == "kin-reflection" and DIARY in mind.engine.source(diary["id"], content=True).read_text()
    assert {ref.get("record_id") for ref in json.loads(diary["data"])["derived_from"]} >= {record}
    # A fact, with the record to read; nothing is sent.
    facts = initiative.facts(mind)["sealed_opened"]
    assert {(f["kind"], f["record_id"]) for f in facts} == {("letter", sealed.root_id(own["id"])),
                                                            ("diary", sealed.root_id(diary["id"]))}
    assert all(f["days_sealed"] == 2 and LETTER not in dumps(f) for f in facts) and contacts == 0
    listed = sealed.entries(mind)["items"]
    assert {item["state"] for item in listed} == {"opened"} and all(item["placeholder"] is None for item in listed)
    # Only where the setting tells the assessment what it is; a day later it is no longer news.
    MemoryContinuity(mind).configure({"sealed_entries": False})
    assert "sealed_opened" not in initiative.facts(mind)
    on(mind)
    clock[0] += timedelta(hours=25)
    assert "sealed_opened" not in initiative.facts(mind)
    # And the diary, once open, is a reflection that goes with what it rests on. (The recall above holds
    # the background back for a moment, as a foreground read does.)
    mind.engine.delete(primary)
    mind.engine.interactive_until = 0.0
    settle(mind.engine)
    assert texts_everywhere(mind.engine, DIARY) == set()


# --- erasure through the lock --------------------------------------------------------------------

def test_an_erase_reaches_a_sealed_entry_itself_and_what_a_sealed_diary_rests_on(setup):
    mind, source, clock = setup
    on(mind)
    letter = write_letter(mind, clock, day_offset=1)
    primary, record, _ = seal_a_diary(mind, source, clock, day_offset=1)
    other = write_letter(mind, clock, day_offset=1, words="qxotherletterqx", command="letter-2")
    with mind.engine.db.connect() as conn:
        reserved = {row["kind"] + row["id"]: row["source_id"] for row in conn.execute(f"SELECT * FROM {sealed.TABLE}")}
    # The letter, from the console, through the one erase path: its row goes and its id is tombstoned.
    response = client_of(mind).delete(f"/v1/sealed/{letter['id']}", headers=HEADERS, params=mind.scope.model_dump())
    assert response.status_code == 200 and response.json()["status"] == "deleted"
    assert client_of(mind).delete(f"/v1/sealed/{letter['id']}", headers=HEADERS,
                                  params=mind.scope.model_dump()).status_code == 404
    # What the diary was written from: the diary goes with it, sealed as it is.
    mind.engine.delete(primary)
    with mind.engine.db.connect() as conn:
        left = [row["id"] for row in conn.execute(f"SELECT id FROM {sealed.TABLE}")]
        tombstoned = {row[0] for row in conn.execute("SELECT key FROM tombstones")}
    assert left == [other["id"]]
    assert reserved["letter" + letter["id"]] in tombstoned
    clock[0] += timedelta(days=1)
    result = sealed.open_due(mind)
    assert result["opened"] == [other["id"]]
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE id=?", (reserved["letter" + letter["id"]],)).fetchone()
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
    # An opened letter goes as the source it is, and its row with it.
    sealed.erase_entry(mind, other["id"])
    settle(mind.engine)
    for needle in (LETTER, DIARY):
        assert texts_everywhere(mind.engine, needle) == set() and files_holding(mind.engine, needle) == [], needle
    # What was opened was a source: the erase takes it from every row and every blob, as any source's.
    assert texts_everywhere(mind.engine, "qxotherletterqx") == set()
    assert not [path for path in files_holding(mind.engine, "qxotherletterqx") if "/blobs/" in path]
    assert sealed.entries(mind)["items"] == []


def test_an_entry_erased_while_it_opens_stays_erased(setup, monkeypatch):
    """The erase lands between the minute's read and its receipt: the receipt meets the tombstone."""
    mind, source, clock = setup
    on(mind)
    letter = write_letter(mind, clock, day_offset=1)
    clock[0] += timedelta(days=1)
    real = sealed._receive

    def erased_first(mind_, row, body):
        sealed.erase_entry(mind, letter["id"])
        return real(mind_, row, body)
    monkeypatch.setattr(sealed, "_receive", erased_first)
    assert sealed.open_due(mind)["dropped"] == [letter["id"]]
    assert texts_everywhere(mind.engine, LETTER) == set() and sealed.entries(mind)["items"] == []
    # A receipt writes its bytes before its transaction: the blob it left is nobody's, and the next
    # sweep takes it once it is a minute old, as it takes any receipt's that a delete beat.
    import os
    import time
    [orphan] = files_holding(mind.engine, LETTER)
    os.utime(orphan, (time.time() - 120, time.time() - 120))
    mind.engine.gc_blobs()
    assert files_holding(mind.engine, LETTER) == []


def test_an_entry_whose_source_is_tombstoned_is_not_received_at_all(setup, monkeypatch):
    """The tombstone is there and the row still is (an erase that came between the minute's read and
    its receipt): not even the bytes a receipt writes before its transaction are written."""
    mind, source, clock = setup
    on(mind)
    letter = write_letter(mind, clock, day_offset=1)
    with mind.engine.db.connect(write=True) as conn:
        reserved = conn.execute(f"SELECT source_id FROM {sealed.TABLE} WHERE id=?", (letter["id"],)).fetchone()[0]
        conn.execute("INSERT INTO tombstones VALUES(?,?)", (reserved, clock[0].isoformat()))
    clock[0] += timedelta(days=1)
    monkeypatch.setattr(sealed, "_receive", lambda *a: (_ for _ in ()).throw(AssertionError("received")))
    assert sealed.open_due(mind)["dropped"] == [letter["id"]]
    assert files_holding(mind.engine, LETTER) == [] and sealed.entries(mind)["items"] == []


# --- the day, the command, the minute ------------------------------------------------------------

def test_a_letter_opens_from_tomorrow_to_a_year_ahead_and_one_command_writes_it_once(setup):
    mind, _, clock = setup
    on(mind)
    first, last = sealed.window(clock[0].isoformat())
    for day in (days(clock, 0), (last + timedelta(days=1)).isoformat(), "2026-13-01", "tomorrow"):
        with pytest.raises(ValueError):
            sealed.seal_letter(mind, "x", day, "bad-" + day)
    assert sealed.seal_letter(mind, "x", last.isoformat(), "last")["unlock_at"] == last.isoformat()
    again = sealed.seal_letter(mind, "x", first.isoformat(), "same")
    assert sealed.seal_letter(mind, "x", first.isoformat(), "same") == again
    with pytest.raises(Conflict):
        sealed.seal_letter(mind, "y", first.isoformat(), "same")
    with pytest.raises(ValueError):
        sealed.seal_letter(mind, " ", first.isoformat(), "blank")


def test_only_a_diary_is_sealed_its_day_is_held_to_the_window_and_unsealed_where_not_offered(setup):
    mind, _, clock = setup
    with pytest.raises(ValidationError):
        A.Understanding(meaning="m", topic="t", importance=1, confidence=.5, basis="inferred", unlock_at="2030-01-01")
    for bad in ("2030-1-1", "2030-02-30"):
        with pytest.raises(ValidationError):
            A.Understanding(meaning="m", topic="t", importance=1, confidence=.5, basis="internal_thought", unlock_at=bad)
    at = clock[0].isoformat()
    first, last = sealed.window(at)

    def proposal(day):
        return Appraisal(reason="r", understanding=A.Understanding(meaning="m", topic="t", importance=1, confidence=.5,
                                                                   basis="internal_thought", unlock_at=day))
    for day, expected in ((days(clock, -3), first), (days(clock, 900), last), (days(clock, 5), first + timedelta(days=4))):
        rest, kept = sealed.take(proposal(day), sealing=True, at=at)
        assert rest.understanding is None and kept["unlock_at"] == expected.isoformat() and kept["understanding"]["meaning"] == "m"
        assert "unlock_at" not in kept["understanding"]
    rest, kept = sealed.take(proposal(days(clock, 5)), sealing=False, at=at)
    assert kept is None and rest.understanding.meaning == "m" and rest.understanding.unlock_at is None
    assert "unlock_at" not in dumps(rest.model_dump())


def test_one_entry_that_cannot_open_never_stops_the_minute(setup, monkeypatch):
    mind, _, clock = setup
    on(mind)
    broken = write_letter(mind, clock, day_offset=1, command="broken")
    fine = write_letter(mind, clock, day_offset=1, words="qxfineletterqx", command="fine")
    clock[0] += timedelta(days=1)
    real = sealed._receive

    def flaky(mind_, row, body):
        if row["id"] == broken["id"]:
            raise RuntimeError("disk full")
        return real(mind_, row, body)
    monkeypatch.setattr(sealed, "_receive", flaky)
    result = sealed.open_due(mind)
    assert result["opened"] == [fine["id"]] and result["failed"] == [broken["id"]]
    monkeypatch.setattr(sealed, "_receive", real)
    assert sealed.open_due(mind)["opened"] == [broken["id"]]


def test_a_repair_of_a_proposal_that_may_seal_a_diary_is_never_cached(monkeypatch):
    """Asked as a look, so no request is ever sent: the cache is read first, or the look stops it."""
    from eventmem.core.db import unrecorded
    provider = A.DeepSeek("https://api.deepseek.com", A.APPRAISAL_MODEL, "SYNTHETIC_SEALED_KEY")
    monkeypatch.setenv("SYNTHETIC_SEALED_KEY", "synthetic")

    def cache(*args):
        raise AssertionError("cache read")
    monkeypatch.setattr(provider, "_cache_get", cache)
    with unrecorded():
        with pytest.raises(AssertionError, match="cache read"):
            provider.structured("repair_appraisal", Appraisal, "s", {"proposal": {}})
        provider.sealing = True
        with pytest.raises(RuntimeError, match="deepseek-session-required"):
            provider.structured("repair_appraisal", Appraisal, "s", {"proposal": {}})
        with pytest.raises(AssertionError, match="cache read"):
            provider.structured("compress", Appraisal, "s", {})


def test_a_main_session_fork_never_seals(setup):
    """Its own transcript would keep the words: a date from one is dropped, and the diary is ordinary."""
    mind, source, clock = setup
    on(mind)

    class Fork(Thinker):
        native_review = True
    primary = source("fork-evening", "她说起那个晚上")
    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    fork = Fork(mind.engine.source(primary)["record_ids"][0], days(clock, 3))
    jobs.run_one(fork)
    assert fork.sealing is False
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE namespace='kin-reflection'").fetchone()[0] == 1
    assert sealed.entries(mind)["items"] == []
