"""Durable host task exchange, separate from semantic memory and model evidence.

The host binds actor/scope. Text and artifact references are data. This store does
not grant permissions, launch an executor, or infer delivery from a saved result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class HandoffConflict(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("A stable identifier is required")
    return value


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Text is missing or too long")
    return value.strip()


class TaskHandoffs:
    actors = frozenset({"mobile", "desktop", "transport"})
    terminal = frozenset({"completed", "failed", "canceled"})

    def __init__(self, file, scope, actor, clock=time.time):
        if actor not in self.actors:
            raise ValueError("Unknown host actor")
        self.file, self.scope, self.actor, self.clock = (
            Path(file),
            packed(scope),
            actor,
            clock,
        )
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS handoffs(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS handoff_commands(
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS handoff_events(
                    task_id TEXT, seq INTEGER, data TEXT NOT NULL,
                    PRIMARY KEY(task_id,seq));
                CREATE TABLE IF NOT EXISTS handoff_outbox(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS handoff_delivery ON handoff_outbox(scope,state);
            """)
        os.chmod(self.file, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.file, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def command(self, command_id, payload, apply):
        key = packed([self.scope, self.actor, identifier(command_id)])
        digest = hashlib.sha256(packed(payload).encode()).hexdigest()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                old = c.execute(
                    "SELECT * FROM handoff_commands WHERE id=?", (key,)
                ).fetchone()
                if old:
                    if old["digest"] != digest:
                        raise HandoffConflict(
                            "Command identifier already has different content"
                        )
                    result = json.loads(old["receipt"])
                else:
                    result = apply(c)
                    c.execute(
                        "INSERT INTO handoff_commands VALUES(?,?,?)",
                        (key, digest, packed(result)),
                    )
                c.commit()
                return result
            except BaseException:
                c.rollback()
                raise

    def get(self, task_id, conn=None):
        if conn is None:
            with self.connect() as c:
                return self.get(task_id, c)
        row = conn.execute(
            "SELECT data FROM handoffs WHERE id=? AND scope=?",
            (identifier(task_id), self.scope),
        ).fetchone()
        if not row:
            raise HandoffConflict("Task is outside this exchange")
        return json.loads(row["data"])

    def event(self, c, task, kind, summary, evidence=None, notify=False):
        task["revision"] += 1
        task["updated_at"] = self.clock()
        event = {
            "schema": 1,
            "task_id": task["id"],
            "seq": task["revision"],
            "kind": kind,
            "actor": self.actor,
            "summary": summary,
            "evidence": evidence or [],
            "at": task["updated_at"],
        }
        c.execute(
            "INSERT INTO handoff_events VALUES(?,?,?)",
            (task["id"], task["revision"], packed(event)),
        )
        c.execute(
            "INSERT INTO handoffs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (task["id"], self.scope, packed(task)),
        )
        if notify:
            out = {
                "id": f"handoff:{task['id']}:{event['seq']}",
                "task_id": task["id"],
                "seq": event["seq"],
                "kind": kind,
                "title": task["title"],
                "text": summary,
                "artifacts": task.get("artifacts", []),
                "state": "pending",
                "created_at": self.clock(),
                "read_at": None,
            }
            c.execute(
                "INSERT INTO handoff_outbox VALUES(?,?,?,?,?,?)",
                (
                    out["id"],
                    self.scope,
                    task["id"],
                    event["seq"],
                    "pending",
                    packed(out),
                ),
            )
        return task

    def submit(
        self, *, command_id, recipient, session_id, title, goal, source, acceptance
    ):
        if (
            self.actor == "transport"
            or recipient not in {"mobile", "desktop"}
            or recipient == self.actor
        ):
            raise ValueError("A different executor host is required")
        args = {
            "recipient": recipient,
            "session_id": identifier(session_id),
            "title": text(title, 160),
            "goal": text(goal, 8000),
            "source": text(source, 2000),
            "acceptance": text(acceptance, 3000),
        }

        def apply(c):
            task_id = (
                "task_"
                + hashlib.sha256(
                    packed([self.scope, self.actor, command_id]).encode()
                ).hexdigest()[:32]
            )
            task = {
                "schema": 1,
                "id": task_id,
                "revision": 0,
                "sender": self.actor,
                "state": "pending",
                "created_at": self.clock(),
                "cancel_requested": False,
                "executor": None,
                "run_id": None,
                **args,
            }
            return self.event(
                c, task, "submitted", "Task saved; executor acceptance pending"
            )

        return self.command(command_id, {"action": "submit", **args}, apply)

    def claim(self, task_id, *, revision, run_id, command_id):
        identifier(run_id)

        def apply(c):
            task = self.get(task_id, c)
            if (
                task["revision"] != revision
                or task["state"] != "pending"
                or task["recipient"] != self.actor
            ):
                raise HandoffConflict("Task changed or cannot be claimed by this host")
            task.update(state="accepted", executor=self.actor, run_id=run_id)
            return self.event(c, task, "accepted", "Executor accepted the task")

        return self.command(
            command_id,
            {
                "action": "claim",
                "task": task_id,
                "revision": revision,
                "run_id": run_id,
            },
            apply,
        )

    def update(
        self,
        task_id,
        *,
        revision,
        run_id,
        command_id,
        state,
        summary,
        evidence=None,
        artifacts=None,
        notify=True,
    ):
        if state not in {"running", "waiting", "completed", "failed", "canceled"}:
            raise ValueError("Unsupported task state")
        summary = text(summary, 6000)
        evidence, artifacts = evidence or [], artifacts or []
        if len(evidence) > 30 or any(
            not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in evidence
        ):
            raise ValueError(
                "Evidence must be bounded references or validation results"
            )
        if state == "completed" and not evidence:
            raise ValueError("Completion requires validation evidence")
        if len(artifacts) > 20:
            raise ValueError("Too many artifacts")
        for artifact in artifacts:
            if (
                set(artifact) != {"path", "sha256", "name"}
                or not Path(artifact["path"]).is_absolute()
                or not re.fullmatch("[a-f0-9]{64}", artifact["sha256"])
            ):
                raise ValueError("Artifact requires an absolute path, name and SHA-256")
            text(artifact["name"], 200)
        args = {
            "task": task_id,
            "revision": revision,
            "run_id": run_id,
            "state": state,
            "summary": summary,
            "evidence": evidence,
            "artifacts": artifacts,
            "notify": bool(notify),
        }

        def apply(c):
            task = self.get(task_id, c)
            if (
                task["revision"] != revision
                or task["state"] in self.terminal
                or task["executor"] != self.actor
                or task["run_id"] != run_id
            ):
                raise HandoffConflict(
                    "Task changed or this executor does not own the run"
                )
            if task["cancel_requested"] and state in {"running", "completed"}:
                raise HandoffConflict(
                    "Cancellation is pending; acknowledge stopping instead of continuing"
                )
            task.update(
                state=state, summary=summary, evidence=evidence, artifacts=artifacts
            )
            return self.event(c, task, state, summary, evidence, notify)

        return self.command(command_id, {"action": "update", **args}, apply)

    def cancel(self, task_id, *, revision, command_id, reason):
        reason = text(reason, 2000)

        def apply(c):
            task = self.get(task_id, c)
            if (
                task["sender"] != self.actor
                or task["revision"] != revision
                or task["state"] in self.terminal
            ):
                raise HandoffConflict(
                    "Task changed or cannot be canceled by this sender"
                )
            task["cancel_requested"] = True
            if task["state"] == "pending":
                task["state"] = "canceled"
            return self.event(c, task, "cancel_requested", reason)

        return self.command(
            command_id,
            {
                "action": "cancel",
                "task": task_id,
                "revision": revision,
                "reason": reason,
            },
            apply,
        )

    def list(self, pending_only=False, limit=30):
        if not 1 <= limit <= 100:
            raise ValueError("Invalid limit")
        with self.connect() as c:
            rows = c.execute(
                "SELECT data FROM handoffs WHERE scope=? ORDER BY rowid DESC",
                (self.scope,),
            )
            tasks = (json.loads(r[0]) for r in rows)
            return [
                t for t in tasks if not pending_only or t["state"] not in self.terminal
            ][:limit]

    def events(self, task_id, after=0):
        self.get(task_id)
        with self.connect() as c:
            return [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT data FROM handoff_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100",
                    (task_id, after),
                )
            ]

    def work_locks(self, sender="mobile"):
        with self.connect() as c:
            return [
                r[0]
                for r in c.execute(
                    """
                SELECT id FROM handoffs h WHERE scope=?
                AND json_extract(data,'$.sender')=?
                AND json_extract(data,'$.state')!='canceled'
                AND (json_extract(data,'$.state')!='completed' OR NOT EXISTS(
                    SELECT 1 FROM handoff_outbox o WHERE o.task_id=h.id
                    AND o.state='accepted' AND json_extract(o.data,'$.kind')='completed'))
            """,
                    (self.scope, sender),
                )
            ]

    def outbox(self, include_accepted=False, task_id=None):
        if task_id:
            self.get(task_id)
        with self.connect() as c:
            condition = "" if include_accepted else " AND state!='accepted'"
            condition += " AND task_id=?" if task_id else ""
            params = (self.scope, task_id) if task_id else (self.scope,)
            return [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT data FROM handoff_outbox WHERE scope=?"
                    + condition
                    + " ORDER BY rowid DESC LIMIT 100",
                    params,
                )
            ][::-1]

    def delivery(
        self, outbox_id, *, command_id, state, message_id=None, message_ids=None
    ):
        if self.actor != "transport" or state not in {
            "sending",
            "accepted",
            "unconfirmed",
            "failed",
        }:
            raise ValueError("Delivery receipts belong to the transport host")
        if state == "accepted":
            text(message_id, 300)
        if message_ids is not None and (
            not isinstance(message_ids, list)
            or len(message_ids) > 21
            or any(not isinstance(x, str) or not x or len(x) > 300 for x in message_ids)
        ):
            raise ValueError("Invalid platform receipt list")

        def apply(c):
            row = c.execute(
                "SELECT data FROM handoff_outbox WHERE id=? AND scope=?",
                (outbox_id, self.scope),
            ).fetchone()
            if not row:
                raise HandoffConflict("Unknown delivery")
            out = json.loads(row[0])
            if out["state"] == "accepted" or (
                state == "sending" and out["state"] != "pending"
            ):
                raise HandoffConflict(
                    "Delivery is settled or already attempted; reconcile original identifier"
                )
            if state != "sending" and out["state"] not in {"sending", "unconfirmed"}:
                raise HandoffConflict("Delivery has not been attempted")
            out.update(
                state=state,
                message_id=message_id,
                message_ids=message_ids or [],
                updated_at=self.clock(),
            )
            c.execute(
                "UPDATE handoff_outbox SET state=?,data=? WHERE id=?",
                (state, packed(out), outbox_id),
            )
            return out

        return self.command(
            command_id,
            {
                "action": "delivery",
                "id": outbox_id,
                "state": state,
                "message_id": message_id,
                "message_ids": message_ids,
            },
            apply,
        )
