"""Compatibility for the retired content-hash judgment cache.

No verdict is cached or reused by content. Legacy tables remain recognizable for
source deletion and cleanup; operation commits validate their own actual references.
"""

from __future__ import annotations

JUDGMENT_CACHE = "semantic_cache_v2"
# The clock is part of an appraisal request; the same question is never asked twice. The memories
# of archived records are written once per item by their own queue (archive_memory): a cached answer
# would only be one more copy of a record's words for an erase to find.
NEVER_CACHED = frozenset({"submit_appraisal", "submit_archive_memories"})
TTL_SECONDS = 300
MAX_VALIDITY_SECONDS = 86400
# Bounded so one wide context cannot grow the dependency index without limit.
MAX_DEPENDENCIES = 500
# Keys whose values are identifiers of what a request rested on, never payload text. A trait is one
# of them: a verdict reached while the shared history carried a trait rested on that trait, and one
# owner sentence can end it.
DEPENDENCY_KEYS = frozenset({"source_id", "source_ids", "record_id", "record_ids",
                             "evidence_ids", "node_id", "node_ids", "dependency_ids",
                             "trait_id", "trait_ids"})
# Containers whose items carry their own `id`, in the rendered appraisal/review contexts.
DEPENDENCY_ITEMS = frozenset({"new_evidence", "sources", "graph_candidates", "records",
                              "verified_artifacts", "items", "established", "candidate"})

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
    return False


def cacheable(tool):
    return False


def identity(tool, judgment):
    """Legacy metadata only; no goal or completion text is fingerprinted."""
    return {"scope": judgment.get("scope"), "judgment_type": judgment.get("type") or tool,
            "obligation_version": str(judgment.get("obligation_version", ""))}


def environment_digest(engine, conn, scope):
    """Legacy call shape; no persona/configuration bytes are hashed."""
    return None


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


# Where a dependency's words are kept, and the columns that say which version of them it is. A
# dependency whose row changed, or went, since the question was put is not the one it was asked
# about; an identifier none of these tables holds has no version to compare.
VERSIONED = {"records": "revision,status,deleted", "sources": "version,deleted",
             "mind_graph_nodes": "revision,state", "mind_memory_nodes": "revision",
             "mind_traits": "revision,status"}


def versions(conn, dependencies):
    """The version of each dependency that has one, read before the question is put and again in
    the write that would keep its answer (CR3-MM-03)."""
    wanted = [one for one in dict.fromkeys(dependencies) if isinstance(one, str) and one]
    found = {}
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in VERSIONED.items():
        if table not in present:
            continue
        for start in range(0, len(wanted), 500):
            page = wanted[start:start + 500]
            for row in conn.execute(f"SELECT id,{columns} FROM {table} WHERE id IN ("
                                    + ",".join("?" * len(page)) + ")", page):
                found[table + ":" + row[0]] = list(row[1:])
    return found


def _present(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_judgment_cache'").fetchone())


def get(engine, conn, tool, request_digest, judgment, *, now):
    return None


def put(engine, tool, request_digest, judgment, value, *, now, depends_on=(), valid_for=None, asked=None):
    return None


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
    return False


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
