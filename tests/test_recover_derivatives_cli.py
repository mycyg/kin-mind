import hashlib
import importlib.util
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventmem.core import Engine
from eventmem.core.jobs import Worker
from eventmem.core.models import Scope, SourceInput
from eventmem.core.vectors import VectorIndex
from kin_mind import derivative_recovery as dr

# Console-entry pytest need not put the checkout root on sys.path. Load the
# standalone maintenance script by its path, as an installed operator would.
_spec = importlib.util.spec_from_file_location(
    "recover_derivatives_cli", Path(__file__).resolve().parents[1] / "scripts" / "recover_derivatives.py"
)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def test_snapshot_db_includes_committed_wal_pages(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    source = sqlite3.connect(root / "memory.sqlite3")
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("PRAGMA wal_autocheckpoint=0")
    source.execute("CREATE TABLE witness(value TEXT)")
    source.commit()
    source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source.execute("INSERT INTO witness VALUES('committed-only-in-wal')")
    source.commit()
    assert (root / "memory.sqlite3-wal").stat().st_size > 0

    workdir = tmp_path / "snapshot"
    workdir.mkdir()
    copied, evidence = cli.snapshot_db(root, workdir)
    with sqlite3.connect(copied) as snapshot:
        assert snapshot.execute("SELECT value FROM witness").fetchone()[0] == "committed-only-in-wal"
    source.close()

    assert evidence["method"] == "sqlite-readonly-backup-api"
    assert evidence["wal_present_before"] is True
    assert evidence["integrity_check"] == "ok"


def test_probe_vectors_uses_exact_contract_and_target_filter(tmp_path):
    pytest.importorskip("lancedb")
    root = tmp_path / "memory"
    engine = Engine(root)
    engine.settings("models", {
        "embedding": {
            "endpoint": "http://127.0.0.1:8321/v1",
            "model": "synthetic-exact-model",
            "dimensions": 4,
            "preprocessing": "synthetic-v1",
            "local_embedding": True,
        }
    })
    index_id = VectorIndex.register(engine, "synthetic-exact-model", 4, "synthetic-v1")
    VectorIndex(engine, index_id).upsert([
        {"id": "wanted", "scope": "scope", "revision": 7, "vector": [1.0, 0.0, 0.0, 0.0]},
        {"id": "not-requested", "scope": "scope", "revision": 9, "vector": [0.0, 1.0, 0.0, 0.0]},
    ])

    # A lexically first distractor proves the probe does not use list_tables()[0].
    import lancedb
    lancedb.connect(str(root / "vectors")).create_table("aaa_distractor", data=[{
        "id": "wrong", "scope": "scope", "revision": 99, "vector": [0.0, 0.0, 1.0, 0.0]
    }])

    with engine.db.connect() as conn:
        contract = dr.embedding_vector_contract(conn)
    assert contract["resolved"] is True and contract["index_id"] == index_id

    evidence = cli.probe_vectors(root, ["wanted", "missing"], contract)
    assert evidence["probed"] is True
    assert evidence["table"] == index_id
    assert evidence["dimensions"] == 4
    assert evidence["rows"] == 2
    assert evidence["revisions"] == {"wanted": 7}
    assert evidence["query"] == "targeted-id-filter"
    assert evidence["targets"] == [
        {"record_id": "wanted", "vector_revision": 7},
        {"record_id": "missing", "vector_revision": None},
    ]

    missing_contract = contract | {"index_id": "vec_missing"}
    unavailable = cli.probe_vectors(root, ["wanted"], missing_contract)
    assert unavailable["probed"] is False
    assert unavailable["reason"] == "configured vector table missing"


def test_dry_run_emits_reviewable_v2_manifest(tmp_path):
    pytest.importorskip("lancedb")
    root = tmp_path / "memory"
    engine = Engine(root)
    model_config = {
        "endpoint": "http://127.0.0.1:8321/v1",
        "model": "synthetic-manifest-model",
        "dimensions": 4,
        "preprocessing": "synthetic-manifest-v1",
        "local_embedding": True,
    }
    engine.settings("models", {"embedding": model_config})
    index_id = VectorIndex.register(
        engine, model_config["model"], model_config["dimensions"], model_config["preprocessing"]
    )
    VectorIndex(engine, index_id).table()
    source = engine.receive(SourceInput(
        namespace="synthetic", key="manifest", text="A reviewed vector gap",
        scope=Scope(persona="synthetic-manifest"), extract=False,
    ))
    record_id = engine.source(source["id"])["record_ids"][0]
    with engine.db.connect(write=True) as conn:
        job = conn.execute(
            "SELECT id FROM jobs WHERE kind='embed' AND json_extract(payload,'$.record_id')=?",
            (record_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET state='failed',attempts=5,error='FileNotFoundError' WHERE id=?",
            (job,),
        )

    output = tmp_path / "manifest.json"
    manifest = cli.dry_run(root, output)
    validated = dr.validate_reviewed_manifest(manifest)
    assert validated["embed_targets"] == 1
    assert manifest["schema_version"] == 2
    assert manifest["store"]["snapshot"]["method"] == "sqlite-readonly-backup-api"
    assert manifest["vector_store"]["table"] == index_id
    assert manifest["vector_store"]["targets"] == [
        {"record_id": record_id, "vector_revision": None}
    ]
    assert manifest["embeds"][0]["action"] == "recover"
    assert output.is_file()


def test_persisted_receipt_replays_without_dependency_probe(tmp_path):
    root = tmp_path / "memory"
    engine = Engine(root)
    selection = {"review_fingerprint": "reviewed", "embeds": [], "digests": []}
    original = dr.Runner(engine, embed_batch=0, digest_batch=0).run(
        command_id="receipt-only", selection=selection, manifest_sha256="b" * 64,
    )
    assert original["stopped"] is None

    replay = cli.persisted_receipt(
        root, command_id="receipt-only", manifest_sha256="b" * 64,
        embed_limit=0, digest_limit=0,
    )
    assert replay["idempotent_replay"] is True
    assert replay["selection_digest"] == original["selection_digest"]
    with pytest.raises(ValueError, match="different reviewed authority"):
        cli.persisted_receipt(
            root, command_id="receipt-only", manifest_sha256="c" * 64,
            embed_limit=0, digest_limit=0,
        )


def test_apply_resumes_expired_bound_selection_after_job_was_requeued(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    root = tmp_path / "memory"
    engine = Engine(root)
    model_config = {
        "endpoint": "http://127.0.0.1:8321/v1",
        "model": "synthetic-resume-model",
        "dimensions": 4,
        "preprocessing": "synthetic-resume-v1",
        "local_embedding": True,
    }
    engine.settings("models", {"embedding": model_config})
    index_id = VectorIndex.register(
        engine, model_config["model"], model_config["dimensions"],
        model_config["preprocessing"],
    )
    VectorIndex(engine, index_id).table()
    source = engine.receive(SourceInput(
        namespace="synthetic", key="resume", text="Resume an immutable command selection",
        scope=Scope(persona="synthetic-resume"), extract=False,
    ))
    record_id = engine.source(source["id"])["record_ids"][0]
    with engine.db.connect(write=True) as conn:
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE kind='embed' AND json_extract(payload,'$.record_id')=?",
            (record_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET state='failed',attempts=5,error='FileNotFoundError' WHERE id=?",
            (job_id,),
        )

    monkeypatch.setattr(cli, "probe_embedding", lambda _: {"alive": True, "loaded": True})
    manifest_path = tmp_path / "manifest.json"
    manifest = cli.dry_run(root, manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with engine.db.connect() as conn:
        current = dr.plan(conn, {})
        current["reviewed_target_states"] = dr.reviewed_target_states(conn, manifest, {})
    current["digests"] = dr.attach_digest_review_evidence(engine, current["digests"])
    selection, review = dr.select_reviewed_targets(
        manifest, current, embed_limit=1, digest_limit=0,
    )
    selection["selection_review"] = review
    claimed = dr.bind_recovery_selection(
        engine, command_id="resume-cli", manifest_sha256=manifest_sha256,
        selection=selection, embed_limit=1, digest_limit=0,
        owner="crashed-cli", lease_seconds=15,
    )
    assert claimed["claimed"] is True
    assert Worker(engine).recover([job_id], command_id="resume-cli")["recovered"] == [job_id]
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE mind_derivative_recovery_runs SET lease_until=? WHERE command_id='resume-cli'",
            (time.time() - 1,),
        )

    captured = {}

    class CapturingRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **kwargs):
            captured.update(kwargs)
            return {
                "kind": "w2-derivative-recovery-apply",
                "command_id": kwargs["command_id"],
                "selection_digest": "captured",
                "inspection_counts": {"embed_selected": 1, "digest_selected": 0},
                "stopped": None,
                "idempotent_replay": True,
            }

    monkeypatch.setattr(cli, "Runner", CapturingRunner)
    output = tmp_path / "apply.json"
    result = cli.apply(root, SimpleNamespace(
        command_id="resume-cli", manifest=manifest_path, manifest_sha256=None,
        embed_batch=1, digest_batch=0, output=output,
    ))
    assert result["stopped"] is None
    assert [entry["job_id"] for entry in captured["selection"]["embeds"]] == [job_id]
    assert captured["manifest_sha256"] == manifest_sha256
    assert output.is_file()
