"""Resumable imports of authenticated host history, without sending anything."""
from __future__ import annotations

import json

from eventmem.core.db import Conflict, Missing, dumps

from .memory import MemoryContinuity


class HistoryImport:
    def __init__(self, mind):
        self.memory = MemoryContinuity(mind)
        self.mind, self.engine = mind, mind.engine

    def events(self, events):
        receipts, deferred = [], []
        # Newest historical records become searchable first. Each source and
        # receipt keeps its original timestamp and durable event identity.
        for event in sorted(events, key=lambda e: (e["at"], e["id"]), reverse=True):
            try:
                receipts.append(self.memory.ingest({**event, "historical": True}))
            except (Missing, Conflict, OSError, ValueError) as error:
                deferred.append({"id": event["id"], "reason": type(error).__name__})
        return {"recorded": len(receipts), "deferred": deferred, "receipts": receipts}

    def sources(self, namespaces, *, limit=100):
        """Host-supplied namespace -> event-kind mapping. Assistant prose is an
        account, never a substitute for a transport or file-operation receipt."""
        if not 1 <= limit <= 500 or not namespaces:
            raise ValueError("A bounded namespace mapping is required")
        name = "sources:" + dumps(namespaces)
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (self.mind.scope.key(), name)).fetchone()
            cursor = json.loads(row[0]) if row else {}
            placeholders = ",".join("?" for _ in namespaces)
            params = [self.mind.scope.key(), *namespaces]
            condition = ""
            if cursor.get("at"):
                condition = " AND (received_at<? OR (received_at=? AND id<?))"
                params.extend([cursor["at"], cursor["at"], cursor["id"]])
            rows = conn.execute(f"SELECT id,namespace,session,occurred_at,received_at FROM sources WHERE scope=? AND namespace IN ({placeholders}) AND deleted=0{condition} ORDER BY received_at DESC,id DESC LIMIT ?", [*params, limit]).fetchall()
        if not rows:
            return {"state": "complete", "recorded": 0}
        result = self.events([{"id": "history-source:" + r["id"], "kind": namespaces[r["namespace"]], "at": r["occurred_at"],
            "source_id": r["id"], "session": r["session"], "text": self.engine.source(r["id"], content=True).read_text()} for r in rows])
        # A deferred source remains explicitly recorded for review; it does not
        # stall the remaining historical pages or pretend to be confirmed.
        end = {"at": rows[-1]["received_at"], "id": rows[-1]["id"], "deferred": result["deferred"]}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)", (self.mind.scope.key(), name, 0, dumps(end)))
        return {"state": "more" if len(rows) == limit else "complete", "recorded": result["recorded"], "deferred": result["deferred"]}
