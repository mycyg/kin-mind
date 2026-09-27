"""Whether the work is moving, as the store says it and a connection that cannot write reads it
(OPS-02): embed jobs that failed for good in the last day, those that wait and since when, the last
vector made; appraisals in quarantine, how many, how old and how many new today; when Kin last
formed a wish, explored and reached out; and whether the local embedding service answers. The
host's health turns these into errors and warnings; the phone's self-check reads them in
`operational-status`. Counts, times, states and the queue's own sanitized errors only."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import Scope, SourceInput
from kin_mind.exploration import Explorations
from kin_mind.operational_status import embedding_service, liveness, operational_status, read_only
from kin_mind.state import Mind

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
iso = lambda **delta: (NOW - timedelta(**delta)).isoformat()


@pytest.fixture
def store(tmp_path):
    engine, scope = Engine(tmp_path / "memory"), Scope(persona="liveness")
    mind = Mind(engine, scope)
    init = engine.receive(SourceInput(namespace="test", key="configuration", scope=scope, text="configuration",
                                      authority="explicit"))["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[init])
    Explorations(mind)
    from kin_mind.appraisal import Appraisals
    Appraisals(mind)
    with engine.db.connect(write=True) as conn:
        conn.execute("DELETE FROM jobs")
        jobs = [("failed", "RuntimeError", iso(hours=30)),  # history: before the last day
                ("failed", "Local embedding service exited during startup (exit 1)", iso(hours=2)),
                ("failed", "Local embedding service exited during startup (exit 1)", (NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")),
                ("retry", "Local embedding service did not become ready within 30 s", iso(minutes=5)),
                ("pending", None, iso(minutes=1)), ("complete", None, iso(hours=40)), ("complete", None, iso(hours=26))]
        for i, (state, error, at) in enumerate(jobs):
            conn.execute("INSERT INTO jobs(id,kind,unique_key,payload,state,attempts,max_attempts,available,error,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"job_{i}", "embed", f"embed:{i}", "{}", state, 1, 5, 0, error,
                                                           iso(hours=50 - i), at))
        conn.execute("INSERT INTO jobs(id,kind,unique_key,payload,state,attempts,max_attempts,available,error,created_at,updated_at) "
                     "VALUES('job_x','extract','extract:x','{}','failed',5,5,0,'ValueError',?,?)", (iso(hours=1), iso(hours=1)))
        for i, (hours, started) in enumerate([(238, iso(hours=238)), (30, iso(hours=30)), (2, iso(hours=2))]):
            conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)",
                         (f"app_{i}", scope.key(), "needs-repair", NOW.timestamp() - hours * 3600,
                          dumps({"attempt_started_at": started, "repair_reason": "compression-passes-exhausted:13"})))
        conn.execute("INSERT INTO mind_appraisals(id,scope,state,available,data) VALUES('app_ok',?,'complete',0,'{}')", (scope.key(),))
        state = json.loads(conn.execute("SELECT data FROM mind_state WHERE scope=?", (scope.key(),)).fetchone()[0])
        state["desires"] = {"d1": {"id": "d1", "created_at": iso(hours=60)}, "d2": {"id": "d2", "created_at": iso(hours=50)}}
        conn.execute("UPDATE mind_state SET data=? WHERE scope=?", (dumps(state), scope.key()))
        conn.execute("INSERT INTO mind_explorations VALUES('explore_1',?,'complete',?,'{}')", (scope.key(), iso(hours=100)))
        conn.execute("INSERT INTO mind_contacts VALUES('contact_1',?,'accepted',?)", (scope.key(), dumps({"updated_at": iso(hours=58)})))
        conn.execute("INSERT INTO mind_contacts VALUES('contact_2',?,'canceled',?)", (scope.key(), dumps({"updated_at": iso(hours=1)})))
    return engine, scope, mind, tmp_path / "memory"


def test_the_store_says_whether_the_work_is_moving_through_a_connection_that_cannot_write(store):
    engine, scope, _mind, root = store
    conn = read_only(root)
    try:
        facts = liveness(conn, scope.key(), now=NOW.timestamp())
        with pytest.raises(Exception, match="readonly|read-only|query_only|attempt to write"):
            conn.execute("DELETE FROM jobs")
    finally:
        conn.close()
    embeddings = facts["embeddings"]
    assert (embeddings["failed_total"], embeddings["failed_24h"]) == (3, 2), "a space-separated time is read too"
    assert embeddings["last_failure"]["error"] == "Local embedding service exited during startup (exit 1)"
    assert (embeddings["waiting"], embeddings["oldest_waiting_hours"]) == (2, 47.0)
    assert embeddings["waiting_error"] == "Local embedding service did not become ready within 30 s"
    assert embeddings["hours_since_last_complete"] == 26.0
    assert facts["quarantine"] == {"count": 3, "oldest_hours": 238.0, "newest_hours": 2.0, "new_24h": 1,
                                   "reasons": {"compression-passes-exhausted:13": 3}}
    assert facts["last"]["hours_since"] == {"wish": 50.0, "exploration": 100.0, "contact": 58.0}
    assert "canceled" not in json.dumps(facts), "a canceled contact reached nobody"


def test_operational_status_carries_it_for_the_phone_self_check(store):
    _engine, _scope, mind, _root = store
    status = operational_status(mind)
    assert status["liveness"]["quarantine"]["count"] == 3
    assert status["liveness"]["embeddings"]["service"] == {"local": False}, "no local role configured"


def test_the_command_prints_it_as_one_line_and_writes_nothing(store):
    """What health runs: the configured interpreter, the source root on the path, the store read-only."""
    engine, scope, _mind, root = store
    before = {path.name: path.stat().st_mtime_ns for path in root.iterdir() if path.is_file()}
    source = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [source, os.environ.get("PYTHONPATH")]))}
    answer = subprocess.run([sys.executable, "-m", "kin_mind.operational_status", "liveness", "--root", str(root),
                             "--scope", json.dumps(scope.model_dump()), "--no-probe"],
                            capture_output=True, text=True, env=env, timeout=120)
    assert answer.returncode == 0, answer.stderr[-2000:]
    facts = json.loads(answer.stdout)
    assert facts["quarantine"]["count"] == 3 and facts["embeddings"]["failed_total"] == 3
    assert "service" not in facts["embeddings"]
    assert {path.name: path.stat().st_mtime_ns for path in root.iterdir() if path.is_file() and path.name in before} == before
    missing = subprocess.run([sys.executable, "-m", "kin_mind.operational_status", "liveness", "--root", str(root / "none"),
                              "--scope", json.dumps(scope.model_dump())], capture_output=True, text=True, env=env, timeout=120)
    assert missing.returncode != 0 and not (root / "none").exists(), "no store is ever created"


class Answer:
    def __init__(self, status, body=None, error=None):
        self.status, self.body, self.error, self.asked = status, body, error, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.asked.append((url, headers))
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.body)


def test_the_embedding_service_is_asked_on_its_own_port_with_its_own_credential(tmp_path):
    engine = Engine(tmp_path / "db")
    engine.settings("models", {"embedding": {"endpoint": "http://127.0.0.1:8399/v1", "model": "Qwen/Qwen3-Embedding-0.6B",
                                             "local_embedding": True, "dimensions": 2}})
    (engine.db.root / "embedding-token").write_text("synthetic-token\n")
    with engine.db.connect() as conn:
        down = Answer(0, error=httpx.ConnectError("refused"))
        assert embedding_service(conn, engine.db.root, client=down) == {"local": True, "reachable": False, "port": 8399, "reason": "ConnectError"}
        assert down.asked == [("http://127.0.0.1:8399/health", {"Authorization": "Bearer synthetic-token"})]
        up = Answer(200, {"service": "memorypalace-embedding", "loaded": True, "pid": 1})
        assert embedding_service(conn, engine.db.root, client=up) == {"local": True, "reachable": True, "port": 8399, "status": 200,
                                                                       "ours": True, "loaded": True}
        other = Answer(200, {"service": "something-else"})
        assert embedding_service(conn, engine.db.root, client=other)["ours"] is False
