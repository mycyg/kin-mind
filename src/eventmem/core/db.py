from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

# The rows `meta` starts with, written only when missing: an INSERT takes the write lock even when
# it ignores, and every Engine() used to take it twice here (E2-08). `schema_version` names the
# backup format family restore() accepts; the structure itself only grows (every statement is
# IF NOT EXISTS, or an ALTER guarded by a look first), and `structure_digest` says what it is.
META_DEFAULTS = (("generation", 0), ("schema_version", 1))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source_key TEXT NOT NULL, version TEXT NOT NULL,
 scope TEXT NOT NULL, session TEXT NOT NULL, received_at TEXT NOT NULL, occurred_at TEXT NOT NULL,
 hash TEXT NOT NULL, blob TEXT, data TEXT NOT NULL, mechanical TEXT NOT NULL DEFAULT 'pending',
 model TEXT NOT NULL DEFAULT 'not_requested', deleted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(namespace,source_key,version,scope));
CREATE INDEX IF NOT EXISTS source_scope ON sources(scope, received_at);
CREATE TABLE IF NOT EXISTS records(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
 valid_from TEXT NOT NULL, valid_until TEXT, received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL, importance REAL NOT NULL, parent_id TEXT,
 data TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS record_scope ON records(scope,deleted,status,kind,updated_at DESC,id);
CREATE INDEX IF NOT EXISTS record_constraints ON records(scope) WHERE deleted=0 AND status='active' AND json_extract(data,'$.attributes.constraint')=1;
CREATE INDEX IF NOT EXISTS record_startup ON records(scope,(CASE kind WHEN 'checkpoint' THEN 0 WHEN 'commitment' THEN 1 WHEN 'preference' THEN 2 ELSE 3 END),importance DESC,updated_at DESC) WHERE deleted=0 AND status='active';
CREATE INDEX IF NOT EXISTS record_parent ON records(parent_id);
CREATE INDEX IF NOT EXISTS record_self_knowledge ON records(scope,json_extract(data,'$.attributes.self_knowledge.entry')) WHERE deleted=0;
CREATE TABLE IF NOT EXISTS revisions(
 record_id TEXT NOT NULL, revision INTEGER NOT NULL, changed_at TEXT NOT NULL,
 action TEXT NOT NULL, reason TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(record_id,revision));
CREATE INDEX IF NOT EXISTS revision_time ON revisions(changed_at, record_id);
CREATE TABLE IF NOT EXISTS evidence(record_id TEXT NOT NULL, source_id TEXT NOT NULL,
 PRIMARY KEY(record_id,source_id));
CREATE INDEX IF NOT EXISTS evidence_source ON evidence(source_id,record_id);
CREATE TABLE IF NOT EXISTS dependencies(record_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 PRIMARY KEY(record_id,evidence_id));
CREATE INDEX IF NOT EXISTS dependency_evidence ON dependencies(evidence_id);
CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
 object TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(subject,predicate,object));
CREATE INDEX IF NOT EXISTS relation_subject ON relations(subject);
CREATE INDEX IF NOT EXISTS relation_object ON relations(object);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, tokens, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, unique_key TEXT UNIQUE NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
 available REAL NOT NULL, lease_until REAL, owner TEXT, fence INTEGER NOT NULL DEFAULT 0,
 error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS job_claim ON jobs(state,available,lease_until);
CREATE INDEX IF NOT EXISTS job_recent ON jobs(updated_at);
CREATE INDEX IF NOT EXISTS job_state_recent ON jobs(state,updated_at);
CREATE TABLE IF NOT EXISTS job_dependencies(job_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
 PRIMARY KEY(job_id,dependency_id));
CREATE TABLE IF NOT EXISTS job_recovery(job_id TEXT NOT NULL, command_id TEXT NOT NULL,
 kind TEXT NOT NULL, target TEXT NOT NULL, prev_state TEXT NOT NULL, prev_error TEXT,
 prev_attempts INTEGER NOT NULL, recovered_at TEXT NOT NULL,
 PRIMARY KEY(job_id,command_id));
CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS family_revisions(id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS members(family_id TEXT NOT NULL, record_id TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(family_id,record_id));
CREATE INDEX IF NOT EXISTS member_record ON members(record_id);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, scope TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, record_id TEXT NOT NULL, session TEXT NOT NULL,
 type TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policies(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, record_id TEXT NOT NULL,
 due_at TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS schedule_due ON schedules(state,due_at);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL, lease_until REAL, data TEXT NOT NULL,
 UNIQUE(schedule_id,id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, name TEXT NOT NULL, value REAL NOT NULL,
 created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metric_name ON metrics(name,id);
CREATE TABLE IF NOT EXISTS vector_indexes(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prefetch(scope TEXT NOT NULL, cue TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope,cue,record_id));
CREATE TABLE IF NOT EXISTS dirty(record_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
"""

_jieba_lock = threading.Lock()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value) -> str:
    return hashlib.sha256(
        (value if isinstance(value, bytes) else dumps(value).encode())
    ).hexdigest()


def structure_digest(conn) -> str:
    """What this store's structure is: one digest over every table, index and trigger definition,
    so two stores, or a store before and after a deploy, can be told apart (E2-08)."""
    rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return digest([list(row) for row in rows])[:16]


def tokenize(text: str) -> str:
    words = re.findall(r"[a-zA-Z0-9_]+", text)
    code_words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+", " ".join(words))
    chinese = re.findall(r"[\u3400-\u9fff]+", text)
    if chinese:
        import jieba

        with _jieba_lock:
            jieba.setLogLevel(40)
            for segment in chinese:
                words.extend(jieba.cut_for_search(segment))
    return " ".join(w.lower() for w in words + code_words)


class Conflict(Exception):
    """Static host message plus optional structured facts. The message stays the
    only positional argument, so str() and the HTTP 409 mapping never change.

    `kind` is the taxonomy of kin_mind.conflicts: what moved, not what to do
    about it. A raise site that leaves it None is classified by the registry."""

    def __init__(self, message="", *, kind=None, code=None, target=None, expected=None, actual=None):
        super().__init__(message)
        self.kind, self.code, self.target = kind, code, target
        self.expected, self.actual = expected, actual


class Missing(Exception):
    """A reference that is gone. It takes the same optional facts as Conflict and
    stays a separate class, so every `except Conflict` keeps its current reach."""

    def __init__(self, message="", *, kind=None, code=None, target=None, expected=None, actual=None):
        super().__init__(message)
        self.kind, self.code, self.target = kind, code, target
        self.expected, self.actual = expected, actual


class Deleted(Conflict):
    pass


# Set inside a look that must leave the store as it found it (S1-02).
_UNRECORDED = contextvars.ContextVar("eventmem_unrecorded", default=False)
# What a model call cost, under the names the structured provider records it by. A look asks no
# model (CR2-MEM-02); should one ever be paid for under a look all the same, its cost is not the
# telemetry a look leaves out, and it stays on record (CR-MEM-07).
BILLED_METRICS = frozenset({"structured_model_usage", "structured_rejected"})


@contextmanager
def unrecorded():
    """A read that records nothing about itself: no telemetry of the memory it looked at, no
    cache of what it computed (S1-02, CR-MEM-07), and no model call — nothing paid for, so no
    cost and no admission to record (CR2-MEM-02). It answers from what is stored: an existing
    cache, or the originals. Paid work takes a session, whose recall records what it costs."""
    token = _UNRECORDED.set(True)
    try:
        yield
    finally:
        _UNRECORDED.reset(token)


def recording():
    """False inside `unrecorded()`: a look, whose results are not kept."""
    return not _UNRECORDED.get()


class Database:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "memory.sqlite3"
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            from kin_mind.lifecycle_schema import initialize
            initialize(conn)
            from .read_policy import SCHEMA as READ_POLICY_SCHEMA
            conn.executescript(READ_POLICY_SCHEMA)
            for key, value in META_DEFAULTS:
                if not conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                    conn.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # No table declares a foreign key; references are kept by engine.delete()'s own cascade.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def generation(self, conn=None):
        if conn is not None:
            return conn.execute(
                "SELECT value FROM meta WHERE key='generation'"
            ).fetchone()[0]
        with self.connect() as c:
            return self.generation(c)

    def bump(self, conn):
        conn.execute("UPDATE meta SET value=value+1 WHERE key='generation'")

    def blob(self, content: bytes) -> str:
        key = digest(content)
        path = self.blobs / key
        if not path.exists():
            # O_EXCL prevents concurrent writers from replacing an existing snapshot.
            import tempfile

            fd, name = tempfile.mkstemp(dir=self.blobs)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.link(name, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(name)
        return key

    def metric(self, name, value, data=None):
        if _UNRECORDED.get() and not (name.startswith("model_") or name in BILLED_METRICS):
            return
        from kin_mind.maintenance import trim_metrics

        from .models import now

        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)",
                (name, value, now(), dumps(data or {})),
            )
            # Bound telemetry independently of user memories. One ring shared by every
            # name is the same bound applied in the wrong place: it lets a name that
            # fires on every model call evict a name that fires when something rare
            # goes wrong, which is the one an operator came to read. The ring is per
            # name behind a flag, and the flag off is this line as it always was.
            trim_metrics(conn, name)
