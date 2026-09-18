from __future__ import annotations
import time
import pytest
from eventmem.core import Engine, SourceInput, RecallRequest
from eventmem.core.models import Scope, ContactPolicy, ScheduleInput
from eventmem.core.jobs import Worker
from eventmem.core.providers import Providers
from eventmem.core.scheduler import Scheduler
from eventmem.core.api import boundary, SessionBoundary, create_app
from fastapi.testclient import TestClient


def drain(e, limit=2000):
    w = Worker(e)
    for _ in range(limit):
        if not w.run_once():
            return
    raise AssertionError("Queue did not settle")


def test_long_extraction_covers_every_paragraph_and_is_resumable(tmp_path, monkeypatch):
    e = Engine(tmp_path)
    seen = []

    def generate(self, role, instruction, payload, **kwargs):
        if role == "extraction":
            seen.append(payload["text"])
        return {"candidates": []}

    monkeypatch.setattr(Providers, "json", generate)
    body = "\n\n".join(
        f"paragraph_{i:04d} " + ("long content " * 35) for i in range(650)
    )
    source = e.receive(
        SourceInput(
            namespace="long",
            key="doc",
            title="Large manual",
            media_type="text/markdown",
            extract=True,
        ),
        body.encode(),
    )
    drain(e)
    assert e.source(source["id"])["model"] == "complete"
    assert len(seen) > 1 and max(map(len, seen)) <= 48000
    assert all(f"paragraph_{i:04d}" in "\n".join(seen) for i in range(650))
    drain(Engine(tmp_path))
    assert e.source(source["id"])["model"] == "complete"
    ids = []
    cursor = ""
    while True:
        page = e.source(source["id"], cursor=cursor, limit=50)
        ids.extend(page["record_ids"])
        cursor = page["cursor"]
        if not cursor:
            break
    assert len(ids) == 651


def test_document_versions_ordered_by_effective_time_and_checkpoint_by_session(
    tmp_path,
):
    e = Engine(tmp_path)
    old = e.receive(
        SourceInput(
            namespace="manual",
            key="same",
            kind="knowledge",
            text="old manual",
            occurred_at="2020-01-01T00:00:00Z",
        )
    )
    new = e.receive(
        SourceInput(
            namespace="manual",
            key="same",
            version="2",
            kind="knowledge",
            text="new manual",
            occurred_at="2021-01-01T00:00:00Z",
        )
    )
    late = e.receive(
        SourceInput(
            namespace="manual",
            key="same",
            version="late",
            kind="knowledge",
            text="late old manual",
            occurred_at="2019-01-01T00:00:00Z",
        )
    )
    assert e.get(e.source(new["id"])["record_ids"][0])["status"] == "active"
    for source in (old, late):
        assert e.get(e.source(source["id"])["record_ids"][0])["status"] == "superseded"
    for i in range(3):
        boundary(
            e,
            SessionBoundary(
                event="checkpoint",
                session="same",
                command_id=str(i),
                checkpoint={"next_entry": str(i)},
            ),
        )
    boundary(
        e,
        SessionBoundary(
            event="checkpoint",
            session="other",
            command_id="other",
            checkpoint={"next_entry": "other"},
        ),
    )
    assert (
        len(
            [
                r
                for r in e.list_records(Scope(), kind="checkpoint")["items"]
                if r["status"] == "active"
            ]
        )
        == 2
    )


def test_outbox_crash_claim_uncertainty_and_pause_resume(tmp_path, monkeypatch):
    e = Engine(tmp_path)
    s = Scheduler(e)
    src = e.receive(
        SourceInput(
            namespace="test", key="remind", text="Pending reminder", kind="reminder"
        )
    )
    rid = e.source(src["id"])["record_ids"][0]
    s.policy(
        ContactPolicy(
            enabled=True,
            channel="http://localhost:9933",
            quiet_start=0,
            quiet_end=0,
            require_confirmation=False,
        )
    )
    s.schedule(
        ScheduleInput(
            command_id="contact", record_id=rid, due_at="2020-01-01T00:00:00Z"
        )
    )
    first = s.tick(deliver=False)["created"][0]
    with e.db.connect(write=True) as c:
        c.execute(
            "UPDATE outbox SET state='sending',lease_until=? WHERE id=?",
            (time.time() - 1, first),
        )
    s.tick(deliver=False)
    with e.db.connect() as c:
        assert (
            c.execute("SELECT state FROM outbox WHERE id=?", (first,)).fetchone()[0]
            == "uncertain"
        )
    second = s.schedule(
        ScheduleInput(command_id="second", record_id=rid, due_at="2020-01-01T00:00:00Z")
    )
    prior = s.tick(deliver=False)["created"][0]
    s.control(second["id"], "pause", 2)
    s.control(second["id"], "resume", 3)
    resumed = s.tick(deliver=False)["created"][0]
    assert resumed != prior


def test_native_pdf_pages_and_backup_ui_endpoints(tmp_path):
    pytest.importorskip("docling")
    from reportlab.pdfgen.canvas import Canvas
    import io

    data = io.BytesIO()
    pdf = Canvas(data)
    pdf.drawString(72, 720, "A located PDF statement")
    pdf.showPage()
    pdf.save()
    e = Engine(tmp_path / "core")
    src = e.receive(
        SourceInput(
            namespace="pdf",
            key="pdf",
            title="document.pdf",
            media_type="application/pdf",
        ),
        data.getvalue(),
    )
    drain(e)
    assert e.source(src["id"])["mechanical"] == "complete"
    records = [e.get(i) for i in e.source(src["id"])["record_ids"]]
    assert any(
        "located PDF" in r["content"] and r["locator"].get("page") == 1 for r in records
    )
    with TestClient(create_app(engine=e, token="local-test", workers=False)) as client:
        client.headers["Authorization"] = "Bearer local-test"
        rendered = client.get(f"/v1/sources/{src['id']}/page?page=1")
        assert (
            rendered.status_code == 200
            and rendered.headers["content-type"] == "image/png"
        )
        schema = client.get("/v1/openapi.json").json()
        routes = {
            v["operationId"]: (p, m)
            for p, methods in schema["paths"].items()
            for m, v in methods.items()
            if "operationId" in v
        }
        path, _ = routes["create_download"]
        res = client.post(path.replace("{kind}", "backup"))
        assert res.status_code == 200
        path, _ = routes["download_export"]
        download = client.get(path.replace("{name}", res.json()["id"]))
        assert download.status_code == 200
        path, _ = routes["restore_backup"]
        restored = client.post(
            path, files={"file": ("backup.tar.gz", download.content)}
        )
        assert restored.status_code == 200


def test_image_and_video_progress_survives_missing_models(tmp_path, monkeypatch):
    pytest.importorskip("docling")
    from PIL import Image
    from eventmem.core.media import run_ffmpeg
    import io

    e = Engine(tmp_path / "core")
    raw = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(raw, format="PNG")
    src = e.receive(
        SourceInput(
            namespace="media", key="image", title="image.png", media_type="image/png"
        ),
        raw.getvalue(),
    )
    drain(e)
    assert (
        e.source(src["id"])["mechanical"] == "complete"
        and e.source(src["id"])["model"] == "pending"
    )
    monkeypatch.setattr(
        Providers,
        "json",
        lambda *a, **k: {"description": "Red test image", "ocr": "Synthetic label"},
    )
    with e.db.connect() as c:
        jobs = [
            r[0] for r in c.execute("SELECT id FROM jobs WHERE kind='analyze_media'")
        ]
    for jid in jobs:
        Worker(e).control(jid, "retry")
    drain(e)
    assert e.source(src["id"])["model"] == "complete"
    image = [
        e.get(r)
        for r in e.source(src["id"])["record_ids"]
        if e.get(r)["locator"].get("type") == "image"
    ][0]
    assert (
        image["generated"]
        and image["confirmation"] == "inferred"
        and not image["attributes"]["analysis_pending"]
    )
    movie = tmp_path / "silent.mp4"
    run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-c:v", "libx264", str(movie)]
    )
    video = e.receive(
        SourceInput(
            namespace="media", key="movie", title="movie.mp4", media_type="video/mp4"
        ),
        movie.read_bytes(),
    )
    drain(e)
    assert (
        e.source(video["id"])["mechanical"] == "complete"
        and e.source(video["id"])["model"] == "complete"
    )
    assert any(
        e.get(r)["locator"].get("type") == "keyframe"
        for r in e.source(video["id"])["record_ids"]
    )


def test_explicit_reads_share_context_ledger_and_budget(tmp_path):
    from eventmem.core.reading import read_segment

    e = Engine(tmp_path)
    src = e.receive(
        SourceInput(
            namespace="reading", key="one", text="Shared context " + ("正文。" * 10000)
        )
    )
    rid = e.source(src["id"])["record_ids"][0]
    first = read_segment(e, rid, budget=90, session="host-session")
    assert first["tokens"] <= 90 and first["cursor"]
    assert not e.recall(
        RecallRequest(query="Shared", session="host-session", phase="passive")
    )["items"]
    second = read_segment(e, rid, offset=int(first["cursor"]), budget=90)
    assert (
        first["content"] + second["content"]
        == e.get(rid)["content"][: int(second["cursor"])]
    )


def test_stale_vector_revision_is_filtered(tmp_path):
    pytest.importorskip("lancedb")
    from eventmem.core.vectors import VectorIndex
    from eventmem.core.models import RevisionInput

    e = Engine(tmp_path)
    source = e.receive(
        SourceInput(namespace="vector-test", key="1", text="A valid old statement")
    )
    rid = e.source(source["id"])["record_ids"][0]
    index_id = VectorIndex.register(e, "test-visual-model", 4, "test-v1")
    index = VectorIndex(e, index_id)
    index.upsert(
        [
            {
                "id": rid,
                "scope": Scope().key(),
                "revision": 1,
                "vector": [1.0, 0.0, 0.0, 0.0],
            }
        ]
    )
    e.revise(
        rid,
        RevisionInput(
            expected_revision=1,
            command_id="correct",
            action="correct",
            content="A corrected statement",
        ),
    )
    q = RecallRequest(
        query="unmatchedterm", vector=[1.0, 0.0, 0.0, 0.0], index=index_id, explain=True
    )
    result = e.recall(q)
    assert not result["items"]
    assert any(
        r["reason"] == "stale_vector_revision" for r in result["trace"]["filtered"]
    )


def test_purge_after_an_erase_needs_nothing_the_vector_extra_omits(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import sys

    from eventmem.core.vectors import VectorIndex

    e = Engine(tmp_path)
    source = e.receive(
        SourceInput(namespace="vector-purge", key="1", text="A sentence to be erased")
    )
    rid = e.source(source["id"])["record_ids"][0]
    index_id = VectorIndex.register(e, "test-purge-model", 4, "test-v1")
    index = VectorIndex(e, index_id)
    index.upsert(
        [{"id": rid, "scope": Scope().key(), "revision": 1, "vector": [1.0, 0.0, 0.0, 0.0]}]
    )
    e.delete(rid)
    with e.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM tombstones WHERE key=?", (rid,)).fetchone()
    # The `vector` extra installs lancedb, numpy and pyarrow — never pylance. A
    # machine that happens to have pylance would hide a purge that reaches for it,
    # so the absence is built here rather than read off whatever runs this: a None
    # in `sys.modules` makes `import lance` fail the way a missing install does.
    monkeypatch.setitem(sys.modules, "lance", None)
    # Nothing to return and nothing to raise: the purge job calls this for effect.
    assert index.purge() is None
    assert index.table().count_rows() == 0


def test_process_death_after_remote_effect_leaves_uncertain_delivery(tmp_path):
    import subprocess, sys

    e = Engine(tmp_path / "core")
    s = Scheduler(e)
    source = e.receive(
        SourceInput(namespace="crash-test", key="1", text="A reminder", kind="reminder")
    )
    rid = e.source(source["id"])["record_ids"][0]
    s.policy(
        ContactPolicy(
            enabled=True,
            channel="http://localhost:9921",
            quiet_start=0,
            quiet_end=0,
            require_confirmation=False,
        )
    )
    s.schedule(
        ScheduleInput(command_id="crash", record_id=rid, due_at="2020-01-01T00:00:00Z")
    )
    code = """import os,sys,httpx
from pathlib import Path
from eventmem.core import Engine
from eventmem.core.scheduler import Scheduler
def delivered(*a,**k):
 Path(sys.argv[2]).write_text('remote effect accepted')
 os._exit(66)
httpx.Client.post=delivered
Scheduler(Engine(sys.argv[1])).tick()
"""
    marker = tmp_path / "effect"
    process = subprocess.run([sys.executable, "-c", code, str(e.db.root), str(marker)])
    assert process.returncode == 66 and marker.exists()
    with e.db.connect(write=True) as c:
        assert c.execute("SELECT state FROM outbox").fetchone()[0] == "sending"
        c.execute("UPDATE outbox SET lease_until=0")
    s.tick(deliver=False)
    with e.db.connect() as c:
        assert c.execute("SELECT state FROM outbox").fetchone()[0] == "uncertain"


def test_autonomous_greeting_requires_explicit_enabled_policy(tmp_path, monkeypatch):
    monkeypatch.setattr("eventmem.core.scheduler.time.monotonic", lambda: 0.0)
    e = Engine(tmp_path)
    s = Scheduler(e)
    s.policy(
        ContactPolicy(triggers=["greeting"], greeting_text="A configured check-in")
    )
    s.tick(deliver=False)
    assert e.overview()["records"] == 0
    s.policy(
        ContactPolicy(
            enabled=True,
            triggers=["greeting"],
            greeting_text="A configured check-in",
            quiet_start=0,
            quiet_end=0,
        )
    )
    s = Scheduler(e)
    s.tick(deliver=False)
    with e.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
        assert c.execute("SELECT state FROM outbox").fetchone()[0] == "suggested"
    Scheduler(e).tick(deliver=False)
    with e.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1


def test_uncertain_acknowledgment_advances_only_its_occurrence(tmp_path):
    e = Engine(tmp_path)
    s = Scheduler(e)
    source = e.receive(
        SourceInput(namespace="ack", key="1", text="Daily reminder", kind="reminder")
    )
    rid = e.source(source["id"])["record_ids"][0]
    s.policy(ContactPolicy(quiet_start=0, quiet_end=0))
    schedule = s.schedule(
        ScheduleInput(
            command_id="daily",
            record_id=rid,
            due_at="2020-01-01T00:00:00Z",
            recurrence="daily",
        )
    )
    delivery = s.tick(deliver=False)["created"][0]
    with e.db.connect(write=True) as c:
        c.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (delivery,))
    s.acknowledge(delivery)
    with e.db.connect() as c:
        row = dict(
            c.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule["id"],)
            ).fetchone()
        )
        assert (
            row["state"] == "scheduled"
            and row["due_at"]
            > __import__("eventmem.core.models", fromlist=["now"]).now()
        )
    s.acknowledge(delivery)
    with e.db.connect() as c:
        assert (
            c.execute("SELECT revision FROM schedules").fetchone()[0] == row["revision"]
        )
    one = s.schedule(
        ScheduleInput(command_id="cancel", record_id=rid, due_at="2020-01-01T00:00:00Z")
    )
    delivery = s.tick(deliver=False)["created"][0]
    with e.db.connect(write=True) as c:
        c.execute("UPDATE outbox SET state='uncertain' WHERE id=?", (delivery,))
    s.control(one["id"], "cancel", 2)
    s.acknowledge(delivery)
    with e.db.connect() as c:
        assert (
            c.execute(
                "SELECT state FROM schedules WHERE id=?", (one["id"],)
            ).fetchone()[0]
            == "canceled"
        )


def test_community_commit_rechecks_corrected_inputs(tmp_path):
    pytest.importorskip("igraph")
    from eventmem.core.organize import prepare_communities
    from eventmem.core.models import RevisionInput
    from eventmem.core.db import Conflict

    e = Engine(tmp_path)
    sources = [
        e.receive(
            SourceInput(
                namespace="cluster",
                key=str(i),
                text=f"Cluster input {i}",
                metadata={"topic": "test"},
            )
        )
        for i in range(2)
    ]
    rid = e.source(sources[0]["id"])["record_ids"][0]
    prepared = prepare_communities(e, Scope())
    e.revise(
        rid,
        RevisionInput(
            command_id="change",
            expected_revision=1,
            action="correct",
            content="Corrected input",
        ),
    )
    with pytest.raises(Conflict), e.db.connect(write=True) as c:
        prepared(c)
    with e.db.connect() as c:
        assert not c.execute("SELECT 1 FROM families").fetchone()


def test_replay_resume_checks_dataset_and_model_identity(tmp_path, monkeypatch):
    from eventmem.core.evaluation import evaluate, fixture_cases
    import json

    calls = []

    def answer(self, role, *args, **kwargs):
        calls.append(role)
        return {
            "answer": "fixture answer",
            "task_success": True,
            "unsupported_answer": False,
            "stale_misuse": False,
        }

    monkeypatch.setattr(Providers, "json", answer)
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps(fixture_cases()[:1]))
    e = Engine(tmp_path / "models")
    output = tmp_path / "replay.json"
    evaluate(output, dataset, answer_engine=e)
    count = len(calls)
    evaluate(output, dataset, answer_engine=e)
    assert len(calls) == count == 12
    e.settings("models", {"answer": {"model": "changed"}})
    with pytest.raises(ValueError, match="different dataset or model"):
        evaluate(output, dataset, answer_engine=e)


def test_narrative_candidate_limit_excludes_generated_records_before_selection(
    tmp_path, monkeypatch
):
    e = Engine(tmp_path)
    source = e.receive(
        SourceInput(
            namespace="narrative",
            key="original",
            text="The user explicitly enjoyed the garden walk.",
        )
    )
    rid = e.source(source["id"])["record_ids"][0]
    for i in range(101):
        e.receive(
            SourceInput(
                namespace="narrative",
                key=str(i),
                text="A generated description",
                authority="model",
            )
        )

    def summarize(self, role, instruction, payload, **kwargs):
        assert [r["id"] for r in payload["records"]] == [rid]
        return {"content": "The user enjoyed the garden walk.", "evidence_ids": [rid]}

    monkeypatch.setattr(Providers, "json", summarize)
    e.enqueue("diary", {"scope": Scope().model_dump()}, "diary-candidates")
    drain(e)
    with e.db.connect() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM records WHERE kind='diary'").fetchone()[0]
            == 1
        )


def test_record_lists_bound_content_and_metadata_without_altering_source(tmp_path):
    import json

    e = Engine(tmp_path)
    text = "A long record. " * 10000
    source = e.receive(
        SourceInput(
            namespace="list", key="large", text=text, metadata={"large": "x" * 100000}
        )
    )
    result = e.list_records(Scope())
    assert len(json.dumps(result)) < 6000
    item = result["items"][0]
    assert item["preview"] and item["content_length"] == len(text)
    assert e.get(e.source(source["id"])["record_ids"][0])["content"] == text
