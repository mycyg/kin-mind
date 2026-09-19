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
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

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
    """A point-in-time copy taken while no WAL exists, verified stable across the copy."""
    src = root / "memory.sqlite3"
    target = workdir / "memory.sqlite3"
    for _ in range(attempts):
        if (root / "memory.sqlite3-wal").exists() or (root / "memory.sqlite3-shm").exists():
            time.sleep(0.5)
            continue
        stamp = src.stat().st_mtime_ns
        shutil.copyfile(src, target)
        if src.stat().st_mtime_ns == stamp and not (root / "memory.sqlite3-wal").exists():
            return target, stamp
        time.sleep(0.5)
    raise RuntimeError("Store stayed write-active; no clean snapshot window found")


def probe_vectors(root: Path, wanted_ids):
    """Read-only Lance evidence: table version and which of the wanted ids hold a row."""
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
    if not names:
        return {"probed": False, "reason": "no vector tables"}
    table = db.open_table(names[0])
    rows = table.search().select(["id", "revision"]).limit(500000).to_list()
    revisions = {r["id"]: r["revision"] for r in rows}
    sample = [{"record_id": rid, "vector_revision": revisions.get(rid)} for rid in wanted_ids[:5]]
    return {
        "probed": True,
        "table": names[0],
        "version": table.version,
        "rows": len(revisions),
        "wanted": len(wanted_ids),
        "present": sum(1 for rid in wanted_ids if rid in revisions),
        "revisions": revisions,
        "sample": sample,
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
            "alive": response.status_code == 200 and body.get("service") == "memorypalace-embedding",
            "status": response.status_code,
            "loaded": body.get("loaded"),
            "model": body.get("model"),
            "pid": body.get("pid"),
        }
    except Exception as exc:  # noqa: BLE001 - any failure here means "not alive"
        return {"alive": False, "reason": type(exc).__name__}


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
    embedding = probe_embedding(root)
    with tempfile.TemporaryDirectory(prefix="w2-recovery-") as tmp:
        _copy, source_mtime = snapshot_db(root, Path(tmp))
        engine = Engine(tmp)
        with engine.db.connect() as conn:
            generation = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            counts = {
                "jobs": {r[0]: r[1] for r in conn.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")},
                "digests": {r[0]: r[1] for r in conn.execute("SELECT state,COUNT(*) FROM mind_event_digests GROUP BY state")},
            }
            wanted = [e["record_id"] for e in dr.inspect_embeds(conn)]
        vectors = probe_vectors(root, wanted)
        with engine.db.connect() as conn:
            current = dr.plan(conn, vectors.get("revisions") if vectors.get("probed") else None)
        vectors.pop("revisions", None)  # the full map stays out of the manifest
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
        "kind": "w2-derivative-recovery-dry-run",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "store": {"root": str(root), "snapshot_source_mtime_ns": source_mtime,
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def apply(root: Path, args):
    if not args.command_id:
        raise SystemExit("--apply requires --command-id (one id per reviewed batch run)")
    engine = Engine(root)
    runner = Runner(engine, embed_batch=args.embed_batch, digest_batch=args.digest_batch,
                    probe=lambda: probe_embedding(root)["alive"])
    result = runner.run(command_id=args.command_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Store root (contains memory.sqlite3)")
    parser.add_argument("--output", type=Path, required=True, help="Manifest/result JSON path")
    parser.add_argument("--apply", action="store_true", help="Requeue for real (default: dry run)")
    parser.add_argument("--command-id", help="Reviewed batch identity; required with --apply")
    parser.add_argument("--embed-batch", type=int, default=3)
    parser.add_argument("--digest-batch", type=int, default=1)
    args = parser.parse_args()
    result = apply(args.root, args) if args.apply else dry_run(args.root, args.output)
    counts = result.get("counts") or {"stopped": result.get("stopped")}
    print(json.dumps(counts, ensure_ascii=False, default=str))


if __name__ == "__main__":
    sys.exit(main())
