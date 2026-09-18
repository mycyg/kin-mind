"""The one hard delete in the programme, and the proof that it costs a model call.

Two claims are on trial here and both are load-bearing for the decision to enable
any of this. The first is that `mind_context_cache` holds nothing an erase or a
sweep could destroy: a removed row is rebuilt, byte for byte, by the reader that
misses it. The second is that a sweep cannot reach anything else — not another
table, not the newest row an appraisal pass is counting against, not a row while a
worker still holds the lease that would be writing them.

Everything below is constructed: an injected clock, rows written through the public
compression path or through plain SQL, and a process table the test writes itself.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_liveness import probe, quiet_host

from eventmem.core.db import dumps
from kin_mind import maintenance
from kin_mind.appraisal import Appraisals
from kin_mind.context import Contexts
from kin_mind.host import dispatch

pytest_plugins = ("test_memory_continuity",)

FAR_FUTURE = datetime(2027, 1, 1, tzinfo=timezone.utc)


def cache_rows(mind):
    with mind.engine.db.connect() as conn:
        return [row["id"] for row in conn.execute(
            "SELECT id FROM mind_context_cache WHERE scope=? ORDER BY rowid", (mind.scope.key(),))]


def count(mind, table):
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def write_cache(mind, identifier, *, at, data=None):
    Contexts(mind)  # the module that owns the table is the one that creates it
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO mind_context_cache VALUES(?,?,?,?)",
                     (identifier, mind.scope.key(), dumps(data or {"text": "A compressed summary."}), at))


def config_for(mind, **extra):
    return {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump(),
            "agent_version": "fixture-v1", "session_id": "fixture", **extra}


def days_ago(mind, days):
    return (datetime.fromisoformat(mind.clock()) - timedelta(days=days)).isoformat()


def test_the_sweep_is_off_and_reports_what_it_would_have_done(system):
    mind, memory, _, _ = system
    for index in range(3):
        write_cache(mind, "old-%d" % index, at=days_ago(mind, 40))
    found = maintenance.tick(mind, config_for(mind), apply=True)
    assert found["context_cache"]["state"] == "disabled"
    assert found["context_cache"]["flag"] == "context_cache_sweep"
    # It says what it would remove, which is the number this decision is taken on.
    assert found["context_cache"]["would_remove"] == 2 and found["context_cache"]["expired"] == 3
    assert found["context_cache"]["retained_newest"] is True
    assert len(cache_rows(mind)) == 3
    assert memory.settings()["context_cache_sweep"] is False


def test_the_age_and_the_cap_are_the_only_two_rules(system):
    mind, memory, _, _ = system
    memory.configure({"context_cache_sweep": True})
    # Written oldest first, as a store writes them: the newest row is the one the
    # sweep holds back, and here it is one the rules would have kept anyway.
    write_cache(mind, "expired", at=days_ago(mind, 15))
    write_cache(mind, "just-inside", at=days_ago(mind, 13))
    for index in (2, 1, 0):
        write_cache(mind, "fresh-%d" % index, at=days_ago(mind, index))
    with mind.engine.db.connect() as conn:
        found = maintenance.cache_plan(conn, mind.scope.key(),
                                       now=datetime.fromisoformat(mind.clock()), cap=2)
    # Fourteen days on the row's own stamp, then the cap on whatever survived it.
    assert found["expired"] == 1 and found["over_cap"] == 2 and found["rows"] == 5
    assert set(found["targets"]) == {"expired", "just-inside", "fresh-2"}
    assert found["retained_newest"] is False
    assert found["bytes_to_remove"] > 0 and found["unreadable"] == 0
    # A stamp that cannot be read is never an old stamp.
    write_cache(mind, "unreadable", at="whenever")
    with mind.engine.db.connect() as conn:
        assert maintenance.cache_plan(conn, mind.scope.key())["unreadable"] == 1


def test_a_swept_summary_is_rebuilt_word_for_word_by_the_next_reader(system):
    """The whole justification for hard-deleting these rows, as an assertion."""
    mind, memory, source, _ = system
    from test_memory_continuity import Compressor
    memory.configure({"context_cache_sweep": True})
    sid = source("long", "A file was created. Delivery failed.\n\n" + "Supporting detail about the task.\n\n" * 70)
    with mind.engine.db.connect() as conn:
        rid = mind._evidence(conn, [sid])[0]["record_id"]
    contexts, provider = Contexts(mind), Compressor()
    item = contexts.record_item(mind.engine.get(rid))
    first = contexts.pack([item], "Did you send it?", 150, provider=provider)
    later = contexts.pack([item], "What was created?", 150, provider=provider)
    assert first["state"] == "compressed" and contexts.pack([item], "Did you send it?", 150, provider=provider)["cache_hit"]
    calls, held = provider.calls, len(cache_rows(mind))
    swept = maintenance.sweep_context_cache(mind, apply=True, now=FAR_FUTURE)
    # Everything is past the age but one row is always kept, so the whole cache goes
    # except the newest, which here is the second question's own pack.
    assert swept["state"] == "swept" and swept["removed"] == held - 1 and len(cache_rows(mind)) == 1
    rebuilt = contexts.pack([item], "Did you send it?", 150, provider=provider)
    assert not rebuilt["cache_hit"] and provider.calls > calls
    assert rebuilt["text"] == first["text"] and rebuilt["covered_ids"] == first["covered_ids"]
    # And the row it did not touch is still served, because one row is always kept.
    kept = contexts.pack([item], "What was created?", 150, provider=provider)
    assert kept["cache_hit"] and kept["text"] == later["text"]


def test_a_sweep_reaches_no_other_table(system):
    mind, memory, _, _ = system
    memory.configure({"context_cache_sweep": True})
    write_cache(mind, "older", at=days_ago(mind, 31))
    write_cache(mind, "old", at=days_ago(mind, 30))
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_context_windows VALUES(?,?,?,?,?)",
                     (mind.scope.key(), "session", "epoch", 10, dumps({"receipts": {}})))
        conn.execute("INSERT INTO mind_context_compactions VALUES(?,?,?)", (mind.scope.key(), "session", "epoch"))
    before = {table: count(mind, table) for table in
              ("mind_events", "mind_state", "mind_context_windows", "mind_context_compactions", "records", "sources")}
    maintenance.sweep_context_cache(mind, apply=True, now=FAR_FUTURE)
    assert cache_rows(mind) == ["old"]
    assert {table: count(mind, table) for table in before} == before
    # And the module cannot be widened by a later edit without this saying so: the
    # only tables it can delete out of at all are the derived cache and the
    # telemetry ring, which is a ring already and holds no memory.
    assert set(re.findall(r"DELETE FROM (\w+)", Path(maintenance.__file__).read_text())) == {
        "mind_context_cache", "metrics"}
    assert maintenance.DELETE_ROW == "DELETE FROM mind_context_cache WHERE scope=? AND id=?"


def test_the_newest_row_survives_so_a_pass_is_never_told_it_stalled(system):
    """`Appraisals._cache_mark` is `MAX(rowid)`, and SQLite reuses a deleted highest
    rowid. Keeping one row is what keeps a compressing pass out of quarantine."""
    mind, memory, _, _ = system
    memory.configure({"context_cache_sweep": True})
    for index in range(4):
        write_cache(mind, "old-%d" % index, at=days_ago(mind, 20))
    appraisals = Appraisals(mind)
    mark = appraisals._cache_mark()
    swept = maintenance.sweep_context_cache(mind, apply=True, now=FAR_FUTURE)
    assert swept["removed"] == 3 and swept["retained_newest"] is True
    write_cache(mind, "a-part-cached-by-this-pass", at=mind.clock(), data={"value": {"entries": []}, "receipt": {}})
    assert appraisals._compression_progress(mark) == 1


def test_a_fresh_appraisal_lease_stops_the_sweep(system):
    mind, memory, source, _ = system
    memory.configure({"context_cache_sweep": True})
    write_cache(mind, "older", at=days_ago(mind, 31))
    write_cache(mind, "old", at=days_ago(mind, 30))
    job = Appraisals(mind).enqueue([source("held")], "fixture-v1")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=? WHERE id=?", (time.time() + 600, job["id"]))
    blocked = maintenance.sweep_context_cache(mind, apply=True)
    assert blocked["state"] == "blocked" and blocked["reason"] == "worker-lease-fresh"
    assert len(cache_rows(mind)) == 2
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET lease=? WHERE id=?", (time.time() - 1, job["id"]))
    assert maintenance.sweep_context_cache(mind, apply=True, now=FAR_FUTURE)["state"] == "swept"


def test_erase_takes_the_compressed_copies_only_where_they_were_asked_for(system):
    mind, memory, source, _ = system
    write_cache(mind, "derived", at=mind.clock())
    # Today an erase leaves the compressed copy of the erased text behind. Pinned as
    # it is, because that gap is what the flag closes rather than what it introduced.
    mind.engine.delete(source("secret", "A sentence the owner asked to have erased."))
    assert cache_rows(mind) == ["derived"]
    memory.configure({"context_cache_sweep": True})
    mind.engine.delete(source("secret-two", "Another sentence to erase."))
    assert cache_rows(mind) == []


def test_one_metric_name_can_no_longer_evict_another(system, monkeypatch):
    mind, memory, _, _ = system
    monkeypatch.setattr(maintenance, "METRIC_RING", 3)
    memory.configure({"metrics_name_ring": True})
    mind.engine.db.metric("appraisal_quarantined", 1, {"reason": "the rare one"})
    for index in range(6):
        mind.engine.db.metric("model_ms", float(index))
    with mind.engine.db.connect() as conn:
        held = dict(conn.execute("SELECT name,COUNT(*) FROM metrics GROUP BY name").fetchall())
    assert held["model_ms"] == 3 and held["appraisal_quarantined"] == 1
    # And with the flag off it is the shared ring again, which keeps everything
    # until twenty thousand rows have been written.
    memory.configure({"metrics_name_ring": False})
    for index in range(6):
        mind.engine.db.metric("model_ms", float(index))
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='model_ms'").fetchone()[0] == 9


def test_a_rare_name_becomes_readable_again(system):
    """The shared window is why the store cannot report its own recall latency: one
    read a day against three telemetry rows per model call."""
    mind, memory, _, _ = system
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES('recall_ms',12.0,?,'{}')", (mind.clock(),))
        conn.executemany("INSERT INTO metrics(name,value,created_at,data) VALUES('model_ms',?,?,'{}')",
                         [(float(index), mind.clock()) for index in range(2100)])
    assert mind.engine.overview()["latency"]["samples"] == 0
    memory.configure({"metrics_name_ring": True})
    read = mind.engine.overview()
    assert read["latency"]["samples"] == 1 and read["latency"]["p95_ms"] == 12.0
    with mind.engine.db.connect() as conn:
        found = maintenance.metrics_plan(conn)
    assert found["enabled"] is True and found["rows"] == 2101
    crowded = {name["name"]: name["crowded_out"] for name in found["names"]}
    assert crowded == {"model_ms": False, "recall_ms": True}


def test_the_tick_applies_the_ring_to_every_name_at_once(system, monkeypatch):
    mind, memory, _, _ = system
    monkeypatch.setattr(maintenance, "METRIC_RING", 2)
    with mind.engine.db.connect(write=True) as conn:
        conn.executemany("INSERT INTO metrics(name,value,created_at,data) VALUES('model_ms',?,?,'{}')",
                         [(float(index), mind.clock()) for index in range(5)])
    planned = maintenance.tick(mind, config_for(mind))
    assert planned["state"] == "planned" and planned["metrics"]["would_remove"] == 3
    assert planned["metrics"]["enabled"] is False
    assert maintenance.tick(mind, config_for(mind), apply=True)["metrics"].get("removed") is None
    memory.configure({"metrics_name_ring": True})
    applied = maintenance.tick(mind, config_for(mind), apply=True)
    assert applied["metrics"]["removed"] == 3
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics WHERE name='model_ms'").fetchone()[0] == 2


def test_the_host_drives_both_and_writes_nothing_without_apply(system):
    mind, memory, _, _ = system
    memory.configure({"context_cache_sweep": True})
    write_cache(mind, "older", at=days_ago(mind, 31))
    write_cache(mind, "old", at=days_ago(mind, 30))
    reported = dispatch(config_for(mind), "maintenance-tick", {"apply": False})
    assert reported["state"] == "planned" and reported["context_cache"]["state"] == "planned"
    assert reported["context_cache"]["would_remove"] == 1 and len(cache_rows(mind)) == 2
    applied = dispatch(config_for(mind), "maintenance-tick", {"apply": True})
    assert applied["context_cache"]["state"] == "swept" and applied["context_cache"]["removed"] == 1
    assert cache_rows(mind) == ["old"]
    with mind.engine.db.connect() as conn:
        receipt = conn.execute("SELECT name,value,data FROM metrics WHERE name=?", (maintenance.SWEEP_METRIC,)).fetchone()
    assert receipt["value"] == 1 and json.loads(receipt["data"])["reason"] == "maintenance-tick"


def test_lance_optimize_needs_the_flag_the_quiet_and_the_apply(system, tmp_path):
    mind, memory, _, _ = system
    config = {**config_for(mind), **quiet_host(tmp_path)}
    assert maintenance.vector_optimize(mind, config, probe=probe({}))["state"] == "disabled"
    memory.configure({"vector_optimize": True})
    # A running bridge is enough to refuse: an old version can be the version a
    # reader is still holding.
    noisy = maintenance.vector_optimize(mind, config, probe=probe({4242: ("Fri Sep 18 10:37:19 2026", "node service.mjs")}))
    assert noisy["state"] == "refused" and noisy["reason"] == "store-not-quiet"
    assert maintenance.vector_optimize(mind, config, probe=probe({}))["state"] == "idle"
    with pytest.raises(ValueError):
        dispatch(config, "vector-optimize", {"older_than_days": -1})


def test_lance_optimize_removes_versions_and_no_rows(system, tmp_path):
    pytest.importorskip("lancedb")
    from eventmem.core.vectors import VectorIndex
    mind, memory, _, _ = system
    memory.configure({"vector_optimize": True})
    index_id = VectorIndex.register(mind.engine, "test-maintenance-model", 4, "test-v1")
    index = VectorIndex(mind.engine, index_id)
    for revision in range(4):
        index.upsert([{"id": "rec_one", "scope": mind.scope.key(), "revision": revision,
                       "vector": [1.0, 0.0, 0.0, float(revision)]}])
    config = {**config_for(mind), **quiet_host(tmp_path)}
    planned = maintenance.vector_optimize(mind, config, probe=probe({}), older_than_days=0)
    assert planned["state"] == "planned" and planned["indexes"][0]["versions_before"] > 1
    assert planned["bytes_after"] == planned["bytes_before"]
    applied = maintenance.vector_optimize(mind, config, probe=probe({}), older_than_days=0, apply=True)
    # Versions go, rows stay: that is the half of the condition the code can check.
    entry = applied["indexes"][0]
    assert applied["state"] == "optimized" and entry["versions_after"] < entry["versions_before"]
    assert entry["rows_after"] == entry["rows_before"] and applied["rows_unchanged"] is True
    assert index.search([1.0, 0.0, 0.0, 3.0], exact=True)[0]["id"] == "rec_one"
