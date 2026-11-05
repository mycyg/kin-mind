"""Housekeeping for what was derived, never for what was remembered: the metrics ring, and the
erase's purge of compressed context (`mind_context_cache`) for a scope that opted into it.

Compressed context is the summary a model produced of records that are all still there; drop a
row and the next reader pays one model call to write it again. `metrics` is telemetry, a ring:
one shared ring of 20,000 rows, or one per name with `metrics_name_ring` on, so a rare name is not
pushed out by a noisy one.
"""

from __future__ import annotations

# Off leaves the shared ring exactly as it was.
RING_FLAG = "metrics_name_ring"
# What one metric name may keep once the ring is per name. Every name written anywhere
# in this source is a static string in the code, so the table's bound stays something
# an operator can work out in advance: the number of names, times this.
METRIC_RING = 2000
# The shared ring as the previous release wrote it, kept verbatim and spelled out
# rather than built, because it is the flag-off path and a difference in it would be
# a difference in behaviour rather than in style.
METRIC_RING_LEGACY = 20000
LEGACY_RING_SQL = "DELETE FROM metrics WHERE id < (SELECT MAX(id)-20000 FROM metrics)"
# What `Engine.overview` reads: the newest 2,000 rows of the table, whatever their
# names. A name whose newest row is older than that window is already unreadable,
# which is how a retained rare name can still be invisible — so the reader moves
# with the ring, and reads that many of each name it actually asks about.
STATS_WINDOW = 2000
STATS_NAMES = ("recall_ms", "model_cost", "model_tokens")

# The one statement here that removes a stored row for good, and the table it names is the derived one.
DELETE_ERASED_SCOPES = (
    "DELETE FROM mind_context_cache WHERE scope IN"
    " (SELECT scope FROM mind_memory_config WHERE json_extract(data,'$.context_cache_sweep')=1)"
)


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def purge_context_cache_on_erase(conn):
    """The whole compressed-context cache of every scope that opted into the sweep.

    Every erase already takes out the cache rows that name what it erased, or a graph item
    it took words from, whatever this setting says (`kin_mind.erasure`). This is the wider
    sweep on top, for a scope that asked for it, and only for that scope: an erase is not an
    age or a cap, and one scope's choice is not performed on another.
    """
    if not (_table(conn, "mind_context_cache") and _table(conn, "mind_memory_config")):
        return 0
    return conn.execute(DELETE_ERASED_SCOPES).rowcount


def ring_enabled(conn):
    """The ring belongs to the store and the switch belongs to the operator, who
    sets it per scope like every other flag. One telemetry table cannot be two
    things at once, so any scope asking for the per-name ring gets it for the table.
    """
    if not _table(conn, "mind_memory_config"):
        return False
    return bool(conn.execute(
        "SELECT 1 FROM mind_memory_config WHERE json_extract(data,'$." + RING_FLAG + "')=1 LIMIT 1").fetchone())


def trim_metrics(conn, name, *, ring=None):
    """Bound telemetry independently of user memories.

    Off: the shared ring, exactly as it was — the newest 20,000 rows of every name
    together. On: this name's own newest `ring` rows, which is the same bound
    applied where it belongs. The subquery walks the `(name,id)` index for one
    name, so the cost does not grow with how noisy the other names are.
    """
    ring = METRIC_RING if ring is None else ring
    if not ring_enabled(conn):
        conn.execute(LEGACY_RING_SQL)
        return
    conn.execute(
        "DELETE FROM metrics WHERE name=? AND id<=COALESCE("
        "(SELECT id FROM metrics WHERE name=? ORDER BY id DESC LIMIT 1 OFFSET ?),-1)",
        (name, name, ring))
