"""Purge references from retired judgment-cache tables in older databases.

No request path creates, reads or accepts cached judgments. Cleanup remains so
deleting an old source also removes its words from legacy cached results.
"""

def _present(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mind_judgment_cache'").fetchone())


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
