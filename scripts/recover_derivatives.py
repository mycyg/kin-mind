"""Review or recover missing derived state (vectors, event digests) after queue failures.

Default is a dry run: the store is read through a consistent snapshot copy (the live
file is never opened for writing), the Lance table and the embedding endpoint are
probed without writes or model calls, and a manifest JSON is written for review.
`--apply` requeues through the bounded recovery entries (`Worker.recover`,
`mark_dirty` + the ordinary event_digest job) in small batches. Nothing here
rewrites records, revisions or graph nodes; corrected/deleted/superseded targets
are skipped, never resurrected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import Scope
from eventmem.core.retrieval import tokens
from kin_mind import derivative_recovery as dr
from kin_mind.derivative_recovery import Runner
from kin_mind.lifecycle import EventLifecycle
from kin_mind.state import Mind


def snapshot_db(root: Path, workdir: Path, attempts=40):
    """Take a consistent online snapshot with SQLite's read-only backup API.

    A file copy can omit committed pages that still live in ``-wal``.  SQLite's
    backup API reads one coherent database snapshot and includes those commits;
    the source connection is URI ``mode=ro`` and never initializes the live DB.
    ``attempts`` remains accepted for callers of the original helper and now
    controls the busy timeout rather than searching for a WAL-free instant.
    """
    src = root / "memory.sqlite3"
    target = workdir / "memory.sqlite3"
    if not src.is_file():
        raise FileNotFoundError(src)
    if target.exists():
        raise FileExistsError(target)
    before = src.stat()
    wal = root / "memory.sqlite3-wal"
    uri = "file:" + quote(str(src.resolve()), safe="/") + "?mode=ro"
    timeout = max(1.0, attempts * 0.5)
    source = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    destination = sqlite3.connect(target, timeout=timeout, isolation_level=None)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        metadata = {
            "method": "sqlite-readonly-backup-api",
            "source_mtime_ns_before": before.st_mtime_ns,
            "source_size_before": before.st_size,
            "wal_present_before": wal.exists(),
            "source_data_version_before": source.execute("PRAGMA data_version").fetchone()[0],
            "source_schema_version": source.execute("PRAGMA schema_version").fetchone()[0],
            "source_journal_mode": source.execute("PRAGMA journal_mode").fetchone()[0],
        }
        source.backup(destination, pages=4096, sleep=0.05)
        check = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Snapshot integrity check failed: {check}")
        after = src.stat()
        metadata.update(
            source_mtime_ns_after=after.st_mtime_ns,
            source_size_after=after.st_size,
            wal_present_after=wal.exists(),
            source_data_version_after=source.execute("PRAGMA data_version").fetchone()[0],
            snapshot_schema_version=destination.execute("PRAGMA schema_version").fetchone()[0],
            snapshot_page_count=destination.execute("PRAGMA page_count").fetchone()[0],
            integrity_check=check,
        )
    finally:
        destination.close()
        source.close()
    return target, metadata


def probe_vectors(root: Path, wanted_ids, contract=None):
    """Read the exact configured Lance table, filtering only the requested ids."""
    if not contract or not contract.get("resolved"):
        return {"probed": False, "reason": "exact vector contract unavailable",
                "contract": contract or {}}
    try:
        import lancedb
    except ImportError:
        return {"probed": False, "reason": "lancedb not importable in this environment"}
    vectors = root / "vectors"
    if not vectors.exists():
        return {"probed": False, "reason": "no vectors directory"}
    db = lancedb.connect(str(vectors))
    listing = db.list_tables()
    names = list(getattr(listing, "tables", listing))  # lancedb>=0.39 wraps the list
    index_id = contract["index_id"]
    if index_id not in names:
        return {"probed": False, "reason": "configured vector table missing",
                "contract": contract, "available_tables": len(names)}
    wanted_ids = list(dict.fromkeys(wanted_ids))
    for _ in range(3):
        table = db.open_table(index_id)
        version_before = table.version() if callable(table.version) else table.version
        schema = table.schema() if callable(table.schema) else table.schema
        vector_field = schema.field("vector")
        actual_dimensions = getattr(vector_field.type, "list_size", None)
        if actual_dimensions != contract["dimensions"]:
            return {"probed": False, "reason": "vector table dimension mismatch",
                    "contract": contract, "actual_dimensions": actual_dimensions}
        rows = []
        for start in range(0, len(wanted_ids), 100):
            batch = wanted_ids[start : start + 100]
            predicate = "id IN (" + ",".join(
                "'" + identifier.replace("'", "''") + "'" for identifier in batch
            ) + ")"
            rows.extend(
                table.search().where(predicate).select(["id", "revision"]).limit(len(batch)).to_list()
            )
        latest = db.open_table(index_id)
        version_after = latest.version() if callable(latest.version) else latest.version
        if version_before == version_after:
            break
    else:
        return {"probed": False, "reason": "vector table moved during targeted probe",
                "contract": contract}
    revisions = {r["id"]: r["revision"] for r in rows}
    version = version_before
    targets = [{"record_id": rid, "vector_revision": revisions.get(rid)} for rid in wanted_ids]
    probe_receipt = hashlib.sha256(json.dumps(
        {"table": index_id, "version": version, "targets": targets},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {
        "probed": True,
        "contract_verified": True,
        "contract": contract,
        "table": index_id,
        "version": version,
        "dimensions": actual_dimensions,
        "rows": table.count_rows(),
        "wanted": len(wanted_ids),
        "present": sum(1 for rid in wanted_ids if rid in revisions),
        "revisions": revisions,
        "targets": targets,
        "query": "targeted-id-filter",
        "probe_receipt": probe_receipt,
    }


def probe_embedding(root: Path):
    """Liveness only: GET /health with the stored token. No embedding is computed."""
    token_path = root / "embedding-token"
    if not token_path.exists():
        return {"alive": False, "reason": "embedding-token missing"}
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            response = client.get(
                "http://127.0.0.1:8321/health",
                headers={"Authorization": "Bearer " + token_path.read_text().strip()},
            )
        body = response.json()
        return {
            "alive": response.status_code == 200
                     and body.get("service") == "memorypalace-embedding"
                     and body.get("loaded") is True,
            "status": response.status_code,
            "loaded": body.get("loaded"),
            "model": body.get("model"),
            "pid": body.get("pid"),
        }
    except Exception as exc:  # noqa: BLE001 - any failure here means "not alive"
        return {"alive": False, "reason": type(exc).__name__}


def persisted_command(root: Path, *, command_id, manifest_sha256,
                      embed_limit, digest_limit):
    """Read and validate a bound command without initializing or writing the store."""
    path = root / "memory.sqlite3"
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='mind_derivative_recovery_runs'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT * FROM mind_derivative_recovery_runs WHERE command_id=?", (command_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if (
        row["manifest_sha256"] != manifest_sha256
        or row["embed_limit"] != embed_limit
        or row["digest_limit"] != digest_limit
    ):
        raise ValueError("Recovery command reused with different reviewed authority")
    value = dict(row)
    value["selection"] = json.loads(value["selection"])
    value["receipt"] = json.loads(value["receipt"]) if value["receipt"] else None
    return value


def persisted_receipt(root: Path, *, command_id, manifest_sha256,
                      embed_limit, digest_limit, command=None):
    """Read a terminal receipt or active-lease status without writing the store."""
    command = command or persisted_command(
        root, command_id=command_id, manifest_sha256=manifest_sha256,
        embed_limit=embed_limit, digest_limit=digest_limit,
    )
    if not command:
        return None
    if not command["receipt"]:
        if (
            command["state"] == "running"
            and (command.get("lease_until") or 0) > time.time()
        ):
            return {
                "kind": "w2-derivative-recovery-apply-status",
                "command_id": command_id,
                "selection_digest": command["selection_digest"],
                "state": "running",
                "lease_until": command.get("lease_until"),
                "fence": command.get("fence"),
                "stopped": "command-running",
                "blocked": ["the same immutable command selection is already leased"],
                "idempotent_replay": True,
            }
        return None
    receipt = dict(command["receipt"])
    receipt["idempotent_replay"] = True
    return receipt


def digest_estimates(engine, entries):
    """Current valid membership per recoverable event, through the real snapshot path."""
    estimates = []
    for entry in entries:
        if entry.get("action") != "recover":
            continue
        mind = Mind(engine, Scope(**entry["scope"]))
        lifecycle = EventLifecycle(mind)
        with engine.db.connect() as conn:
            snap = lifecycle.snapshot(conn, entry["event_id"])
        inputs = [
            {"id": r["id"], "revision": r["revision"], "basis": r["confirmation"],
             "occurred_at": r["valid_from"], "received_at": r["received_at"], "text": r["content"]}
            for r in snap["records"].values()
        ]
        estimates.append({
            "event_id": entry["event_id"],
            "valid_members": len(inputs),
            "missing_members": len(snap["missing"]),
            "input_tokens_estimate": tokens(dumps(inputs)) if inputs else 0,
            "projection_only": bool(inputs) and len(inputs) == 1 and len(inputs[0]["text"]) <= 3000,
        })
    return estimates


def dry_run(root: Path, output: Path):
    root = root.expanduser().resolve()
    embedding = probe_embedding(root)
    with tempfile.TemporaryDirectory(prefix="w2-recovery-") as tmp:
        _copy, snapshot_evidence = snapshot_db(root, Path(tmp))
        engine = Engine(tmp)
        with engine.db.connect() as conn:
            generation = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            counts = {
                "jobs": {r[0]: r[1] for r in conn.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")},
                "digests": {r[0]: r[1] for r in conn.execute("SELECT state,COUNT(*) FROM mind_event_digests GROUP BY state")},
            }
            contract = dr.embedding_vector_contract(conn)
            wanted = [
                e["record_id"] for e in dr.inspect_embeds(conn, {})
                if e.get("action") == "recover"
            ]
        vectors = probe_vectors(root, wanted, contract)
        with engine.db.connect() as conn:
            current = dr.plan(conn, vectors.get("revisions") if vectors.get("probed") else None)
        current["digests"] = dr.attach_digest_review_evidence(engine, current["digests"])
        vectors.pop("revisions", None)
        embed_estimates = []
        with engine.db.connect() as conn:
            for entry in current["embeds"]:
                if entry.get("action") != "recover":
                    continue
                row = conn.execute("SELECT data FROM records WHERE id=?", (entry["record_id"],)).fetchone()
                data = json.loads(row[0])
                embed_estimates.append({
                    "record_id": entry["record_id"],
                    "characters": len(data["title"] + "\n" + data["content"]),
                    "record_status": entry["record_status"],
                })
        digests = digest_estimates(engine, current["digests"])
    manifest = {
        "schema_version": 2,
        "kind": "w2-derivative-recovery-dry-run",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "store": {"root": str(root),
                  "snapshot_source_mtime_ns": snapshot_evidence["source_mtime_ns_before"],
                  "snapshot": snapshot_evidence,
                  "generation": generation, "counts": counts},
        "embedding_service": embedding,
        "vector_store": vectors,
        "counts": current["counts"],
        "embeds": current["embeds"],
        "digests": current["digests"],
        "out_of_scope": current["out_of_scope"],
        "cost_estimate": {
            "embeds": embed_estimates,
            "digests": digests,
            "embedding_priced": False,
            "note": "Local embedding is unpriced (configured price 0). Digest tokens are input estimates "
                    "from the current valid membership; deepseek roles are configured with price 0, so "
                    "dollars are unpriced and token counts are the cost signal.",
        },
    }
    manifest["review_fingerprint"] = dr.manifest_review_fingerprint(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def apply(root: Path, args):
    if not args.command_id:
        raise ValueError("--apply requires --command-id (one id per reviewed batch run)")
    if not args.manifest:
        raise ValueError("--apply requires --manifest (the reviewed schema_version=2 dry run)")
    root = root.expanduser().resolve()
    raw_manifest = args.manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    if args.manifest_sha256 and args.manifest_sha256.lower() != manifest_sha256:
        raise ValueError("--manifest-sha256 does not match the reviewed manifest file")
    manifest = json.loads(raw_manifest)
    dr.validate_reviewed_manifest(manifest)
    if Path(manifest["store"]["root"]).expanduser().resolve() != root:
        raise ValueError("Reviewed manifest belongs to a different store root")
    command = persisted_command(
        root, command_id=args.command_id, manifest_sha256=manifest_sha256,
        embed_limit=args.embed_batch, digest_limit=args.digest_batch,
    )
    replay = persisted_receipt(
        root, command_id=args.command_id, manifest_sha256=manifest_sha256,
        embed_limit=args.embed_batch, digest_limit=args.digest_batch, command=command,
    )
    if replay is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(replay, ensure_ascii=False, indent=2) + "\n")
        return replay
    embedding = probe_embedding(root)
    if not embedding.get("alive"):
        raise RuntimeError("Embedding service is not healthy and loaded; apply was not started")

    with tempfile.TemporaryDirectory(prefix="w2-apply-review-") as tmp:
        _copy, snapshot_evidence = snapshot_db(root, Path(tmp))
        snapshot_engine = Engine(tmp)
        with snapshot_engine.db.connect() as conn:
            contract = dr.embedding_vector_contract(conn)
            models_row = conn.execute("SELECT data FROM settings WHERE key='models'").fetchone()
            models = json.loads(models_row[0]) if models_row else {}
        reviewed_contract = manifest["vector_store"]["contract"]
        contract_keys = ("index_id", "model", "dimensions", "preprocessing")
        if not contract.get("resolved") or any(
            contract.get(key) != reviewed_contract.get(key) for key in contract_keys
        ):
            raise ValueError("Embedding vector contract drifted since manifest review")
        if command:
            # A crashed command owns its immutable original selection.  Do not
            # reselect from current failed-only scans: its jobs may already be
            # pending/running/complete and are reconciled by the same-command
            # job_recovery audit plus Runner's fenced preflight.
            selection = command["selection"]
            review = selection.get("selection_review", {})
            reviewed_embed_ids = [entry["record_id"] for entry in selection["embeds"]]
        else:
            reviewed_embed_ids = [
                entry["record_id"] for entry in manifest["embeds"]
                if entry.get("action") == "recover"
            ]
        vectors = probe_vectors(root, reviewed_embed_ids, contract)
        if reviewed_embed_ids and not vectors.get("probed"):
            raise RuntimeError("Current target-specific vector evidence is unavailable")
        if not command:
            with snapshot_engine.db.connect() as conn:
                current = dr.plan(conn, vectors.get("revisions") if vectors.get("probed") else None)
                current["reviewed_target_states"] = dr.reviewed_target_states(
                    conn, manifest, vectors.get("revisions") if vectors.get("probed") else None,
                )
            current["digests"] = dr.attach_digest_review_evidence(
                snapshot_engine, current["digests"]
            )
            selection, review = dr.select_reviewed_targets(
                manifest, current, embed_limit=args.embed_batch, digest_limit=args.digest_batch,
            )
            if review["blocked"] or review["drifted"]:
                raise RuntimeError("Reviewed targets are blocked or drifted; apply was not started")
        if selection["digests"]:
            key_env = models.get("summary", {}).get("api_key_env", "EVENTMEM_API_KEY")
            if not os.environ.get(key_env):
                raise RuntimeError(f"Required digest credential environment variable is absent: {key_env}")

    selection["selection_review"] = review
    selection["preapply_snapshot"] = snapshot_evidence
    selection["embedding_service"] = embedding
    vector_evidence = {key: value for key, value in vectors.items() if key != "revisions"}
    engine = Engine(root)
    runner = Runner(
        engine, embed_batch=args.embed_batch, digest_batch=args.digest_batch,
        probe=lambda: probe_embedding(root)["alive"],
        vector_probe=lambda record_ids: probe_vectors(root, record_ids, contract),
    )
    result = runner.run(
        command_id=args.command_id,
        selection=selection,
        manifest_sha256=manifest_sha256,
        vector_revisions=vectors.get("revisions") if vectors.get("probed") else None,
        vector_evidence=vector_evidence,
    )
    result.setdefault("selection_review", review)
    result.setdefault("preapply_snapshot", snapshot_evidence)
    result.setdefault("embedding_service", embedding)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Store root (contains memory.sqlite3)")
    parser.add_argument("--output", type=Path, required=True, help="Manifest/result JSON path")
    parser.add_argument("--apply", action="store_true", help="Requeue for real (default: dry run)")
    parser.add_argument("--command-id", help="Reviewed batch identity; required with --apply")
    parser.add_argument("--manifest", type=Path,
                        help="Reviewed schema_version=2 dry-run manifest; required with --apply")
    parser.add_argument("--manifest-sha256",
                        help="Optional out-of-band reviewed SHA-256 for --manifest")
    parser.add_argument("--embed-batch", type=int, default=3,
                        help="Maximum reviewed embed targets bound to this command (default: 3)")
    parser.add_argument("--digest-batch", type=int, default=1,
                        help="Maximum reviewed digest targets bound to this command (default: 1)")
    args = parser.parse_args()
    try:
        result = apply(args.root, args) if args.apply else dry_run(args.root, args.output)
    except Exception as exc:  # noqa: BLE001 - every apply failure must leave a result file
        result = {
            "kind": "w2-derivative-recovery-error",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command_id": args.command_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "stopped": "precondition",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"stopped": result["stopped"], "error_type": result["error_type"]},
                         ensure_ascii=False))
        return 2
    summary = result.get("counts") or result.get("inspection_counts") or {}
    if args.apply:
        summary = {**summary, "stopped": result.get("stopped"),
                   "idempotent_replay": result.get("idempotent_replay", False)}
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 2 if args.apply and result.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
