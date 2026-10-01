"""Sealed entries (暗房 and 时光信): a diary Kin seals, a letter the owner writes, each opened on its day.

**Where the lock is.** Not in a reader. Until its day a sealed entry is not in the store at all: no
source, no record, no blob, no index row, no extraction job, no vector, nothing in the state, a queue
row or a cache. Its words are kept in one row of `mind_sealed_entries`, packed (zlib, then base64) so
that not even a scan of the store's bytes finds them, and only this module reads that table: the
opening (`open_due`), which the review minute runs and which hands the words to `Engine.receive` on
the day they are due, and the console's list (`entries`), which shows a placeholder and never the
words. Every reader there is -- recall and the context, an assessment and its recall tools, archive
memories and digests, the graph, the MCP reads, exploration briefs, the console's record views -- reads
the store as if the entry were not there, and so does every reader added later. Extraction, embedding
and indexing start when the entry becomes a source, which is when it opens. The packing is not
encryption: it keeps the words out of anything that reads or searches the store as text.

A diary is sealed inside the commit of the assessment that wrote it (`take`, then `seal_diary`): the
words leave the proposal before anything stores or applies it, so the queue row, the state, the event
history and the reflection never hold them. The main-session fork never seals: its own transcript
would keep the words outside the store.

**Erasure reaches through the lock.** A row names the source its words will become (`source_id`, fixed
from the start) and everything it was written from (`inputs`). An erase that takes any of them deletes
the row inside the erase's own transaction (`erase`, called by `kin_mind.erasure.erase`): erasing the
reserved source id -- what the console's delete does (`erase_entry`) -- or anything a sealed diary rests
on. The tombstone the erase writes for the reserved id keeps the words from ever being received. An
opened entry is a source like any other and goes as one.

**On its day** the entry becomes the source it would have been: a letter as the owner's own explicit
words (extracted and indexed then), a diary as the kin-reflection it would have been, resting on what
it was written from (`derived_from`). The row keeps its dates and the source id, and nothing else.
`opened` tells an assessment, as a fact, which entries opened lately; whether to mention one is Kin's.

`sealed_entries` (memory setting, off by default) decides whether anything can be sealed and whether
an assessment is told about sealed entries. An entry already sealed opens on its day whatever it says.
"""
from __future__ import annotations

import base64
import json
import zlib
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, Deleted, Missing, digest, dumps
from eventmem.core.models import SourceInput

from .state import timestamp

SETTING = "sealed_entries"
TABLE = "mind_sealed_entries"
# Made by the first seal, never by a store that has not used the feature: a store with the setting
# off keeps exactly the structure it had.
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS mind_sealed_entries(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,"
    "state TEXT NOT NULL,unlock_at TEXT NOT NULL,created_at TEXT NOT NULL,opened_at TEXT,source_id TEXT NOT NULL,"
    "inputs TEXT NOT NULL,data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS mind_sealed_due ON mind_sealed_entries(scope,state,unlock_at)",
)
KINDS = ("letter", "diary")
# A day is a day in the owner's time zone, the one the diary's `today_count` uses.
ZONE = ZoneInfo("Asia/Singapore")
LETTER_NAMESPACE = "kin-owner-letter"
DIARY_NAMESPACE = "kin-reflection"
LETTER_MAX = 20_000
# How long an opened entry stays among an assessment's facts.
OPENED_HOURS = 24
# Placeholders of sealed diaries an assessment is shown, newest first.
DIARIES_SHOWN = 5
# Entries opened in one review minute; the rest open in the next.
OPEN_BATCH = 20
DERIVED_CONFLICTS = frozenset({"derived-from-deleted", "derived-from-changed"})


def _table(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def _ensure(conn):
    for statement in SCHEMA:
        conn.execute(statement)


def enabled(conn, scope_key):
    from .autonomy_schema import enabled as flag
    return flag(conn, scope_key, SETTING)


def _pack(value):
    return base64.b64encode(zlib.compress(dumps(value).encode())).decode()


def _unpack(text):
    return json.loads(zlib.decompress(base64.b64decode(text)).decode())


def source_id(namespace, key, scope):
    """The id `Engine.receive` gives the source an entry becomes (version 1)."""
    return "src_" + digest([namespace, key, "1", scope.key()])[:32]


def root_id(sid):
    return "mem_" + digest([sid, "root"])[:32]


def today(at):
    return timestamp(at).astimezone(ZONE).date()


def window(at):
    """The days an entry may open on: from tomorrow to the same day a year from today."""
    day = today(at)
    try:
        last = day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        last = day.replace(year=day.year + 1, day=28)
    return day + timedelta(days=1), last


def parse_day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("An unlock day is YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("An unlock day is YYYY-MM-DD") from None


def clamp(day, at):
    """A diary's day held to the window: one too early opens tomorrow, one too late a year from today."""
    first, last = window(at)
    return min(max(day, first), last)


def take(proposal, *, sealing, at):
    """Before anything stores or applies a proposal: a diary Kin sealed leaves it, and comes back
    as what `seal_diary` keeps. Where sealing is not offered a date is dropped and the diary is an
    ordinary one, which is what a proposal with a field the schema did not have always came to."""
    understanding = proposal.understanding
    if understanding is None or understanding.unlock_at is None:
        return proposal, None
    if not sealing or understanding.basis != "internal_thought":
        return proposal.model_copy(update={"understanding": understanding.model_copy(update={"unlock_at": None})}), None
    day = clamp(parse_day(understanding.unlock_at), at)
    kept = understanding.model_dump(exclude={"unlock_at"})
    return proposal.model_copy(update={"understanding": None}), {"understanding": kept, "unlock_at": day.isoformat()}


def _bare(ref):
    return {key: ref[key] for key in ("source_id", "record_id") if isinstance(ref.get(key), str)}


def seal_diary(mind, conn, event_id, sealed, *, derived_from, shown, at):
    """Keep a sealed diary in the commit of the assessment that wrote it. What it is written from is
    checked here, as `Engine.receive` checks a reflection's: one deleted or changed since it was read,
    and the diary is not kept. Returns the entry id, or None."""
    sid = source_id(DIARY_NAMESPACE, event_id, mind.scope)
    if conn.execute("SELECT 1 FROM tombstones WHERE key=?", (sid,)).fetchone():
        return None
    try:
        kept, rests = mind.engine._derivable(conn, derived_from, mind.scope, shown)
    except Conflict as error:
        if getattr(error, "code", None) in DERIVED_CONFLICTS:
            return None
        raise
    _ensure(conn)
    entry = "seal_" + digest([mind.scope.key(), "diary", event_id])[:32]
    refs = [_bare(ref) for ref in kept if _bare(ref)]
    inputs = sorted({sid, root_id(sid), *rests, *(value for ref in refs for value in ref.values())})
    body = {"understanding": sealed["understanding"], "event_id": event_id, "occurred_at": at, "derived_from": refs}
    conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (entry, mind.scope.key(), "diary", "sealed", sealed["unlock_at"], at, None, sid, dumps(inputs),
                  dumps({"packed": _pack(body)})))
    return entry


def seal_letter(mind, text, unlock_at, command_id):
    """A letter the owner writes to Kin, opened on `unlock_at` (YYYY-MM-DD, tomorrow to a year ahead).
    The same command id with the same letter answers what it answered; with another, a conflict."""
    if not isinstance(text, str) or not text.strip() or len(text) > LETTER_MAX:
        raise ValueError(f"A letter is 1..{LETTER_MAX} characters")
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 200:
        raise ValueError("A letter needs a command id")
    at = mind.clock()
    day = parse_day(unlock_at)
    first, last = window(at)
    if not first <= day <= last:
        raise ValueError("A letter opens on a day from tomorrow to a year from today")
    entry = "seal_" + digest([mind.scope.key(), "letter", command_id])[:32]
    fingerprint = digest(["letter", text, day.isoformat()])
    with mind.engine.db.connect(write=True) as conn:
        if not enabled(conn, mind.scope.key()):
            raise Conflict("Sealed entries are switched off", code="sealed-entries-off")
        _ensure(conn)
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE id=?", (entry,)).fetchone()
        if row:
            if json.loads(row["data"]).get("fingerprint", fingerprint) != fingerprint:
                raise Conflict("Idempotency key reused with different content", code="payload-changed", target=entry)
            return view(row)
        sid = source_id(LETTER_NAMESPACE, entry, mind.scope)
        conn.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (entry, mind.scope.key(), "letter", "sealed", day.isoformat(), at, None, sid,
                      dumps(sorted({sid, root_id(sid)})), dumps({"packed": _pack({"text": text}), "fingerprint": fingerprint})))
        return view(conn.execute(f"SELECT * FROM {TABLE} WHERE id=?", (entry,)).fetchone())


PLACEHOLDER = {"letter": "一封 {} 才能打开的信", "diary": "一篇 {} 才能打开的日记"}


def view(row):
    """What any reader is shown of an entry: never its words."""
    opened = row["state"] == "opened"
    return {"id": row["id"], "kind": row["kind"], "state": row["state"], "unlock_at": row["unlock_at"],
            "created_at": row["created_at"], "opened_at": row["opened_at"],
            "placeholder": None if opened else PLACEHOLDER[row["kind"]].format(row["unlock_at"]),
            **({"source_id": row["source_id"], "record_id": root_id(row["source_id"])} if opened else {})}


def entries(mind, *, cursor="", limit=50):
    """The scope's entries, newest first, as placeholders and dates."""
    with mind.engine.db.connect() as conn:
        if not _table(conn):
            return {"items": [], "cursor": None, "enabled": enabled(conn, mind.scope.key())}
        where, args = "", []
        if cursor:
            at, _, last = cursor.partition("|")
            where, args = " AND (created_at<? OR (created_at=? AND id<?))", [at, at, last]
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE scope=?{where} ORDER BY created_at DESC,id DESC LIMIT ?",
                            [mind.scope.key(), *args, limit + 1]).fetchall()
        return {"items": [view(row) for row in rows[:limit]],
                "cursor": f"{rows[limit - 1]['created_at']}|{rows[limit - 1]['id']}" if len(rows) > limit else None,
                "enabled": enabled(conn, mind.scope.key())}


def erase_entry(mind, entry):
    """Erase one entry, sealed or opened: the erase of the source it is or will be, through the one
    path every erase takes (`Engine.delete`), which takes this row with it."""
    with mind.engine.db.connect() as conn:
        row = conn.execute(f"SELECT source_id FROM {TABLE} WHERE id=? AND scope=?", (entry, mind.scope.key())).fetchone() \
            if _table(conn) else None
    if not row:
        raise Missing(entry)
    mind.engine.delete(row["source_id"])
    return {"id": entry, "status": "deleted"}


def erase(conn, ids, *, write=True):
    """Inside an erase's transaction: every entry that is, or rests on, any of `ids` goes whole. A
    row's `inputs` name its own source and root record too. Returns how many go (or would)."""
    if not ids or not _table(conn):
        return 0
    from .erasure import mentions
    doomed = sorted({row["id"] for row in mentions(conn, TABLE, ids, "id", column="inputs")})
    if write:
        for entry in doomed:
            conn.execute(f"DELETE FROM {TABLE} WHERE id=?", (entry,))
    return len(doomed)


def _receive(mind, row, body):
    if row["kind"] == "letter":
        return mind.engine.receive(SourceInput(
            namespace=LETTER_NAMESPACE, key=row["id"], scope=mind.scope, session="sealed-letter", occurred_at=row["created_at"],
            text=body["text"], authority="explicit", kind="episode", extract=True,
            metadata={"host_event": "letter", "role": "user", "sealed_until": row["unlock_at"]}))
    from .memory import MemoryContinuity
    # The kin-reflection it would have been, resting on what it was written from: one of them deleted
    # since, and it is not received. What it rests on changing since is no reason (its words are as old).
    return MemoryContinuity(mind)._reflect({"event_id": body["event_id"]}, body["understanding"],
                                           {"occurred_at": body["occurred_at"]}, body["derived_from"], None)


def open_due(mind):
    """The review minute's: every entry of this scope whose day has come becomes its source. One the
    erase or a deletion beneath it took meanwhile is gone with its words. Asks no model. An entry that
    cannot be opened for any other reason stays sealed and is tried again the next minute; the minute
    itself is never stopped by one (its error class goes to the metrics, never its words)."""
    at = mind.clock()
    with mind.engine.db.connect() as conn:
        if not _table(conn):
            return {"state": "idle"}
        rows = conn.execute(f"SELECT * FROM {TABLE} WHERE scope=? AND state='sealed' AND unlock_at<=? ORDER BY unlock_at,id LIMIT ?",
                            (mind.scope.key(), today(at).isoformat(), OPEN_BATCH)).fetchall()
    opened, dropped, failed = [], [], []
    for row in rows:
        try:
            with mind.engine.db.connect() as conn:
                # Taken by an erase already: nothing to receive, not even the bytes a receipt writes first.
                inputs = json.loads(row["inputs"])
                gone = conn.execute("SELECT 1 FROM tombstones WHERE key IN (" + ",".join("?" * len(inputs)) + ") LIMIT 1",
                                    inputs).fetchone() if inputs else None
            try:
                received = None if gone else _receive(mind, row, _unpack(json.loads(row["data"])["packed"]))
            except Conflict as error:
                if not (isinstance(error, Deleted) or getattr(error, "code", None) in DERIVED_CONFLICTS):
                    raise
                received = None
            with mind.engine.db.connect(write=True) as conn:
                if received is None:
                    conn.execute(f"DELETE FROM {TABLE} WHERE id=? AND state='sealed'", (row["id"],))
                    dropped.append(row["id"])
                    continue
                conn.execute(f"UPDATE {TABLE} SET state='opened',opened_at=?,data='{{}}' WHERE id=? AND state='sealed'",
                             (at, row["id"]))
                opened.append(row["id"])
        except Exception as error:  # noqa: BLE001 - one entry never stops the minute
            failed.append(row["id"])
            mind.engine.db.metric("sealed_open_failed", 1, {"entry": row["id"], "error": type(error).__name__})
    return {"state": "opened" if opened or dropped else "failed" if failed else "idle",
            "opened": opened, "dropped": dropped, **({"failed": failed} if failed else {})}


def opened(conn, scope_key, at):
    """Entries opened in the last day, as an assessment's facts: kind, the source and record to read,
    how long ago and for how many days it was sealed. Counts and ids, never words."""
    if not _table(conn):
        return []
    now = timestamp(at)
    since = (now - timedelta(hours=OPENED_HOURS)).isoformat()
    found = []
    for row in conn.execute(f"SELECT * FROM {TABLE} WHERE scope=? AND state='opened' AND opened_at>=? ORDER BY opened_at DESC,id",
                            (scope_key, since)):
        found.append({"kind": row["kind"], "source_id": row["source_id"], "record_id": root_id(row["source_id"]),
                      "hours_since_opened": round(max(0.0, (now - timestamp(row["opened_at"])).total_seconds()) / 3600, 1),
                      "days_sealed": (today(row["opened_at"]) - today(row["created_at"])).days})
    return found


def sealed_diaries(conn, scope_key):
    """Sealed diaries still waiting, newest first, as placeholders: when written, when they open."""
    if not _table(conn):
        return []
    return [{"at": row["created_at"], "unlock_at": row["unlock_at"]} for row in conn.execute(
        f"SELECT created_at,unlock_at FROM {TABLE} WHERE scope=? AND kind='diary' AND state='sealed'"
        " ORDER BY created_at DESC,id DESC LIMIT ?", (scope_key, DIARIES_SHOWN))]


def diaries_written(conn, scope_key, start, end):
    """How many diaries were sealed between `start` and `end`, opened since or not."""
    if not _table(conn):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE scope=? AND kind='diary' AND julianday(created_at)>=julianday(?)"
                        " AND julianday(created_at)<julianday(?)", (scope_key, start, end)).fetchone()[0]
