"""Quarantined appraisals back to the queue: after a history compaction (`resume_compaction_waits`),
once the provider answers again (`resume_after_provider`), and on the store repair's word
(`recover_quarantined`, eventmem.core.repair `quarantine`), or as the operator's triage sorts
them (`triage_quarantined`, `appraisal-triage`, which her health page points to). Each one is
judged afresh.
"""
import json
import re
import time

from eventmem.core.db import Conflict, Missing, digest, dumps

from .appraisal import queue_row

# Retry bookkeeping a resume gives back, preserved in recovery_history.
RETRY_COUNTERS = ("error_signature", "error_repeats", "compression_waits", "compression_stalls",
                  "compression_parts", "compression_calls", "transient_failures", "admission_waits",
                  "light_attempts", "deletion_refusals")
# What a conflict left for the next attempt to reuse or revalidate. A resumed row is judged afresh.
REUSE_FIELDS = ("reuse", "tier", "revalidation")


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


# A row set aside only because its provider was out (`transient-failures-exhausted`) goes back to the
# queue by itself once an appraisal commits again -- at most AUTO_RESUMES times a row, a few at a time.
RECOVERABLE_REASON = "transient-failures-exhausted:"
AUTO_RESUMES, AUTO_RESUME_PER_SLOT, AUTO_RESUME_SPACING_SECONDS = 3, 2, 600
PROVIDER_RECOVERED = "provider-recovered"


def recoverable_sql(column="data"):
    """SQL true for a quarantined row the provider's recovery gives back (liveness counts them too)."""
    return (f"json_extract({column},'$.repair_reason') LIKE '{RECOVERABLE_REASON}%' AND (SELECT COUNT(*) FROM json_each({column},'$.recovery_history') "
            f"WHERE json_extract(value,'$.command_id')='{PROVIDER_RECOVERED}')<{AUTO_RESUMES}")


def resume_after_provider(conn, scope, at):
    """Inside the transaction of a commit that shows the provider answers again: the rows its outage
    set aside are pending again, judged afresh as after an operator's resume, spread over time."""
    resumed, now = [], time.time()
    rows = conn.execute("SELECT id,attempts,data FROM mind_appraisals WHERE scope=? AND state='needs-repair' AND " + recoverable_sql()
                        + " ORDER BY available,id", (scope,)).fetchall()
    events = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_action_events'").fetchone()
    for row in rows:
        data = json.loads(row["data"])
        data.setdefault("recovery_history", []).append({
            "command_id": PROVIDER_RECOVERED, "source": "host", "at": at, "attempts": row["attempts"],
            **{field: data.get(field) for field in ("error", "error_detail", "repair_reason")},
            **{field: data[field] for field in RETRY_COUNTERS if field in data}})
        for field in ("error", "error_detail", "repair_reason", "waiting_reason", "frozen_memory_context", *RETRY_COUNTERS, *REUSE_FIELDS):
            data.pop(field, None)
        conn.execute("UPDATE mind_appraisals SET state='pending',available=?,lease=0,attempts=0,data=? WHERE id=?",
                     (now + len(resumed) // AUTO_RESUME_PER_SLOT * AUTO_RESUME_SPACING_SECONDS, queue_row(conn, scope, data), row["id"]))
        if events:
            # The internal event follows its appraisal to the end again (ActionEvents.drain).
            conn.execute("UPDATE mind_action_events SET state='queued' WHERE scope=? AND state='needs-review' "
                         "AND json_extract(data,'$.job_id')=?", (scope, row["id"]))
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


def _source_committed(conn, mind, source_id):
    """Presence in the source index is not proof; its event must exist."""
    row = conn.execute("SELECT event_id FROM mind_semantic_sources WHERE scope=? AND source_id=?", (mind.scope.key(), source_id)).fetchone()
    return bool(row and conn.execute("SELECT 1 FROM mind_events WHERE id=? AND scope=?", (row[0], mind.scope.key())).fetchone())


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
    that integrated them still there."""
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
