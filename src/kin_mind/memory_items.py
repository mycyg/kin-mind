"""Item-level isolation inside the memory section of one appraisal commit.

The memory section is not one of the stage-1 isolated sections: without the event there is
nothing to anchor it to, so a fault anywhere in it used to fail the whole appraisal. Its most
frequent faults are ones the host decides alone and no retry can change - a note or a coverage
mapping citing evidence this evaluation never saw - so an enrichment failed deterministically,
was quarantined, and lost every valid item it carried.

**What is dropped.** One item whose failure the host classifies as `block`, and not as
`unknown`: it is undone alone, inside its own savepoint, and the rest of the section commits.
A version that moved (`reuse`), a `terminal` conflict and anything unclassified still fail the
attempt exactly as before; the host never edits an item, it commits it whole or not at all.

**Cascade by key.** A note or graph node introduces a key other items may name. When it is
dropped, its key names nothing, so whatever names it is dropped with it, transitively, with the
cause's code and a `cascaded_from`. A reference by stored id to a node the proposal only meant
to *update* still names the stored node, so it is kept. Items are applied in a fixed order, and a
dependent may be applied before its dependency turns out to be blocked; the pass is then undone
as a whole and run again with what is known, so nothing dangling is ever committed. Every
restart adds at least one drop, which bounds the passes by the number of items.

**The record.** `{section:"memory", item:{kind, key}, code, message}` beside the stage-1 refused
sections. `key` is the item's position in the assessment the host applied (`notes[1]`,
`graph.edges[0]`), never the model's own key, which is free text like any other field; `code`
and `message` are the registry's static code and the raise site's literal.

**Experience is never marked organised when its carrier was dropped.** Notes and graph nodes
carry a source's content into memory; everything else relates what is already stored. A root
source is withheld when a dropped carrier cites it (or cites no root at all, so that it cannot
be attributed) and no committed carrier does. A withheld source is left out of
`mind_semantic_sources` and written to `mind_memory_unorganized`, from where the host's minute
review gives it one more memory-only pass. The cursor still advances: holding it back would
hand an already scored event to a full appraisal a second time, and every later event with it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager

from eventmem.core.db import dumps

from .conflicts import classify, static_message

# Default-on switch (mind_memory_config). An explicit false restores the previous behavior:
# any fault in the memory section fails the whole appraisal.
SWITCH = "memory_item_isolation"
SECTION = "memory"
METRIC = "memory_items_dropped"
# The host's own text for a cascaded drop; the cause's code travels with it.
CASCADED = "Dropped with the memory item it depends on"
# Items that carry a source's content into memory.
CARRIERS = ("note", "graph-node")
# Memory-only passes a withheld source is given before it is left for an operator.
REORGANIZE_LIMIT = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_memory_unorganized(
 scope TEXT NOT NULL,source_id TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL,
 updated_at TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,source_id));
"""


class Restart(Exception):
    """A dependent of a newly dropped item was already applied in this pass."""


def positions(prefix, original, kept=None):
    """The proposal's own positions of the items the host applies, whatever it filtered out first."""
    index = {id(item): place for place, item in enumerate(original)}
    return [prefix + "[" + str(index[id(item)]) + "]" for item in (original if kept is None else kept)]


def cited_sources(conn, identifiers):
    """Sources an item names as evidence, read without resolving anything that could fail."""
    found = set()
    for identifier in identifiers:
        if identifier.startswith("src_"):
            found.add(identifier)
        elif identifier.startswith("mem_"):
            row = conn.execute("SELECT data FROM records WHERE id=? AND deleted=0", (identifier,)).fetchone()
            if row:
                found.update(json.loads(row[0]).get("source_ids", []))
    return found


class Items:
    """One memory section's items: a savepoint each, the keys that went with a drop, the record."""

    def __init__(self, conn, enabled=True):
        self.conn, self.enabled = conn, enabled
        self.meta = {}      # key -> what the item is, introduces and names
        self.dropped = {}   # key -> its record, in the order the host decided; kept across passes
        self.gone = {}      # a name a dropped item introduced -> that item's key
        self.applied = {}   # this pass only
        self.carriers = {}  # this pass only: a committed carrier's key -> the sources it rests on

    def run(self, apply):
        """`apply()` is one pass over every item. It is repeated until a pass ends consistent."""
        if not self.enabled:
            return apply()
        while True:
            self.applied, self.carriers = {}, {}
            self.conn.execute("SAVEPOINT memory_items")
            try:
                result = apply()
            except Restart:
                self.conn.execute("ROLLBACK TO memory_items")
                self.conn.execute("RELEASE memory_items")
                continue
            self.conn.execute("RELEASE memory_items")
            return result

    @contextmanager
    def item(self, kind, key, *, names=(), needs=(), evidence=()):
        """Apply one item, or one later part of it. Yields False when it is not to be applied."""
        if not self.enabled:
            yield True
            return
        meta = self.meta.setdefault(key, {"kind": kind, "names": tuple(n for n in names if n),
                                           "needs": tuple(n for n in needs if n), "evidence": tuple(evidence)})
        if key in self.dropped:
            yield False
            return
        cause = next((self.gone[name] for name in meta["needs"] if name in self.gone), None)
        if cause is not None:
            self._drop(key, self.dropped[cause]["code"], CASCADED, cause)
            yield False
            return
        self.conn.execute("SAVEPOINT memory_item")
        try:
            yield True
        except sqlite3.Error:
            # The database, not the proposal, failed: nothing about this transaction can be trusted.
            raise
        except Exception as error:  # noqa: BLE001 - recorded as a static code and host text, never the payload
            self.conn.execute("ROLLBACK TO memory_item")
            self.conn.execute("RELEASE memory_item")
            found = classify(error)
            if found.handling != "block" or found.kind == "unknown":
                raise
            code = found.code if isinstance(found.code, str) and re.fullmatch(r"[a-z0-9-]{1,60}", found.code) else "conflict"
            self._drop(key, code, static_message(error))
            return
        self.conn.execute("RELEASE memory_item")
        self.applied[key] = True

    def _drop(self, key, code, message, cause=None):
        meta = self.meta[key]
        self.dropped[key] = {"section": SECTION, "item": {"kind": meta["kind"], "key": key}, "code": code,
                             "message": message, **({"cascaded_from": cause} if cause else {})}
        for name in meta["names"]:
            self.gone.setdefault(name, key)
        # An earlier part of this item, or something that names it, is already in this pass.
        if key in self.applied or any(set(self.meta[other]["needs"]) & set(meta["names"]) for other in self.applied):
            raise Restart()

    def carry(self, key, sources):
        """A committed carrier and the sources whose content it took into memory."""
        if self.enabled:
            self.carriers[key] = set(sources)

    def kept(self, key):
        return key not in self.dropped

    def records(self):
        return list(self.dropped.values())

    def withheld(self, conn, roots):
        """Root sources whose content no committed item carries, although one was proposed."""
        lost = [self.meta[key] for key in self.dropped if self.meta[key]["kind"] in CARRIERS]
        if not lost:
            return set()
        root_ids = {ref["source_id"] for ref in roots}
        uncovered = root_ids - set().union(*self.carriers.values())
        named, unattributed = set(), False
        for meta in lost:
            cited = cited_sources(conn, meta["evidence"]) & root_ids
            named |= cited
            # A dropped carrier that cites no root may have carried any of them.
            unattributed = unattributed or not cited
        return uncovered if unattributed else uncovered & named

    def metric(self, conn, at, event_id, withheld, abandoned):
        """Counts per static code and kind. No key, no identifier the model wrote, no content."""
        if not self.dropped:
            return
        codes, kinds = {}, {}
        for record in self.dropped.values():
            codes[record["code"]] = codes.get(record["code"], 0) + 1
            kinds[record["item"]["kind"]] = kinds.get(record["item"]["kind"], 0) + 1
        conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)", (METRIC, len(self.dropped), at, dumps({
            "event_id": event_id, "codes": codes, "kinds": kinds,
            "cascaded": sum("cascaded_from" in record for record in self.dropped.values()),
            "withheld_sources": len(withheld), "abandoned_sources": abandoned})))


def settle(conn, scope, processed, withheld, event_id, at):
    """The ledger of sources still to be organised, inside the commit that decided it.

    A source this commit did organise leaves the ledger, whoever had put it there. A withheld
    one waits for its memory-only pass; withheld again by that pass, it is left for an operator
    rather than paid for without end. It is never written down as organised.
    """
    organized = [sid for sid in processed if sid not in withheld]
    for start in range(0, len(organized), 400):
        chunk = organized[start:start + 400]
        conn.execute("DELETE FROM mind_memory_unorganized WHERE scope=? AND source_id IN (" + ",".join("?" * len(chunk)) + ")",
                     (scope, *chunk))
    abandoned = 0
    for sid in sorted(withheld):
        row = conn.execute("SELECT attempts FROM mind_memory_unorganized WHERE scope=? AND source_id=?", (scope, sid)).fetchone()
        attempts = (row[0] if row else 0) + 1
        state = "pending" if attempts <= REORGANIZE_LIMIT else "abandoned"
        abandoned += state == "abandoned"
        conn.execute("INSERT INTO mind_memory_unorganized VALUES(?,?,?,?,?,?) ON CONFLICT(scope,source_id) DO UPDATE SET "
                     "state=excluded.state,attempts=excluded.attempts,updated_at=excluded.updated_at,data=excluded.data",
                     (scope, sid, state, attempts, at, dumps({"event_id": event_id})))
    return abandoned


def waiting(conn, scope):
    return [dict(row, data=json.loads(row["data"])) for row in conn.execute(
        "SELECT source_id,state,attempts,data FROM mind_memory_unorganized WHERE scope=? AND state IN ('pending','queued') "
        "ORDER BY updated_at,source_id", (scope,)).fetchall()]


def mark(conn, scope, source_ids, state, at, **facts):
    """Static facts only: a job id, a host reason."""
    for sid in source_ids:
        row = conn.execute("SELECT data FROM mind_memory_unorganized WHERE scope=? AND source_id=?", (scope, sid)).fetchone()
        if row:
            conn.execute("UPDATE mind_memory_unorganized SET state=?,updated_at=?,data=? WHERE scope=? AND source_id=?",
                         (state, at, dumps({**json.loads(row[0]), **facts}), scope, sid))
