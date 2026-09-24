"""The HTTP service as its clients meet it: the credential (E3-01), the session boundary's reply
(E3-02), health that covers the background loop (E2-02, H3-09), lease answers (E3-05), recall
and read in a kin context scope (E3-20, S1-01, S1-02), times in reads (E3-16), what the chat
model may ask for (E3-14, E3-15), list order (S1-16), one loop per store (S1-11), the text
index rebuild (E3-15, E2-09), deep recall with a model down (E3-07) and the tokenizer without
its download (E3-08)."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core import retrieval
from eventmem.core.api import create_app, credential
from eventmem.core.jobs import REBUILD_BATCH, Worker
from eventmem.core.models import RecallRequest, Scope, SourceInput
from eventmem.core.reading import read_segment

from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

TOKEN = "synthetic-test-credential"
KIN = Scope(persona="synthetic-kin")


def auth(token=TOKEN):
    return {"Authorization": "Bearer " + token}


def receive(engine, key, text, scope=None, **extra):
    return engine.receive(SourceInput(namespace="kin-owner-input", key=key, text=text, scope=scope or Scope(),
                                      authority="explicit", extract=False,
                                      metadata={"role": "user", "host_event": "message"}, **extra))["id"]


def record_of(engine, sid):
    return engine.source(sid)["record_ids"][0]


@pytest.fixture
def kin(tmp_path):
    """A scope whose memory context is on, as the production Kin scope is."""
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, KIN)
    memory = MemoryContinuity(mind)
    first = receive(engine, "initial", "Initial owner setup", KIN)
    mind.initialize(agent_version="fixture-v1", evidence_ids=[first])
    memory.configure({"context": True, "usage_reinforcement": True, "temperature_shadow": True,
                      "event_lifecycle": True})
    return engine


def test_an_empty_credential_file_is_replaced_and_never_admits_anyone(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "local-token").write_text("  \n")
    token = credential(root)
    assert token and (root / "local-token").read_text().strip() == token
    assert credential(root) == token  # a second start reads the same one
    with pytest.raises(RuntimeError):
        create_app(engine=Engine(tmp_path / "other"), token=" ", workers=False, mcp_enabled=False)
    client = TestClient(create_app(engine=Engine(root), workers=False, mcp_enabled=False))
    assert client.get("/v1/overview").status_code == 401
    assert client.get("/v1/overview", headers={"Authorization": "Bearer "}).status_code == 401
    # A header with bytes outside ASCII is a wrong credential, not a server error.
    assert client.get("/v1/overview", headers={"Authorization": "Bearer é".encode("latin-1")}).status_code == 401
    assert client.get("/v1/overview", headers=auth(token)).status_code == 200


def test_the_boundary_says_what_it_kept(tmp_path):
    client = TestClient(create_app(engine=Engine(tmp_path / "db"), token=TOKEN, workers=False, mcp_enabled=False))
    body = {"session": "s1", "event": "checkpoint", "command_id": "c1",
            "checkpoint": {"goals": ["ship"], "secret_notes": "not a checkpoint field"}}
    answer = client.post("/v1/sessions/boundary", json=body, headers=auth()).json()
    assert answer["saved"] and answer["status"] == "saved"
    assert answer["checkpoint"] == {"goals": ["ship"]} and answer["dropped_keys"] == ["secret_notes"]
    empty = client.post("/v1/sessions/boundary", headers=auth(),
                        json={**body, "command_id": "c2", "checkpoint": {"other": 1}}).json()
    assert not empty["saved"] and empty["status"] == "nothing_saved" and empty["checkpoint"] == {}


def test_health_is_ready_only_while_the_background_loop_goes_round(tmp_path):
    engine = Engine(tmp_path / "db")
    app = create_app(engine=engine, token=TOKEN, workers=True, mcp_enabled=False)
    # The loop was never started: the service answers, and says it is not healthy.
    stopped = TestClient(app).get("/v1/health", headers=auth())
    assert stopped.status_code == 503 and stopped.json()["worker"]["thread_alive"] is False
    with TestClient(app) as client:
        deadline = time.time() + 10
        while app.state.worker.last_tick is None and time.time() < deadline:
            time.sleep(0.05)
        ready = client.get("/v1/health", headers=auth())
        assert ready.status_code == 200 and ready.json()["worker"]["alive"]
        # A loop that has not gone round, nor renewed a job, for too long is reported.
        worker = app.state.worker
        worker.stopped.set()
        app.state.worker_thread.join(5)
        worker.stopped.clear()
        worker.last_tick = worker.last_beat = time.time() - 3600
        assert client.get("/v1/health", headers=auth()).status_code == 503
        worker.stopped.set()
    without = TestClient(create_app(engine=engine, token=TOKEN, workers=False, mcp_enabled=False))
    assert without.get("/v1/health", headers=auth()).json()["worker"] == {"enabled": False}


def test_a_busy_ledger_is_an_answer_not_a_failure(tmp_path, monkeypatch):
    from kin_mind import model_lanes

    monkeypatch.setattr(model_lanes.Ledger, "acquire", lambda self, *a, **k: {"state": "busy", "reason": "ledger-busy"})
    client = TestClient(create_app(engine=Engine(tmp_path / "db"), token=TOKEN, workers=False, mcp_enabled=False))
    answer = client.post("/v1/model-leases/acquire", headers=auth(),
                         json={"lane": "background", "purpose": "synthetic", "id": "synthetic-lease"})
    assert answer.status_code == 200 and answer.json()["state"] == "busy"


def test_recall_and_read_in_a_kin_context_scope_answer_their_contracts(kin):
    engine = kin
    rid = record_of(engine, receive(engine, "walk", "We walked along the harbour at dusk", KIN))
    client = TestClient(create_app(engine=engine, token=TOKEN, workers=False, mcp_enabled=False))
    with engine.db.connect() as conn:
        usage_before = conn.execute("SELECT COUNT(*) FROM mind_event_usage WHERE origin!='maintenance'").fetchone()[0]
    found = client.post("/v1/recall", headers=auth(), json={"query": "harbour dusk", "scope": KIN.model_dump()})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["items"] == body["index"] and "latency_ms" in body and body["instruction_authority"] == "data"
    # A look from the console is not Kin using the memory: nothing is strengthened by it, and
    # it does not hold the foreground.
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_event_usage WHERE origin!='maintenance'").fetchone()[0] == usage_before
        assert not conn.execute("SELECT 1 FROM mind_foreground_leases").fetchone()
    read = client.get(f"/v1/memories/{rid}", headers=auth())
    assert read.status_code == 200, read.text
    record = read.json()
    assert record["content"] == "We walked along the harbour at dusk" and record["kind"] and record["scope"]
    # The model's read still goes through the context, now with the record's own fields.
    through = read_segment(engine, rid)
    assert through["id"] == rid and through["kind"] == record["kind"] and "depth" in through


def test_times_in_a_read_are_compared_as_utc(tmp_path):
    engine = Engine(tmp_path / "db")
    rid = record_of(engine, receive(engine, "clock", "A note"))
    stamp = engine.get(rid)["received_at"]
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=1)
    local = moment.astimezone(timezone(timedelta(hours=8))).isoformat()
    utc = moment.astimezone(timezone.utc).isoformat()
    assert read_segment(engine, rid, known_at=local)["revision"] == read_segment(engine, rid, known_at=utc)["revision"]
    before = (datetime.fromisoformat(stamp) - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat()
    with pytest.raises(Exception):
        read_segment(engine, rid, known_at=before)


def test_the_chat_model_cannot_rebuild_indexes_or_write_feedback(tmp_path):
    import asyncio

    from eventmem.core.mcp import create_mcp

    engine = Engine(tmp_path / "db")
    tools = {tool.name: tool for tool in asyncio.run(create_mcp(engine).list_tools())}
    assert "memory_feedback" not in tools
    kinds = json.dumps(tools["request_maintenance"].inputSchema)
    assert "organize" in kinds and not any(kind in kinds for kind in ("rebuild", "build_vectors", "purge_vectors"))
    client = TestClient(create_app(engine=engine, token=TOKEN, workers=False, mcp_enabled=False))
    assert client.post("/v1/feedback", headers=auth(), json={"record_id": "x", "type": "read"}).status_code in {404, 405}


def test_jobs_and_deliveries_can_be_listed_latest_first(tmp_path):
    engine = Engine(tmp_path / "db")
    ids = []
    for i in range(5):
        ids.append(engine.enqueue("organize", {"scope": Scope().model_dump(), "n": i}, f"list-{i}"))
        with engine.db.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (f"2026-09-2{i}T00:00:00+00:00", ids[-1]))
    client = TestClient(create_app(engine=engine, token=TOKEN, workers=False, mcp_enabled=False))
    first = client.get("/v1/jobs?order=recent&limit=2", headers=auth()).json()
    second = client.get(f"/v1/jobs?order=recent&limit=2&cursor={first['cursor']}", headers=auth()).json()
    assert [j["id"] for j in first["items"] + second["items"]] == ids[::-1][:4]
    assert client.get("/v1/contact/outbox?order=recent", headers=auth()).status_code == 200


def test_a_second_loop_on_the_same_store_waits(tmp_path):
    engine = Engine(tmp_path / "db")
    first, second = Worker(engine), Worker(Engine(tmp_path / "db"))
    held = first.hold_store()
    runner = threading.Thread(target=second.run, daemon=True)
    runner.start()
    deadline = time.time() + 5
    while not second.standby and time.time() < deadline:
        time.sleep(0.05)
    assert second.standby and second.last_tick is None and not second.health()["alive"]
    held.close()
    deadline = time.time() + 10
    while second.last_tick is None and time.time() < deadline:
        time.sleep(0.05)
    assert second.last_tick is not None and not second.standby
    second.stopped.set()
    runner.join(10)


def test_the_text_index_is_rebuilt_in_scoped_batches_and_invalidates_nothing(tmp_path, monkeypatch):
    from eventmem.core import jobs

    monkeypatch.setattr(jobs, "REBUILD_BATCH", 3)
    engine = Engine(tmp_path / "db")
    other = Scope(persona="synthetic-other")
    ours = [record_of(engine, receive(engine, f"r{i}", f"harbour note number {i}")) for i in range(7)]
    theirs = record_of(engine, receive(engine, "o", "harbour elsewhere", other))
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM search")
        conn.execute("DELETE FROM dirty")
        embeds = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed'").fetchone()[0]
    engine.enqueue("rebuild", {"scope": Scope().model_dump()}, "rebuild-ours")
    worker = Worker(engine)
    while worker.run_once():
        pass
    with engine.db.connect() as conn:
        indexed = {row[0] for row in conn.execute("SELECT id FROM search")}
        steps = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='rebuild' AND state='complete'").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM dirty").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed'").fetchone()[0] == embeds
    assert set(ours) <= indexed and theirs not in indexed
    assert steps == 3  # 3 + 3 + 1 of the scope's records, each batch a job of its own
    assert REBUILD_BATCH == 500


def test_a_deep_recall_with_its_models_down_still_answers(tmp_path):
    engine = Engine(tmp_path / "db")
    receive(engine, "deep", "The harbour festival is in October")
    down = {"endpoint": "http://127.0.0.1:9/v1", "model": "synthetic", "timeout_seconds": 2}
    engine.settings("models", {role: down for role in ("embedding", "query", "rerank", "visual_embedding")})
    found = engine.recall(RecallRequest(query="harbour festival", mode="deep", explain=True))
    assert found["items"] and "embedding_unavailable:ConnectError" in found["trace"]["degraded"]


def test_tokens_are_counted_without_downloading_the_encoding(tmp_path, monkeypatch):
    import shutil
    import tempfile

    source = retrieval.encoding_cache() / retrieval.CL100K_CACHE_NAME
    assert retrieval._cached(source.parent), "the test run provides the encoding in its cache"
    try:
        # No encoding at hand: a byte count, which is never below the real count.
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty"))
        retrieval.encoding.cache_clear()
        assert retrieval.tokens("你好 harbour") == len("你好 harbour".encode())
        # The store's own cache is filled from the old default place and used from then on.
        old = tmp_path / "system-temp" / "data-gym-cache"
        old.mkdir(parents=True)
        shutil.copyfile(source, old / retrieval.CL100K_CACHE_NAME)
        monkeypatch.delenv("TIKTOKEN_CACHE_DIR")
        monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(old.parent))
        assert retrieval.pin_encoding_cache(tmp_path / "store-cache")
        assert retrieval._cached(tmp_path / "store-cache")
        retrieval.encoding.cache_clear()
        assert retrieval.tokens("你好 harbour") < len("你好 harbour".encode())
    finally:
        retrieval.encoding.cache_clear()
