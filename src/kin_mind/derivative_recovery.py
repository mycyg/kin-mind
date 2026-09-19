"""Targeted recovery of missing derived state: vectors and event digests.

`Worker.claim` never takes `failed` rows, so a record whose embed job exhausted its
attempts keeps its canonical text but loses the derived vector, and an event whose
digest job failed keeps a `failed` digest that the scheduler will not pick up again
(`schedule` only looks at `dirty`). The originals are intact; what is missing is
derived and reproducible.

Everything here enumerates the gap fresh from the tables at call time. Recovery
moves work through the queue's own paths — `Worker.recover` for embeds, `mark_dirty`
plus the ordinary `event_digest` job for digests — so the existing revision guards,
the single-generation-per-event identity and the foreground preemption in `claim`
stay in force. Canonical state (records, revisions, graph nodes) is only read here,
never written: a corrected, deleted or superseded target is skipped, not resurrected.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from eventmem.core.db import digest, dumps
from eventmem.core.jobs import Worker
from eventmem.core.models import Scope, now

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
        if vector_revisions is not None and vector_revisions.get(rid) == revision:
            entries.append(entry | {
                "action": "skip",
                "skip_reason": "vector-present",
                "reason": "A vector at the current revision already exists; the gap healed itself",
            })
            continue
        entry["vector_at_target"] = (
            None if vector_revisions is None else rid in vector_revisions
        )
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
            "digest_failed_jobs": sum(len(e["failed_job_ids"]) for e in digests),
            "digest_events": len(digests),
            "digest_recover": sum(1 for e in digests if e["action"] == "recover"),
            "digest_skip": sum(1 for e in digests if e["action"] == "skip"),
        },
    }


def recover_embeddings(engine, entries, *, command_id) -> dict:
    """Requeue the failed embed jobs whose target is still the current version.

    The revision check is repeated inside the write transaction: a record that
    moved since inspection abandons its entry here, and even a raced requeue is
    harmless because `Worker.prepare` no-ops an embed whose revision moved on."""
    todo, abandoned = [], []
    with engine.db.connect() as conn:
        for entry in entries:
            if entry.get("action") != "recover":
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
            todo.append(entry["job_id"])
    result = {"recovered": [], "skipped": [], "abandoned": abandoned, "command_id": command_id}
    for page in range(0, len(todo), 50):
        batch = Worker(engine).recover(todo[page : page + 50], command_id=command_id)
        result["recovered"].extend(batch["recovered"])
        result["skipped"].extend(batch["skipped"])
    return result


def recover_digests(engine, entries, *, command_id, at=None) -> dict:
    """Return failed digests to the normal dirty path at a fresh generation.

    `mark_dirty` bumps the generation, so the enqueued job gets a new identity and
    the old failed jobs stay as linked history. The digest row is re-checked inside
    the transaction: an event that turned `ready` or `dirty` since inspection is
    left exactly as it is."""
    at = at or now()
    recovered, skipped = [], []
    with engine.db.connect(write=True) as conn:
        for entry in entries:
            if entry.get("action") != "recover":
                continue
            scope_key = _scope_key(entry["scope"])
            event_id = entry["event_id"]
            row = conn.execute(
                "SELECT state,generation,data FROM mind_event_digests WHERE scope=? AND event_id=?",
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
            recovered.append({"event_id": event_id, "job_id": jid, "generation": generation})
    return {"recovered": recovered, "skipped": skipped, "command_id": command_id}


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

    costs = []
    with engine.db.connect() as conn:
        for cycle in result["cycles"]:
            for job_id in cycle["embeds"].get("recovered", []):
                job = conn.execute(
                    "SELECT state,error,updated_at FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                audit = conn.execute(
                    "SELECT recovered_at FROM job_recovery WHERE job_id=? AND command_id=?",
                    (job_id, result["command_id"]),
                ).fetchone()
                costs.append({
                    "kind": "embed", "job_id": job_id,
                    "state": job["state"] if job else "missing",
                    "error": job["error"] if job else None,
                    "wall_seconds": seconds_between(audit["recovered_at"], job["updated_at"]) if job and audit else None,
                    "usage_status": "unknown", "priced": False,
                })
            for rec in cycle["digests"].get("recovered", []):
                row = conn.execute(
                    "SELECT state,revision,data FROM mind_event_digests WHERE event_id=?",
                    (rec["event_id"],),
                ).fetchone()
                receipt = json.loads(row["data"]).get("model_receipt", {}) if row and row["state"] == "ready" else {}
                costs.append({
                    "kind": "event_digest", "event_id": rec["event_id"], "job_id": rec["job_id"],
                    "state": row["state"] if row else "missing",
                    "digest_revision": row["revision"] if row else None,
                    "model": receipt.get("model"),
                    "reasoning": receipt.get("reasoning"),
                    "input_tokens": receipt.get("input_tokens"),
                    "output_tokens": receipt.get("output_tokens"),
                    "elapsed_ms": receipt.get("elapsed_ms"),
                    "method": receipt.get("method"),
                    "priced": False,
                })
    return costs


class Runner:
    """Batch discipline for an apply run: small batches, environmental stop with
    backoff, foreground preemption, per-item outcomes.

    The runner only requeues; execution belongs to whatever `Worker` is live
    (the service's own loop in production, a test double here). `settle` waits
    for a batch's jobs to reach a terminal state and returns their outcomes;
    `sleep` and `probe` are injectable so tests never wait wall time.
    `probe` answers "is the shared dependency healthy right now" (embedding
    endpoint, model reachability). An environmental failure always backs off once;
    the run continues only if the probe says healthy — without a probe it stops."""

    def __init__(self, engine, *, embed_batch=3, digest_batch=1, probe=None,
                 backoff=(120.0, 600.0, 1800.0), sleep=time.sleep, poll=5.0):
        self.engine = engine
        self.embed_batch, self.digest_batch = max(1, embed_batch), max(1, digest_batch)
        self.probe, self.backoff, self.sleep, self.poll = probe, tuple(backoff), sleep, poll
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

    def run(self, *, command_id, settle=None, max_cycles=200, vector_revisions=None) -> dict:
        settle = settle or self.settle
        with self.engine.db.connect() as conn:
            current = plan(conn, vector_revisions)
        embeds = [e for e in current["embeds"] if e["action"] == "recover"]
        digests = [e for e in current["digests"] if e["action"] == "recover"]
        result = {"command_id": command_id, "cycles": [], "costs": [],
                  "blocked": [], "stopped": None, "inspection_counts": current["counts"]}
        backoff_step = 0
        cycle = 0
        preempted = 0
        while (embeds or digests) and cycle < max_cycles:
            if self.foreground_active():
                # User work owns the store; recovery waits without consuming a batch.
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
            applied_e = recover_embeddings(self.engine, e_batch, command_id=command_id) if e_batch else {"recovered": [], "abandoned": []}
            applied_d = recover_digests(self.engine, d_batch, command_id=command_id) if d_batch else {"recovered": [], "skipped": []}
            job_ids = applied_e["recovered"] + [r["job_id"] for r in applied_d["recovered"]]
            outcomes = settle(job_ids) if job_ids else []
            environmental = [o for o in outcomes
                             if o["state"] == "failed" and (o["error"] or "") in ENVIRONMENTAL_ERRORS]
            result["cycles"].append({
                "cycle": cycle,
                "embeds": applied_e, "digests": applied_d,
                "outcomes": outcomes,
                "environmental": [o["id"] for o in environmental],
            })
            if environmental:
                # The shared dependency is down for everyone, not just these items:
                # stop rather than burn each remaining item's own retry budget.
                # Failed-again items keep their audit trail and go back to review.
                result["blocked"].extend(o["id"] for o in environmental)
                self.sleep(self.backoff[min(backoff_step, len(self.backoff) - 1)])
                healthy = bool(self.probe and self.probe())
                if not healthy:
                    result["stopped"] = "environmental"
                    result["blocked"].extend(e["job_id"] for e in embeds)
                    result["blocked"].extend(d["event_id"] for d in digests)
                    break
                backoff_step += 1
            else:
                backoff_step = 0
        if result["stopped"] is None and (embeds or digests):
            result["stopped"] = "cycle-limit"
        result["costs"] = collect_costs(self.engine, result)
        return result
