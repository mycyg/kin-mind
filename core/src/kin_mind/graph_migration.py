"""The newest-first semantic replay of runtime events into the graph. Never schedules contact
or affect. (The unwired `batch` and `match_shares` backfills were removed: nothing called them,
and `match_shares` would have made a 65,536-token model call if anything had; K4-08.)"""
from __future__ import annotations

import json

from eventmem.core.db import Conflict, Missing, dumps

from .memory import MemoryContinuity


class GraphMigration:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        self.memory = MemoryContinuity(mind)

    def queue_history(self, jobs, agent_version):
        """Newest-first semantic replay in the existing, low-priority DS queue.

        Events marked historical are the `semantic` replay's (MemoryContinuity.queue_history)
        and are not evaluated a second time here (K4-03). A tracked job that ended in any way
        moves the replay on (K4-02), and a finished replay writes nothing more."""
        from eventmem.core.retrieval import tokens

        from .memory import job_ended, note_ended
        name = "event-graph-semantic-v1"
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT cursor,data FROM mind_memory_migrations WHERE scope=? AND name=?", (self.scope.key(),name)).fetchone()
            cursor,data = (row[0],json.loads(row[1])) if row else (None,{})
        if data.get("state") == "complete" and not data.get("job_id"):
            return data
        if data.get("job_id"):
            job_state = job_ended(jobs, data["job_id"])
            if job_state is None:
                return {"state":"pending","job_id":data["job_id"]}
            note_ended(data, data["job_id"], job_state)
            cursor = data["through_seq"]
        sources, size, through = [], 0, cursor
        with self.engine.db.connect() as conn:
            if cursor is None:
                cursor=conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM mind_runtime_events WHERE scope=?",(self.scope.key(),)).fetchone()[0]
            rows=conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND seq<? "
                              "AND COALESCE(json_extract(data,'$.historical'),0)!=1 ORDER BY seq DESC LIMIT 16",(self.scope.key(),cursor)).fetchall()
            for row in rows:
                source_id=json.loads(row["data"])["source_id"]
                try:
                    proof=self.memory.graph.proof(conn,[source_id])
                    cost=tokens(self.engine._get(conn,proof[0]["record_id"])["content"])
                except (Missing,Conflict):
                    through=row["seq"];continue
                if sources and size+cost>24000:
                    break
                sources.append(source_id);size+=cost;through=row["seq"]
        receipt=jobs.enqueue(list(dict.fromkeys(sources)),agent_version,origin="reflection",stimulus="memory-backfill") if sources else None
        data={"deferred_repairs":data.get("deferred_repairs",[]),**({"ended":data["ended"]} if data.get("ended") else {}),
              "through_seq":through or cursor,"job_id":receipt["id"] if receipt else None,"state":"pending" if rows else "complete"}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",(self.scope.key(),name,through or cursor,dumps(data)))
        return data
