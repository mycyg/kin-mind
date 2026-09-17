"""Judgment cache v2: reuse a verdict only when the same question was asked again.

The key is the digest of the **fully rendered request** — tool name, system prompt,
schema, context, endpoint and model/effort — and it no longer carries the database-wide
`generation`. A bump somewhere unrelated stops throwing away a judgment that is still
answerable. Three rules keep dropping the generation safe:

* A request digest proves that two requests are equal; it does not prove that the
  context behind them is still fresh. A hit therefore **never** skips the caller's
  pre-commit validation. It saves one model call, nothing else.
* The three "completion" verdicts are not one verdict. Reuse matches the judgment
  type, goal, completion condition and obligation version as well, so "the step is
  complete" can never answer "the owner's work is complete".
* Two-phase acceptance. `put()` writes a **pending** row; only the caller that
  validated the result makes it servable, with `accept()`. A result the host threw
  away is never replayed. The old cache wrote its row before validation and let the
  global generation hide the mistake.

`submit_appraisal` can never be served from here: the host clock is part of that
request, so an appraisal is never literally the same question twice. The reuse
mechanism for that path is the Tier A revalidation, not this cache.

Persona and the scope's configuration are part of a judgment's validity rather than
of its request, so each row carries the environment digest it was produced under and
only matches while that digest still holds.

Only the cached value holds model output; it never leaves this table. Dependencies are
identifiers, and an erased, corrected or deleted source purges every row resting on it.
"""

from __future__ import annotations

import json

from eventmem.core.db import digest, dumps

JUDGMENT_CACHE = "semantic_cache_v2"
# The clock is part of an appraisal request; the same question is never asked twice.
NEVER_CACHED = frozenset({"submit_appraisal"})
TTL_SECONDS = 300
MAX_VALIDITY_SECONDS = 86400
# Bounded so one wide context cannot grow the dependency index without limit.
MAX_DEPENDENCIES = 500
# Keys whose values are identifiers of what a request rested on, never payload text.
DEPENDENCY_KEYS = frozenset({"source_id", "source_ids", "record_id", "record_ids",
                             "evidence_ids", "node_id", "node_ids", "dependency_ids"})
# Containers whose items carry their own `id`, in the rendered appraisal/review contexts.
DEPENDENCY_ITEMS = frozenset({"new_evidence", "sources", "graph_candidates", "records",
                              "verified_artifacts", "items"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_judgment_cache(
 id TEXT NOT NULL,judgment_type TEXT NOT NULL,goal_digest TEXT NOT NULL,
 completion_digest TEXT NOT NULL,obligation_version TEXT NOT NULL,token TEXT NOT NULL,
 scope TEXT NOT NULL,env_digest TEXT NOT NULL,accepted INTEGER NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL,valid_until REAL NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(id,judgment_type,goal_digest,completion_digest,obligation_version));
CREATE INDEX IF NOT EXISTS mind_judgment_cache_token ON mind_judgment_cache(token);
CREATE INDEX IF NOT EXISTS mind_judgment_cache_expiry ON mind_judgment_cache(expires_at);
CREATE TABLE IF NOT EXISTS mind_judgment_cache_deps(
 token TEXT NOT NULL,dependency TEXT NOT NULL,PRIMARY KEY(token,dependency));
CREATE INDEX IF NOT EXISTS mind_judgment_cache_dep ON mind_judgment_cache_deps(dependency);
"""


# The three "completion" verdicts are not merged: a step being done is not the plan
# being done, and neither is the owner's own work being done. The judgment type says
# which question was asked, so one can never answer another.
STEP_COMPLETE = "step-complete"
PLAN_COMPLETE = "plan-complete"
WORK_COMPLETE = "owner-work-complete"
COMPLETION_TYPES = {"owner": WORK_COMPLETE}


def completion_type(actor):
    """A step the owner owes and a step Kin owes are different judgments."""
    return COMPLETION_TYPES.get(actor, STEP_COMPLETE)


def enabled(conn, scope):
    from .autonomy_schema import optimized
    return optimized(conn, scope, JUDGMENT_CACHE)


def cacheable(tool):
    """`submit_appraisal` is excluded here and not only by the absence of a caller."""
    return tool not in NEVER_CACHED


def identity(tool, judgment):
    """The five things that have to match before a verdict may answer again.

    The goal and the completion condition are stored as digests: they are owner text,
    and this module keeps payload text out of every column but the cached value.
    """
    if not isinstance(judgment, dict):
        raise ValueError("Judgment cache needs a declared judgment")
    scope = judgment.get("scope")
    kind = judgment.get("type") or tool
    if not isinstance(scope, str) or not scope or not isinstance(kind, str):
        raise ValueError("Judgment cache needs a typed judgment and a scope")
    return {
        "scope": scope,
        "judgment_type": kind,
        "goal_digest": digest(["goal", judgment.get("goal")]),
        "completion_digest": digest(["completion", judgment.get("completion")]),
        "obligation_version": str(judgment.get("obligation_version", "")),
    }


def environment_digest(engine, conn, scope):
    """Persona and configuration change the answer without changing the request."""
    try:
        path = engine.db.root / "persona-policy.json"
        persona = digest(path.read_bytes()) if path.exists() else "none"
    except OSError:
        persona = "unreadable"
    from .autonomy_schema import settings
    return digest(["judgment-env-v1", persona, settings(conn, scope)])


def dependencies(context, declared=None):
    """Identifiers this request rested on: what the caller declared, plus what the
    rendered context states about itself. Identifiers only, never text."""
    found = []

    def walk(value, depth, container=None):
        if len(found) >= MAX_DEPENDENCIES or depth > 6:
            return
        if isinstance(value, dict):
            if container in DEPENDENCY_ITEMS and isinstance(value.get("id"), str):
                found.append(value["id"])
            for key, item in value.items():
                if key in DEPENDENCY_KEYS:
                    for one in item if isinstance(item, list) else [item]:
                        if isinstance(one, str) and one:
                            found.append(one)
                else:
                    walk(item, depth + 1, key)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1, container)

    for one in declared or []:
        if isinstance(one, str) and one:
            found.append(one)
    walk(context, 0)
    return list(dict.fromkeys(found))[:MAX_DEPENDENCIES]


def _present(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_judgment_cache'").fetchone())


def get(engine, conn, tool, request_digest, judgment, *, now):
    """The accepted, unexpired and still-valid verdict for this exact question."""
    if not cacheable(tool) or not _present(conn):
        return None
    marks = identity(tool, judgment)
    if not enabled(conn, marks["scope"]):
        return None
    row = conn.execute(
        "SELECT token,data,env_digest FROM mind_judgment_cache WHERE id=? AND judgment_type=? "
        "AND goal_digest=? AND completion_digest=? AND obligation_version=? AND accepted=1 "
        "AND expires_at>? AND valid_until>?",
        (request_digest, marks["judgment_type"], marks["goal_digest"], marks["completion_digest"],
         marks["obligation_version"], now, now)).fetchone()
    if not row or row["env_digest"] != environment_digest(engine, conn, marks["scope"]):
        return None
    return row["token"], json.loads(row["data"])


def put(engine, tool, request_digest, judgment, value, *, now, depends_on=(), valid_for=None):
    """Write the verdict as **pending**. Only `accept()` makes it servable."""
    if not cacheable(tool):
        return None
    marks = identity(tool, judgment)
    validity = TTL_SECONDS if valid_for is None else max(1.0, min(float(valid_for), MAX_VALIDITY_SECONDS))
    token = digest([request_digest, marks["judgment_type"], marks["goal_digest"],
                    marks["completion_digest"], marks["obligation_version"]])[:32]
    with engine.db.connect(write=True) as conn:
        conn.executescript(SCHEMA)
        if not enabled(conn, marks["scope"]):
            return None
        _expire(conn, now)
        conn.execute(
            "INSERT OR REPLACE INTO mind_judgment_cache VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?)",
            (request_digest, marks["judgment_type"], marks["goal_digest"], marks["completion_digest"],
             marks["obligation_version"], token, marks["scope"],
             environment_digest(engine, conn, marks["scope"]), now, now + TTL_SECONDS,
             now + validity, dumps(value)))
        conn.execute("DELETE FROM mind_judgment_cache_deps WHERE token=?", (token,))
        for dependency in depends_on:
            conn.execute("INSERT OR IGNORE INTO mind_judgment_cache_deps VALUES(?,?)",
                         (token, dependency))
    return token


def token_of(receipt):
    if isinstance(receipt, str):
        return receipt
    return receipt.get("judgment_cache") if isinstance(receipt, dict) else None


def _phase(engine, receipt, action, conn):
    """Second phase of acceptance. A caller that validates inside its own write
    transaction passes that connection instead of opening a second one."""
    token = token_of(receipt)
    if not token:
        return False
    if conn is not None:
        return _present(conn) and action(conn, token)
    with engine.db.connect(write=True) as connection:
        return _present(connection) and action(connection, token)


def accept(engine, receipt, *, conn=None):
    """The caller validated this result, so from now on it may answer again."""
    return _phase(engine, receipt, lambda c, token: c.execute(
        "UPDATE mind_judgment_cache SET accepted=1 WHERE token=?", (token,)).rowcount > 0, conn)


def reject(engine, receipt, *, conn=None):
    """The host threw this result away; it never becomes servable."""
    return _phase(engine, receipt, lambda c, token: _drop(c, [token]) > 0, conn)


def invalidate(conn, identifiers):
    """Targeted purge: every cached judgment that rested on one of these identifiers.

    Called from the record, deletion and graph-change paths. A deleted source purges
    rather than expires, so no erased evidence survives inside a cached value.
    """
    wanted = [i for i in dict.fromkeys(identifiers) if isinstance(i, str) and i]
    if not wanted or not _present(conn):
        return 0
    marks = ",".join("?" * len(wanted))
    tokens = [r[0] for r in conn.execute(
        "SELECT DISTINCT token FROM mind_judgment_cache_deps WHERE dependency IN (" + marks + ")",
        wanted)]
    return _drop(conn, tokens)


def _drop(conn, tokens):
    dropped = 0
    for token in tokens:
        dropped += conn.execute("DELETE FROM mind_judgment_cache WHERE token=?", (token,)).rowcount
        conn.execute("DELETE FROM mind_judgment_cache_deps WHERE token=?", (token,))
    return dropped


def _expire(conn, now):
    """Every write sweeps what the clock has already ended, so nothing accumulates."""
    stale = [r[0] for r in conn.execute(
        "SELECT token FROM mind_judgment_cache WHERE expires_at<=? OR valid_until<=?", (now, now))]
    _drop(conn, stale)
