"""Bounds the memory layer keeps.

A disclosure only summarises a share the assessment's own sources reach (K3-02). A date is cut at
the owner's midnight, and a continued recall round searches nothing again (K3-16). A reflection
names no owner and no role, and one kept under the old wording is returned as it is (K1-21, K3-03).
Nothing is claimed or paid for while history compaction holds the store, and what compaction alone
set aside goes back to the queue with the marker (K3-14). Frequency ranking is gated on the
replay's own output file (E3-10). A cached judgment is written in one transaction and keeps the
validity it was given (K3-17). A context delivery from an earlier window ends (DB1-12)."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, digest, dumps
from eventmem.core.models import Scope, SourceInput

from kin_mind import adaptive_recall, history_compaction, judgment_cache, reinforcement
from kin_mind.adaptive_recall import AdaptiveRecall, RecallRanking
from kin_mind.context import Contexts
from kin_mind.context_delivery import ContextDelivery
from kin_mind.memory import Disclosure, MemoryAssessment, MemoryContinuity
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-bounds")


def owner(engine, key, text, at):
    return engine.receive(SourceInput(namespace="kin-owner-input", key=key, text=text, scope=SCOPE,
                                      authority="explicit", extract=False, occurred_at=at,
                                      metadata={"role": "user", "host_event": "message"}))["id"]


@pytest.fixture
def world(tmp_path):
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, SCOPE)
    first = owner(engine, "init", "setup", "2026-09-01T00:00:00+00:00")
    mind.initialize(agent_version="fixture-v1", evidence_ids=[first])
    return engine, mind, MemoryContinuity(mind)


def deliver(memory, n):
    return memory.ingest({"kind": "delivery", "id": f"delivery-{n}", "at": f"2026-09-0{n}T10:00:00+00:00",
                          "channel": "synthetic", "delivery_id": f"d{n}", "text": f"synthetic share {n}",
                          "state": "accepted", "message_id": f"m{n}"})


def test_a_disclosure_only_summarises_a_share_this_assessment_reached(world):
    engine, mind, memory = world
    memory.configure({"records": True, "sharing": True})
    old, new = deliver(memory, 2), deliver(memory, 3)
    with engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [new["source_id"]])
        dropped = memory.apply_assessment(conn, MemoryAssessment(disclosures=[
            Disclosure(share_id=old["share_id"], topic="unrelated", summary="rewritten from elsewhere"),
            Disclosure(share_id=new["share_id"], topic="this one", summary="what was shared now")]),
            refs, "evt-bounds", 0, 20, {}, schedule=False)
        shares = {sid: memory._get(conn, sid) for sid in (old["share_id"], new["share_id"])}
    assert [d["code"] for d in dropped] == ["evidence-out-of-bounds"]
    assert "summary" not in shares[old["share_id"]] and shares[old["share_id"]]["semantic_state"] == "pending"
    assert shares[new["share_id"]]["summary"] == "what was shared now"


def test_a_date_is_cut_at_the_owners_own_midnight(world):
    engine, mind, memory = world
    memory.configure({"records": True, "context": True})
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_state SET data=json_set(data,'$.profile.contact.timezone','America/New_York') WHERE scope=?",
                     (SCOPE.key(),))
    # 20:00 on 3 September in New York is already 4 September in Singapore.
    late = owner(engine, "late", "我们周末去看海吧", "2026-09-03T20:00:00-04:00")
    found, info = AdaptiveRecall(Contexts(mind)).collect("2026年9月3日说了什么", mode="light", allow_model=False)
    assert "mem_" + digest([late, "root"])[:32] in {item["id"] for item in found}


class Ranker:
    """The reranking model: its first answer asks for what came after the first candidate."""

    def __init__(self):
        self.calls, self.timeout, self.absolute_deadline = 0, None, None

    def structured(self, name, model, prompt, payload, **kwargs):
        self.calls += 1
        followups = [{"candidate_id": "c1", "direction": "after"}] if self.calls == 1 else []
        return RecallRanking(ids=["c1"], followups=followups), {"usage": {"input_tokens": 1, "output_tokens": 1}}


def test_a_continued_round_searches_nothing_again(world, monkeypatch):
    engine, mind, memory = world
    memory.configure({"records": True, "context": True})
    for n in range(4):
        owner(engine, f"sea-{n}", f"看海的计划第{n}步", f"2026-09-1{n}T10:00:00+00:00")
    searched, real = [], adaptive_recall.candidates
    monkeypatch.setattr(adaptive_recall, "candidates", lambda *a, **k: searched.append(1) or real(*a, **k))

    def no_embedding(*args, **kwargs):
        raise RuntimeError("no embedding service in this test")

    monkeypatch.setattr("eventmem.core.providers.Providers.embed", no_embedding)
    ranker = Ranker()
    found, info = AdaptiveRecall(Contexts(mind)).collect("看海的计划", mode="deep", allow_model=True, provider=ranker)
    assert ranker.calls >= 2 and info["rounds"] >= 2
    assert len(searched) == 1  # the follow-up round went on from the pool it had


def test_a_reflection_names_no_owner_and_an_earlier_one_stays_as_kept(world):
    engine, mind, memory = world
    with engine.db.connect() as conn:
        event_id = conn.execute("SELECT id FROM mind_events WHERE scope=? ORDER BY revision LIMIT 1",
                                (SCOPE.key(),)).fetchone()[0]
    result = {"event_id": event_id, "proposal": {"understanding": {
        "basis": "internal_thought", "meaning": "想画一颗会唱歌的紫色陨石", "topic": "陨石"}}}
    kept = engine.receive(SourceInput(namespace="kin-reflection", key=event_id, scope=SCOPE, authority="model",
                                      kind="episode", text="小Kin自己琢磨的（旧的写法）：想画一颗会唱歌的紫色陨石",
                                      metadata={"role": "assistant", "basis": "internal_thought", "internal": True}))
    assert memory.remember_reflection(result)["id"] == kept["id"]  # a replay, not a conflict
    other = Engine(engine.db.root.parent / "other")
    other_mind = Mind(other, SCOPE)
    other_mind.initialize(agent_version="fixture-v1", evidence_ids=[owner(other, "init", "setup", "2026-09-01T00:00:00+00:00")])
    with other.db.connect() as conn:
        event_id = conn.execute("SELECT id FROM mind_events WHERE scope=? ORDER BY revision LIMIT 1",
                                (SCOPE.key(),)).fetchone()[0]
    fresh = MemoryContinuity(other_mind).remember_reflection({**result, "event_id": event_id})
    text = other.source(fresh["id"], content=True).read_text()
    assert text.startswith("Kin 自己的想法") and "小光" not in text and "小Kin" not in text


class Untouchable:
    """A provider that must never be asked anything."""

    def __getattr__(self, name):
        raise AssertionError("no model is called while history is being compacted")


def test_nothing_is_claimed_while_history_is_compacted_and_its_refusals_come_back(world):
    engine, mind, memory = world
    from kin_mind.appraisal import Appraisals

    jobs = Appraisals(mind)
    sid = owner(engine, "during", "compaction is running", "2026-09-02T00:00:00+00:00")
    job = jobs.enqueue([sid], "fixture-v1")
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO meta VALUES('history_compaction_active',1)")
        conn.execute("INSERT INTO mind_appraisals VALUES(?,?,?,?,0,2,?)",
                     ("refused-twice", SCOPE.key(), "needs-repair", time.time(),
                      dumps({"stimulus": "interaction", "error": "Conflict",
                             "repair_reason": "repeated-failure:history-compaction-active",
                             "error_detail": {"class": "Conflict", "code": "history-compaction-active", "kind": "runtime"},
                             "error_signature": "sig", "error_repeats": 2})))
    assert jobs.run_one(Untouchable()) == {"state": "paused", "reason": "history-compaction-active"}
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state,attempts FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[:] == ("pending", 0)
    history_compaction._set_marker(engine.db, False)
    with engine.db.connect() as conn:
        state, attempts, data = conn.execute("SELECT state,attempts,data FROM mind_appraisals WHERE id='refused-twice'").fetchone()
    data = json.loads(data)
    assert (state, attempts) == ("pending", 0) and "repair_reason" not in data and "error_repeats" not in data
    assert data["recovery_history"][-1]["command_id"] == "history-compaction-finished"


def replay(tmp_path, *, critical_hits=21, other_hits=23):
    outcomes = ([{"id": f"k{i}", "critical": True, "hit_at_8": i < critical_hits} for i in range(21)]
                + [{"id": f"o{i}", "critical": False, "hit_at_8": i < other_hits} for i in range(27)])
    path = tmp_path / f"replay-{critical_hits}-{other_hits}.json"
    path.write_text(json.dumps({"cases": 48, "critical_cases": 21, "outcomes": outcomes}))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_frequency_ranking_is_gated_on_the_replays_own_output(world, tmp_path):
    engine, mind, memory = world
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with engine.db.connect(write=True) as conn:
        for day in range(8):
            at = started + timedelta(days=day, hours=1)
            conn.execute("INSERT INTO mind_strength_observations VALUES(?,?,?,?,?)",
                         (SCOPE.key(), reinforcement.VERSION, (at + timedelta(hours=8)).date().isoformat(), at.isoformat(), "{}"))
    path, sha = replay(tmp_path)

    def config(**validation):
        return {"reinforcement_started_at": started.isoformat(), "reinforcement_validation": {
            "weight_version": reinforcement.VERSION, "owner_approved": True,
            "evaluated_at": (started + timedelta(days=8)).isoformat(), **validation}}

    at = (started + timedelta(days=9)).isoformat()
    with engine.db.connect() as conn:
        reinforcement.validate_enable(conn, SCOPE.key(), config(replay_output=str(path), replay_sha256=sha), at)
        # Hand-copied numbers are no longer a replay.
        with pytest.raises(Conflict):
            reinforcement.validate_enable(conn, SCOPE.key(), config(critical_hits=21, hit_at_8=44, background_heating=0), at)
        short, short_sha = replay(tmp_path, critical_hits=20, other_hits=27)
        with pytest.raises(Conflict):
            reinforcement.validate_enable(conn, SCOPE.key(), config(replay_output=str(short), replay_sha256=short_sha), at)
        path.write_text(path.read_text().replace('"hit_at_8": false', '"hit_at_8": true'))
        with pytest.raises(Conflict, match="changed since"):
            reinforcement.validate_enable(conn, SCOPE.key(), config(replay_output=str(path), replay_sha256=sha), at)


def test_a_cached_judgment_is_written_in_one_transaction(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "db")
    held, real = [], judgment_cache.environment_digest

    def probe(engine_, conn, scope):
        # Inside put(): another writer has to wait for the row and its dependencies.
        other = sqlite3.connect(engine.db.path, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)
        finally:
            other.close()
        return real(engine_, conn, scope)

    monkeypatch.setattr(judgment_cache, "environment_digest", probe)
    token = judgment_cache.put(engine, "judge_step", "request-1", {"scope": "synthetic", "type": "step-complete"},
                               {"verdict": "done"}, now=1000.0, depends_on=["src_synthetic"], valid_for=3600)
    assert held == [True]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT expires_at,valid_until FROM mind_judgment_cache WHERE token=?", (token,)).fetchone()[:] == (4600.0, 4600.0)
        assert conn.execute("SELECT dependency FROM mind_judgment_cache_deps WHERE token=?", (token,)).fetchone()[0] == "src_synthetic"


def test_a_delivery_that_can_no_longer_arrive_ends(world):
    engine, mind, memory = world
    contexts = Contexts(mind)
    delivery, session = ContextDelivery(contexts), "thread-1"
    old = delivery.prepare(session, "initial", "event-1", "synthetic context one", [])
    delivery.begin(session, "initial", old["id"])
    delivery.uncertain(session, "initial", old["id"])
    abandoned = delivery.prepare(session, "initial", "event-2", "synthetic context two", [])
    assert [p["id"] for p in delivery.pending(session)] == [old["id"]]
    contexts.compact_ack(session, "epoch-2", actual_session=session, completed=True)
    assert delivery.pending(session) == []  # an earlier window's leftover no longer leads the list
    later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    first = delivery.prepare(session, "epoch-2", "event-3", "synthetic context three", [])
    mind.clock = lambda: later
    delivery.prepare(session, "epoch-2", "event-4", "synthetic context four", [])
    with engine.db.connect() as conn:
        rows = {r[0]: (r[1], json.loads(r[2]).get("stale_reason")) for r in conn.execute(
            "SELECT id,state,data FROM mind_context_deliveries")}
    assert rows[old["id"]] == ("stale", "window-moved") and rows[abandoned["id"]] == ("stale", "window-moved")
    assert rows[first["id"]] == ("stale", "never-begun")
