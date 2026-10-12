"""Stable receipt ids and database revisions decide persistence, without content hashes."""
import json
import uuid

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Deleted, dumps
from eventmem.core.engine import root_id
from eventmem.core.models import RevisionInput, Scope, SourceInput
from eventmem.core import idempotency


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "store")


def source(key, text="A source", **change):
    return SourceInput(namespace="receipt-test", key=key, text=text, **change)


def test_a_command_id_replays_its_first_receipt_without_payload_or_fingerprint_gate(engine):
    with engine.db.connect(write=True) as conn:
        first = engine.command(conn, "stable-command", {"content": "first"}, lambda: {"id": "effect", "revision": 1})
        # A legacy digest also has no authority over an already committed receipt.
        conn.execute("UPDATE commands SET digest='legacy-content-digest' WHERE id='stable-command'")
        retried = engine.command(conn, "stable-command", {"content": "new body", "expected_revision": 44},
                                 lambda: pytest.fail("A retry must not apply another effect"))
        assert first == retried
        assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_command_fingerprints'").fetchone()
    assert idempotency.stamp("mind-state", "scope", {"content": "words"}) is None
    assert idempotency.unchanged(None, None, "stable-command", legacy=("old", "new"))


def test_a_source_identity_reuses_its_database_blob_reference(engine):
    first = engine.receive(source("stable-source", "first body"))
    with engine.db.connect() as conn:
        row = conn.execute("SELECT blob,hash FROM sources WHERE id=?", (first["id"],)).fetchone()
    blob = row["blob"]
    assert blob.startswith("blob_") and uuid.UUID(hex=blob[5:]).version == 4
    assert row["hash"] == "" and "hash" not in first
    second = engine.receive(source("stable-source", "a different retry body"))
    assert second == first
    assert engine.source(first["id"], content=True).read_text() == "first body"
    assert {path.name for path in engine.db.blobs.iterdir()} == {blob}


def test_identical_content_is_stored_under_each_source_reference(engine):
    first = engine.receive(source("one"))
    second = engine.receive(source("two"))
    with engine.db.connect() as conn:
        blobs = {row[0] for row in conn.execute("SELECT blob FROM sources")}
    assert first["id"] != second["id"] and len(blobs) == 2


def test_legacy_blob_key_is_read_by_database_reference_without_a_content_gate(engine):
    found = engine.receive(source("legacy"))
    path = engine.source(found["id"], content=True)
    legacy_key = "a" * 64
    legacy_path = engine.db.blobs / legacy_key
    path.rename(legacy_path)
    legacy_path.write_text("Legacy file contents are read as stored")
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET blob=?,hash=? WHERE id=?", (legacy_key, "historical-digest", found["id"]))
    assert engine.source(found["id"], content=True).read_text() == "Legacy file contents are read as stored"
    assert "hash" not in engine.source(found["id"])
    assert engine.receive(source("legacy", "a retry"))["id"] == found["id"]


def test_derived_source_keeps_ids_and_revisions_but_no_content_hash(engine):
    original = engine.receive(source("input"))
    rid = root_id(original["id"])
    ref = {"source_id": original["id"], "record_id": rid, "revision": 1, "hash": "does-not-match"}
    result = engine.receive(source("derived", "A derived account", authority="model"), derived_from=[ref])
    assert result["derived_from"] == [{"source_id": original["id"], "record_id": rid, "revision": 1}]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM dependencies WHERE record_id=? AND evidence_id=?",
                            (root_id(result["id"]), rid)).fetchone()
    engine.revise(rid, RevisionInput(expected_revision=1, command_id="correct-input", action="correct", content="corrected"))
    with pytest.raises(Conflict) as stale:
        engine.receive(source("stale-derived", "Old read", authority="model"), derived_from=[ref])
    assert stale.value.code == "derived-from-changed"
    engine.delete(original["id"])
    with pytest.raises(Deleted):
        engine.receive(source("deleted-derived", "Deleted input", authority="model"), derived_from=[ref])


def test_a_derived_source_still_cannot_cite_another_scope(engine):
    original = engine.receive(source("private-input", scope=Scope(persona="another")))
    with pytest.raises(Conflict) as foreign:
        engine.receive(source("foreign-derived", "Wrong scope", authority="model"), derived_from=[original["id"]])
    assert foreign.value.code == "derived-from-scope"


def test_deleted_source_retry_writes_no_replacement_blob(engine):
    original = engine.receive(source("deleted"))
    engine.delete(original["id"])
    with pytest.raises(Deleted):
        engine.receive(source("deleted", "Retry after deletion"))
    assert not list(engine.db.blobs.iterdir())


def test_url_snapshots_use_capture_ids_instead_of_web_body_digests(engine, monkeypatch):
    from eventmem.core.maintenance import snapshot_url
    import httpx
    import socket
    class Response:
        is_redirect = False
        headers = {"content-type": "text/plain"}
        def raise_for_status(self):
            pass
        def iter_bytes(self):
            yield b"The same fetched document"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def stream(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(httpx, "Client", Client)
    first = snapshot_url(engine, "https://source.invalid/document", Scope())
    second = snapshot_url(engine, "https://source.invalid/document", Scope())
    assert first["version"].startswith("capture_") and uuid.UUID(hex=first["version"][8:]).version == 4
    assert first["version"] != second["version"] and first["id"] != second["id"]
    assert first["key"] == second["key"] == "https://source.invalid/document"
    assert "hash" not in first
