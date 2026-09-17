"""Append-only ledger of appraisal attempts and of every model call inside one.

One row per attempt, inserted when the attempt **ends**, so a process that dies mid
attempt leaves no half-written record. The next claimer of an expired `running` row
backfills the attempt that ended without one (`abandoned`, usage unknown); that
placeholder is the only row a real record ever replaces.

Only digests, identifiers, static codes and provider-reported usage are stored: never a
proposal body, a context body or chat text. A call whose usage the provider did not
report is written as `usage_status: "unknown"` and counted nowhere as zero; a role with
no configured price is `cost_status: "unpriced"`, never a cost of 0.0.

The queue row keeps its own `error` / `failed_call_receipt` / `proposed_result` /
`receipt` exactly as before. This ledger is additional history beside them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from eventmem.core.db import digest, dumps

ATTEMPT_LEDGER = "attempt_ledger"

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_appraisal_attempts(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, appraisal_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 attempt_token TEXT NOT NULL, lane TEXT NOT NULL, stimulus TEXT, outcome TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_appraisal_attempt_job ON mind_appraisal_attempts(scope,appraisal_id,ordinal);
"""

# `reused` and `revalidated` are reserved for WP4's Tier A/B; nothing writes them yet.
OUTCOMES = ("committed", "failed", "quarantined", "discarded", "abandoned", "reused", "revalidated")
PURPOSES = ("appraise", "schema-repair", "advice-repair", "sharing-repair",
            "compression", "expansion", "revalidate", "other")
# The purpose of a structured call follows its tool name, never the calling function:
# compression has both a foreground and a background caller.
TOOL_PURPOSES = {
    "submit_appraisal": "appraise",
    "repair_appraisal": "schema-repair",
    "repair_session_advice": "advice-repair",
    "repair_sharing": "sharing-repair",
    "submit_compression": "compression",
    "submit_recall_ranking": "expansion",
    "revalidate_appraisal": "revalidate",
}
# Fields an operator may read back. Digests and static codes only.
CALL_FIELDS = ("purpose", "tool", "model", "request_id", "elapsed_ms", "outcome",
               "usage", "usage_status", "cost", "cost_status", "context_digest", "detail")


def purpose_of(tool):
    return TOOL_PURPOSES.get(tool, "other")


def usage_entry(usage):
    """What the provider reported, or an explicit unknown. A missing count is never a zero."""
    if isinstance(usage, dict) and usage:
        return {"usage": usage, "usage_status": "reported"}
    return {"usage": None, "usage_status": "unknown"}


def token_counts(usage):
    """The reported prompt/completion counts, or None when neither was reported.

    A count the provider omitted stays None: no zero stands in for it anywhere.
    """
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if type(input_tokens) not in {int, float} or type(output_tokens) not in {int, float}:
        return None
    return input_tokens, output_tokens


def cost_entry(input_tokens, output_tokens, input_price, output_price):
    """An unpriced role reports that it is unpriced; it never reports a cost of 0.0."""
    if not input_price and not output_price:
        return {"cost": None, "cost_status": "unpriced"}
    return {"cost": (input_tokens * input_price + output_tokens * output_price) / 1_000_000,
            "cost_status": "priced"}


@contextmanager
def collect(provider):
    """Accumulate this attempt's model calls on the provider, including nested ones."""
    previous = getattr(provider, "attempt_calls", None)
    calls = provider.attempt_calls = []
    try:
        yield calls
    finally:
        provider.attempt_calls = previous


@contextmanager
def appraise_purpose(provider, purpose):
    """The purpose the next appraise() call is made for (expansion rounds, revalidation)."""
    previous = getattr(provider, "call_purpose", None)
    provider.call_purpose = purpose
    try:
        yield
    finally:
        provider.call_purpose = previous


def record_call(provider, tool, *, purpose=None, outcome, model=None, request_id=None,
                elapsed_ms=None, usage=None, usage_status=None, context_digest=None, detail=None):
    """One entry per model call, on every path: HTTP error, network error, timeout,
    missing tool call, unverified model and schema fault all leave a record.

    Missing usage additionally writes the `model_usage_unknown` metric, so an
    unattributable call is visible without any zero being written for it.
    """
    call = {"purpose": purpose or purpose_of(tool), "tool": tool, "outcome": outcome,
            "model": model, "request_id": request_id, "elapsed_ms": elapsed_ms,
            **usage_entry(usage), **({"usage_status": usage_status} if usage_status else {})}
    if context_digest:
        call["context_digest"] = context_digest
    if detail:
        call["detail"] = detail
    calls = getattr(provider, "attempt_calls", None)
    if calls is not None:
        calls.append(call)
    engine = getattr(provider, "engine", None)
    if engine is not None and call["usage_status"] == "unknown":
        engine.db.metric("model_usage_unknown", 1,
                         {k: call[k] for k in ("purpose", "tool", "model", "request_id", "outcome")})
    return call


def failure_receipt(call, **extra):
    """The queue row's receipt shape, built from a call record. Kept for operators."""
    return {"provider": "deepseek", "reasoning": "high", **{k: call[k] for k in
            ("purpose", "model", "request_id", "elapsed_ms", "usage", "usage_status", "outcome")}, **extra}


def enabled(conn, scope):
    from .autonomy_schema import optimized
    return optimized(conn, scope, ATTEMPT_LEDGER)


def outcome_for(state, data, *, owned):
    """The queue state an attempt ended in, as a ledger outcome.

    A lost attempt token means this worker's result was thrown away. A top-level
    "already committed" finished the row from another attempt's receipt, so this
    attempt committed nothing either.
    """
    if not owned:
        return "discarded"
    if state == "complete":
        return "discarded" if data.get("completed_from") == "already-committed" else "committed"
    if state == "needs-repair":
        return "quarantined"
    return "failed"


def _row(scope, entry, ordinal):
    data = {"calls": list(entry.get("calls") or []), "charged": bool(entry.get("charged"))}
    for field in ("error", "error_detail", "repair_reason", "proposal_digest",
                  "context_digest", "waiting_reason", "attempts"):
        if entry.get(field) is not None:
            data[field] = entry[field]
    reported = [c for c in data["calls"] if c.get("usage_status") == "reported"]
    data["usage_status"] = "reported" if reported and len(reported) == len(data["calls"]) else (
        "unknown" if not reported else "partial-unknown")
    return (digest([entry["appraisal_id"], entry["attempt_token"]])[:32], scope, entry["appraisal_id"],
            ordinal, entry["attempt_token"], entry.get("lane") or "action", entry.get("stimulus"),
            entry["outcome"], entry.get("started_at"), entry.get("finished_at"), dumps(data))


def record(engine, scope, entry, *, placeholder=False):
    """Insert one attempt row.

    A real record completes the `abandoned` placeholder a later claimer left for a
    killed attempt, and never overwrites another real record.
    """
    if entry["outcome"] not in OUTCOMES:
        raise ValueError("Unknown appraisal attempt outcome")
    if not entry.get("attempt_token"):
        return None
    with engine.db.connect(write=True) as conn:
        conn.executescript(LEDGER_SCHEMA)
        ordinal = conn.execute(
            "SELECT COUNT(*) FROM mind_appraisal_attempts WHERE scope=? AND appraisal_id=?",
            (scope, entry["appraisal_id"])).fetchone()[0] + 1
        row = _row(scope, entry, ordinal)
        if placeholder:
            conn.execute("INSERT OR IGNORE INTO mind_appraisal_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
        else:
            conn.execute(
                "INSERT INTO mind_appraisal_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "outcome=excluded.outcome,finished_at=excluded.finished_at,data=excluded.data "
                "WHERE mind_appraisal_attempts.outcome='abandoned'", row)
    return row[0]


def backfill_abandoned(engine, scope, row, data, *, at):
    """The attempt that left this row `running` past its lease recorded nothing itself.

    Its usage is unknowable from here, so it is stated as unknown rather than guessed.
    """
    return record(engine, scope, {
        "appraisal_id": row["id"], "attempt_token": data.get("attempt_token"), "outcome": "abandoned",
        "lane": lane_of(data), "stimulus": data.get("stimulus"), "started_at": data.get("attempt_started_at"),
        "finished_at": at, "calls": [], "charged": True,
        "error": "appraisal-attempt-abandoned", "attempts": row["attempts"],
    }, placeholder=True)


def lane_of(data):
    stimulus = data.get("stimulus")
    if stimulus in {"memory-backfill", "memory-enrichment"}:
        return "enrichment"
    return "maintenance" if stimulus == "session-maintenance" else "action"


def read(engine, scope, *, job_id=None, limit=20):
    """Operator view of the ledger: outcomes, classes, digests and usage, no private text."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Attempt ledger limit must be between 1 and 200")
    with engine.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_appraisal_attempts'").fetchone():
            return {"attempts": []}
        rows = conn.execute(
            "SELECT * FROM mind_appraisal_attempts WHERE scope=?" + (" AND appraisal_id=?" if job_id else "")
            + " ORDER BY finished_at DESC,ordinal DESC LIMIT ?",
            (scope, job_id, limit) if job_id else (scope, limit)).fetchall()
    attempts = []
    for row in rows:
        data = json.loads(row["data"])
        attempts.append({k: row[k] for k in ("appraisal_id", "ordinal", "attempt_token", "lane",
                                             "stimulus", "outcome", "started_at", "finished_at")}
                        | {k: v for k, v in data.items() if k != "calls"}
                        | {"calls": [{k: c[k] for k in CALL_FIELDS if k in c} for c in data.get("calls", [])]})
    return {"attempts": attempts}
