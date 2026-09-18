"""Housekeeping for what was derived, never for what was remembered.

Two things in this store grow without an end and neither of them is a memory.

`mind_context_cache` holds **compressed context**: the summary a model produced of
records that are all still there. Three writers fill it — a whole pack for one
question, one batch of a pack, and a reusable overview of a single item — and each
row is keyed by a digest of the inputs that produced it, so a row can only ever be
answered by recomputing exactly the same summary. Nothing reads it for a fact it is
the only holder of: drop a row and the next reader misses, pays one model call, and
writes the same row again. That is not a rhetorical claim about the design, it is
something the store has already lived through — the evidence-isolation migration
took 80 of these rows out of service by changing the classification underneath
them, and the whole consequence was that they were compressed again.

So this module contains the **one hard delete in the whole programme**, and it is
deliberate. Everywhere else "prune" means "move to an archive and keep it for
ever", because everywhere else the rows are the owner's. Here they are the model's
working copy of rows that stay where they are. The distinction is the whole
justification, and the code is written so that it cannot quietly stop being true:

* the only two statements that can remove a cached summary name
  `mind_context_cache` literally and live at the top of this module. Nowhere here
  is a table name built, passed in or parameterised, so a later caller cannot
  point any of this at a table that holds an original;
* the sweep deletes one row at a time by `(scope, id)`, out of a plan the caller
  can read first — read only, and printable without writing anything at all, which
  is what the maintenance tick does until an operator adds `--apply`. The erase
  purge is the one statement that takes a whole scope, and only a scope whose own
  configuration asked for it;
* it is behind `context_cache_sweep`, which is **off** until someone decides
  otherwise, and off it changes nothing — the rows stay exactly as they are today.

Two rules decide what goes: an age of `CACHE_TTL_DAYS` on the row's own
`created_at`, and a cap of `CACHE_ROWS` newest rows per scope for whatever survives
that. The cap is what bounds a burst; the age is what bounds a store that is quiet
for a month.

Two guards keep the sweep from costing anything but a recompression:

* it **never deletes the newest row**. `Appraisals._cache_mark` takes `MAX(rowid)`
  of this table before a pass and counts the rows written after it to decide
  whether compression made progress; SQLite hands out `MAX(rowid)+1`, so deleting
  the highest row would let a later insert reuse a number below a mark that is
  still in flight, and an appraisal that did make progress would look stalled —
  which at `COMPRESSION_STALL_LIMIT` is a quarantine. Keeping one row keeps the
  numbering monotonic. A sweep that would otherwise take everything reports it;
* it **refuses while an appraisal lease is fresh**. Those are the rows a running
  pass is caching right now, and the point of waiting is that a sweep should never
  be the reason a model is called twice.

The second thing is `metrics`, which is telemetry and already a ring: today one
shared ring of 20,000 rows for every name together. That is why the store cannot
say what its recall latency is — `recall_ms` fires when somebody reads, `model_ms`
fires on every model call, and the noisy name pushed the rare one out of the window
days ago. A ring **per name** fixes exactly that: a name can only ever evict
itself, so the one that fires twice a week is still there when an operator asks.
That is a change to what telemetry is kept, never to what is remembered, and it too
is behind a flag (`metrics_name_ring`) that starts off.

`maintenance-tick` drives both, and reports what it would do before it is allowed
to do anything. `vector-optimize` is separate and stays a command: see below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .autonomy_schema import enabled
from .liveness import quiescence, running_appraisal_leases

# Off until an owner has been told what it deletes and has agreed to it. Off, every
# path here reports and writes nothing, which is exactly today's behaviour.
CACHE_FLAG = "context_cache_sweep"
# How old a compressed summary may be before the next reader is made to pay for a
# fresh one. Two weeks is well past the point where the records under it have moved.
CACHE_TTL_DAYS = 14
# And how many of them one scope may keep whatever their age. Production holds a few
# hundred, so this is a bound on a burst rather than a routine trim.
CACHE_ROWS = 2000
# The sweep's own receipt. Counts only: no key, no query, no summary text.
SWEEP_METRIC = "context_cache_swept"

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

# Off refuses the Lance command outright, so the flag is the first of its three locks.
VECTOR_FLAG = "vector_optimize"
# Versions younger than this are left alone even once everything is quiet: quiet says
# nothing is running now, not that nothing had just finished.
VECTOR_KEEP_DAYS = 7
VECTOR_METRIC = "vector_versions_cleaned"

# Beside the telemetry ring above, the only two statements in the programme that
# remove a stored row for good — and the table they name is the derived one. They are
# written out here, whole, so that reading this module is enough to see all of it.
DELETE_ROW = "DELETE FROM mind_context_cache WHERE scope=? AND id=?"
DELETE_ERASED_SCOPES = (
    "DELETE FROM mind_context_cache WHERE scope IN"
    " (SELECT scope FROM mind_memory_config WHERE json_extract(data,'$.context_cache_sweep')=1)"
)


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def sweep_enabled(conn, scope):
    """Explicitly on, or nothing here deletes. `enabled()`, not `optimized()`: this
    is the one switch in the stage whose wrong default would remove something."""
    return enabled(conn, scope, CACHE_FLAG)


def _moment(value):
    """A stored timestamp, or None when it cannot be read. Unreadable means kept:
    an age rule that cannot establish an age has not established anything."""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def cache_plan(conn, scope, *, now=None, ttl_days=None, cap=None):
    """What the two rules point at, and why. Read only, and safe on a live store.

    `targets` is the identifiers a sweep would delete; everything else is what an
    operator needs to decide whether to let it. `retained_newest` says the newest
    row was held back to keep `rowid` monotonic for a pass that may be running.
    """
    now = datetime.now(timezone.utc) if now is None else now
    ttl_days = CACHE_TTL_DAYS if ttl_days is None else ttl_days
    cap = CACHE_ROWS if cap is None else cap
    if not _table(conn, "mind_context_cache"):
        return {"rows": 0, "bytes": 0, "expired": 0, "over_cap": 0, "unreadable": 0,
                "targets": [], "bytes_to_remove": 0, "retained_newest": False,
                "ttl_days": ttl_days, "cap": cap, "cutoff": None}
    cutoff = now - timedelta(days=ttl_days)
    rows = conn.execute(
        "SELECT rowid AS position,id,created_at,LENGTH(CAST(data AS BLOB)) AS size FROM mind_context_cache"
        " WHERE scope=? ORDER BY created_at DESC,rowid DESC", (scope,)).fetchall()
    newest = max((row["position"] for row in rows), default=None)
    expired, unreadable, kept, targets = [], 0, [], []
    for row in rows:
        stamp = _moment(row["created_at"])
        if stamp is None:
            unreadable += 1
            kept.append(row)
        elif stamp < cutoff:
            expired.append(row)
        else:
            kept.append(row)
    over_cap = kept[cap:]
    retained_newest = False
    for row in [*expired, *over_cap]:
        if row["position"] == newest:
            # The mark an appraisal pass may be holding. One row is a cheap price for
            # never turning a sweep into a quarantined job.
            retained_newest = True
            continue
        targets.append(row["id"])
    sizes = {row["id"]: row["size"] or 0 for row in rows}
    return {"rows": len(rows), "bytes": sum(sizes.values()), "expired": len(expired),
            "over_cap": len(over_cap), "unreadable": unreadable, "targets": targets,
            "bytes_to_remove": sum(sizes[i] for i in targets), "retained_newest": retained_newest,
            "ttl_days": ttl_days, "cap": cap, "cutoff": cutoff.isoformat()}


def _reported(found):
    """The plan without its identifier list: the counts are the answer, and a few
    thousand digests on one line are not."""
    return {k: v for k, v in found.items() if k != "targets"}


def sweep_context_cache(mind, *, apply=False, now=None):
    """The hard delete, with its three answers before it: disabled, blocked, planned.

    Nothing is written unless the flag is on, no worker is holding an appraisal, and
    the caller asked to apply. The plan is taken again inside the write transaction,
    so a row cached between the report and the write is not deleted unread.
    """
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        on = sweep_enabled(conn, scope)
        found = cache_plan(conn, scope, now=now)
        held = running_appraisal_leases(conn, scope)
    if not on:
        return {"state": "disabled", "flag": CACHE_FLAG, "would_remove": len(found["targets"]), **_reported(found)}
    if held:
        # A pass is compressing right now. Its parts are the newest rows here and its
        # progress is counted off this table, so the sweep waits for the next tick.
        return {"state": "blocked", "reason": "worker-lease-fresh", "appraisals": len(held),
                "would_remove": len(found["targets"]), **_reported(found)}
    if not apply:
        return {"state": "planned", "would_remove": len(found["targets"]), **_reported(found)}
    with mind.engine.db.connect(write=True) as conn:
        found = cache_plan(conn, scope, now=now)
        for identifier in found["targets"]:
            conn.execute(DELETE_ROW, (scope, identifier))
    removed = len(found["targets"])
    if removed:
        mind.engine.db.metric(SWEEP_METRIC, removed, {"expired": found["expired"], "over_cap": found["over_cap"],
                                                      "bytes": found["bytes_to_remove"], "reason": "maintenance-tick"})
    return {"state": "swept", "removed": removed, **_reported(found)}


def purge_context_cache_on_erase(conn):
    """Erase takes the text out of the store; the compressed copies of that text
    have to go the same way or the erase is a half-measure.

    Scoped to the configurations that asked for the sweep, and to those only. A
    scope that has not opted into the hard delete does not have one performed on it
    by another scope's erase — which is also why this cannot use the sweeping rules
    above: an erase is not an age or a cap, it takes everything derived.
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


def metrics_plan(conn, *, ring=None):
    """The name distribution, and what the per-name ring would hold back or remove.

    `crowded_out` is the point of the whole change: a name whose newest row already
    sits below the shared read window is one nobody can read any more, however rare
    and however wanted it is.
    """
    ring = METRIC_RING if ring is None else ring
    if not _table(conn, "metrics"):
        return {"rows": 0, "names": [], "enabled": False, "ring": ring, "would_remove": 0}
    rows = conn.execute(
        "SELECT name,COUNT(*) AS rows,MIN(created_at) AS oldest,MAX(created_at) AS newest,MAX(id) AS last"
        " FROM metrics GROUP BY name ORDER BY rows DESC").fetchall()
    # Where the shared read window begins, by row identity rather than by clock: two
    # rows written in the same microsecond are still one before the other here.
    edge = conn.execute("SELECT MIN(id),MIN(created_at) FROM (SELECT id,created_at FROM metrics"
                        " ORDER BY id DESC LIMIT ?)", (STATS_WINDOW,)).fetchone()
    names = [{"name": row["name"], "rows": row["rows"], "oldest": row["oldest"], "newest": row["newest"],
              "over_ring": max(0, row["rows"] - ring),
              "crowded_out": bool(edge[0] is not None and row["last"] < edge[0])} for row in rows]
    return {"rows": sum(row["rows"] for row in rows), "names": names, "enabled": ring_enabled(conn),
            "ring": ring, "would_remove": sum(name["over_ring"] for name in names),
            "read_window_starts": edge[1]}


def trim_all_metrics(conn, *, ring=None):
    """Apply the per-name ring to every name at once, instead of waiting for each
    name's next write to trim it. Nothing else about the table changes."""
    ring = METRIC_RING if ring is None else ring
    removed = 0
    for row in conn.execute("SELECT name FROM metrics GROUP BY name HAVING COUNT(*)>?", (ring,)).fetchall():
        removed += conn.execute(
            "DELETE FROM metrics WHERE name=? AND id<=COALESCE("
            "(SELECT id FROM metrics WHERE name=? ORDER BY id DESC LIMIT 1 OFFSET ?),-1)",
            (row["name"], row["name"], ring)).rowcount
    return removed


def tick(mind, config=None, *, apply=False, now=None):
    """The maintenance tick: one derived cache and one telemetry ring, and nothing
    else. It reports whatever it did not do, so the report is the thing to read
    before either flag is turned on."""
    cache = sweep_context_cache(mind, apply=apply, now=now)
    with mind.engine.db.connect() as conn:
        metrics = metrics_plan(conn)
    if apply and metrics["enabled"]:
        # `removed` appears only where something was actually applied, and then it
        # appears even when the answer is none: a missing count and a count of zero
        # are different facts about a tick.
        removed = 0
        if metrics["would_remove"]:
            with mind.engine.db.connect(write=True) as conn:
                removed = trim_all_metrics(conn)
        metrics = {**metrics, "removed": removed}
    return {"state": "applied" if apply else "planned", "at": mind.clock(),
            "context_cache": cache, "metrics": metrics}


def _vector_bytes(root):
    total = 0
    directory = root / "vectors"
    for path in directory.rglob("*") if directory.exists() else ():
        if path.is_file():
            total += path.stat().st_size
    return total


def vector_optimize(mind, config=None, *, apply=False, older_than_days=VECTOR_KEEP_DAYS, now=None, probe=None):
    """Remove superseded Lance versions. A command, and never a tick.

    The store keeps one manifest per write, and a store that writes all day keeps
    thousands of them against a much smaller amount of data. Those old **versions**
    are what this is for, and no row of the current data is removed by it — which is
    why the answer carries `rows_before` and `rows_after`: unchanged rows are the
    in-code half of the condition this was agreed under, and a recall comparison by
    the operator is the other half, because the same call also merges small files and
    folds new rows into the existing index.

    `now` is the clock the quiescence proof reads, in `time.time()` seconds.

    Three locks, in this order: the flag is off unless somebody turned it on, the
    store has to be provably quiet (an old version can still be the version an
    ongoing read is holding), and a dry run is the default.
    """
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        if not enabled(conn, scope, VECTOR_FLAG):
            return {"state": "disabled", "flag": VECTOR_FLAG}
        indexes = [row[0] for row in conn.execute("SELECT id FROM vector_indexes ORDER BY id")]
    quiet = quiescence(mind, config, now=now, probe=probe)
    if not quiet["quiet"]:
        return {"state": "refused", "reason": "store-not-quiet", "blocking": quiet["blocking"]}
    if not indexes:
        return {"state": "idle", "reason": "no-vector-index"}
    try:
        from eventmem.core.vectors import VectorIndex
    except ImportError:  # pragma: no cover - the vector extra is not installed
        return {"state": "unavailable", "reason": "lancedb-missing"}
    root = mind.engine.db.root
    report = {"bytes_before": _vector_bytes(root), "indexes": [], "older_than_days": older_than_days}
    cleaned = 0
    for index_id in indexes:
        try:
            table = VectorIndex(mind.engine, index_id).table()
            versions, rows = len(table.list_versions()), table.count_rows()
        except Exception as error:  # noqa: BLE001 - an unreadable index is reported, never optimized
            report["indexes"].append({"index": index_id, "state": "unreadable", "error": type(error).__name__})
            continue
        entry = {"index": index_id, "versions_before": versions, "rows_before": rows}
        if apply:
            # `optimize` is the current API and does three things, not one: it merges
            # small files, prunes versions older than the age, and folds new rows into
            # the existing index. Only the second is what this was asked for, and the
            # other two are why an operator still has to compare recall afterwards —
            # nothing here can prove that an approximate search returns what it did.
            # `delete_unverified` stays false even with quiet proven: a file that
            # looks like an unfinished write is not this command's to judge.
            table.optimize(cleanup_older_than=timedelta(days=older_than_days), delete_unverified=False)
            entry.update(versions_after=len(table.list_versions()), rows_after=table.count_rows())
            entry["rows_unchanged"] = entry["rows_after"] == rows
            cleaned += versions - entry["versions_after"]
        report["indexes"].append({**entry, "state": "optimized" if apply else "planned"})
    report["bytes_after"] = _vector_bytes(root) if apply else report["bytes_before"]
    report["state"] = "optimized" if apply else "planned"
    report["rows_unchanged"] = all(entry.get("rows_unchanged", True) for entry in report["indexes"])
    report["verify"] = "Compare recall against the same queries before enabling this in a release."
    if apply and cleaned:
        mind.engine.db.metric(VECTOR_METRIC, cleaned, {"bytes_freed": report["bytes_before"] - report["bytes_after"]})
    return report
