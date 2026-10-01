"""Enrichment waits for a quiet conversation (`enrichment_settle_minutes`, 0 = off).

With `operational_lanes`, every assessment of new conversation leaves an enrichment job on the
background lane, and each job used to be available the moment it was made: notes were written
from half a conversation, and the correction or the answer that came after became a note of its
own. Emotion, wishes and the contact decision are untouched by this: the action lane still runs
every turn. Only the memory lane waits.

* **When.** An enrichment job made with the setting on is available at the scope's last
  conversation activity -- an owner message, an assistant message or a delivery, by the time it
  happened -- plus the setting's minutes. Each new conversation event moves every waiting job to
  the new time (`push`), from the event's own time, so the jobs of one conversation wait together.
  The latest activity being an outreach of Kin's own (a delivery of `OUTREACH_ORIGINS`) keeps the
  conversation open for its answer up to `OUTREACH_WAIT_MINUTES`.
* **One request.** The job the lane claims takes in every other enrichment job of the scope that is
  due, as one batch (the action lane's batch rows: `batch_ids`, `own`, children `batched` and
  settled by the commit): the union of their evidence, as long as it stays within `MERGE_IDS`
  sources and half of the appraisal input budget. A memory proposal its parent left for it
  (`seed_memory`) was written from part of that, so a row that takes others in is judged afresh;
  a job that runs alone still uses its seed as before.
* **Meanwhile.** The sources are stored at intake: lexical and source recall find the raw words
  before any note is written.

Only the host's own times decide this; nothing of it is shown to a model. Off, a job is made
available at once and with the data it always had, and nothing is pushed or merged."""
from __future__ import annotations

from eventmem.core.db import Missing, digest

from .state import timestamp

SETTING = "enrichment_settle_minutes"
SETTLE_RANGE = (0, 1440)
STIMULUS = "memory-enrichment"
# What counts as conversation, and which deliveries are Kin starting one.
CONVERSATION = ("owner-message", "assistant-message", "delivery")
OUTREACH_ORIGINS = ("proactive", "reminder")
OUTREACH_WAIT_MINUTES = 180
# The most sources one merged request carries, and the share of the input budget their text may take.
MERGE_IDS = 50
MERGE_BUDGET_SHARE = 2
# How many waiting jobs one claim looks at.
MERGE_CANDIDATES = 24
# What a row keeps of the memory its parent proposed; a row that takes others in no longer uses it.
SEED_FIELDS = ("seed_memory", "seed_receipt", "seed_sources", "seed_manifest", "seed_tombstone_mark", "seed_rejected")


def minutes(settings):
    value = settings.get(SETTING) or 0
    return value if type(value) is int and value > 0 else 0


def due(conn, scope, settle_minutes):
    """When the scope's conversation will have been quiet `settle_minutes`, in epoch seconds, or
    None when it has had none."""
    row = conn.execute(
        "SELECT kind,occurred_at,json_extract(data,'$.origin') AS origin FROM mind_runtime_events "
        "WHERE scope=? AND kind IN (?,?,?) AND COALESCE(json_extract(data,'$.historical'),0)=0 "
        "ORDER BY julianday(occurred_at) DESC,seq DESC LIMIT 1", (scope, *CONVERSATION)).fetchone()
    if not row:
        return None
    wait = settle_minutes
    if row["kind"] == "delivery" and row["origin"] in OUTREACH_ORIGINS:
        wait = max(settle_minutes, OUTREACH_WAIT_MINUTES)
    return timestamp(row["occurred_at"]).timestamp() + wait * 60


def available(conn, scope, settle_minutes, now):
    """When a job made now becomes available: when its conversation is quiet, which may have been
    already -- the same time `push` gives every job waiting with it."""
    quiet = due(conn, scope, settle_minutes)
    return now if quiet is None else quiet


def push(conn, scope, settle_minutes):
    """Every job still waiting for the conversation waits from its latest event. Inside the
    transaction that records the event."""
    if not settle_minutes or not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_appraisals'").fetchone():
        return 0
    quiet = due(conn, scope, settle_minutes)
    if quiet is None:
        return 0
    return conn.execute("UPDATE mind_appraisals SET available=? WHERE scope=? AND state='pending' AND available<>? "
                        "AND json_extract(data,'$.stimulus')=? AND json_extract(data,'$.settle')=1",
                        (quiet, scope, quiet, STIMULUS)).rowcount


def evidence_tokens(engine, conn, identifiers, cache):
    """What the text of `identifiers` costs, as the action lane's batch counts it."""
    from eventmem.core.retrieval import tokens
    total = 0
    for identifier in identifiers:
        if identifier not in cache:
            record_id = "mem_" + digest([identifier, "root"])[:32] if identifier.startswith("src_") else identifier
            try:
                cache[identifier] = tokens(engine._get(conn, record_id)["content"])
            except Missing:
                # Gone since: the attempt's own preparation finds it so; it costs nothing here.
                cache[identifier] = 0
        total += cache[identifier]
    return total
