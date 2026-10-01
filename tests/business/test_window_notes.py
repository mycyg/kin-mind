"""这一段的我们 (window_notes): a note Kin writes about a window's stretch, carried by the next checkpoint.

Asserted here: off by default, nothing is queued, sent or carried and the checkpoint is what it was;
a review at elevated pressure queues one note per stretch; the note is one background structured call
shown the stretch's dialogue alone, written as a derived source that is never evidence and changes no
persona and no state; the next checkpoint carries it as plain text when it fits and never becomes
incomplete for it; an erase of a turn takes the note, and a checkpoint carrying it stops validating;
the host's background lane writes it.

The endpoint is a script behind httpx.MockTransport. No network, no paid call."""
import json
from datetime import timedelta

import httpx
import pytest

from eventmem.core.engine import root_id
from eventmem.core.persona import load_persona
from eventmem.core.read_policy import ReadPolicy
from kin_mind import window_notes
from kin_mind.appraisal import APPRAISAL_MODEL, DeepSeek
from kin_mind.evidence_classes import never_evidence
from kin_mind.host import dispatch
from kin_mind.memory import MemoryContinuity
from kin_mind.model_view import leaks
from kin_mind.session_checkpoint import SessionCheckpoint
from kin_mind.strict_schema import strict_schema
from test_erasure import settle, texts_everywhere
from test_strict_schema import violations

pytest_plugins = ("test_kin_mind",)

MARKER = "lanternquux"
USAGE = {"input_tokens": 900, "output_tokens": 120}
NOTE = {"you": "你今天叫我小笨蛋，说明天面试有点紧张。", "me": "我一直在，答应明天问你面试怎么样。",
        "stretch": f"从{MARKER}聊到面试，后来一起笑着道了晚安。", "unfinished": "明天面试的结果。"}
BINDING = {"conversationId": "conv-1", "generation": 1}


class Endpoint:
    """DeepSeek's messages endpoint, answering NOTE unless told otherwise."""

    def __init__(self, mind, monkeypatch, *answers):
        monkeypatch.setenv("KIN_TEST_DS_KEY", "synthetic")
        self.answers, self.sent = list(answers), []
        self.provider = DeepSeek("https://api.deepseek.com", APPRAISAL_MODEL, "KIN_TEST_DS_KEY", timeout=60,
                                 transport=httpx.MockTransport(self.respond))
        self.provider.engine = mind.engine

    def respond(self, request):
        sent = json.loads(request.content)
        self.sent.append(sent)
        answer = self.answers.pop(0) if self.answers else NOTE
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={
            "id": f"msg-{len(self.sent)}", "model": APPRAISAL_MODEL, "stop_reason": "tool_use", "usage": USAGE,
            "content": [{"type": "tool_use", "id": f"toolu_{len(self.sent)}", "name": window_notes.TOOL, "input": answer}]})


def talk(mind, clock, *turns):
    """Public dialogue as the host records it; returns the source ids."""
    memory, ids = MemoryContinuity(mind), []
    for index, (role, text) in enumerate(turns):
        clock[0] += timedelta(minutes=1)
        kind = "owner-message" if role == "user" else "assistant-message"
        ids.append(memory.ingest({"id": f"{kind}-{clock[0].timestamp()}-{index}", "kind": kind,
                                  "at": clock[0].isoformat(), "text": text})["source_id"])
    return ids


def observation(level="elevated", compaction=None, snapshot="obs-1", conversation="conv-1", generation=1):
    return {"id": snapshot, "binding": {"conversationId": conversation, "generation": generation, "threadId": "t-1"},
            "pressure": {"level": level, "ratio": 0.7}, "lastCompaction": {"id": compaction} if compaction else None,
            "evidence": [], "recent": []}


def on(mind, **extra):
    MemoryContinuity(mind).configure({"records": True, "window_notes": True, **extra})


def dialogue(mind, clock):
    return talk(mind, clock, ("user", f"今天去了{MARKER}，好累呀小笨蛋"), ("assistant", "辛苦啦，抱抱你"),
                ("user", "明天面试，有点紧张"), ("assistant", "你准备得很好，明天我等你消息"))


def rows(mind):
    with mind.engine.db.connect() as conn:
        if not window_notes.installed(conn):
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM mind_window_notes ORDER BY created_at")]


def build(mind, **options):
    checkpoints = SessionCheckpoint(mind, agent_version="synthetic-v1")
    options = {"adaptive_budget": True, "allow_model": False, **options}
    return checkpoints.build(checkpoints.snapshot(), BINDING, **options)


def written(mind, monkeypatch, clock):
    on(mind)
    turns = dialogue(mind, clock)
    assert window_notes.observe(mind, observation())["created"]
    endpoint = Endpoint(mind, monkeypatch)
    assert window_notes.run(mind, endpoint.provider)["state"] == "complete"
    return turns, endpoint


# --- off ---------------------------------------------------------------------------------------

def test_off_by_default_nothing_is_queued_sent_or_carried(setup, monkeypatch):
    mind, _, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    dialogue(mind, clock)
    before = build(mind)
    assert window_notes.observe(mind, observation()) is None and rows(mind) == []
    endpoint = Endpoint(mind, monkeypatch)
    assert window_notes.due(mind) == 0 and window_notes.run(mind, endpoint.provider)["state"] == "idle"
    assert not endpoint.sent
    after = build(mind)
    assert "windowNote" not in after["payload"] and "windowNoteOmitted" not in after
    assert (after["id"], after["payload"], after["tokens"], after["complete"]) == (before["id"], before["payload"], before["tokens"], before["complete"])


def test_a_note_written_while_on_is_not_carried_once_the_switch_is_off(setup, monkeypatch):
    mind, _, clock = setup
    written(mind, monkeypatch, clock)
    assert "windowNote" in build(mind)["payload"]
    MemoryContinuity(mind).configure({"window_notes": False})
    built = build(mind)
    assert "windowNote" not in built["payload"] and "windowNoteSource" not in built
    assert window_notes.observe(mind, observation(compaction="cmp-2")) is None


# --- queueing ----------------------------------------------------------------------------------

def test_a_review_at_elevated_pressure_queues_one_note_per_stretch(setup):
    mind, _, _ = setup
    on(mind)
    assert window_notes.observe(mind, observation(level="normal")) is None
    first = window_notes.observe(mind, observation())
    assert first["created"]
    # Asked again about the same stretch -- another cause, the critical level -- it is the same note.
    again = window_notes.observe(mind, observation(level="critical", snapshot="obs-2"))
    assert again == {"id": first["id"], "created": False}
    # The stretch after a compaction is another one.
    later = window_notes.observe(mind, observation(compaction="cmp-1", snapshot="obs-3"))
    assert later["created"] and later["id"] != first["id"]
    assert window_notes.observe(mind, {**observation(), "binding": {}}) is None
    assert [row["state"] for row in rows(mind)] == ["pending", "pending"]


# --- one note ------------------------------------------------------------------------------------

def test_the_note_is_one_background_call_shown_only_the_dialogue(setup, monkeypatch):
    mind, _, clock = setup
    persona = load_persona(mind.engine, mind.scope)
    revision = mind.read()["revision"]
    turns, endpoint = written(mind, monkeypatch, clock)
    [sent] = endpoint.sent
    [tool] = sent["tools"]
    assert tool["name"] == window_notes.TOOL and tool["input_schema"] == window_notes.WindowNote.model_json_schema()
    # Every part required, nothing else allowed: strict as it stands.
    assert violations(strict_schema(tool["input_schema"])) == []
    assert sent["system"] == window_notes.WINDOW_NOTE_SYSTEM
    shown = json.loads(sent["messages"][0]["content"])
    assert set(shown) == {"prompt_version", "local_time", "dialogue"}
    assert [set(turn) for turn in shown["dialogue"]] == [{"role", "text", "at"}] * 4
    assert [turn["role"] for turn in shown["dialogue"]] == ["user", "assistant", "user", "assistant"]
    # No ids, no metadata, nothing of the state.
    assert "src_" not in sent["messages"][0]["content"] and leaks(shown) == []
    # It changes no persona, no identity description and no state.
    assert load_persona(mind.engine, mind.scope) == persona and mind.read()["revision"] == revision


def test_the_note_is_a_derived_source_that_is_never_evidence(setup, monkeypatch):
    mind, _, clock = setup
    turns, _ = written(mind, monkeypatch, clock)
    [row] = rows(mind)
    assert row["state"] == "written" and MARKER not in row["data"], "the queue keeps ids, never the words"
    with mind.engine.db.connect() as conn:
        source = dict(conn.execute("SELECT * FROM sources WHERE id=?", (row["source_id"],)).fetchone())
        data = json.loads(source["data"])
        record = mind.engine._get(conn, root_id(row["source_id"]))
    assert source["namespace"] == window_notes.NAMESPACE
    assert data["metadata"]["basis"] == "internal_thought"
    assert {ref["source_id"] for ref in data["derived_from"]} == set(turns)
    assert record["content"].startswith(window_notes.WINDOW_NOTE_LABEL) and "没聊完的：明天面试的结果。" in record["content"]
    # Evidence of nothing: no trait, plan, wish or concern can rest on it, and no recall shows it.
    assert never_evidence({"namespace": source["namespace"], "metadata": data["metadata"]})
    assert not ReadPolicy.load(mind.engine, mind.scope, "experience_recall").visible(record)


# --- carried ------------------------------------------------------------------------------------

def test_the_next_checkpoint_carries_it_as_plain_text_and_never_becomes_incomplete_for_it(setup, monkeypatch):
    mind, _, clock = setup
    written(mind, monkeypatch, clock)
    [row] = rows(mind)
    built = build(mind)
    assert built["complete"]
    assert built["payload"]["windowNote"].startswith(window_notes.WINDOW_NOTE_LABEL)
    assert built["payload"]["windowNoteSource"] == row["source_id"]
    assert leaks(built["payload"]) == [] and built["tokens"] <= built["budgetPlan"]["effective"]
    assert SessionCheckpoint(mind, agent_version="synthetic-v1").validate(built)["valid"]
    # Where it does not fit it stays out, and the checkpoint is what it would have been without it.
    MemoryContinuity(mind).configure({"window_notes": False})
    plain = build(mind, adaptive_budget=False)
    MemoryContinuity(mind).configure({"window_notes": True})
    tight = build(mind, adaptive_budget=False, budget=plain["tokens"] + 20)
    assert "windowNote" not in tight["payload"] and tight["windowNoteOmitted"] == "budget"
    assert tight["complete"] is True and tight["payload"] == {**plain["payload"], "checkpointId": tight["id"]}


def test_an_erase_of_a_turn_takes_the_note_and_the_checkpoint_carrying_it_stops_validating(setup, monkeypatch):
    mind, _, clock = setup
    turns, _ = written(mind, monkeypatch, clock)
    built = build(mind)
    assert MARKER in built["payload"]["windowNote"]
    mind.engine.delete(turns[0])
    settle(mind.engine)
    assert not SessionCheckpoint(mind, agent_version="synthetic-v1").validate(built)["valid"]
    assert texts_everywhere(mind.engine, MARKER) == set()
    [row] = rows(mind)
    with mind.engine.db.connect() as conn:
        assert not window_notes.live(conn, row["source_id"])
    assert "windowNote" not in build(mind)["payload"]


def test_a_note_the_sealed_checkpoint_has_no_room_for_after_all_is_dropped_not_made_incomplete(setup, monkeypatch):
    """The fit is estimated on a placeholder id; should the sealed payload come out larger, the note
    goes and the checkpoint is sealed again without it."""
    from kin_mind import session_checkpoint
    mind, _, clock = setup
    written(mind, monkeypatch, clock)
    counted = session_checkpoint.tokens
    monkeypatch.setattr(session_checkpoint, "tokens", lambda text: counted(text) + (
        20000 if "windowNote" in text and "checkpoint:" + "f" * 64 not in text else 0))
    built = build(mind)
    assert built["complete"] is True and "windowNote" not in built["payload"] and built["windowNoteOmitted"] == "budget"
    assert "windowNote" not in built.get("budgetPlan", {})


def test_a_checkpoint_whose_carried_note_alone_was_erased_stops_validating(setup, monkeypatch):
    mind, _, clock = setup
    written(mind, monkeypatch, clock)
    built = build(mind)
    checkpoints = SessionCheckpoint(mind, agent_version="synthetic-v1")
    assert checkpoints.validate(built)["valid"]
    mind.engine.delete(built["windowNoteSource"])
    settle(mind.engine)
    # Every turn is still there; only the note went, and its words with it.
    assert not checkpoints.validate(built)["valid"]
    assert texts_everywhere(mind.engine, "一起笑着道了晚安") == set()
    rebuilt = build(mind)
    assert "windowNote" not in rebuilt["payload"] and checkpoints.validate(rebuilt)["valid"]


# --- failures ------------------------------------------------------------------------------------

def test_a_failed_call_goes_back_with_a_backoff_and_a_stretch_without_dialogue_is_withheld(setup, monkeypatch):
    mind, _, clock = setup
    on(mind)
    window_notes.observe(mind, observation())
    endpoint = Endpoint(mind, monkeypatch)
    assert window_notes.run(mind, endpoint.provider)["state"] == "withheld" and not endpoint.sent
    assert rows(mind)[0]["state"] == "withheld" and json.loads(rows(mind)[0]["data"])["error"] == "no-dialogue"
    dialogue(mind, clock)
    window_notes.observe(mind, observation(compaction="cmp-1"))
    endpoint = Endpoint(mind, monkeypatch, httpx.Response(500, json={"error": "synthetic"}))
    assert window_notes.run(mind, endpoint.provider)["state"] == "pending" and len(endpoint.sent) == 1
    assert window_notes.due(mind) == 0, "it waits its backoff"
    clock[0] += timedelta(seconds=window_notes.BACKOFF_SECONDS + 1)
    assert window_notes.due(mind) == 1
    assert window_notes.run(mind, endpoint.provider)["state"] == "complete"


def test_a_newer_stretch_supersedes_an_older_note_still_waiting(setup, monkeypatch):
    mind, _, clock = setup
    on(mind)
    dialogue(mind, clock)
    old = window_notes.observe(mind, observation())["id"]
    clock[0] += timedelta(minutes=1)
    new = window_notes.observe(mind, observation(compaction="cmp-1"))["id"]
    endpoint = Endpoint(mind, monkeypatch)
    result = window_notes.run(mind, endpoint.provider)
    assert (result["id"], result["superseded"]) == (new, 1) and len(endpoint.sent) == 1
    assert {row["id"]: row["state"] for row in rows(mind)} == {old: "superseded", new: "written"}
    assert window_notes.run(mind, endpoint.provider) == {"state": "idle"}


# --- the host -------------------------------------------------------------------------------------

def test_the_host_queues_it_on_a_session_review_and_its_background_lane_writes_it(setup, monkeypatch, tmp_path):
    mind, _, clock = setup
    dialogue_on = {"records": True, "operational_lanes": True}
    MemoryContinuity(mind).configure(dialogue_on)
    # The host's own clock is the real one: the stretch happened before it.
    clock[0] -= timedelta(hours=1)
    dialogue(mind, clock)
    observed = tmp_path / "observation.json"
    observed.write_text(json.dumps(observation()))
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(), "agent_version": "synthetic-v1",
              "session_id": "synthetic-session", "adaptive_sessions": True, "session_observation_file": str(observed)}
    endpoint = Endpoint(mind, monkeypatch)
    monkeypatch.setattr(DeepSeek, "from_engine", classmethod(lambda cls, engine: endpoint.provider))
    # Off: the review is queued as before and nothing else.
    review = dispatch(config, "session-review", {"id": "review:obs-1:1", "snapshotId": "obs-1"})
    assert review["snapshotId"] == "obs-1" and review["created"] and rows(mind) == []
    assert dispatch(config, "review-due", {"tick": False})["enrichment"] is False
    MemoryContinuity(mind).configure({"window_notes": True})
    again = dispatch(config, "session-review", {"id": "review:obs-1:2", "snapshotId": "obs-1"})
    assert set(again) == set(review), "the answer stays the review's own"
    assert [row["state"] for row in rows(mind)] == ["pending"]
    assert dispatch(config, "review-due", {"tick": False})["enrichment"] is True
    ran = dispatch(config, "review-enrichment", {})
    assert ran["state"] == "complete" and len(endpoint.sent) == 1, (ran, rows(mind))
    assert dispatch(config, "review-due", {"tick": False})["enrichment"] is False
    status = dispatch(config, "window-notes", {})
    assert status["counts"] == {"written": 1} and status["due"] == 0 and status["enabled"] is True
