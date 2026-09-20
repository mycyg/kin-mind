from __future__ import annotations

import json

import subprocess

import sys

import threading

import time

from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timedelta, timezone

import httpx

import pytest

from fastapi.testclient import TestClient

from eventmem.core import Engine, RecallRequest, SourceInput

from eventmem.core.api import create_app

from eventmem.core.db import Conflict, Deleted, Missing

from eventmem.core.jobs import Worker

from eventmem.core.models import (
    ContactPolicy,
    RecordInput,
    RevisionInput,
    ScheduleInput,
    Scope,
    now,
)

from eventmem.core.scheduler import Scheduler

@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "core")

def remember(engine, text="port range allocation", key="a", **kwargs):
    source = engine.receive(SourceInput(namespace="test", key=key, text=text, **kwargs))
    return engine.source(source["id"])["record_ids"][0]

def test_idempotence_durable_receipt_and_concurrent_revisions(engine):
    source = SourceInput(namespace="test", key="same", text="A durable source")
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(lambda _: Engine(engine.db.root).receive(source), range(100))
        )
    assert len({r["id"] for r in results}) == 1
    assert engine.overview()["sources"] == 1
    assert engine.overview()["records"] == 1
    rid = engine.source(results[0]["id"])["record_ids"][0]

    def revise(i):
        try:
            return engine.revise(
                rid,
                RevisionInput(
                    expected_revision=1,
                    command_id=str(i),
                    action="correct",
                    content=f"Revision {i}",
                ),
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(r is not None for r in pool.map(revise, range(10))) == 1
    assert engine.get(rid)["revision"] == 2
    with pytest.raises(Conflict):
        engine.receive(source.model_copy(update={"text": "changed without version"}))

def test_correction_invalidates_warm_cache_and_revision_reads(engine):
    rid = remember(engine)
    before = now()
    q = RecallRequest(query="allocation", budget=2000)
    assert rid in [r["id"] for r in engine.recall(q)["items"]]
    revision = RevisionInput(
        expected_revision=1,
        command_id="change",
        action="correct",
        content="Use a dedicated port allocator",
    )
    assert engine.revise(rid, revision)["revision"] == 2
    assert engine.revise(rid, revision)["revision"] == 2
    assert "dedicated" in engine.recall(RecallRequest(query="port"))["text"]
    assert engine.get(rid, known_at=before)["content"] == "port range allocation"
    engine.revise(
        rid, RevisionInput(expected_revision=2, command_id="remove", action="retract")
    )
    assert engine.recall(RecallRequest(query="port"))["items"] == []
    historical = engine.recall(
        RecallRequest(query="allocation", known_at=before, at=before)
    )
    assert rid in [r["id"] for r in historical["items"]]
    assert (
        "retracted" in engine.recall(RecallRequest(query="port", history=True))["text"]
    )

@pytest.mark.parametrize("field", ["project", "persona", "collection", "world"])
def test_scope_isolation(engine, field):
    a = remember(engine, "same overlapping text", "one")
    foreign = Scope(**{field: "other"})
    b = remember(engine, "same overlapping text", "two", scope=foreign)
    assert {
        r["id"] for r in engine.recall(RecallRequest(query="overlapping"))["items"]
    } == {a}
    assert {
        r["id"]
        for r in engine.recall(RecallRequest(query="overlapping", scope=foreign))[
            "items"
        ]
    } == {b}
    with pytest.raises(Conflict):
        engine.relate(a, "related", b)

def test_evidence_independence_and_generated_facts(engine):
    ids = [remember(engine, "One independent statement", str(i)) for i in range(3)]
    source_ids = [engine.get(rid)["source_ids"][0] for rid in ids]
    fact = engine.add_record(
        RecordInput(
            kind="fact",
            content="Inferred statement",
            source_ids=source_ids,
            generated=True,
        ),
        "fact",
    )
    assert fact["status"] == "unverified"
    assert engine.get(fact["id"])["independent_sources"] == 1
    diary = engine.add_record(
        RecordInput(
            kind="diary",
            content="Supported account",
            generated=True,
            source_ids=source_ids,
            evidence_ids=ids,
        ),
        "diary",
    )
    assert diary["generated"]
    engine.revise(
        ids[0],
        RevisionInput(
            expected_revision=1,
            command_id="correction",
            action="correct",
            content="Changed source statement",
        ),
    )
    assert engine.get(diary["id"])["status"] == "unverified"

def test_budget_dedup_and_compaction_across_hosts(engine):
    remember(engine, "small relevant text", "1", title="Relevant")
    engine.settings(
        "budgets", {"tool": {"startup": 100, "passive": 100, "cumulative": 100}}
    )
    q = RecallRequest(
        query="relevant", session="shared-session", phase="passive", budget=100
    )
    first = engine.recall(q)
    assert first["tokens"] <= 100 and first["items"]
    assert not Engine(engine.db.root).recall(q)["items"]
    assert engine.recall(q.model_copy(update={"phase": "read"}))["items"]
    assert engine.recall(q.model_copy(update={"phase": "compact"}))["items"]
    from eventmem.core.retrieval import tokens

    for budget in (0, 1, 10, 64, 256):
        result = engine.recall(RecallRequest(query="relevant", budget=budget))
        assert tokens(result["text"]) <= budget

def test_document_versions_and_locators(engine):
    worker = Worker(engine)
    source = SourceInput(
        namespace="docs",
        key="manual",
        media_type="text/markdown",
        authority="document",
        title="Manual",
    )
    old = engine.receive(source, b"# Installation\n\nUse version one.")
    while worker.run_once():
        pass
    new = engine.receive(
        source.model_copy(update={"version": "2"}),
        b"# Installation\n\nUse version two.",
    )
    while worker.run_once():
        pass
    rows = engine.list_records(Scope())["items"]
    assert all(
        r["status"] == "superseded" for r in rows if old["id"] in r["source_ids"]
    )
    new_rows = [r for r in rows if new["id"] in r["source_ids"]]
    assert len(new_rows) == 3
    assert any(r["locator"].get("char_start") == 16 for r in new_rows)
    assert "version two" in engine.recall(RecallRequest(query="version"))["text"]

def test_backup_restore_keeps_history_and_archive(engine, tmp_path):
    from eventmem.core.transfer import backup, restore

    rid = remember(engine, "Back up this memory")
    engine.revise(
        rid, RevisionInput(expected_revision=1, command_id="archive", action="archive")
    )
    archive = tmp_path / "backup.tar.gz"
    backup(engine, archive)
    target = tmp_path / "restored"
    restore(archive, target)
    other = Engine(target)
    assert other.get(rid)["status"] == "archived"
    assert len(other.history(rid)) == 2
    assert not other.recall(RecallRequest(query="Back"))["items"]
    assert other.recall(RecallRequest(query="Back", history=True))["items"]
