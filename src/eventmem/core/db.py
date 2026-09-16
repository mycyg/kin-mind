from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES('generation',0);
INSERT OR IGNORE INTO meta VALUES('schema_version',1);
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
CREATE TABLE IF NOT EXISTS job_dependencies(job_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
 PRIMARY KEY(job_id,dependency_id));
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
    pass


class Missing(Exception):
    pass


class Deleted(Conflict):
    pass


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
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
        from .models import now

        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)",
                (name, value, now(), dumps(data or {})),
            )
            # Bound telemetry independently of user memories.
            conn.execute(
                "DELETE FROM metrics WHERE id < (SELECT MAX(id)-20000 FROM metrics)"
            )
