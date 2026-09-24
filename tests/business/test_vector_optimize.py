"""The vector store is compacted once a day on a quiet store (DB1-02, K4-22; test T-29): old
versions go, every row stays, and a search answers exactly as before."""
import json
import time
from datetime import timedelta

import pytest

from eventmem.core import Engine
from eventmem.core import vectors
from eventmem.core.jobs import Worker

pytest.importorskip("lancedb")


def vector(i, dimensions=8):
    return [float((i * 7 + k) % 11) + 0.5 for k in range(dimensions)]


@pytest.fixture
def store(tmp_path):
    engine = Engine(tmp_path / "db")
    index_id = vectors.VectorIndex.register(engine, "synthetic-embedding", 8)
    index = vectors.VectorIndex(engine, index_id)
    for i in range(30):  # one write, one version, as the embed job makes them
        index.upsert([{"id": f"mem_{i:032x}", "scope": "synthetic", "revision": 1, "vector": vector(i)}])
    return engine, index_id


def search(engine, index_id):
    found = vectors.VectorIndex(engine, index_id).search(vector(3), scopes=["synthetic"], limit=10, exact=True)
    return [(hit["id"], round(hit["_distance"], 6)) for hit in found]


def test_compaction_keeps_every_row_and_every_answer(store):
    engine, index_id = store
    before = search(engine, index_id)
    table = vectors.VectorIndex(engine, index_id).table()
    versions = len(table.list_versions())
    report = vectors.optimize_all(engine, keep=timedelta(0))
    assert report[0]["rows_before"] == report[0]["rows_after"] == 30
    assert report[0]["versions_after"] < versions
    assert search(engine, index_id) == before
    with engine.db.connect() as conn:
        config = json.loads(conn.execute("SELECT data FROM vector_indexes WHERE id=?", (index_id,)).fetchone()[0])
    assert config["rows"] == 30 and config["optimized_at"]


def test_the_daily_job_waits_for_a_quiet_store(store, monkeypatch):
    engine, index_id = store
    monkeypatch.setattr(vectors, "OPTIMIZE_KEEP", timedelta(0))
    worker = Worker(engine)
    worker.schedule_maintenance()
    worker.last_maintenance = float("-inf")
    worker.schedule_maintenance()  # once a day, however often the tick comes
    with engine.db.connect(write=True) as conn:
        jobs = conn.execute("SELECT id,priority FROM jobs WHERE kind='vector_optimize'").fetchall()
        assert len(jobs) == 1 and jobs[0]["priority"] == 250
        # Something else is running: the compaction waits, and is not charged for it.
        engine.enqueue("organize", {"scope": {"project": "p"}}, "busy-elsewhere", conn=conn)
        conn.execute("UPDATE jobs SET state='running',lease_until=? WHERE unique_key='busy-elsewhere'", (time.time() + 60,))
    assert worker.run_once()
    with engine.db.connect(write=True) as conn:
        state, attempts, error = conn.execute("SELECT state,attempts,error FROM jobs WHERE kind='vector_optimize'").fetchone()
        assert (state, attempts, error) == ("retry", 0, "store-not-quiet")
        conn.execute("UPDATE jobs SET state='complete' WHERE unique_key='busy-elsewhere'")
        conn.execute("UPDATE jobs SET available=0 WHERE kind='vector_optimize'")
    before = search(engine, index_id)
    assert worker.run_once()
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE kind='vector_optimize'").fetchone()[0] == "complete"
        metric = json.loads(conn.execute("SELECT data FROM metrics WHERE name='vector_optimized'").fetchone()[0])
    assert metric["rows_unchanged"] and metric["versions_removed"] > 0
    assert search(engine, index_id) == before
