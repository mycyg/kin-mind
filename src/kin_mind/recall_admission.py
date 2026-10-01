"""Relevance admission for the automatic memory context (`recall_admission`), and 小光's
"不主动提起" marks (`recall_quiet_marks`).

**What it gates.** Only what the host injects on its own before a chat reply or a proactive draft
(`Contexts.build(automatic=True)`, purpose chat or proactive), and only the recall items of it: the
lexical records, the graph nodes and the one neighbour, the works and the shares, the deep pool.
Host runtime, intent, habits, affect, the trait ledger, constraint records, the manifests and an
item named by its exact id are never gated. Explicit reads (purpose read, the MCP tools, the
console's recall lab) are not touched at all.

**How.** Light: the cosine of the resolved recall query and an excerpt of the item's own text, from
the local embedding service (`docs/local-embedding.md`); an item passes at `recall_admission_threshold`.
Deep: the RecallRanking selection is the admission -- the ranked tail is not. The passing items are
taken best first, at most `recall_admission_quota` of them; then what this window has already seen
goes, and nothing weaker takes its place. A note that a ready event digest beside it covers goes too.

**Fail-closed.** No score is no admission: a scoring service that is off, slow, busy, remote, or a
look (which asks no model) leaves only the exempt items, and the context says in one line that more
can be read with the tools (`ADMISSION_MORE_NOTE`).

**Shadow.** `recall_admission: "shadow"` decides exactly as "on" would and records it, and injects
what it always did.

**What is kept.** One observation per build (`mind_recall_observations`): purpose, mode, setting,
state, each candidate's id, revision, route and score, what was admitted, what was dropped and why,
and timings -- ids and numbers, never text, never the query, never a source's metadata. The newest
`OBSERVATIONS_KEPT` per scope. An erase deletes every observation naming what it erased
(kin_mind.erasure). Query vectors and scores are never stored. An item's vector is kept for reuse
(`mind_recall_vectors`: the hash of the model and the scored text, the item's id, the vector), the
newest `VECTORS_KEPT` per scope, and an erase deletes every one kept for an item it reached.

**Marks.** `mind_recall_quiet` holds the items 小光 asked Kin not to bring up on its own: set through
the main session's tool with 小光's own message as evidence, or by 小光 in the console. With
`recall_quiet_marks` on, an automatic context skips them (and, for a graph node, the records it
holds); explicit reads still return them. A mark goes with its item when the item is erased.
"""
from __future__ import annotations

import json
import time
import uuid
from array import array
from collections import Counter

from eventmem.core.db import Conflict, Missing, digest, dumps

SETTING = "recall_admission"
MODES = ("off", "shadow", "on")
# The automatic purposes the gate applies to. Startup and work contexts are left as they are.
PURPOSES = frozenset({"chat", "proactive"})
# Items that pass without a score and do not count against the quota.
EXEMPT_ROUTES = frozenset({"constraint", "exact", "pinned"})
# How many candidates of each route are scored at most, in the order the lane found them. One
# beyond is dropped as `pool`: never scored, never admitted.
POOL = {"lexical": 10, "graph": 6, "neighbor": 1, "work": 3, "share": 5, "deep": 40}
DEFAULT_THRESHOLD = 0.55
DEFAULT_QUOTA = 4
DEFAULT_TIMEOUT_MS = 3000
QUOTA_RANGE = (1, 16)
TIMEOUT_RANGE = (200, 20000)
OBSERVATIONS_KEPT = 500
# What of an item is scored: an excerpt of its own text near the query's words, at most this long.
SCORE_TOKENS = 200
SCORE_CHARS = 600
QUERY_CHARS = 1000
# The embedding service takes 1-16 texts a request.
BATCH = 16
# Item vectors kept per scope, newest first: about 4 KB each.
VECTORS_KEPT = 4000
# Reason codes an observation names a drop by.
REASONS = ("below_threshold", "no_score", "no_query", "quota", "seen", "covered", "quiet", "pool",
           "not_selected")

# Model-facing (NEEDS 小光 OK): the one line an automatic context carries when its recall items
# could not be scored, so none were taken.
ADMISSION_MORE_NOTE = "本轮未能按相关性筛选旧记忆，因此没有自动带入；需要时可用 read_continuity_context 查询。"

# Model-facing (NEEDS 小光 OK): the main session's tool for a quiet mark (kin_mind.mcp). The host offers
# it only once `set_memory_quiet` is in its chat-permissions.json.
QUIET_TOOL_DESCRIPTION = (
    "小光明确说某件旧事以后不要主动提起时使用，也可以按小光的话撤回。需要 command_id、item_id（记录或图谱节点编号）、"
    "quiet（true 为不主动提起，false 为撤回）、evidence_ids（小光说这句话的消息编号）和 reason。"
    "设置后自动带入的背景资料不再包含这一项；小光问起或你主动查询时照常可以读取。")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_recall_observations(
 scope TEXT NOT NULL,id TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,id));
CREATE INDEX IF NOT EXISTS mind_recall_observations_at ON mind_recall_observations(scope,at);
CREATE TABLE IF NOT EXISTS mind_recall_quiet(
 scope TEXT NOT NULL,item_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,item_id));
CREATE TABLE IF NOT EXISTS mind_recall_vectors(
 scope TEXT NOT NULL,key TEXT NOT NULL,item_id TEXT NOT NULL,at REAL NOT NULL,vector BLOB NOT NULL,PRIMARY KEY(scope,key));
CREATE INDEX IF NOT EXISTS mind_recall_vectors_item ON mind_recall_vectors(item_id);
CREATE INDEX IF NOT EXISTS mind_recall_vectors_at ON mind_recall_vectors(scope,at);
"""
OBSERVATIONS = "mind_recall_observations"
QUIET = "mind_recall_quiet"
VECTORS = "mind_recall_vectors"

class Unavailable(Exception):
    """Scoring could not run. The message is a short code, never a provider's body."""


def ensure(engine):
    from .state import ensure_schema
    ensure_schema(engine, "recall-admission", SCHEMA)


def stored_settings(conn, scope):
    """The memory settings of `scope` as `MemoryContinuity.settings` reads them, on the caller's
    connection: a write that depends on one checks it inside its own transaction."""
    import sqlite3

    from .memory import DEFAULTS, RETIRED_SETTINGS
    try:
        row = conn.execute("SELECT data FROM mind_memory_config WHERE scope=?", (scope.key(),)).fetchone()
    except sqlite3.OperationalError:
        row = None
    stored = json.loads(row[0]) if row else {}
    return DEFAULTS | {key: value for key, value in stored.items() if key not in RETIRED_SETTINGS}


def settings_valid(config):
    """The admission settings as `MemoryContinuity.configure` checks them."""
    if config.get(SETTING, "off") not in MODES:
        raise ValueError("recall_admission is off, shadow or on")
    threshold = config.get("recall_admission_threshold", DEFAULT_THRESHOLD)
    if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
        raise ValueError("recall_admission_threshold is a number from 0 to 1")
    quota = config.get("recall_admission_quota", DEFAULT_QUOTA)
    if type(quota) is not int or not QUOTA_RANGE[0] <= quota <= QUOTA_RANGE[1]:
        raise ValueError(f"recall_admission_quota is a whole number, {QUOTA_RANGE[0]}..{QUOTA_RANGE[1]}")
    timeout = config.get("recall_admission_timeout_ms", DEFAULT_TIMEOUT_MS)
    if type(timeout) is not int or not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
        raise ValueError(f"recall_admission_timeout_ms is a whole number, {TIMEOUT_RANGE[0]}..{TIMEOUT_RANGE[1]}")


# --- scoring ---------------------------------------------------------------------------------------

def _plain(text):
    """An item's words without the JSON around them: a graph digest and a node's facts are stored
    as JSON text, and their keys are not what it is about."""
    if not isinstance(text, str):
        return ""
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            value = json.loads(stripped)
        except ValueError:
            return text
        words = []

        def walk(node):
            if isinstance(node, str):
                words.append(node)
            elif isinstance(node, dict):
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(value)
        return "\n".join(words)
    return text


def score_text(item, query):
    """What of an item is scored: complete sentences of its own text near the query's words."""
    from .adaptive_recall import evidence_excerpt
    excerpt, _ = evidence_excerpt(_plain(item.get("text")), query, budget=SCORE_TOKENS)
    return excerpt[:SCORE_CHARS]


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm = (sum(a * a for a in left) ** .5) * (sum(b * b for b in right) ** .5)
    return dot / norm if norm else 0.0


def embed(engine, scope, entries, deadline):
    """Vectors of `entries` -- `(item_id, text)`, item_id None for the query -- from the local
    embedding service, within `deadline` (monotonic), and how many the service computed now. An
    item's vector is kept (`mind_recall_vectors`, by the hash of the model and the text, beside the
    item's id, which an erase finds it by) and reused while its text is the same; the query's never
    is. Raises Unavailable with a code. Only the local service is ever asked: a remote embedding role
    would be a paid call on the reply's path, and is refused (`embedding-not-local`)."""
    from eventmem.core.db import recording
    from eventmem.core.providers import NotConfigured, Providers

    from .adaptive_recall import bounded

    if not recording():
        # A look asks no model, not even for an embedding (CR3-MM-04).
        raise Unavailable("session-required")
    try:
        config = Providers(engine).role("embedding")
    except NotConfigured:
        raise Unavailable("embedding-not-configured") from None
    if not config.local_embedding:
        raise Unavailable("embedding-not-local")
    base = [config.model, config.preprocessing, config.dimensions]
    keys = [digest([*base, text]) if item_id else None for item_id, text in entries]
    out, kept = [None] * len(entries), {}
    wanted = sorted({key for key in keys if key})
    if wanted:
        with engine.db.connect() as conn:
            for start in range(0, len(wanted), 400):
                page = wanted[start:start + 400]
                for key, blob in conn.execute(f"SELECT key,vector FROM {VECTORS} WHERE scope=? AND key IN ({','.join('?' for _ in page)})",
                                              (scope, *page)):
                    vector = array("f")
                    vector.frombytes(blob)
                    kept[key] = vector
    missing = []
    for index, key in enumerate(keys):
        if key in kept:
            out[index] = kept[key]
        else:
            missing.append(index)
    computed = []
    for start in range(0, len(missing), BATCH):
        page = missing[start:start + BATCH]
        remaining = deadline - time.monotonic()
        if remaining <= .05:
            raise Unavailable("deadline")

        def call(page=page, remaining=remaining):
            payload = {"model": config.model, "input": [entries[i][1] for i in page]}
            if config.dimensions:
                payload["dimensions"] = config.dimensions
            return Providers(engine, timeout=remaining).request("embedding", "embeddings", json_=payload)
        try:
            response = bounded(call, remaining)
        except TimeoutError as error:
            raise Unavailable("capacity" if "capacity" in str(error) else "deadline") from None
        except Exception as error:  # noqa: BLE001 - a code, never the provider's body
            raise Unavailable("embedding:" + type(error).__name__) from None
        rows = sorted(response.get("data") or [], key=lambda r: r.get("index", 0))
        if len(rows) != len(page):
            raise Unavailable("embedding-incomplete")
        for index, row in zip(page, rows):
            out[index] = array("f", row["embedding"])
            if keys[index]:
                computed.append((keys[index], entries[index][0], out[index]))
    if computed:
        import sqlite3
        try:
            with engine.db.connect(write=True) as conn:
                at = time.time()
                conn.executemany(f"INSERT OR REPLACE INTO {VECTORS} VALUES(?,?,?,?,?)",
                                 [(scope, key, item_id, at, vector.tobytes()) for key, item_id, vector in computed])
                conn.execute(f"DELETE FROM {VECTORS} WHERE scope=? AND key IN (SELECT key FROM {VECTORS} WHERE scope=? "
                             "ORDER BY at DESC,key LIMIT -1 OFFSET ?)", (scope, scope, VECTORS_KEPT))
        except sqlite3.OperationalError:
            # A busy store keeps no vector this time; the next build computes it again.
            pass
    return out, len(missing)


# --- the decision -----------------------------------------------------------------------------------

def decide(candidates, *, scores, threshold, quota, seen, state, selected=None, covered=None):
    """Which recall candidates go in. Pure: everything it needs is handed to it.

    `candidates`: `(item, route)` in the order the lanes found them. `scores`: id -> cosine, for the
    candidates scored. `selected`: the deep ranking's admitted ids in its order, or None. `state`:
    "scored", "ranked", or why no score exists ("unavailable:<code>", "no_query"). `covered(admitted)`
    returns `{id: covering_id}` for notes another admitted or seen item covers.

    Exempt routes pass without counting. Every other candidate passes only on a score at the
    threshold, or on the deep ranking's selection; the passing ones are taken best first (the
    ranking's order, then score), at most `quota`; what the window has seen goes after that, and
    nothing takes a dropped one's place."""
    admitted, dropped, passing = [], [], []
    for position, (item, route) in enumerate(candidates):
        if route in EXEMPT_ROUTES:
            continue
        identifier = item["id"]
        if route == "deep":
            if selected is None:
                dropped.append((identifier, "no_score"))
            elif identifier in selected:
                passing.append((0, selected.index(identifier), 0.0, position, item))
            else:
                dropped.append((identifier, "not_selected"))
            continue
        score = scores.get(identifier)
        if score is None:
            dropped.append((identifier, "no_query" if state == "no_query" else "no_score"))
        elif score >= threshold:
            passing.append((1, 0, -score, position, item))
        else:
            dropped.append((identifier, "below_threshold"))
    passing.sort(key=lambda entry: entry[:4])
    chosen = [entry[4] for entry in passing[:quota]]
    dropped.extend((item["id"], "quota") for *_, item in passing[quota:])
    for item in chosen:
        if seen.get(item["id"]) == item.get("revision"):
            dropped.append((item["id"], "seen"))
        else:
            admitted.append(item)
    if covered is not None and admitted:
        gone = covered(admitted)
        dropped.extend((identifier, "covered") for identifier in gone)
        admitted = [item for item in admitted if item["id"] not in gone]
    return admitted, dropped


class Admission:
    def __init__(self, contexts):
        self.contexts, self.mind, self.engine = contexts, contexts.mind, contexts.engine
        ensure(self.engine)

    def run(self, query, candidates, *, settings, seen, deep=None, notes=None, record=True):
        """Score the candidates and decide. Returns the decision and its observation."""
        started = time.monotonic()
        threshold = float(settings.get("recall_admission_threshold", DEFAULT_THRESHOLD))
        quota = int(settings.get("recall_admission_quota", DEFAULT_QUOTA))
        timeout = int(settings.get("recall_admission_timeout_ms", DEFAULT_TIMEOUT_MS)) / 1000
        pool_counts, scored, beyond = Counter(), [], []
        for item, route in candidates:
            if route in EXEMPT_ROUTES or route == "deep":
                continue
            pool_counts[route] += 1
            (scored if pool_counts[route] <= POOL.get(route, 0) else beyond).append((item, route))
        beyond_ids = {item["id"] for item, _ in beyond}
        kept = [(item, route) for item, route in candidates if item["id"] not in beyond_ids]
        scores, state, embedded, cached = {}, "scored", 0, 0
        selected = None
        if deep is not None:
            selected = deep.get("selected") if deep.get("state") == "ranked" else None
            state = "ranked" if selected is not None else "unavailable:" + (deep.get("reason") or "rank")
        if scored:
            if not query.strip():
                state = "no_query" if state in {"scored", "ranked"} else state
            else:
                entries = [(None, query[:QUERY_CHARS]), *((item["id"], score_text(item, query)) for item, _ in scored)]
                usable = [index for index, (_, text) in enumerate(entries) if text.strip()]
                try:
                    if not record:
                        # A look asks no model, not even for an embedding (CR3-MM-04).
                        raise Unavailable("session-required")
                    vectors, embedded = embed(self.engine, self.mind.scope.key(), [entries[i] for i in usable],
                                              time.monotonic() + timeout)
                    cached = len(usable) - embedded
                    by_index = dict(zip(usable, vectors))
                    if 0 not in by_index:
                        raise Unavailable("empty-query")
                    for index, (item, _) in enumerate(scored, start=1):
                        if index in by_index:
                            scores[item["id"]] = round(_cosine(by_index[0], by_index[index]), 4)
                except Unavailable as error:
                    # The deep ranking's selection still stands for its own items; the rest go unscored.
                    scores, state = {}, ("ranked:" if state == "ranked" else "unavailable:") + str(error)
        seen = seen or {}
        covered = None
        if notes:
            covered = lambda admitted: self.covered(admitted, notes, seen)  # noqa: E731
        admitted, dropped = decide(kept, scores=scores, threshold=threshold, quota=quota, seen=seen,
                                   state=state, selected=selected, covered=covered)
        dropped = [*dropped, *((item["id"], "pool") for item, _ in beyond)]
        exempt = [item for item, route in candidates if route in EXEMPT_ROUTES]
        observation = {
            "v": 1, "state": state.split(":")[0], "reason": state.split(":", 1)[1] if ":" in state else None,
            "threshold": threshold, "quota": quota,
            "candidates": [{"id": item["id"], "revision": item.get("revision"), "route": route,
                            "score": scores.get(item["id"])} for item, route in candidates],
            "admitted": [item["id"] for item in admitted],
            "exempt": [item["id"] for item in exempt],
            "dropped": [{"id": identifier, "reason": reason} for identifier, reason in dropped],
            "timings": {"score_ms": round((time.monotonic() - started) * 1000, 1), "scored": len(scores),
                        "embedded": embedded, "cached": cached},
        }
        # Nothing went in because nothing could be scored: the context says so, in one line.
        unscored = (not admitted and state.split(":")[0] in {"unavailable", "no_query"}
                    and any(reason in {"no_score", "no_query"} for _, reason in dropped))
        return {"admitted": admitted, "dropped": dropped, "state": state, "observation": observation,
                "unscored": unscored}

    def covered(self, admitted, notes, seen):
        """Notes (the appraisal's generated records) another admitted item, or one this window has
        seen, already carries: a ready event digest whose sources include every source of the note.
        Within the admitted, the one ranked lower of the two goes; past the window, the note goes."""
        wanted = [item["id"] for item in admitted if item["id"] in notes]
        if not wanted:
            return set()
        admitted_ids = [item["id"] for item in admitted]
        gone = set()
        with self.engine.db.connect() as conn:
            for note_id in wanted:
                for event_id in covering_digests(conn, self.mind.scope.key(), notes[note_id]):
                    if event_id in seen:
                        gone.add(note_id)
                    elif event_id in admitted_ids and event_id not in gone:
                        gone.add(note_id if admitted_ids.index(event_id) < admitted_ids.index(note_id) else event_id)
                    if note_id in gone:
                        break
        return gone

    def observe(self, observation, *, purpose, mode, setting, extra=None):
        """Keep one observation, and only the newest OBSERVATIONS_KEPT of the scope."""
        row = {**observation, "purpose": purpose, "mode": mode, "setting": setting, **(extra or {})}
        scope = self.mind.scope.key()
        with self.engine.db.connect(write=True) as conn:
            conn.execute(f"INSERT INTO {OBSERVATIONS} VALUES(?,?,?,?)",
                         (scope, uuid.uuid4().hex, self.mind.clock(), dumps(row)))
            conn.execute(f"DELETE FROM {OBSERVATIONS} WHERE scope=? AND id IN (SELECT id FROM {OBSERVATIONS} "
                         "WHERE scope=? ORDER BY at DESC,id DESC LIMIT -1 OFFSET ?)", (scope, scope, OBSERVATIONS_KEPT))


def note_facts(record):
    """What coverage asks of a record: its sources and evidence records, when it is a note."""
    attributes = record.get("attributes") or {}
    if not record.get("generated") or not attributes.get("semantic_event"):
        return None
    return {"source_ids": list(record.get("source_ids") or ()), "evidence_ids": list(record.get("evidence_ids") or ())}


def covering_digests(conn, scope, note):
    """The events whose ready digest rests on every source of `note`."""
    import sqlite3
    sources, evidence = set(note.get("source_ids") or ()), list(dict.fromkeys(note.get("evidence_ids") or ()))
    if not sources or not evidence:
        return []
    try:
        events = None
        for record_id in evidence:
            found = {row[0] for row in conn.execute(
                "SELECT event_id FROM mind_event_dependencies WHERE scope=? AND record_id=?", (scope, record_id))}
            events = found if events is None else events & found
            if not events:
                return []
        out = []
        for event_id in sorted(events):
            row = conn.execute("SELECT data FROM mind_event_digests WHERE scope=? AND event_id=? AND state='ready'",
                               (scope, event_id)).fetchone()
            if not row:
                continue
            members = list(json.loads(row[0]).get("source_versions") or {})
            held = set()
            for start in range(0, len(members), 200):
                page = members[start:start + 200]
                marks = ",".join("?" for _ in page)
                for (ids,) in conn.execute(f"SELECT json_extract(data,'$.source_ids') FROM records WHERE id IN ({marks})", page):
                    held.update(json.loads(ids or "[]"))
            if sources <= held:
                out.append(event_id)
        return out
    except sqlite3.OperationalError:
        return []


# --- the marks ------------------------------------------------------------------------------------

class QuietMarks:
    """Items 小光 asked not to be brought up unprompted."""

    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        ensure(self.engine)

    def active(self, conn):
        """Every id an automatic context leaves out: the marked items, and the records a marked
        graph node holds."""
        ids = set()
        rows = conn.execute(f"SELECT item_id,data FROM {QUIET} WHERE scope=?", (self.scope.key(),)).fetchall()
        for item_id, raw in rows:
            ids.add(item_id)
            if json.loads(raw).get("kind") == "graph":
                found = conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND id=?",
                                     (self.scope.key(), item_id)).fetchone()
                if found:
                    ids.update(i for i in json.loads(found[0]).get("record_ids") or () if isinstance(i, str))
        return ids

    def _kind(self, conn, item_id):
        if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
            raise ValueError("item_id names one record or graph node")
        try:
            record = self.engine._get(conn, item_id)
            if record["scope"] != self.scope.model_dump():
                raise Conflict("Item belongs to another scope")
            return "record"
        except Missing:
            pass
        if conn.execute("SELECT 1 FROM mind_graph_nodes WHERE scope=? AND id=?", (self.scope.key(), item_id)).fetchone():
            return "graph"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_memory_nodes'").fetchone() and conn.execute(
                "SELECT 1 FROM mind_memory_nodes WHERE scope=? AND id=?", (self.scope.key(), item_id)).fetchone():
            return "memory"
        raise Missing(item_id)

    def change(self, request, *, actor):
        """Set or clear one mark. `actor` "owner" is the main session's tool, which cites 小光's own
        current message (`evidence_ids`), checked as a conversation habit's is; "console" is 小光 in
        the console. Idempotent by `command_id`."""
        from eventmem.core.read_policy import ReadPolicy

        from . import evidence_refs
        from .evidence_classes import owner_statement

        if actor not in {"owner", "console"}:
            raise ValueError("Unknown actor")
        command_id, item_id, quiet = request.get("command_id"), request.get("item_id"), request.get("quiet", True)
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("command_id is required")
        if type(quiet) is not bool:
            raise ValueError("quiet is true or false")
        evidence_ids = request.get("evidence_ids") or []
        reason = request.get("reason", "")
        if actor == "owner":
            if not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 8 or any(not isinstance(i, str) for i in evidence_ids):
                raise ValueError("evidence_ids name 小光's own message")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
                raise ValueError("reason is required")
        payload = {"item_id": item_id, "quiet": quiet, "actor": actor, "evidence_ids": sorted(evidence_ids),
                   "reason": reason if actor == "owner" else ""}
        key = "recall-quiet:" + digest([self.scope.key(), command_id])
        with self.engine.db.connect(write=True) as conn:
            if not stored_settings(conn, self.scope)["recall_quiet_marks"]:
                raise Conflict("Quiet marks are not enabled (recall_quiet_marks)", kind="runtime", code="disabled")

            def run():
                kind = self._kind(conn, item_id)
                refs = []
                if actor == "owner":
                    proof = self.mind._evidence(conn, evidence_ids)
                    policy = ReadPolicy.load(self.engine, self.scope, conn=conn)
                    if not proof or not all(owner_statement(self.engine._get(conn, r["record_id"]), policy, sources=[r]) for r in proof) \
                            or not self.mind._fresh(conn, proof):
                        raise Conflict("A quiet mark needs 小光's own current message as evidence")
                    refs = [evidence_refs.trace(r) for r in proof]
                if not quiet:
                    conn.execute(f"DELETE FROM {QUIET} WHERE scope=? AND item_id=?", (self.scope.key(), item_id))
                    return {"state": "cleared", "item_id": item_id, "kind": kind}
                data = {"item_id": item_id, "kind": kind, "actor": actor, "at": self.mind.clock(), "command_id": command_id,
                        **({"reason": reason, "evidence": refs} if actor == "owner" else {})}
                data = evidence_refs.as_stored(conn, self.scope.key(), QUIET, data)
                conn.execute(f"INSERT OR REPLACE INTO {QUIET} VALUES(?,?,?)", (self.scope.key(), item_id, dumps(data)))
                return {"state": "quiet", "item_id": item_id, "kind": kind}
            return self.engine.command(conn, key, payload, run)

    def list(self, conn=None):
        if conn is None:
            with self.engine.db.connect() as connection:
                return self.list(connection)
        rows = conn.execute(f"SELECT item_id,data FROM {QUIET} WHERE scope=? ORDER BY item_id", (self.scope.key(),)).fetchall()
        return [{k: v for k, v in json.loads(raw).items() if k in {"item_id", "kind", "actor", "at"}} for _, raw in rows]


# --- reading the observations ---------------------------------------------------------------------

def titles(conn, scope, ids):
    """The current title of each id, where the store still has one: for the console only."""
    found = {}
    for identifier in dict.fromkeys(ids):
        row = conn.execute("SELECT json_extract(data,'$.title'),json_extract(data,'$.scope') FROM records WHERE id=? AND deleted=0",
                           (identifier,)).fetchone()
        if row and row[0] and json.loads(row[1] or "null") == json.loads(scope):
            found[identifier] = row[0]
            continue
        for table in ("mind_graph_nodes", "mind_memory_nodes"):
            try:
                row = conn.execute(f"SELECT json_extract(data,'$.title') FROM {table} WHERE scope=? AND id=?",
                                   (scope, identifier)).fetchone()
            except Exception:  # noqa: BLE001 - a table this store does not have
                row = None
            if row and row[0]:
                found[identifier] = row[0]
                break
    return found


def recent(mind, limit=30):
    """The newest observations, as the console's transparency page shows them: the titles of what
    was admitted (as the store titles it now), and what was dropped, counted by reason."""
    ensure(mind.engine)
    limit = max(1, min(100, int(limit)))
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        rows = conn.execute(f"SELECT at,data FROM {OBSERVATIONS} WHERE scope=? ORDER BY at DESC,id DESC LIMIT ?",
                            (scope, limit)).fetchall()
        quiet = {mark["item_id"] for mark in QuietMarks(mind).list(conn)}
        observations = [(at, json.loads(raw)) for at, raw in rows]
        named = titles(conn, scope, [i for _, o in observations for i in [*o.get("admitted", ()), *o.get("exempt", ())]]
                       + sorted(quiet))
        config = stored_settings(conn, mind.scope)
    items = []
    for at, o in observations:
        items.append({"at": at, "purpose": o.get("purpose"), "mode": o.get("mode"), "setting": o.get("setting"),
                      "state": o.get("state"), "reason": o.get("reason"), "candidates": len(o.get("candidates", ())),
                      "admitted": [{"id": i, "title": named.get(i), "quiet": i in quiet} for i in o.get("admitted", ())],
                      "exempt": len(o.get("exempt", ())),
                      "dropped": dict(Counter(d["reason"] for d in o.get("dropped", ()))),
                      "score_ms": (o.get("timings") or {}).get("score_ms")})
    return {"items": items, "setting": config[SETTING], "quiet_enabled": bool(config["recall_quiet_marks"]),
            "quiet": [{"id": i, "title": named.get(i)} for i in sorted(quiet)],
            "instruction_authority": "data"}


def calibrate(mind, thresholds=None, quota=None):
    """Replay the kept observations under other thresholds: numbers only. Only light scores replay;
    the deep path's admission is the model's selection and has no threshold."""
    ensure(mind.engine)
    with mind.engine.db.connect() as conn:
        config = stored_settings(conn, mind.scope)
    quota = int(quota or config.get("recall_admission_quota", DEFAULT_QUOTA))
    thresholds = thresholds or [round(.3 + .05 * n, 2) for n in range(10)]
    if not isinstance(thresholds, list) or not thresholds or len(thresholds) > 40 or any(
            type(t) not in (int, float) or not 0 <= t <= 1 for t in thresholds):
        raise ValueError("thresholds are up to forty numbers from 0 to 1")
    with mind.engine.db.connect() as conn:
        rows = [json.loads(r[0]) for r in conn.execute(f"SELECT data FROM {OBSERVATIONS} WHERE scope=?", (mind.scope.key(),))]
    states = Counter(row.get("state") for row in rows)
    scored = [row for row in rows if row.get("state") == "scored"]
    values, by_route = [], {}
    for row in scored:
        for candidate in row.get("candidates", ()):
            if candidate.get("score") is not None:
                values.append(candidate["score"])
                by_route.setdefault(candidate["route"], []).append(candidate["score"])

    def percentiles(xs):
        xs = sorted(xs)
        if not xs:
            return {}
        return {f"p{p}": round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 4) for p in (10, 25, 50, 75, 90)} | {"n": len(xs)}
    report = []
    for threshold in sorted(set(thresholds)):
        passing = [sum(1 for c in row.get("candidates", ()) if c.get("score") is not None and c["score"] >= threshold)
                   for row in scored]
        admitted = [min(n, quota) for n in passing]
        report.append({"threshold": threshold,
                       "mean_passing": round(sum(passing) / len(passing), 3) if passing else None,
                       "mean_admitted": round(sum(admitted) / len(admitted), 3) if admitted else None,
                       "builds_with_none": sum(1 for n in passing if n == 0),
                       "builds_over_quota": sum(1 for n in passing if n > quota),
                       "candidates_passing_share": round(sum(passing) / len(values), 4) if values else None})
    timings = sorted((row.get("timings") or {}).get("score_ms", 0) for row in rows)
    return {"observations": len(rows), "states": dict(states), "scored_builds": len(scored), "quota": quota,
            "current_threshold": config.get("recall_admission_threshold", DEFAULT_THRESHOLD),
            "scores": percentiles(values), "scores_by_route": {k: percentiles(v) for k, v in sorted(by_route.items())},
            "score_ms": percentiles(timings), "thresholds": report}
