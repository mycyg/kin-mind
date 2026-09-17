"""Source-backed task management for hosts with configured contact policies."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator

from .db import Conflict, Missing, digest
from .models import ContactPolicy, Model, ScheduleInput, Scope, SourceInput, utc
from .scheduler import Scheduler


class ContactTaskInput(Model):
    command_id: str = Field(min_length=1, max_length=200)
    policy_id: str = Field(default="default", min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=6000)
    basis: str = Field(min_length=1, max_length=2000)
    due_at: str
    trigger: Literal["reminder", "commitment", "anniversary", "checkin", "greeting"] = (
        "reminder"
    )
    recurrence: Literal["none", "daily", "weekly", "yearly"] = "none"
    authority: Literal["explicit", "operation", "model"] = "model"

    _time = field_validator("due_at")(utc)

    @field_validator("command_id", "policy_id", "title", "text", "basis")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Task fields cannot be blank")
        return value


class ContactTasks:
    """Limit task operations to a host's scope and selected policy IDs.

    This is query isolation within a single-user database, not an authorization
    boundary for other memory tools. Delivery credentials stay with the host.
    """

    def __init__(
        self,
        engine,
        scope: Scope,
        policy_ids: list[str],
        *,
        namespace: str = "contact-tasks",
        origin: str = "MemoryPalace contact tasks",
    ):
        if not 1 <= len(policy_ids) <= 20 or any(not p.strip() for p in policy_ids):
            raise ValueError("Select between 1 and 20 nonempty policy IDs")
        if not namespace.strip() or len(namespace) > 200:
            raise ValueError("A bounded source namespace is required")
        self.engine = engine
        self.scope = scope
        self.policy_ids = tuple(dict.fromkeys(policy_ids))
        self.namespace = namespace
        self.origin = origin
        self.scheduler = Scheduler(engine)

    def _owned(self, conn, task_id):
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (task_id,)).fetchone()
        if not row or row["policy_id"] not in self.policy_ids:
            raise Missing("Task is outside the selected policies")
        record = self.engine._get(conn, row["record_id"])
        if record["scope"] != self.scope.model_dump():
            raise Conflict("Task scope differs from the selected scope")
        return row, record

    def create(self, task: ContactTaskInput, *, metadata: dict | None = None) -> dict:
        if task.policy_id not in self.policy_ids:
            raise Missing("Task is outside the selected policies")
        # Reject missing or incompatible policies before creating a source.
        with self.engine.db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM policies WHERE id=?", (task.policy_id,)
            ).fetchone()
        if not row:
            raise Missing("Configure the contact policy before creating a task")
        policy = ContactPolicy.model_validate_json(row[0])
        if policy.scope != self.scope:
            raise Conflict("Contact policy and task scopes differ")
        if (
            task.trigger not in policy.triggers
            or "reminder" not in policy.allowed_kinds
        ):
            raise Conflict("Task is outside the contact policy")
        attributes = (
            metadata
            if metadata is not None
            else {
                "task_basis": task.basis,
                "trigger": task.trigger,
                "policy_id": task.policy_id,
            }
        )
        source_input = SourceInput(
            namespace=self.namespace,
            key=task.command_id,
            scope=self.scope,
            title=task.title,
            text=task.text,
            kind="reminder",
            authority=task.authority,
            origin=self.origin,
            metadata=attributes,
        )
        # Reserve the full intent, including its provenance, before either durable
        # stage. A retry after interruption can finish both stages idempotently.
        with self.engine.db.connect(write=True) as conn:
            self.engine.command(
                conn,
                "contact-task:" + task.command_id,
                {
                    "task": task.model_dump(),
                    "scope": self.scope.model_dump(),
                    "namespace": self.namespace,
                    "origin": self.origin,
                    "metadata": attributes,
                },
                lambda: {"id": "schedule_" + digest(task.command_id)[:32]},
            )
        source = self.engine.receive(source_input)
        record_ids = self.engine.source(source["id"])["record_ids"]
        if not record_ids:
            raise Missing("The task's source record was deleted")
        record_id = record_ids[0]
        result = self.scheduler.schedule(
            ScheduleInput(
                command_id=task.command_id,
                policy_id=task.policy_id,
                record_id=record_id,
                due_at=task.due_at,
                trigger=task.trigger,
                recurrence=task.recurrence,
            )
        )
        # Scheduler command receipts describe creation; report the live revision
        # so repeating a canceled or rescheduled request never appears to undo it.
        with self.engine.db.connect() as conn:
            current, _ = self._owned(conn, result["id"])
        return result | {
            "revision": current["revision"],
            "status": current["state"],
            "due_at": current["due_at"],
            "recurrence": task.recurrence,
            "policy_id": task.policy_id,
            "timezone": policy.timezone,
        }

    def list(self, limit: int = 30) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        placeholders = ",".join("?" for _ in self.policy_ids)
        with self.engine.db.connect() as conn:
            rows = conn.execute(
                f"SELECT s.* FROM schedules s JOIN records r ON r.id=s.record_id "
                f"WHERE s.policy_id IN ({placeholders}) AND r.scope=? AND r.deleted=0 "
                "ORDER BY s.due_at DESC,s.id LIMIT ?",
                (*self.policy_ids, self.scope.key(), limit),
            ).fetchall()
            items = []
            for row in rows:
                _, record = self._owned(conn, row["id"])
                config = json.loads(row["data"])
                deliveries = conn.execute(
                    "SELECT id,state FROM outbox WHERE schedule_id=? ORDER BY available DESC,id LIMIT 3",
                    (row["id"],),
                ).fetchall()
                items.append(
                    {
                        "id": row["id"],
                        "policy_id": row["policy_id"],
                        "revision": row["revision"],
                        "state": row["state"],
                        "title": record["title"],
                        "text": record["content"][:6000],
                        "due_at": row["due_at"],
                        "recurrence": config["recurrence"],
                        "source_ids": record["source_ids"],
                        "deliveries": [dict(delivery) for delivery in deliveries],
                    }
                )
        return {"items": items}

    def manage(
        self,
        task_id: str,
        expected_revision: int,
        action: Literal["cancel", "pause", "resume", "snooze", "confirm"],
        due_at: str | None = None,
    ) -> dict:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if due_at is not None and action != "snooze":
            raise ValueError("Use snooze to change the due time")
        with self.engine.db.connect() as conn:
            self._owned(conn, task_id)
        # Passed through whole: `deliveries` and `reconciliation_required` tell the host
        # which pending deliveries were canceled and which may already have been sent.
        return self.scheduler.control(task_id, action, expected_revision, due_at)
