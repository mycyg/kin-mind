"""Idempotent operational-lane migration, after the owning workers stop.

"After they stop" used to mean whatever the caller asserted in `workers_stopped`.
The parameter is still accepted, so every existing caller keeps working, but with
`liveness_checks` on it decides nothing: the lease ledger is asked instead. The two
commands that release every `running` row refuse while one of those rows is still
leased, and the historical resume, which only ever touched quarantined rows that no
worker can hold, asks for nothing at all.
"""
import json
import re
import time

from eventmem.core.db import Conflict, Missing, digest, dumps

from . import liveness
from .appraisal import queue_row
from .memory import MemoryContinuity
from .model_lanes import WAIT_CAPACITY, WAIT_FOREGROUND, WAIT_LEDGER, WAIT_USER_WORK

# Every reason a model admission turned a job away: waits, never failures of the job (E2-03).
ADMISSION_WAITS = frozenset({WAIT_CAPACITY, WAIT_FOREGROUND, WAIT_LEDGER, WAIT_USER_WORK})

# Retry bookkeeping an approved resume gives back, preserved in recovery_history.
RETRY_COUNTERS = ("error_signature", "error_repeats", "compression_waits", "compression_stalls",
                  "compression_parts", "compression_calls", "transient_failures", "admission_waits",
                  "light_attempts", "deletion_refusals")
# What a conflict left for the next attempt to reuse or revalidate. A resumed row is judged afresh.
REUSE_FIELDS = ("reuse", "tier", "revalidation")


def migrate_operational(mind, *, workers_stopped):
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    from .appraisal import Appraisals
    jobs = Appraisals(mind)
    memory = MemoryContinuity(mind)
    name = "operational-lanes-v1"
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            return json.loads(previous[0])
        if evidence:
            # This releases every `running` row below, so one still under lease is
            # an evaluation this command would interrupt. A replay is already past
            # this point: it returned the recorded receipt and writes nothing.
            liveness.refuse_while_appraisal_leased(conn, mind.scope.key())
        config = memory.settings(conn) | {"operational_lanes": True}
        conn.execute("INSERT OR REPLACE INTO mind_memory_config VALUES(?,?)", (mind.scope.key(), dumps(config)))
        # Collapse overdue clock wakeups into one current judgment. Their old
        # evidence, errors and results remain readable, never replayed as chat.
        old = conn.execute("SELECT id,data FROM mind_action_events WHERE scope=? AND kind='idle-review' AND state IN ('pending','queued')", (mind.scope.key(),)).fetchall()
        superseded = []
        for row in old:
            item = json.loads(row["data"])
            conn.execute("UPDATE mind_action_events SET state='superseded' WHERE id=?", (row["id"],))
            if item.get("job_id"):
                job = conn.execute("SELECT data FROM mind_appraisals WHERE id=? AND state IN ('pending','running')", (item["job_id"],)).fetchone()
                conn.execute("UPDATE mind_appraisals SET state='superseded',lease=0 WHERE id=? AND state IN ('pending','running')", (item["job_id"],))
                if job:
                    # A superseded parent must release what it had absorbed.
                    jobs._settle_children(conn, item["job_id"], json.loads(job[0]), "superseded")
            superseded.append(row["id"])
        released = conn.execute("UPDATE mind_appraisals SET state='pending',lease=0,available=? WHERE scope=? AND state='running'", (time.time(), mind.scope.key())).rowcount
        conn.execute("INSERT INTO mind_action_schedule VALUES(?,?,0,?) ON CONFLICT(scope) DO UPDATE SET next_review=excluded.next_review,data=excluded.data",
                     (mind.scope.key(), mind.clock(), dumps({"reason": "recovery-current-state-review", "migration": name})))
        result = {"state": "migrated", "at": mind.clock(), "superseded_idle_events": superseded, "released_terminated_leases": released}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def recover_history(mind, *, job_ids, command_id, source, workers_stopped, replacements=None, admission_only=False):
    """Resume approved historical jobs, preserving failures and original IDs.

    A resumed job is judged afresh: no stored or supplied proposal is replayed as its result.
    Seeding used to accept only proposals made by one model name, which no longer matched the
    model background work runs on, and a stored proposal of an older shape failed the whole
    batch (K4-06). The stored proposal stays readable in recovery_history.
    This operation neither writes emotions nor submits a chat/send operation.

    It touches `needs-repair` rows only. A quarantined row is held by no worker and
    carries no lease, so there is nothing here for a shutdown to protect: with
    `liveness_checks` on, `workers_stopped` is accepted and ignored, exactly as
    `recover_quarantined` has always worked.
    """
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    if not command_id or not source or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
        raise ValueError("Recovery requires a sourced command and unique bounded jobs")
    from .appraisal import Appraisals
    Appraisals(mind)
    if replacements:
        raise ValueError("A resumed job is judged afresh; replacement proposals are not replayed")
    replacements = {}
    name = "history-recovery:" + command_id
    fingerprint = digest([job_ids, source, replacements, *([True] if admission_only else [])])
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            result = json.loads(previous[0])
            if result["fingerprint"] != fingerprint:
                raise Conflict("Recovery command belongs to another batch")
            return result
        resumed, completed = [], []
        for identifier in job_ids:
            row = conn.execute("SELECT * FROM mind_appraisals WHERE id=? AND scope=?", (identifier, mind.scope.key())).fetchone()
            if not row:
                raise Missing(identifier)
            data = json.loads(row["data"])
            if data.get("stimulus") not in {"memory-backfill", "memory-enrichment"}:
                raise Conflict("Recovery is limited to historical enrichment")
            if row["state"] == "complete":
                completed.append(identifier)
                continue
            if row["state"] != "needs-repair":
                raise Conflict("Only quarantined historical jobs can be resumed")
            if admission_only and data.get("error") not in ADMISSION_WAITS:
                raise Conflict("Recovery is limited to the approved admission waits")
            data.setdefault("recovery_history", []).append({"command_id": command_id, "source": source, "at": mind.clock(),
                "attempts": row["attempts"], "error": data.get("error"), "proposed_result": data.get("proposed_result"), "receipt": data.get("receipt")})
            data.pop("seed_manifest", None)
            data["seed_rejected"] = True
            if admission_only:
                # Previous usage and proposals stay in recovery_history. Refresh
                # source/configuration context; never replay an unrelated proposal.
                for field in ("error", "error_detail", "repair_reason", "receipt", "proposed_result",
                              "seed_memory", "seed_receipt", "seed_sources", "seed_manifest", "seed_tombstone_mark"):
                    data.pop(field, None)
                data["waiting_reason"] = "admission-recovered-current-review"
            data.pop("frozen_memory_context", None)
            # An approved resume restores the whole retry budget, like attempts=0.
            for field in (*RETRY_COUNTERS, *REUSE_FIELDS):
                data.pop(field, None)
            conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                         (time.time(), queue_row(conn, mind.scope.key(), data), identifier))
            resumed.append(identifier)
        result = {"state": "resumed", "resumed": resumed, "already_complete": completed, "at": mind.clock(), "fingerprint": fingerprint}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def resume_compaction_waits(conn, at):
    """Give back to the queue what history compaction alone set aside (K3-14).

    While `history_compaction_active` is set no revision can be written, so an assessment that
    reached its commit then was refused there, and the same refusal twice quarantined it; nothing
    about the assessment was at fault. When the marker is lifted each such row is pending again,
    judged afresh as after an operator's resume, with the refusal kept in recovery_history. Runs
    inside the transaction that lifts the marker."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_appraisals'").fetchone():
        return []
    from .history import COMPACTING
    resumed = []
    for row in conn.execute("SELECT id,scope,attempts,data FROM mind_appraisals WHERE state='needs-repair' "
                            "AND json_extract(data,'$.error_detail.code')=?", (COMPACTING,)).fetchall():
        data = json.loads(row["data"])
        data.setdefault("recovery_history", []).append({
            "command_id": "history-compaction-finished", "source": "host", "at": at, "attempts": row["attempts"],
            **{field: data.get(field) for field in ("error", "error_detail", "repair_reason")},
            **{field: data[field] for field in RETRY_COUNTERS if field in data}})
        for field in ("error", "error_detail", "repair_reason", "waiting_reason",
                      "frozen_memory_context", *RETRY_COUNTERS, *REUSE_FIELDS):
            data.pop(field, None)
        conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                     (time.time(), queue_row(conn, row["scope"], data), row["id"]))
        resumed.append(row["id"])
    return resumed


def recover_quarantined(mind, *, job_ids, command_id, source, retire=False):
    """Resume quarantined appraisals of any lane. Each one is judged afresh.

    A quarantined row is held by no worker (state `needs-repair`, no lease), so
    this needs no worker shutdown: one write transaction returns the batch to the
    queue and the claim query takes it from there atomically. The failure stays
    readable in `recovery_history`; no stored proposal is replayed as a seed and
    no model is called here.

    With `retire`, the rows end instead of running again: each becomes `superseded`, the
    existing terminal state, for a moment that has passed or evidence that is gone, where a
    resume would only pay to be quarantined again. The reason stays in recovery_history and
    what a row had absorbed is released as for any superseded row (K4-06).
    """
    if not command_id or not source or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
        raise ValueError("Recovery requires a sourced command and unique bounded jobs")
    from .appraisal import Appraisals
    jobs = Appraisals(mind)
    name = ("quarantine-retire:" if retire else "quarantine-recovery:") + command_id
    fingerprint = digest([job_ids, source, *(["retire"] if retire else [])])
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            result = json.loads(previous[0])
            if result["fingerprint"] != fingerprint:
                raise Conflict("Recovery command belongs to another batch")
            return result
        resumed, completed = [], []
        for identifier in job_ids:
            row = conn.execute("SELECT * FROM mind_appraisals WHERE id=? AND scope=?", (identifier, mind.scope.key())).fetchone()
            if not row:
                raise Missing(identifier)
            data = json.loads(row["data"])
            if row["state"] == "complete":
                completed.append(identifier)
                continue
            if row["state"] != "needs-repair":
                raise Conflict("Only a quarantined appraisal can be resumed")
            data.setdefault("recovery_history", []).append({
                "command_id": command_id, "source": source, "at": mind.clock(), "attempts": row["attempts"],
                **{field: data.get(field) for field in ("error", "error_detail", "repair_reason",
                                                        "proposed_result", "receipt", "failed_call_receipt")},
                **{field: data[field] for field in RETRY_COUNTERS if field in data}})
            if retire:
                data["recovery_history"][-1]["retired"] = True
                data["retired_reason"] = data.get("repair_reason") or data.get("error") or "retired-by-operator"
                conn.execute("UPDATE mind_appraisals SET state='superseded',lease=0,data=? WHERE id=?",
                             (queue_row(conn, mind.scope.key(), data), identifier))
                jobs._settle_children(conn, identifier, data, "superseded")
                resumed.append(identifier)
                continue
            if data.get("seed_memory"):
                # The stored proposal stays audit data; this attempt judges again.
                data["seed_rejected"] = True
            for field in ("error", "error_detail", "repair_reason", "waiting_reason",
                          "frozen_memory_context", *RETRY_COUNTERS, *REUSE_FIELDS):
                data.pop(field, None)
            conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                         (time.time(), queue_row(conn, mind.scope.key(), data), identifier))
            resumed.append(identifier)
        result = {"state": "retired" if retire else "resumed", "retired" if retire else "resumed": resumed,
                  "already_complete": completed, "at": mind.clock(), "fingerprint": fingerprint}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


def _committed_ancestor(conn, mind, child_id, parents):
    """The nearest ancestor of a batched job whose commit receipt really exists."""
    seen, current = {child_id}, parents.get(child_id)
    while current and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT data FROM mind_appraisals WHERE id=? AND scope=?", (current, mind.scope.key())).fetchone()
        receipt = conn.execute("SELECT result FROM commands WHERE id=?", (mind._key(current),)).fetchone()
        event_id = json.loads(receipt[0]).get("event_id") if receipt else None
        if row and event_id and conn.execute("SELECT 1 FROM mind_events WHERE id=? AND scope=?", (event_id, mind.scope.key())).fetchone():
            return current, event_id, json.loads(row[0])
        current = parents.get(current)
    return None, None, {}


def _source_committed(conn, mind, source_id):
    """Presence in the source index is not proof; its event must exist."""
    row = conn.execute("SELECT event_id FROM mind_semantic_sources WHERE scope=? AND source_id=?", (mind.scope.key(), source_id)).fetchone()
    return bool(row and conn.execute("SELECT 1 FROM mind_events WHERE id=? AND scope=?", (row[0], mind.scope.key())).fetchone())


def recover_batched(mind, *, command_id, workers_stopped):
    """Settle the jobs an interrupted parent left batched. One shot, idempotent.

    A child is completed only when a really committed ancestor evaluated its
    exact source version; everything else returns to the queue for a new
    judgment. This operation calls no model and writes no memory of its own.
    """
    with mind.engine.db.connect() as conn:
        evidence = liveness.checks_enabled(conn, mind.scope.key())
    if not evidence and workers_stopped is not True:
        raise ValueError("Verify termination of the owning workers first")
    if not command_id:
        raise ValueError("Recovery requires a sourced command")
    from .appraisal import Appraisals
    Appraisals(mind)
    name = "batched-recovery:" + command_id
    with mind.engine.db.connect(write=True) as conn:
        previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (mind.scope.key(), name)).fetchone()
        if previous:
            return json.loads(previous[0])
        if evidence:
            # A `batched` child is settled against its parent's committed work. A
            # parent still under lease may yet commit, so its children are not
            # orphans yet and this command would decide their fate too early.
            liveness.refuse_while_appraisal_leased(conn, mind.scope.key())
        parents = {}
        for row in conn.execute("SELECT id,data FROM mind_appraisals WHERE scope=? AND json_extract(data,'$.batch_ids') IS NOT NULL", (mind.scope.key(),)).fetchall():
            for child_id in json.loads(row["data"]).get("batch_ids", []):
                parents.setdefault(child_id, row["id"])
        settled = []
        for row in conn.execute("SELECT id,data FROM mind_appraisals WHERE scope=? AND state='batched' ORDER BY id", (mind.scope.key(),)).fetchall():
            data = json.loads(row["data"])
            ancestor, event_id, ancestor_data = _committed_ancestor(conn, mind, row["id"], parents)
            try:
                refs = mind._evidence(conn, data.get("evidence_ids", [])) if ancestor else []
            except (Conflict, Missing):
                refs = []
            manifest = {(r["source_id"], r["hash"], r["revision"]) for r in ancestor_data.get("evaluated_sources", [])}
            if not ancestor:
                state, reason = "pending", "no-committed-ancestor"
            elif not refs:
                state, reason = "pending", "evidence-unresolved"
            elif not mind._fresh(conn, refs):
                state, reason = "pending", "source-no-longer-current"
            elif any((r["source_id"], r["hash"], r["revision"]) not in manifest for r in refs):
                state, reason = "pending", "outside-ancestor-manifest"
            elif not all(_source_committed(conn, mind, r["source_id"]) for r in refs):
                state, reason = "pending", "commit-receipt-missing"
            else:
                state, reason = "complete", "settled-by-committed-ancestor"
            data["recovery_command"] = command_id
            if state == "complete":
                data["result"] = {"batch_id": ancestor, "event_id": event_id}
                if ancestor_data.get("receipt"):
                    data["receipt"] = ancestor_data["receipt"]
                conn.execute("UPDATE mind_appraisals SET state='complete',lease=0,data=? WHERE id=?", (queue_row(conn, mind.scope.key(), data), row["id"]))
            else:
                conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,data=? WHERE id=?",
                             (time.time(), queue_row(conn, mind.scope.key(), data), row["id"]))
            settled.append({"id": row["id"], "state": state, "reason": reason, "ancestor": ancestor,
                            "event_id": event_id if state == "complete" else None})
        result = {"state": "recovered", "at": mind.clock(), "rows": settled,
                  "completed": [s["id"] for s in settled if s["state"] == "complete"],
                  "requeued": [s["id"] for s in settled if s["state"] == "pending"]}
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result


# --- Triage of the quarantined appraisals (2026-09-27) ----------------------------------------------
# 43 rows sat in `needs-repair`: some held nothing another commit had not already judged, some were
# the only holders of owner messages, some had only failed to be prepared. `appraisal-triage` sorts
# every such row into one class, says what it would do with it, and does it only with `apply`:
#   committed               its own commit receipt exists: resumed, it finishes from the receipt,
#                           with no model call.
#   integrated              every source it was for is integrated by another appraisal: superseded.
#   answered                a session review the host has asked again and answered since: superseded.
#   organized               memory enrichment whose every source another enrichment organized, or
#                           that is gone: superseded.
#   evidence-unavailable    what it still has to judge is deleted or no longer current: superseded
#                           with that code (a resume would end there too, after a claim).
#   evidence-needs-review   what it still has to judge has a newer version waiting for review:
#                           reported; a resume would only spend its preparation retries.
#   native-review / compression / enrichment / other   by the failure that set it aside: reported,
#                           resumed when the request names the class in `resume`, superseded when it
#                           names it in `retire`.
# A resume is judged afresh (recover_quarantined's rule) and spread over time -- `per_slot` rows every
# `spacing_minutes` -- so the single action lane is never handed a backlog at once. Nothing here calls
# a model, and no row is deleted.
TRIAGE_CLASSES = ("committed", "integrated", "answered", "organized", "evidence-unavailable",
                  "evidence-needs-review", "native-review", "compression", "enrichment", "other")
# The classes whose rows are superseded without being named: nothing a model could do is left in them.
SETTLED_CLASSES = ("integrated", "answered", "organized", "evidence-unavailable")
# The classes an operator may name in `resume` or `retire`.
CHOSEN_CLASSES = ("native-review", "compression", "enrichment", "other", "evidence-needs-review")
TRIAGE_PER_SLOT, TRIAGE_SPACING_MINUTES = 2, 10


def _committed_sources(conn, mind, ids):
    """Those of `ids` (sources) an appraisal's commit integrated: in the source index, with the event
    that integrated them still there (recover_batched's proof)."""
    found = set()
    for source_id in sorted({i for i in ids if isinstance(i, str) and i.startswith("src_")}):
        if _source_committed(conn, mind, source_id):
            found.add(source_id)
    return found


def _material_kinds(conn, ids):
    """How many of each kind of source a row holds: static labels (the host event), never text."""
    kinds = {}
    for source_id in sorted({i for i in ids if isinstance(i, str) and i.startswith("src_")}):
        row = conn.execute("SELECT data FROM sources WHERE id=?", (source_id,)).fetchone()
        label = ((json.loads(row[0]).get("metadata") or {}).get("host_event") if row else None) or "unknown"
        kinds[label] = kinds.get(label, 0) + 1
    return kinds


def _organized(conn, mind, ids, organized_elsewhere):
    """Whether memory enrichment is left to do for these sources: none is, when another enrichment
    or backfill that completed carried each of them, or the source is gone."""
    sources = [i for i in ids if isinstance(i, str) and i.startswith("src_")]
    if not sources:
        return False
    for source_id in sources:
        row = conn.execute("SELECT deleted FROM sources WHERE id=? AND scope=?", (source_id, mind.scope.key())).fetchone()
        if row and not row[0] and source_id not in organized_elsewhere:
            return False
    return True


def _failure_class(data, lane):
    error, reason = str(data.get("error") or ""), str(data.get("repair_reason") or "")
    if error.startswith("native-review-") or reason.startswith("native-review-") or "native-review" in reason:
        return "native-review"
    if (error.startswith(("deepseek-evidence-compression-pending", "deepseek-appraisal-budget"))
            or reason.startswith(("compression-", "repeated-failure:deepseek-appraisal-budget"))):
        return "compression"
    return "enrichment" if lane == "enrichment" else "other"


def _triage_row(mind, conn, row, organized_elsewhere):
    """One quarantined row: its class, what would be done, and the static reason."""
    from .attempts import lane_of
    from .conflicts import classify
    data = json.loads(row["data"])
    lane, evidence = lane_of(data), list(data.get("evidence_ids") or [])
    integrated = _committed_sources(conn, mind, evidence)
    entry = {"id": row["id"], "stimulus": data.get("stimulus"), "lane": lane, "attempts": row["attempts"],
             "error": data.get("error") if re.fullmatch(r"[A-Za-z0-9:._-]{1,120}", str(data.get("error") or "")) else None,
             "repair_reason": data.get("repair_reason") if re.fullmatch(r"[A-Za-z0-9:._-]{1,120}", str(data.get("repair_reason") or "")) else None,
             "members": len(data.get("batch_ids") or []),
             "evidence": {"sources": sum(1 for i in evidence if i.startswith("src_")),
                          "records": sum(1 for i in evidence if not i.startswith("src_")),
                          "integrated": len(integrated), "kinds": _material_kinds(conn, evidence)}}

    def decided(kind, reason=None):
        return {**entry, "class": kind, "reason": reason or kind}
    if conn.execute("SELECT 1 FROM commands WHERE id=?", (mind._key(row["id"]),)).fetchone():
        return decided("committed", "commit-receipt-exists")
    if lane == "maintenance":
        later = conn.execute(
            "SELECT 1 FROM mind_appraisals WHERE scope=? AND state='complete' AND id<>? "
            "AND json_extract(data,'$.stimulus')='session-maintenance' "
            "AND COALESCE(json_extract(data,'$.attempt_started_at'),'')>? LIMIT 1",
            (mind.scope.key(), row["id"], data.get("attempt_started_at") or "")).fetchone()
        if later:
            return decided("answered", "answered-by-later-session-review")
    elif lane == "enrichment":
        if _organized(conn, mind, evidence, organized_elsewhere):
            return decided("organized", "organized-elsewhere")
    elif any(i.startswith("src_") for i in evidence) and all(i in integrated for i in evidence if i.startswith("src_")):
        return decided("integrated", "integrated-elsewhere")
    # What a resume would still judge: the integrated sources are left out at its claim.
    remaining = [i for i in evidence if i not in integrated]
    if remaining:
        try:
            refs = mind._evidence(conn, remaining)
        except (Conflict, Missing) as error:
            code = classify(error).code
            return decided("evidence-unavailable", {"evidence-source-unavailable": "root-evidence-unavailable",
                                                    "evidence-not-current": "root-evidence-changed"}.get(code, code or "root-evidence-unavailable"))
        if not mind._fresh(conn, refs):
            return decided("evidence-needs-review", "source-needs-review")
    return decided(_failure_class(data, lane))


def triage_quarantined(mind, *, apply=False, command_id=None, source=None, resume=(), retire=(), job_ids=None,
                       per_slot=TRIAGE_PER_SLOT, spacing_minutes=TRIAGE_SPACING_MINUTES):
    """Sort every quarantined appraisal of this scope (or the ones named in `job_ids`) into a class,
    and say what would be done with each. Writes nothing unless `apply`: then the settled classes are
    superseded, the classes named in `retire` too, and those named in `resume` go back to the queue,
    spread `per_slot` rows every `spacing_minutes`. Idempotent under `command_id`, like
    recover_quarantined; ids, classes, counts and static codes only, never a proposal or a text."""
    resume, retire = tuple(resume or ()), tuple(retire or ())
    unknown = (set(resume) | set(retire)) - set(CHOSEN_CLASSES)
    if unknown or set(resume) & set(retire):
        raise ValueError("Name each chosen class once: " + ", ".join(CHOSEN_CLASSES))
    if type(per_slot) is not int or not 1 <= per_slot <= 10 or type(spacing_minutes) not in {int, float} \
            or not 1 <= spacing_minutes <= 240:
        raise ValueError("Resume one to ten rows every one to 240 minutes")
    if job_ids is not None and (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 200
                                or len(set(job_ids)) != len(job_ids)):
        raise ValueError("Name up to 200 unique quarantined jobs, or none for all of them")
    if apply and (not command_id or not source):
        raise ValueError("Applying a triage requires a sourced command")
    from .appraisal import Appraisals
    jobs = Appraisals(mind)
    name = "quarantine-triage:" + str(command_id)
    with mind.engine.db.connect(write=apply) as conn:
        if apply:
            previous = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?",
                                    (mind.scope.key(), name)).fetchone()
            if previous:
                result = json.loads(previous[0])
                if result["fingerprint"] != digest([source, list(resume), list(retire), job_ids, per_slot, spacing_minutes]):
                    raise Conflict("Triage command belongs to another batch")
                return result
        rows = conn.execute("SELECT * FROM mind_appraisals WHERE scope=? AND state='needs-repair' "
                            "ORDER BY COALESCE(json_extract(data,'$.attempt_started_at'),''),id", (mind.scope.key(),)).fetchall()
        if job_ids is not None:
            missing = set(job_ids) - {row["id"] for row in rows}
            if missing:
                raise Conflict("Only a quarantined appraisal can be triaged", target=sorted(missing)[0])
            rows = [row for row in rows if row["id"] in set(job_ids)]
        organized_elsewhere = set()
        for done in conn.execute("SELECT data FROM mind_appraisals WHERE scope=? AND state='complete' "
                                 "AND json_extract(data,'$.stimulus') IN ('memory-enrichment','memory-backfill')",
                                 (mind.scope.key(),)):
            organized_elsewhere.update(json.loads(done[0]).get("evidence_ids") or ())
        plan = []
        for row in rows:
            entry = _triage_row(mind, conn, row, organized_elsewhere)
            entry["action"] = ("supersede" if entry["class"] in SETTLED_CLASSES or entry["class"] in retire
                               else "resume" if entry["class"] == "committed" or entry["class"] in resume else "report")
            plan.append(entry)
        at, now, slot = mind.clock(), time.time(), 0
        for entry in plan:
            if entry["action"] == "resume":
                # Spread over time; the worker takes each when it is due, with anything else that is.
                entry["available_in_minutes"] = (slot // per_slot) * spacing_minutes
                slot += 1
        result = {"state": "applied" if apply else "dry-run", "at": at,
                  "counts": {kind: sum(1 for e in plan if e["class"] == kind) for kind in TRIAGE_CLASSES
                             if any(e["class"] == kind for e in plan)},
                  "actions": {kind: sum(1 for e in plan if e["action"] == kind) for kind in ("supersede", "resume", "report")},
                  "resume_spread_minutes": max([e.get("available_in_minutes", 0) for e in plan] or [0]),
                  "rows": plan}
        if not apply:
            return result
        for entry in plan:
            if entry["action"] == "report":
                continue
            row = conn.execute("SELECT * FROM mind_appraisals WHERE id=? AND state='needs-repair'", (entry["id"],)).fetchone()
            data = json.loads(row["data"])
            data.setdefault("recovery_history", []).append({
                "command_id": command_id, "source": source, "at": at, "attempts": row["attempts"],
                "triage": entry["class"], "reason": entry["reason"],
                **{field: data.get(field) for field in ("error", "error_detail", "repair_reason",
                                                        "proposed_result", "receipt", "failed_call_receipt")},
                **{field: data[field] for field in RETRY_COUNTERS if field in data}})
            if entry["action"] == "supersede":
                data["recovery_history"][-1]["retired"] = True
                data["retired_reason"] = entry["reason"]
                conn.execute("UPDATE mind_appraisals SET state='superseded',lease=0,data=? WHERE id=?",
                             (queue_row(conn, mind.scope.key(), data), entry["id"]))
                jobs._settle_children(conn, entry["id"], data, "superseded")
                event_state = "superseded"
            else:
                if data.get("seed_memory"):
                    data["seed_rejected"] = True
                for field in ("error", "error_detail", "repair_reason", "waiting_reason",
                              "frozen_memory_context", *RETRY_COUNTERS, *REUSE_FIELDS):
                    data.pop(field, None)
                conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                             (now + entry["available_in_minutes"] * 60, queue_row(conn, mind.scope.key(), data), entry["id"]))
                # The internal event follows its appraisal to the end again (ActionEvents.drain).
                event_state = "queued"
            conn.execute("UPDATE mind_action_events SET state=? WHERE scope=? AND state='needs-review' "
                         "AND json_extract(data,'$.job_id')=?", (event_state, mind.scope.key(), entry["id"]))
        result["fingerprint"] = digest([source, list(resume), list(retire), job_ids, per_slot, spacing_minutes])
        result["command_id"] = command_id
        conn.execute("INSERT INTO mind_memory_migrations VALUES(?,?,0,?)", (mind.scope.key(), name, dumps(result)))
    return result
