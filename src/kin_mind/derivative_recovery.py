"""Targeted recovery of missing derived state: vectors and event digests.

`Worker.claim` never takes `failed` rows, so a record whose embed job exhausted its
attempts keeps its canonical text but loses the derived vector, and an event whose
digest job failed keeps a `failed` digest that the scheduler will not pick up again
(`schedule` only looks at `dirty`). The originals are intact; what is missing is
derived and reproducible.

A dry run enumerates the gap fresh; apply intersects that fresh view with an exact,
reviewed manifest and binds one bounded selection to its command id. Recovery then
moves work through the queue's own paths — `Worker.recover` for embeds, `mark_dirty`
plus the ordinary `event_digest` job for digests — so the existing revision guards,
the single-generation-per-event identity and the foreground preemption in `claim`
stay in force. Canonical state (records, revisions, graph nodes) is only read here,
never written: a corrected, deleted or superseded target is skipped, not resurrected.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.jobs import Worker
from eventmem.core.models import Scope, now

from .attempts import token_counts
from .lifecycle import mark_dirty

#: States a record may be in for its derivative to be worth rebuilding. Anything
#: else (superseded/retracted/refuted/archived/deleted) is history, not fact.
CURRENT_STATUSES = {"active", "unverified"}

#: Stored `jobs.error` strings that indict the environment, not the item. One item
#: failing this way predicts the next; the batch stops instead of burning attempts.
ENVIRONMENTAL_ERRORS = frozenset({
    "FileNotFoundError",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
})

#: Recovery of historical gaps yields to fresh work. `historical-backfill` digests
#: already run at 200; recovered work is the same kind of traffic. Anything above
#: 30 is also preempted wholesale while a foreground lease is held.
RECOVERY_DIGEST_PRIORITY = 200

#: A recovery command is bound to its reviewed manifest and exact selected targets
#: before any failed job is moved.  The row is derivative-operation metadata only;
#: canonical records, revisions and graph tables are never part of the receipt.
RECOVERY_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_derivative_recovery_runs(
 command_id TEXT PRIMARY KEY,
 manifest_sha256 TEXT NOT NULL,
 selection_digest TEXT NOT NULL,
 embed_limit INTEGER NOT NULL,
 digest_limit INTEGER NOT NULL,
 state TEXT NOT NULL,
 owner TEXT,
 lease_until REAL,
 fence INTEGER NOT NULL DEFAULT 0,
 selected_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 selection TEXT NOT NULL,
 receipt TEXT
)
"""

TERMINAL_JOB_STATES = frozenset({"complete", "failed", "canceled"})


def embedding_vector_contract(conn) -> dict:
    """Resolve the one text-vector table through the same naming contract as
    :class:`VectorIndex`, without registering or opening anything for writing.

    Model settings name the model, dimensions and preprocessing version.  The
    table id is therefore deterministic; choosing an arbitrary Lance table is
    never acceptable evidence that a record has (or lacks) its current vector.
    """
    row = conn.execute("SELECT data FROM settings WHERE key='models'").fetchone()
    if not row:
        return {"resolved": False, "reason": "models-setting-missing"}
    models = json.loads(row[0])
    config = models.get("embedding")
    if not isinstance(config, dict):
        return {"resolved": False, "reason": "embedding-role-missing"}
    model = config.get("model")
    dimensions = config.get("dimensions")
    preprocessing = config.get("preprocessing", "text-v1")
    if not model or not isinstance(preprocessing, str):
        return {"resolved": False, "reason": "embedding-contract-incomplete"}

    registered = []
    for candidate in conn.execute("SELECT id,data FROM vector_indexes ORDER BY id"):
        data = json.loads(candidate["data"])
        if data.get("model") == model and data.get("preprocessing") == preprocessing:
            registered.append((candidate["id"], data))
    if dimensions is None:
        dimensions_found = {data.get("dimensions") for _, data in registered}
        if len(dimensions_found) != 1 or None in dimensions_found:
            return {"resolved": False, "reason": "embedding-dimensions-ambiguous"}
        dimensions = dimensions_found.pop()
    if not isinstance(dimensions, int) or dimensions < 1:
        return {"resolved": False, "reason": "embedding-dimensions-invalid"}

    index_id = "vec_" + digest([model, dimensions, preprocessing])[:24]
    row = conn.execute("SELECT data FROM vector_indexes WHERE id=?", (index_id,)).fetchone()
    if not row:
        return {
            "resolved": False,
            "reason": "expected-vector-index-not-registered",
            "index_id": index_id,
            "model": model,
            "dimensions": dimensions,
            "preprocessing": preprocessing,
        }
    stored = json.loads(row[0])
    exact = (
        stored.get("id") == index_id
        and stored.get("model") == model
        and stored.get("dimensions") == dimensions
        and stored.get("preprocessing") == preprocessing
    )
    return {
        "resolved": exact,
        "reason": None if exact else "registered-vector-contract-mismatch",
        "index_id": index_id,
        "model": model,
        "dimensions": dimensions,
        "preprocessing": preprocessing,
        "registry_state": stored.get("state"),
    }


def _scope_key(scope) -> str:
    """The canonical digest-row key. Job payloads store the scope alphabetically
    (`dumps` sorts), digest rows store `Scope.key()` (field order) — only the
    model reconciles them."""
    return scope if isinstance(scope, str) else Scope(**scope).key()


def _job_summary(row) -> dict:
    return {
        "job_id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "error": row["error"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "priority": row["priority"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def inspect_embeds(conn, vector_revisions=None) -> list[dict]:
    """Failed embed jobs, each classified against the record's *current* version.

    `vector_revisions` maps record id to the revision present in the vector store
    (one row per id; the merge key is `id`). None means the store was not probed:
    presence is then reported as unknown instead of guessed."""
    entries = []
    for job in conn.execute(
        "SELECT * FROM jobs WHERE kind='embed' AND state='failed' ORDER BY id"
    ).fetchall():
        payload = json.loads(job["payload"])
        rid, revision = payload.get("record_id"), payload.get("revision")
        entry = {
            **_job_summary(job),
            "record_id": rid,
            "target_revision": revision,
        }
        rec = conn.execute(
            "SELECT revision,status,deleted,data FROM records WHERE id=?", (rid,)
        ).fetchone()
        if rec is None or rec["deleted"]:
            tombstoned = conn.execute(
                "SELECT 1 FROM tombstones WHERE key=?", (rid,)
            ).fetchone()
            entries.append(entry | {
                "action": "skip",
                "skip_reason": "record-deleted" if tombstoned else "record-missing",
                "reason": "The canonical record is gone; its derivative is never rebuilt",
            })
            continue
        data = json.loads(rec["data"])
        entry.update(
            current_revision=rec["revision"],
            record_status=rec["status"],
            confirmation=data.get("confirmation"),
            record_kind=data.get("kind"),
            generated=bool(data.get("generated")),
            scope=data.get("scope"),
        )
        if rec["status"] not in CURRENT_STATUSES:
            entries.append(entry | {
                "action": "skip",
                "skip_reason": "record-not-current",
                "reason": f"Record is {rec['status']}; recovery never resurrects it as current fact",
            })
            continue
        if rec["revision"] != revision:
            covering = conn.execute(
                "SELECT state FROM jobs WHERE kind='embed' AND json_extract(payload,'$.record_id')=? AND json_extract(payload,'$.revision')=?",
                (rid, rec["revision"]),
            ).fetchone()
            entries.append(entry | {
                "action": "skip",
                "skip_reason": "target-moved",
                "current_version_job_state": covering["state"] if covering else None,
                "reason": "The record advanced past the failed job's target; the current version has its own job",
            })
            continue
        if vector_revisions is None:
            entries.append(entry | {
                "action": "blocked",
                "block_reason": "vector-evidence-unavailable",
                "vector_at_target": None,
                "vector_revision": None,
                "reason": "The current vector table was not proven, so recovery must not recompute this target",
            })
            continue
        if vector_revisions.get(rid) == revision:
            entries.append(entry | {
                "action": "skip",
                "skip_reason": "vector-present",
                "vector_at_target": True,
                "vector_revision": revision,
                "reason": "A vector at the current revision already exists; the gap healed itself",
            })
            continue
        entry["vector_at_target"] = False
        entry["vector_revision"] = vector_revisions.get(rid)
        entries.append(entry | {
            "action": "recover",
            "reason": "Embed job failed and the record still sits at the same revision without a vector at it",
        })
    return entries


def inspect_digests(conn) -> list[dict]:
    """Failed digest jobs grouped by distinct event, classified against live state.

    An event whose digest turned `ready` through a newer generation keeps its old
    failed jobs as history; redirtying it would redo finished work, so it is a skip.
    Digest rows that are `failed` without any failed job left are still a gap and
    are included with `origin: digest-state`."""
    groups = defaultdict(list)
    for job in conn.execute(
        "SELECT * FROM jobs WHERE kind='event_digest' AND state='failed' ORDER BY id"
    ).fetchall():
        payload = json.loads(job["payload"])
        groups[(_scope_key(payload["scope"]), payload["event_id"])].append(job)

    entries, covered = [], set()

    def classify(scope_key, event_id, jobs, origin):
        digest_row = conn.execute(
            "SELECT state,generation,revision,dirty_at,due_at,data FROM mind_event_digests WHERE scope=? AND event_id=?",
            (scope_key, event_id),
        ).fetchone()
        node = conn.execute(
            "SELECT state,kind FROM mind_graph_nodes WHERE scope=? AND id=?",
            (scope_key, event_id),
        ).fetchone()
        active = conn.execute(
            "SELECT id,state FROM jobs WHERE kind='event_digest' AND state IN ('pending','retry','running','waiting_config') AND json_extract(payload,'$.event_id')=? AND json_extract(payload,'$.scope')=?",
            (event_id, dumps(json.loads(scope_key))),
        ).fetchone()
        data = json.loads(digest_row["data"]) if digest_row else {}
        entry = {
            "event_id": event_id,
            "scope": json.loads(scope_key),
            "origin": origin,
            "failed_job_ids": [_job_summary(j)["job_id"] for j in jobs],
            "failed_jobs": [_job_summary(j) for j in jobs],
            "error_classes": sorted({j["error"] or "unknown" for j in jobs}),
            "digest_state": digest_row["state"] if digest_row else None,
            "generation": digest_row["generation"] if digest_row else None,
            "digest_revision": digest_row["revision"] if digest_row else None,
            "last_error": data.get("last_error"),
            "summarized_at": data.get("summarized_at"),
            "event_node_state": node["state"] if node else None,
        }
        if digest_row is None:
            return entry | {"action": "skip", "skip_reason": "digest-row-missing",
                            "reason": "No digest row exists; backfill owns first publication"}
        if digest_row["state"] == "ready":
            return entry | {"action": "skip", "skip_reason": "ready-substitute",
                            "reason": "A newer generation already produced a ready digest"}
        if digest_row["state"] in {"dirty", "refreshing"}:
            return entry | {"action": "skip", "skip_reason": "already-scheduled",
                            "reason": "The normal dirty path is already carrying this event"}
        if active:
            return entry | {"action": "skip", "skip_reason": "job-in-flight",
                            "active_job_id": active["id"],
                            "reason": "A live digest job already covers this event"}
        if node is None or node["state"] != "active":
            return entry | {"action": "skip", "skip_reason": "event-not-active",
                            "reason": "The event node is not active; never resurrect it"}
        return entry | {
            "action": "recover",
            "reason": "Digest is failed, the event is active and no live job covers it",
        }

    for (scope_key, event_id), jobs in sorted(groups.items()):
        covered.add((scope_key, event_id))
        entries.append(classify(scope_key, event_id, jobs, "failed-job"))
    for row in conn.execute(
        "SELECT scope,event_id FROM mind_event_digests WHERE state='failed'"
    ).fetchall():
        if (row["scope"], row["event_id"]) not in covered:
            entries.append(classify(row["scope"], row["event_id"], [], "digest-state"))
    return entries


def inspect_needs_repair(conn) -> list[dict]:
    """Quarantined appraisals are listed for visibility only; the semantic repair
    flow in `kin_mind.recovery` owns them and this module never touches them."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_appraisals'"
    ).fetchone():
        return []
    return [
        {"id": r["id"], "state": r["state"], "attempts": r["attempts"],
         "stimulus": json.loads(r["data"]).get("stimulus")}
        for r in conn.execute(
            "SELECT id,state,attempts,data FROM mind_appraisals WHERE state='needs-repair' ORDER BY id"
        ).fetchall()
    ]


def inspect_other_failures(conn) -> dict:
    """Failed jobs of kinds this recovery does not cover (extraction and friends).

    They lose no derivative this run rebuilds — a failed extract means the records
    themselves were never proposed, which is a source-level question, not a missing
    derivative — so they are counted and left alone."""
    return {
        r["kind"]: r["c"]
        for r in conn.execute(
            "SELECT kind,COUNT(*) c FROM jobs WHERE state='failed' AND kind NOT IN ('embed','event_digest') GROUP BY kind"
        ).fetchall()
    }


def plan(conn, vector_revisions=None) -> dict:
    embeds = inspect_embeds(conn, vector_revisions)
    digests = inspect_digests(conn)
    return {
        "embeds": embeds,
        "digests": digests,
        "out_of_scope": {"needs_repair_appraisals": inspect_needs_repair(conn),
                         "other_failed_jobs": inspect_other_failures(conn)},
        "counts": {
            "embed_failed": len(embeds),
            "embed_recover": sum(1 for e in embeds if e["action"] == "recover"),
            "embed_skip": sum(1 for e in embeds if e["action"] == "skip"),
            "embed_blocked": sum(1 for e in embeds if e["action"] == "blocked"),
            "digest_failed_jobs": sum(len(e["failed_job_ids"]) for e in digests),
            "digest_events": len(digests),
            "digest_recover": sum(1 for e in digests if e["action"] == "recover"),
            "digest_skip": sum(1 for e in digests if e["action"] == "skip"),
        },
    }


def _digest_review_evidence(engine, entry, conn) -> dict:
    """Exact event input identity reviewed before a recovery is allowed.

    ``input_hash`` already covers graph node/edge versions, every member record
    version, missing sources and the read policy.  The explicit member list stays
    beside it so a human can see what the hash commits to.
    """
    from .lifecycle import EventLifecycle
    from .state import Mind

    lifecycle = EventLifecycle(Mind(engine, Scope(**entry["scope"])))
    snapshot = lifecycle.snapshot(conn, entry["event_id"])
    active = set(snapshot["records"])
    return {
        "event_revision": snapshot["event"]["revision"],
        "membership_input_hash": snapshot["input_hash"],
        "member_versions": [
            {
                "record_id": record_id,
                "revision": record["revision"],
                "status": record["status"],
                "active_for_digest": record_id in active,
            }
            for record_id, record in sorted(snapshot["all_records"].items())
        ],
        "missing_members": snapshot["missing"],
        "excluded_members": snapshot["excluded"],
    }


def attach_digest_review_evidence(engine, entries) -> list[dict]:
    """Attach generation-independent membership evidence to recoverable digests."""
    enriched = []
    with engine.db.connect() as conn:
        for entry in entries:
            value = dict(entry)
            if entry.get("action") == "recover":
                value.update(_digest_review_evidence(engine, entry, conn))
            enriched.append(value)
    return enriched


def manifest_review_fingerprint(manifest) -> str:
    """Fingerprint only the reviewed recovery authority, not estimates or prose."""
    contract = manifest.get("vector_store", {}).get("contract", {})
    core = {
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "store_root": manifest.get("store", {}).get("root"),
        "vector_evidence": {
            "contract": {
                key: contract.get(key)
                for key in ("index_id", "model", "dimensions", "preprocessing")
            },
            "table": manifest.get("vector_store", {}).get("table"),
            "version": manifest.get("vector_store", {}).get("version"),
            "dimensions": manifest.get("vector_store", {}).get("dimensions"),
            "query": manifest.get("vector_store", {}).get("query"),
            "targets": manifest.get("vector_store", {}).get("targets"),
            "probe_receipt": manifest.get("vector_store", {}).get("probe_receipt"),
        },
        "embeds": [
            {
                key: entry.get(key)
                for key in (
                    "job_id", "record_id", "target_revision", "scope", "state",
                    "error", "attempts", "updated_at", "vector_at_target",
                    "vector_revision", "action",
                )
            }
            for entry in manifest.get("embeds", [])
            if entry.get("action") == "recover"
        ],
        "digests": [
            {
                key: entry.get(key)
                for key in (
                    "event_id", "scope", "failed_job_ids", "error_classes",
                    "digest_state", "generation", "digest_revision", "event_revision",
                    "membership_input_hash", "member_versions", "missing_members",
                    "excluded_members", "action",
                )
            }
            for entry in manifest.get("digests", [])
            if entry.get("action") == "recover"
        ],
    }
    return digest(core)


def validate_reviewed_manifest(manifest) -> dict:
    """Reject legacy or incomplete manifests before a live Engine is created."""
    if manifest.get("kind") != "w2-derivative-recovery-dry-run":
        raise ValueError("Unsupported derivative recovery manifest kind")
    if manifest.get("schema_version") != 2:
        raise ValueError("Apply requires a schema_version=2 dry-run manifest")
    if manifest.get("review_fingerprint") != manifest_review_fingerprint(manifest):
        raise ValueError("Manifest review fingerprint does not match its targets")
    vectors = manifest.get("vector_store", {})
    contract = vectors.get("contract", {})
    if not vectors.get("probed") or not vectors.get("contract_verified"):
        raise ValueError("Reviewed manifest has no verified current vector evidence")
    if not contract.get("resolved") or not all(
        contract.get(key) is not None
        for key in ("index_id", "model", "dimensions", "preprocessing")
    ):
        raise ValueError("Reviewed manifest has no exact embedding index contract")
    if (
        vectors.get("table") != contract.get("index_id")
        or vectors.get("version") is None
        or vectors.get("dimensions") != contract.get("dimensions")
        or vectors.get("query") != "targeted-id-filter"
        or not isinstance(vectors.get("targets"), list)
        or not vectors.get("probe_receipt")
    ):
        raise ValueError("Reviewed manifest has no exact versioned target-vector probe")
    vector_targets = {}
    for target in vectors["targets"]:
        record_id = target.get("record_id") if isinstance(target, dict) else None
        if not record_id or record_id in vector_targets or "vector_revision" not in target:
            raise ValueError("Reviewed vector targets must be unique and versioned")
        vector_targets[record_id] = target["vector_revision"]

    embed_keys = set()
    for entry in manifest.get("embeds", []):
        if entry.get("action") != "recover":
            continue
        key = entry.get("job_id")
        if not key or key in embed_keys:
            raise ValueError("Reviewed embed targets must have unique job ids")
        embed_keys.add(key)
        if (
            not entry.get("record_id")
            or not isinstance(entry.get("target_revision"), int)
            or entry.get("state") != "failed"
            or entry.get("vector_at_target") is not False
            or "vector_revision" not in entry
            or entry["record_id"] not in vector_targets
            or vector_targets.get(entry["record_id"]) != entry.get("vector_revision")
            or entry.get("vector_revision") == entry.get("target_revision")
        ):
            raise ValueError("Reviewed embed target is missing exact failed-version evidence")

    digest_keys = set()
    for entry in manifest.get("digests", []):
        if entry.get("action") != "recover":
            continue
        key = (_scope_key(entry.get("scope", {})), entry.get("event_id"))
        if not key[1] or key in digest_keys:
            raise ValueError("Reviewed digest targets must have unique scoped event ids")
        digest_keys.add(key)
        if (
            entry.get("digest_state") != "failed"
            or not isinstance(entry.get("generation"), int)
            or not isinstance(entry.get("digest_revision"), int)
            or not isinstance(entry.get("event_revision"), int)
            or not entry.get("membership_input_hash")
            or not isinstance(entry.get("member_versions"), list)
        ):
            raise ValueError("Reviewed digest target is missing generation or membership evidence")
    return {
        "review_fingerprint": manifest["review_fingerprint"],
        "embed_targets": len(embed_keys),
        "digest_targets": len(digest_keys),
    }


def reviewed_target_states(conn, manifest, vector_revisions=None) -> dict:
    """Read the reviewed targets even when they no longer appear in failed-only scans."""
    embeds = {}
    for entry in manifest.get("embeds", []):
        if entry.get("action") != "recover":
            continue
        row = conn.execute(
            "SELECT kind,payload,state,error,attempts,updated_at FROM jobs WHERE id=?",
            (entry["job_id"],),
        ).fetchone()
        embeds[entry["job_id"]] = {
            "job": dict(row) if row else None,
            "vector_revision": (
                vector_revisions.get(entry["record_id"])
                if vector_revisions is not None else None
            ),
            "vector_evidence_available": vector_revisions is not None,
        }
    digests = {}
    for entry in manifest.get("digests", []):
        if entry.get("action") != "recover":
            continue
        scope_key = _scope_key(entry["scope"])
        row = conn.execute(
            "SELECT state,generation,revision,input_hash,data FROM mind_event_digests "
            "WHERE scope=? AND event_id=?",
            (scope_key, entry["event_id"]),
        ).fetchone()
        value = dict(row) if row else None
        if value:
            value["data"] = json.loads(value["data"])
        digests[(scope_key, entry["event_id"])] = value
    return {"embeds": embeds, "digests": digests}


def select_reviewed_targets(manifest, current, *, embed_limit, digest_limit) -> tuple[dict, dict]:
    """Choose at most one reviewed batch from the fresh inspection.

    New failures are reported but never enter the selection.  Targets already
    healed by ordinary work or an earlier batch are skipped, allowing a new
    command to select the next reviewed targets.  Once bound to a command the
    selection itself is immutable (see :func:`bind_recovery_selection`).
    """
    validate_reviewed_manifest(manifest)
    if embed_limit < 0 or digest_limit < 0:
        raise ValueError("Recovery batch limits must be non-negative")
    reviewed_embeds = [e for e in manifest["embeds"] if e.get("action") == "recover"]
    reviewed_digests = [e for e in manifest["digests"] if e.get("action") == "recover"]
    current_embeds = {e["job_id"]: e for e in current["embeds"]}
    current_digests = {(_scope_key(e["scope"]), e["event_id"]): e for e in current["digests"]}
    embed_reviewed_ids = {e["job_id"] for e in reviewed_embeds}
    digest_reviewed_ids = {(_scope_key(e["scope"]), e["event_id"]) for e in reviewed_digests}
    selected_embeds, selected_digests = [], []
    resolved, drifted, blocked = [], [], []
    target_states = current.get("reviewed_target_states", {"embeds": {}, "digests": {}})

    for reviewed in reviewed_embeds:
        current_entry = current_embeds.get(reviewed["job_id"])
        identity = {"kind": "embed", "job_id": reviewed["job_id"],
                    "record_id": reviewed["record_id"], "target_revision": reviewed["target_revision"]}
        if current_entry is None:
            state = target_states.get("embeds", {}).get(reviewed["job_id"], {})
            job = state.get("job")
            payload = json.loads(job["payload"]) if job else {}
            if (
                job
                and job["kind"] == "embed"
                and job["state"] == "complete"
                and payload.get("record_id") == reviewed["record_id"]
                and payload.get("revision") == reviewed["target_revision"]
                and state.get("vector_evidence_available")
                and state.get("vector_revision") == reviewed["target_revision"]
            ):
                resolved.append(identity | {"reason": "complete-with-target-vector"})
            elif job and job["state"] in {"pending", "retry", "running", "waiting_config"}:
                blocked.append(identity | {"reason": "reviewed-job-in-flight",
                                           "state": job["state"]})
            else:
                drifted.append(identity | {"reason": "failed-job-no-longer-proven"})
            continue
        if current_entry.get("action") == "blocked":
            blocked.append(identity | {"reason": current_entry.get("block_reason")})
            continue
        exact = all(current_entry.get(key) == reviewed.get(key) for key in (
            "job_id", "record_id", "target_revision", "scope", "state", "error",
            "attempts", "updated_at", "vector_at_target", "vector_revision",
        ))
        if current_entry.get("action") == "skip" and current_entry.get("skip_reason") == "vector-present":
            resolved.append(identity | {"reason": "vector-present"})
            continue
        if current_entry.get("action") != "recover" or not exact:
            drifted.append(identity | {"reason": current_entry.get("skip_reason", "target-version-drift")})
            continue
        if len(selected_embeds) < embed_limit:
            selected_embeds.append(current_entry | {
                "reviewed_failure_error": reviewed.get("error"),
                "reviewed_failure_updated_at": reviewed.get("updated_at"),
            })

    digest_fields = (
        "event_id", "scope", "failed_job_ids", "error_classes", "digest_state",
        "generation", "digest_revision", "event_revision", "membership_input_hash",
        "member_versions", "missing_members", "excluded_members",
    )
    for reviewed in reviewed_digests:
        key = (_scope_key(reviewed["scope"]), reviewed["event_id"])
        current_entry = current_digests.get(key)
        identity = {"kind": "event_digest", "scope": reviewed["scope"],
                    "event_id": reviewed["event_id"], "generation": reviewed["generation"]}
        if current_entry is None:
            state = target_states.get("digests", {}).get(key)
            active_versions = {
                member["record_id"]: member["revision"]
                for member in reviewed["member_versions"]
                if member["active_for_digest"]
            }
            if (
                state
                and state["state"] == "ready"
                and state["input_hash"] == reviewed["membership_input_hash"]
                and state["data"].get("source_versions") == active_versions
            ):
                resolved.append(identity | {"reason": "ready-with-reviewed-membership"})
            elif state and state["state"] in {"dirty", "refreshing"}:
                blocked.append(identity | {"reason": "reviewed-digest-in-flight",
                                           "state": state["state"]})
            else:
                drifted.append(identity | {"reason": "failed-digest-no-longer-proven"})
            continue
        if current_entry.get("action") == "skip" and current_entry.get("skip_reason") == "ready-substitute":
            resolved.append(identity | {"reason": current_entry["skip_reason"]})
            continue
        if current_entry.get("action") == "skip" and current_entry.get("skip_reason") in {
            "already-scheduled", "job-in-flight",
        }:
            blocked.append(identity | {"reason": current_entry["skip_reason"]})
            continue
        exact = all(current_entry.get(field) == reviewed.get(field) for field in digest_fields)
        if current_entry.get("action") != "recover" or not exact:
            drifted.append(identity | {"reason": current_entry.get("skip_reason", "target-version-drift")})
            continue
        if len(selected_digests) < digest_limit:
            selected_digests.append(current_entry)

    unreviewed_embeds = [
        {"job_id": e["job_id"], "record_id": e["record_id"], "target_revision": e["target_revision"]}
        for e in current["embeds"]
        if e.get("action") == "recover" and e["job_id"] not in embed_reviewed_ids
    ]
    unreviewed_digests = [
        {"scope": e["scope"], "event_id": e["event_id"], "generation": e["generation"]}
        for e in current["digests"]
        if e.get("action") == "recover"
        and (_scope_key(e["scope"]), e["event_id"]) not in digest_reviewed_ids
    ]
    selection = {
        "review_fingerprint": manifest["review_fingerprint"],
        "embeds": selected_embeds,
        "digests": selected_digests,
    }
    review = {
        "selected": {"embeds": len(selected_embeds), "digests": len(selected_digests)},
        "remaining_reviewed": {
            "embeds": max(0, sum(1 for e in reviewed_embeds if current_embeds.get(e["job_id"], {}).get("action") == "recover") - len(selected_embeds)),
            "digests": max(0, sum(1 for e in reviewed_digests if current_digests.get((_scope_key(e["scope"]), e["event_id"]), {}).get("action") == "recover") - len(selected_digests)),
        },
        "already_resolved": resolved,
        "drifted": drifted,
        "blocked": blocked,
        "unreviewed": {"embeds": unreviewed_embeds, "digests": unreviewed_digests},
    }
    return selection, review


def _ensure_recovery_run_schema(conn):
    conn.execute(RECOVERY_RUN_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mind_derivative_recovery_runs)")}
    if "owner" not in columns:
        conn.execute("ALTER TABLE mind_derivative_recovery_runs ADD COLUMN owner TEXT")
    if "lease_until" not in columns:
        conn.execute("ALTER TABLE mind_derivative_recovery_runs ADD COLUMN lease_until REAL")
    if "fence" not in columns:
        conn.execute(
            "ALTER TABLE mind_derivative_recovery_runs ADD COLUMN fence INTEGER NOT NULL DEFAULT 0"
        )


def bind_recovery_selection(engine, *, command_id, manifest_sha256, selection,
                            embed_limit, digest_limit, owner=None, lease_seconds=300.0) -> dict:
    """Persist a command's target set before moving any job.

    Reusing the command with the same manifest and limits returns the original
    selection (and terminal receipt, if any), never the next currently failed
    targets.  Reusing it with different authority is rejected.
    """
    if not command_id or len(command_id) > 200:
        raise ValueError("Recovery command id must be 1..200 characters")
    if not manifest_sha256:
        raise ValueError("Reviewed manifest sha256 is required")
    selection_digest = digest(selection)
    if not owner:
        owner = uuid.uuid4().hex
    if not 15 <= lease_seconds <= 3600:
        raise ValueError("Recovery command lease must be between 15 and 3600 seconds")
    stamp, clock = now(), time.time()
    with engine.db.connect(write=True) as conn:
        _ensure_recovery_run_schema(conn)
        row = conn.execute(
            "SELECT * FROM mind_derivative_recovery_runs WHERE command_id=?", (command_id,)
        ).fetchone()
        if row:
            if (
                row["manifest_sha256"] != manifest_sha256
                or row["embed_limit"] != embed_limit
                or row["digest_limit"] != digest_limit
            ):
                raise Conflict("Recovery command reused with different reviewed authority")
            if row["receipt"]:
                return {
                    "selection": json.loads(row["selection"]),
                    "selection_digest": row["selection_digest"],
                    "state": row["state"],
                    "receipt": json.loads(row["receipt"]),
                    "claimed": False,
                    "idempotent_replay": True,
                }
            if row["state"] == "running" and (row["lease_until"] or 0) > clock:
                return {
                    "selection": json.loads(row["selection"]),
                    "selection_digest": row["selection_digest"],
                    "state": "running",
                    "receipt": None,
                    "claimed": False,
                    "lease_until": row["lease_until"],
                    "fence": row["fence"],
                    "idempotent_replay": True,
                }
        else:
            conn.execute(
                "INSERT INTO mind_derivative_recovery_runs("
                "command_id,manifest_sha256,selection_digest,embed_limit,digest_limit,state,"
                "owner,lease_until,fence,selected_at,updated_at,selection,receipt"
                ") VALUES(?,?,?,?,?,'selected',NULL,NULL,0,?,?,?,NULL)",
                (command_id, manifest_sha256, selection_digest, embed_limit, digest_limit,
                 stamp, stamp, dumps(selection)),
            )
        conn.execute(
            "UPDATE mind_derivative_recovery_runs SET state='running',owner=?,lease_until=?,"
            "fence=fence+1,updated_at=? WHERE command_id=? AND receipt IS NULL "
            "AND (state!='running' OR lease_until IS NULL OR lease_until<=?)",
            (owner, clock + lease_seconds, stamp, command_id, clock),
        )
        claimed = conn.execute(
            "SELECT * FROM mind_derivative_recovery_runs WHERE command_id=?", (command_id,)
        ).fetchone()
        if claimed["owner"] != owner or claimed["state"] != "running":
            return {
                "selection": json.loads(claimed["selection"]),
                "selection_digest": claimed["selection_digest"],
                "state": claimed["state"],
                "receipt": json.loads(claimed["receipt"]) if claimed["receipt"] else None,
                "claimed": False,
                "lease_until": claimed["lease_until"],
                "fence": claimed["fence"],
                "idempotent_replay": True,
            }
    return {
        "selection": json.loads(claimed["selection"]),
        "selection_digest": claimed["selection_digest"],
        "state": "running",
        "receipt": None,
        "claimed": True,
        "lease_until": claimed["lease_until"],
        "fence": claimed["fence"],
        "owner": owner,
        "idempotent_replay": row is not None,
    }


def _renew_recovery_run(engine, command_id, owner, fence, lease_seconds) -> bool:
    clock = time.time()
    with engine.db.connect(write=True) as conn:
        return bool(conn.execute(
            "UPDATE mind_derivative_recovery_runs SET lease_until=?,updated_at=? "
            "WHERE command_id=? AND state='running' AND owner=? AND fence=? "
            "AND receipt IS NULL AND lease_until>?",
            (clock + lease_seconds, now(), command_id, owner, fence, clock),
        ).rowcount)


def _finish_recovery_run(engine, command_id, result, *, owner, fence) -> dict:
    state = "complete" if result.get("stopped") is None else "stopped"
    clock = time.time()
    with engine.db.connect(write=True) as conn:
        written = conn.execute(
            "UPDATE mind_derivative_recovery_runs SET state=?,owner=NULL,lease_until=0,"
            "updated_at=?,receipt=? WHERE command_id=? AND state='running' AND owner=? "
            "AND fence=? AND receipt IS NULL AND lease_until>?",
            (state, now(), dumps(result), command_id, owner, fence, clock),
        )
        row = conn.execute(
            "SELECT state,lease_until,fence,receipt,selection_digest "
            "FROM mind_derivative_recovery_runs WHERE command_id=?", (command_id,)
        ).fetchone()
    if written.rowcount and row["receipt"]:
        return json.loads(row["receipt"])
    if row and row["receipt"]:
        receipt = json.loads(row["receipt"])
        receipt["idempotent_replay"] = True
        return receipt
    return {
        "kind": "w2-derivative-recovery-apply-status",
        "command_id": command_id,
        "selection_digest": row["selection_digest"] if row else result.get("selection_digest"),
        "state": row["state"] if row else "missing",
        "lease_until": row["lease_until"] if row else None,
        "fence": row["fence"] if row else None,
        "stopped": "command-lease-lost",
        "blocked": ["another owner may resume the immutable selection"],
        "idempotent_replay": True,
    }


def recover_embeddings(engine, entries, *, command_id, vector_revisions=None,
                       require_vector_evidence=False) -> dict:
    """Requeue the failed embed jobs whose target is still the current version.

    The revision check is repeated inside the write transaction: a record that
    moved since inspection abandons its entry here, and even a raced requeue is
    harmless because `Worker.prepare` no-ops an embed whose revision moved on."""
    if require_vector_evidence and vector_revisions is None:
        raise ValueError("Current vector evidence is required before embed recovery")
    todo, abandoned, replayed, target_receipts = [], [], [], []
    with engine.db.connect() as conn:
        for entry in entries:
            if entry.get("action") != "recover":
                continue
            prior = conn.execute(
                "SELECT 1 FROM job_recovery WHERE job_id=? AND command_id=? AND kind='embed'",
                (entry["job_id"], command_id),
            ).fetchone()
            target = {
                "job_id": entry["job_id"],
                "record_id": entry["record_id"],
                "target_revision": entry["target_revision"],
                "scope": entry.get("scope"),
            }
            if prior:
                replayed.append(entry["job_id"])
                target_receipts.append(target | {"mode": "idempotent-replay"})
                continue
            rec = conn.execute(
                "SELECT revision,status,deleted FROM records WHERE id=?",
                (entry["record_id"],),
            ).fetchone()
            if (
                rec is None
                or rec["deleted"]
                or rec["status"] not in CURRENT_STATUSES
                or rec["revision"] != entry["target_revision"]
            ):
                abandoned.append({"job_id": entry["job_id"], "record_id": entry["record_id"],
                                  "reason": "target-moved-or-not-current"})
                continue
            if (
                vector_revisions is not None
                and vector_revisions.get(entry["record_id"]) == entry["target_revision"]
            ):
                abandoned.append({"job_id": entry["job_id"], "record_id": entry["record_id"],
                                  "reason": "vector-now-present"})
                continue
            todo.append(entry["job_id"])
            target_receipts.append(target | {"mode": "recovered"})
    result = {"recovered": [], "replayed": replayed, "skipped": [],
              "abandoned": abandoned, "targets": target_receipts, "command_id": command_id}
    for page in range(0, len(todo), 50):
        batch = Worker(engine).recover(todo[page : page + 50], command_id=command_id)
        result["recovered"].extend(batch["recovered"])
        result["skipped"].extend(batch["skipped"])
    recovered = set(result["recovered"])
    skipped = {item["id"] for item in result["skipped"]}
    for target in result["targets"]:
        if target["mode"] == "recovered" and target["job_id"] not in recovered:
            target["mode"] = "concurrent-state-change" if target["job_id"] in skipped else "not-recovered"
    return result


def recover_digests(engine, entries, *, command_id, at=None, require_reviewed=False) -> dict:
    """Return failed digests to the normal dirty path at a fresh generation.

    `mark_dirty` bumps the generation, so the enqueued job gets a new identity and
    the old failed jobs stay as linked history. The digest row is re-checked inside
    the transaction: an event that turned `ready` or `dirty` since inspection is
    left exactly as it is."""
    at = at or now()
    recovered, replayed, skipped, abandoned = [], [], [], []
    with engine.db.connect(write=True) as conn:
        prior_rows = conn.execute(
            "SELECT j.id,j.payload,jr.target FROM job_recovery jr JOIN jobs j ON j.id=jr.job_id "
            "WHERE jr.command_id=? AND jr.kind='event_digest'",
            (command_id,),
        ).fetchall()
        prior = {}
        for old in prior_rows:
            payload = json.loads(old["payload"])
            prior[(_scope_key(payload["scope"]), payload["event_id"])] = old
        for entry in entries:
            if entry.get("action") != "recover":
                continue
            scope_key = _scope_key(entry["scope"])
            event_id = entry["event_id"]
            old = prior.get((scope_key, event_id))
            if old:
                generation = int(old["target"].rsplit("@g", 1)[-1])
                replayed.append({"event_id": event_id, "scope": entry["scope"],
                                 "job_id": old["id"], "generation": generation,
                                 "mode": "idempotent-replay"})
                continue
            row = conn.execute(
                "SELECT state,generation,revision,data FROM mind_event_digests WHERE scope=? AND event_id=?",
                (scope_key, event_id),
            ).fetchone()
            node = conn.execute(
                "SELECT state FROM mind_graph_nodes WHERE scope=? AND id=?",
                (scope_key, event_id),
            ).fetchone()
            if row is None or row["state"] != "failed":
                skipped.append({"event_id": event_id,
                                "state": row["state"] if row else None,
                                "reason": "no-longer-failed"})
                continue
            if node is None or node["state"] != "active":
                skipped.append({"event_id": event_id, "reason": "event-not-active"})
                continue
            if require_reviewed:
                actual = _digest_review_evidence(engine, entry, conn)
                expected = {
                    key: entry.get(key)
                    for key in (
                        "event_revision", "membership_input_hash", "member_versions",
                        "missing_members", "excluded_members",
                    )
                }
                observed = {key: actual.get(key) for key in expected}
                if (
                    row["generation"] != entry.get("generation")
                    or row["revision"] != entry.get("digest_revision")
                    or observed != expected
                ):
                    abandoned.append({
                        "event_id": event_id,
                        "scope": entry["scope"],
                        "reason": "reviewed-generation-or-membership-moved",
                        "expected_generation": entry.get("generation"),
                        "actual_generation": row["generation"],
                        "expected_digest_revision": entry.get("digest_revision"),
                        "actual_digest_revision": row["revision"],
                        "expected_input_hash": entry.get("membership_input_hash"),
                        "actual_input_hash": actual.get("membership_input_hash"),
                    })
                    continue
            last_error = json.loads(row["data"]).get("last_error")
            mark_dirty(conn, scope_key, [event_id], at, "derivative-recovery")
            generation = conn.execute(
                "SELECT generation FROM mind_event_digests WHERE scope=? AND event_id=?",
                (scope_key, event_id),
            ).fetchone()[0]
            jid = engine.enqueue(
                "event_digest",
                {"scope": entry["scope"], "event_id": event_id},
                f"event-digest:{digest(scope_key)}:{event_id}:{generation}",
                conn=conn,
                priority=RECOVERY_DIGEST_PRIORITY,
            )
            conn.execute(
                "INSERT OR IGNORE INTO job_recovery VALUES(?,?,?,?,?,?,?,?)",
                (jid, command_id, "event_digest", f"{event_id}@g{generation}",
                 "failed", (last_error or "")[:200] or None, len(entry["failed_job_ids"]), at),
            )
            recovered.append({"event_id": event_id, "scope": entry["scope"], "job_id": jid,
                              "generation": generation,
                              "reviewed_generation": entry.get("generation"),
                              "membership_input_hash": entry.get("membership_input_hash"),
                              "mode": "recovered"})
    return {"recovered": recovered, "replayed": replayed, "skipped": skipped,
            "abandoned": abandoned, "command_id": command_id}


def _model_usage(receipt) -> tuple[int | float | None, int | float | None, str]:
    """Normalize current nested usage and legacy top-level token receipts.

    A complete provider-reported pair may legitimately contain zero. Missing or
    incomplete counts remain unknown; neither side is synthesized as zero.
    """
    receipt = receipt if isinstance(receipt, dict) else {}
    nested = receipt.get("usage")
    for candidate in (nested, receipt):
        counts = token_counts(candidate)
        if counts is not None:
            return counts[0], counts[1], "reported"
    token_fields = {"prompt_tokens", "input_tokens", "completion_tokens", "output_tokens"}
    candidates = [candidate for candidate in (nested, receipt) if isinstance(candidate, dict)]
    status = "partial-unknown" if any(token_fields & candidate.keys() for candidate in candidates) else "unknown"
    return None, None, status


def _digest_cost_identity(row, data, rec, verification, *, require_verification=False) -> list[str]:
    """Return every reason a current digest cannot own the recovered job's receipt."""
    issues = []
    expected_generation = rec.get("generation")
    expected_hash = rec.get("membership_input_hash")
    if row is None:
        issues.append("current-digest-missing")
    else:
        if row["state"] != "ready":
            issues.append("current-digest-not-ready")
        if not isinstance(expected_generation, int) or row["generation"] != expected_generation:
            issues.append("current-generation-mismatch")
        if expected_hash is not None and row["input_hash"] != expected_hash:
            issues.append("current-membership-mismatch")
    if verification is None:
        if require_verification:
            issues.append("digest-verification-missing")
        return issues
    if verification.get("verified") is not True:
        issues.append("digest-verification-failed")
    if verification.get("generation") != expected_generation:
        issues.append("verification-generation-mismatch")
    if expected_hash is not None and verification.get("membership_input_hash") != expected_hash:
        issues.append("verification-membership-mismatch")
    if row is not None:
        if row["generation"] != verification.get("generation"):
            issues.append("current-verification-generation-mismatch")
        if row["revision"] != verification.get("digest_revision"):
            issues.append("current-verification-revision-mismatch")
        if row["input_hash"] != verification.get("membership_input_hash"):
            issues.append("current-verification-membership-mismatch")
        if data.get("source_versions") != verification.get("source_versions"):
            issues.append("current-verification-sources-mismatch")
    return issues


def collect_costs(engine, result) -> list[dict]:
    """Per-item cost of a run: final state and wall time for every recovered job;
    for digests also the model receipt the normal digest path committed.

    Local embeddings report no usage and both roles are configured at price 0, so
    tokens/time are the honest cost signal here — never a fabricated dollar figure."""
    from datetime import datetime

    def seconds_between(a, b):
        try:
            return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 3)
        except (TypeError, ValueError):
            return None

    costs, seen = [], set()
    with engine.db.connect() as conn:
        for cycle in result["cycles"]:
            vector_revisions = (cycle.get("vector_verification") or {}).get("revisions", {})
            verifications = cycle.get("digest_verification") or []
            verification_by_target = {
                (_scope_key(item["scope"]), item["event_id"], item["job_id"]): item
                for item in verifications
            }
            for target in cycle["embeds"].get("targets", []):
                job_id = target["job_id"]
                if ("embed", job_id) in seen:
                    continue
                seen.add(("embed", job_id))
                job = conn.execute(
                    "SELECT state,error,updated_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                audit = conn.execute(
                    "SELECT recovered_at FROM job_recovery WHERE job_id=? AND command_id=?",
                    (job_id, result["command_id"]),
                ).fetchone()
                costs.append({
                    "kind": "embed", "scope": target.get("scope"),
                    "record_id": target["record_id"],
                    "target_revision": target["target_revision"], "job_id": job_id,
                    "state": job["state"] if job else "missing",
                    "error": job["error"] if job else None,
                    "wall_seconds": seconds_between(audit["recovered_at"], job["updated_at"]) if job and audit else None,
                    "vector_revision": vector_revisions.get(target["record_id"]),
                    "vector_verified": vector_revisions.get(target["record_id"]) == target["target_revision"],
                    "usage_status": "unknown", "priced": False,
                })
            digest_targets = cycle["digests"].get("recovered", []) + cycle["digests"].get("replayed", [])
            for rec in digest_targets:
                if ("event_digest", rec["job_id"]) in seen:
                    continue
                seen.add(("event_digest", rec["job_id"]))
                scope_key = _scope_key(rec["scope"])
                row = conn.execute(
                    "SELECT state,generation,revision,input_hash,data FROM mind_event_digests "
                    "WHERE scope=? AND event_id=?",
                    (scope_key, rec["event_id"]),
                ).fetchone()
                data = json.loads(row["data"]) if row else {}
                verification = verification_by_target.get((scope_key, rec["event_id"], rec["job_id"]))
                attribution_issues = _digest_cost_identity(
                    row, data, rec, verification, require_verification=bool(verifications)
                )
                identity_matched = not attribution_issues
                receipt = data.get("model_receipt") if identity_matched else None
                receipt = receipt if isinstance(receipt, dict) else {}
                input_tokens, output_tokens, usage_status = _model_usage(receipt)
                costs.append({
                    "kind": "event_digest", "scope": rec["scope"],
                    "event_id": rec["event_id"], "job_id": rec["job_id"],
                    "state": row["state"] if row else "missing",
                    "generation": row["generation"] if row else None,
                    "digest_revision": row["revision"] if row else None,
                    "membership_input_hash": row["input_hash"] if row else None,
                    "expected_generation": rec.get("generation"),
                    "expected_digest_revision": verification.get("digest_revision") if verification else None,
                    "expected_membership_input_hash": (
                        verification.get("membership_input_hash") if verification
                        else rec.get("membership_input_hash")
                    ),
                    "digest_identity_matched": identity_matched,
                    "attribution_status": "matched" if identity_matched else "unknown-digest-identity-mismatch",
                    "attribution_issues": attribution_issues,
                    "source_versions": data.get("source_versions") if identity_matched else None,
                    "model": receipt.get("model"),
                    "reasoning": receipt.get("reasoning"),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "usage_status": usage_status,
                    "elapsed_ms": receipt.get("elapsed_ms"),
                    "method": receipt.get("method"),
                    "priced": False,
                })
    return costs


def costs_by_scope(costs) -> list[dict]:
    """Keep usage receipts partitioned by scope without turning unknown into zero."""
    grouped = {}
    for cost in costs:
        scope = cost.get("scope") or {}
        key = _scope_key(scope) if scope else "unknown"
        group = grouped.setdefault(key, {
            "scope": scope,
            "items": 0,
            "complete": 0,
            "input_tokens": None,
            "output_tokens": None,
            "token_receipts": 0,
            "usage_status": "unknown",
            "unpriced": True,
            "_unknown_model_items": 0,
            "_partial_model_items": 0,
        })
        group["items"] += 1
        group["complete"] += cost.get("state") == "complete" or (
            cost.get("kind") == "event_digest" and cost.get("state") == "ready"
        )
        if cost.get("kind") != "event_digest":
            continue
        counts = token_counts(cost)
        usage_status = cost.get("usage_status")
        if usage_status is None:
            usage_status = "reported" if counts is not None else "unknown"
        if usage_status == "reported" and counts is not None:
            if group["token_receipts"] == 0:
                group["input_tokens"] = 0
                group["output_tokens"] = 0
            group["input_tokens"] += counts[0]
            group["output_tokens"] += counts[1]
            group["token_receipts"] += 1
        else:
            group["_unknown_model_items"] += 1
            group["_partial_model_items"] += usage_status == "partial-unknown"
    result = []
    for key in sorted(grouped):
        group = grouped[key]
        if group["token_receipts"]:
            group["usage_status"] = (
                "partial-unknown" if group["_unknown_model_items"] else "reported"
            )
        elif group["_partial_model_items"]:
            group["usage_status"] = "partial-unknown"
        group.pop("_unknown_model_items")
        group.pop("_partial_model_items")
        result.append(group)
    return result


def _selection_preflight(engine, selection, command_id, vector_revisions) -> list[dict]:
    """Recheck the bound targets on the live DB immediately before any requeue."""
    issues = []
    with engine.db.connect() as conn:
        for entry in selection.get("embeds", []):
            prior = conn.execute(
                "SELECT 1 FROM job_recovery WHERE job_id=? AND command_id=? AND kind='embed'",
                (entry["job_id"], command_id),
            ).fetchone()
            if prior:
                continue
            if vector_revisions is None:
                issues.append({"kind": "embed", "job_id": entry["job_id"],
                               "reason": "vector-evidence-unavailable"})
                continue
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (entry["job_id"],)).fetchone()
            rec = conn.execute(
                "SELECT revision,status,deleted,data FROM records WHERE id=?", (entry["record_id"],)
            ).fetchone()
            payload = json.loads(job["payload"]) if job else {}
            record_scope = json.loads(rec["data"]).get("scope") if rec else None
            if (
                not job
                or job["kind"] != "embed"
                or job["state"] != "failed"
                or payload.get("record_id") != entry["record_id"]
                or payload.get("revision") != entry["target_revision"]
                or job["error"] != entry.get("reviewed_failure_error")
                or job["updated_at"] != entry.get("reviewed_failure_updated_at")
                or not rec
                or rec["deleted"]
                or rec["status"] not in CURRENT_STATUSES
                or rec["revision"] != entry["target_revision"]
                or record_scope != entry.get("scope")
                or vector_revisions.get(entry["record_id"]) == entry["target_revision"]
            ):
                issues.append({"kind": "embed", "job_id": entry["job_id"],
                               "record_id": entry["record_id"], "reason": "target-version-drift"})

        prior_digest = {}
        for old in conn.execute(
            "SELECT j.payload FROM job_recovery jr JOIN jobs j ON j.id=jr.job_id "
            "WHERE jr.command_id=? AND jr.kind='event_digest'",
            (command_id,),
        ):
            payload = json.loads(old[0])
            prior_digest[(_scope_key(payload["scope"]), payload["event_id"])] = True
        for entry in selection.get("digests", []):
            scope_key = _scope_key(entry["scope"])
            if prior_digest.get((scope_key, entry["event_id"])):
                continue
            row = conn.execute(
                "SELECT state,generation,revision FROM mind_event_digests WHERE scope=? AND event_id=?",
                (scope_key, entry["event_id"]),
            ).fetchone()
            node = conn.execute(
                "SELECT state FROM mind_graph_nodes WHERE scope=? AND id=?",
                (scope_key, entry["event_id"]),
            ).fetchone()
            try:
                evidence = _digest_review_evidence(engine, entry, conn)
            except (Conflict, Missing, KeyError, TypeError, ValueError) as exc:
                # A missing/moved graph is a drift receipt, not a partial apply.
                issues.append({"kind": "event_digest", "scope": entry["scope"],
                               "event_id": entry["event_id"], "reason": type(exc).__name__})
                continue
            expected = {key: entry.get(key) for key in (
                "event_revision", "membership_input_hash", "member_versions",
                "missing_members", "excluded_members",
            )}
            if (
                not row
                or row["state"] != "failed"
                or row["generation"] != entry["generation"]
                or row["revision"] != entry["digest_revision"]
                or not node
                or node["state"] != "active"
                or evidence != expected
            ):
                issues.append({"kind": "event_digest", "scope": entry["scope"],
                               "event_id": entry["event_id"],
                               "reason": "reviewed-generation-or-membership-moved"})
    return issues


def _verify_digest_results(engine, entries, recovered) -> list[dict]:
    expected = {(_scope_key(e["scope"]), e["event_id"]): e for e in entries}
    receipts = []
    with engine.db.connect() as conn:
        for item in recovered:
            scope_key = _scope_key(item["scope"])
            target = expected[(scope_key, item["event_id"])]
            row = conn.execute(
                "SELECT state,generation,revision,input_hash,data FROM mind_event_digests "
                "WHERE scope=? AND event_id=?",
                (scope_key, item["event_id"]),
            ).fetchone()
            data = json.loads(row["data"]) if row else {}
            active_versions = {
                member["record_id"]: member["revision"]
                for member in target["member_versions"]
                if member["active_for_digest"]
            }
            ok = bool(
                row
                and row["state"] == "ready"
                and row["generation"] == item["generation"]
                and row["input_hash"] == target["membership_input_hash"]
                and data.get("source_versions") == active_versions
                and isinstance(data.get("model_receipt"), dict)
                and data.get("model_receipt")
            )
            receipts.append({
                "scope": item["scope"], "event_id": item["event_id"],
                "job_id": item["job_id"], "generation": item["generation"],
                "state": row["state"] if row else "missing",
                "digest_revision": row["revision"] if row else None,
                "membership_input_hash": row["input_hash"] if row else None,
                "source_versions": data.get("source_versions"), "verified": ok,
            })
    return receipts


class Runner:
    """Batch discipline for an apply run: small batches, environmental stop with
    backoff, foreground preemption, per-item outcomes.

    The runner only requeues; execution belongs to whatever `Worker` is live
    (the service's own loop in production, a test double here). `settle` waits
    for a batch's jobs to reach a terminal state and returns their outcomes;
    `sleep` and `probe` are injectable so tests never wait wall time.
    `probe` answers "is the shared dependency healthy right now" (embedding
    endpoint, model reachability). An environmental failure always backs off and
    probes once for evidence, then stops; health never authorizes a larger batch."""

    def __init__(self, engine, *, embed_batch=3, digest_batch=1, probe=None,
                 vector_probe=None, backoff=(120.0, 600.0, 1800.0),
                 sleep=time.sleep, poll=5.0, command_lease_seconds=300.0,
                 owner=None):
        self.engine = engine
        if embed_batch < 0 or digest_batch < 0:
            raise ValueError("Recovery batch limits must be non-negative")
        if not 15 <= command_lease_seconds <= 3600:
            raise ValueError("Recovery command lease must be between 15 and 3600 seconds")
        self.embed_batch, self.digest_batch = embed_batch, digest_batch
        self.probe, self.vector_probe = probe, vector_probe
        self.backoff, self.sleep, self.poll = tuple(backoff), sleep, poll
        self.command_lease_seconds = command_lease_seconds
        self.owner = owner or uuid.uuid4().hex
        self.log = []

    def foreground_active(self) -> bool:
        if self.engine.interactive_until > time.monotonic():
            return True
        with self.engine.db.connect() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='mind_foreground_leases'"
            ).fetchone():
                return False
            return bool(conn.execute(
                "SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1",
                (time.time(),),
            ).fetchone())

    def settle(self, job_ids, *, timeout=900.0):
        """Default: poll the queue until every job is terminal or time runs out."""
        if not job_ids:
            return []
        deadline = time.monotonic() + timeout
        pending = set(job_ids)
        while pending and time.monotonic() < deadline:
            with self.engine.db.connect() as conn:
                rows = conn.execute(
                    f"SELECT id,state,error,updated_at FROM jobs WHERE id IN ({','.join('?' for _ in pending)})",
                    tuple(pending),
                ).fetchall()
            done = {r["id"] for r in rows if r["state"] in ("complete", "failed", "canceled")}
            if done == pending:
                break
            pending -= done
            self.sleep(self.poll)
        with self.engine.db.connect() as conn:
            return [
                dict(zip(("id", "state", "error", "updated_at"), r))
                for r in conn.execute(
                    f"SELECT id,state,error,updated_at FROM jobs WHERE id IN ({','.join('?' for _ in job_ids)})",
                    tuple(job_ids),
                ).fetchall()
            ]

    def settle_with_command_lease(self, command_id, fence, settle, job_ids):
        """Keep the command fence alive while an injected/default settle blocks."""
        done, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not done.wait(max(0.05, self.command_lease_seconds / 3)):
                if not _renew_recovery_run(
                    self.engine, command_id, self.owner, fence, self.command_lease_seconds
                ):
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            outcomes = settle(job_ids) if job_ids else []
        finally:
            done.set()
            thread.join(timeout=1)
        return outcomes, lost.is_set()

    def run(self, *, command_id, selection=None, manifest_sha256=None, settle=None,
            max_cycles=200, vector_revisions=None, vector_evidence=None,
            allow_unreviewed=False) -> dict:
        settle = settle or self.settle
        persisted = False
        if selection is None:
            if not allow_unreviewed:
                raise ValueError("Runner requires an explicit reviewed selection")
            with self.engine.db.connect() as conn:
                current = plan(conn, {} if vector_revisions is None else vector_revisions)
            selection = {
                "review_fingerprint": "unreviewed-test-only",
                "embeds": [e for e in current["embeds"] if e["action"] == "recover"],
                "digests": attach_digest_review_evidence(
                    self.engine, [e for e in current["digests"] if e["action"] == "recover"]
                ),
            }
            inspection_counts = current["counts"]
        else:
            if not manifest_sha256:
                raise ValueError("Runner requires the reviewed manifest sha256")
            if len(selection.get("embeds", [])) > self.embed_batch or len(selection.get("digests", [])) > self.digest_batch:
                raise ValueError("Reviewed selection exceeds this command's batch limits")
            if selection.get("embeds") and (vector_revisions is None or self.vector_probe is None):
                raise ValueError("Reviewed embed selection requires current and post-run vector evidence")
            bound = bind_recovery_selection(
                self.engine, command_id=command_id, manifest_sha256=manifest_sha256,
                selection=selection, embed_limit=self.embed_batch, digest_limit=self.digest_batch,
                owner=self.owner, lease_seconds=self.command_lease_seconds,
            )
            if bound["receipt"] is not None:
                receipt = dict(bound["receipt"])
                receipt["idempotent_replay"] = True
                return receipt
            if not bound["claimed"]:
                return {
                    "kind": "w2-derivative-recovery-apply-status",
                    "command_id": command_id,
                    "selection_digest": bound["selection_digest"],
                    "state": "running",
                    "lease_until": bound.get("lease_until"),
                    "fence": bound.get("fence"),
                    "stopped": "command-running",
                    "blocked": ["the same immutable command selection is already leased"],
                    "idempotent_replay": True,
                }
            selection = bound["selection"]
            persisted = True
            command_fence = bound["fence"]
            inspection_counts = {
                "embed_selected": len(selection.get("embeds", [])),
                "digest_selected": len(selection.get("digests", [])),
            }
        embeds = list(selection.get("embeds", []))
        digests = list(selection.get("digests", []))
        result = {
            "kind": "w2-derivative-recovery-apply",
            "command_id": command_id,
            "manifest_sha256": manifest_sha256,
            "selection_digest": digest(selection),
            "selected": {
                "embeds": [
                    {key: e.get(key) for key in ("job_id", "record_id", "target_revision", "scope")}
                    for e in embeds
                ],
                "digests": [
                    {key: e.get(key) for key in ("scope", "event_id", "generation", "digest_revision", "membership_input_hash")}
                    for e in digests
                ],
            },
            "vector_evidence": vector_evidence,
            "selection_review": selection.get("selection_review"),
            "preapply_snapshot": selection.get("preapply_snapshot"),
            "embedding_service": selection.get("embedding_service"),
            "cycles": [], "costs": [], "costs_by_scope": [],
            "blocked": [], "stopped": None, "inspection_counts": inspection_counts,
            "idempotent_replay": False,
            "command_lease": {"fence": command_fence if persisted else None,
                              "ttl_seconds": self.command_lease_seconds if persisted else None},
        }
        active_vector_revisions = vector_revisions
        preflight_probe_issue = []
        if embeds and not allow_unreviewed:
            try:
                preflight_vectors = self.vector_probe([entry["record_id"] for entry in embeds])
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                preflight_vectors = {"probed": False, "reason": type(exc).__name__}
            result["preflight_vector_evidence"] = {
                key: value for key, value in preflight_vectors.items() if key != "revisions"
            }
            if preflight_vectors.get("probed"):
                active_vector_revisions = preflight_vectors.get("revisions", {})
            else:
                active_vector_revisions = None
                preflight_probe_issue.append({
                    "kind": "embed", "reason": "vector-evidence-unavailable-at-live-preflight",
                    "detail": preflight_vectors.get("reason"),
                })
        preflight = [] if allow_unreviewed else (
            preflight_probe_issue
            + _selection_preflight(self.engine, selection, command_id, active_vector_revisions)
        )
        result["preflight"] = {"ok": not preflight, "issues": preflight}
        if preflight:
            result["stopped"] = "preflight-drift"
            result["blocked"] = preflight
            return _finish_recovery_run(
                self.engine, command_id, result, owner=self.owner, fence=command_fence
            ) if persisted else result

        cycle = 0
        preempted = 0
        while (embeds or digests) and cycle < max_cycles:
            if self.foreground_active():
                # User work owns the store; recovery waits without consuming a batch.
                if persisted and not _renew_recovery_run(
                    self.engine, command_id, self.owner, command_fence,
                    self.command_lease_seconds,
                ):
                    result["stopped"] = "command-lease-lost"
                    break
                preempted += 1
                self.log.append({"cycle": cycle + 1, "state": "preempted"})
                if preempted > max_cycles * 4:
                    result["stopped"] = "preempted"
                    break
                self.sleep(self.poll)
                continue
            cycle += 1
            e_batch, embeds = embeds[: self.embed_batch], embeds[self.embed_batch :]
            d_batch, digests = digests[: self.digest_batch], digests[self.digest_batch :]
            applied_e = recover_embeddings(
                self.engine, e_batch, command_id=command_id,
                vector_revisions=active_vector_revisions,
                require_vector_evidence=not allow_unreviewed,
            ) if e_batch else {"recovered": [], "replayed": [], "skipped": [],
                               "abandoned": [], "targets": []}
            # A raced embed target stops this batch before any digest is expanded.
            if applied_e["abandoned"] or applied_e["skipped"]:
                applied_d = {"recovered": [], "replayed": [], "skipped": [],
                             "abandoned": [], "command_id": command_id}
            else:
                applied_d = recover_digests(
                    self.engine, d_batch, command_id=command_id,
                    require_reviewed=not allow_unreviewed,
                ) if d_batch else {"recovered": [], "replayed": [], "skipped": [],
                                    "abandoned": []}
            digest_jobs = applied_d["recovered"] + applied_d["replayed"]
            job_ids = list(dict.fromkeys(
                applied_e["recovered"] + applied_e["replayed"]
                + [r["job_id"] for r in digest_jobs]
            ))
            if persisted:
                outcomes, lease_lost = self.settle_with_command_lease(
                    command_id, command_fence, settle, job_ids
                )
            else:
                outcomes, lease_lost = (settle(job_ids) if job_ids else []), False
            by_id = {outcome["id"]: outcome for outcome in outcomes}
            missing = [job_id for job_id in job_ids if job_id not in by_id]
            nonterminal = [o for o in outcomes if o["state"] not in TERMINAL_JOB_STATES]
            failures = [o for o in outcomes if o["state"] in {"failed", "canceled"}]
            environmental = [o for o in outcomes
                             if o["state"] == "failed" and (o["error"] or "") in ENVIRONMENTAL_ERRORS]
            unexpected_errors = []
            embed_errors = {e["job_id"]: {e.get("reviewed_failure_error")} for e in e_batch}
            digest_errors = {
                item["job_id"]: set(next(
                    e.get("error_classes", []) for e in d_batch
                    if _scope_key(e["scope"]) == _scope_key(item["scope"]) and e["event_id"] == item["event_id"]
                ))
                for item in digest_jobs
            }
            allowed_errors = embed_errors | digest_errors
            for failed in failures:
                if failed.get("error") not in allowed_errors.get(failed["id"], set()):
                    unexpected_errors.append({"job_id": failed["id"], "error": failed.get("error")})
            cycle_receipt = {
                "cycle": cycle,
                "embeds": applied_e, "digests": applied_d,
                "outcomes": outcomes,
                "environmental": [o["id"] for o in environmental],
                "unexpected_failure_errors": unexpected_errors,
                "missing_outcomes": missing,
                "nonterminal": [o["id"] for o in nonterminal],
                "command_lease_lost": lease_lost,
            }
            result["cycles"].append(cycle_receipt)

            if lease_lost:
                result["stopped"] = "command-lease-lost"
                result["blocked"].extend(job_ids)
                break
            raced = (applied_e.get("abandoned", []) + applied_e.get("skipped", [])
                     + applied_d.get("abandoned", []) + applied_d.get("skipped", []))
            if raced:
                result["stopped"] = "target-drift"
                result["blocked"].extend(raced)
                break
            if missing:
                result["stopped"] = "settle-incomplete"
                result["blocked"].extend(missing)
                break
            if nonterminal:
                result["stopped"] = "settle-timeout"
                result["blocked"].extend(o["id"] for o in nonterminal)
                break
            if failures:
                result["blocked"].extend(o["id"] for o in failures)
                if environmental:
                    # Back off once for shared-dependency evidence, then stop. A
                    # healthy probe does not authorize expanding to another target.
                    self.sleep(self.backoff[0])
                    cycle_receipt["health_after_backoff"] = bool(self.probe and self.probe())
                    result["stopped"] = "environmental"
                else:
                    result["stopped"] = "job-failure"
                break

            if e_batch and not allow_unreviewed:
                vector_check = self.vector_probe([entry["record_id"] for entry in e_batch])
                cycle_receipt["vector_verification"] = vector_check
                expected = {entry["record_id"]: entry["target_revision"] for entry in e_batch}
                if (
                    not vector_check.get("probed")
                    or any(vector_check.get("revisions", {}).get(rid) != revision
                           for rid, revision in expected.items())
                ):
                    result["stopped"] = "vector-verification"
                    result["blocked"].extend(
                        {"record_id": rid, "target_revision": revision}
                        for rid, revision in expected.items()
                        if vector_check.get("revisions", {}).get(rid) != revision
                    )
                    break
            if digest_jobs and not allow_unreviewed:
                digest_check = _verify_digest_results(self.engine, d_batch, digest_jobs)
                cycle_receipt["digest_verification"] = digest_check
                if any(not item["verified"] for item in digest_check):
                    result["stopped"] = "digest-verification"
                    result["blocked"].extend(item for item in digest_check if not item["verified"])
                    break
        if result["stopped"] is not None:
            result["blocked"].extend(e["job_id"] for e in embeds)
            result["blocked"].extend(
                {"scope": d["scope"], "event_id": d["event_id"]} for d in digests
            )
        if result["stopped"] is None and (embeds or digests):
            result["stopped"] = "cycle-limit"
            result["blocked"].extend(e["job_id"] for e in embeds)
            result["blocked"].extend({"scope": d["scope"], "event_id": d["event_id"]} for d in digests)
        result["costs"] = collect_costs(self.engine, result)
        result["costs_by_scope"] = costs_by_scope(result["costs"])
        if persisted:
            return _finish_recovery_run(
                self.engine, command_id, result, owner=self.owner, fence=command_fence
            )
        return result
