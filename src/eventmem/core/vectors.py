from __future__ import annotations

import json
import threading

from .db import Missing, digest, dumps
from .models import now

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


class VectorIndex:
    def __init__(self, engine, index_id):
        import lancedb

        self.engine = engine
        self.id = index_id
        with engine.db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM vector_indexes WHERE id=?", (index_id,)
            ).fetchone()
            if not row:
                raise Missing("Vector index")
            self.config = json.loads(row[0])
        self.connection = lancedb.connect(str(engine.db.root / "vectors"))
        with _guard:
            self.lock = _locks.setdefault(
                str(engine.db.root) + index_id, threading.RLock()
            )

    @staticmethod
    def register(engine, model, dimensions, preprocessing="text-v1"):
        index_id = "vec_" + digest([model, dimensions, preprocessing])[:24]
        config = {
            "id": index_id,
            "model": model,
            "dimensions": dimensions,
            "preprocessing": preprocessing,
            "state": "pending",
            "indexed_at": None,
            "index_type": "IVF_HNSW_SQ",
        }
        with engine.db.connect(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO vector_indexes VALUES(?,?)",
                (index_id, dumps(config)),
            )
        return index_id

    def table(self):
        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("scope", pa.string()),
                pa.field("revision", pa.int64()),
                pa.field("vector", pa.list_(pa.float32(), self.config["dimensions"])),
            ]
        )
        return self.connection.create_table(self.id, schema=schema, exist_ok=True)

    def upsert(self, rows):
        import numpy as np

        for row in rows:
            vector = np.asarray(row["vector"])
            if (
                vector.shape != (self.config["dimensions"],)
                or not np.isfinite(vector).all()
            ):
                raise ValueError("Embedding dimension mismatch or non-finite vector")
        with self.lock:
            self.table().merge_insert(
                "id"
            ).when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def build(self, partitions=None):
        with self.lock:
            table = self.table()
            count = table.count_rows()
            if count >= 256:
                table.create_index(
                    metric="cosine",
                    index_type="IVF_HNSW_SQ",
                    num_partitions=partitions or max(1, count // 16384),
                    m=32,
                    ef_construction=200,
                    replace=True,
                )
                table.create_scalar_index("scope", replace=True)
            self.config.update(state="ready", indexed_at=now(), rows=count)
            with self.engine.db.connect(write=True) as conn:
                conn.execute(
                    "UPDATE vector_indexes SET data=? WHERE id=?",
                    (dumps(self.config), self.id),
                )
        return self.config

    def search(self, vector, scopes=None, limit=20, exact=False, nprobes=32, ef=256):
        if len(vector) != self.config["dimensions"]:
            raise ValueError("Query vector dimension mismatch")
        table = self.table()
        query = table.search(vector).distance_type("cosine").limit(limit)
        if scopes:
            # Values originate from typed scopes, escaped as SQL literals.
            query = query.where(
                "scope IN ("
                + ",".join("'" + s.replace("'", "''") + "'" for s in scopes)
                + ")",
                prefilter=True,
            )
        if exact:
            query = query.bypass_vector_index()
        else:
            query = query.nprobes(nprobes).ef(ef).refine_factor(4)
        return query.select(["id", "revision", "scope", "_distance"]).to_list()

    def purge(self):
        with self.lock:
            table = self.table()
            with self.engine.db.connect() as conn:
                ids = [r[0] for r in conn.execute("SELECT key FROM tombstones")]
            for start in range(0, len(ids), 100):
                batch = ids[start : start + 100]
                table.delete(
                    "id IN ("
                    + ",".join("'" + i.replace("'", "''") + "'" for i in batch)
                    + ")"
                )
            if ids:
                from datetime import timedelta

                # The erased rows are gone from the current version above, but an
                # old version still holds their vectors, so the versions have to go
                # too. `optimize` is the call that does it: `cleanup_old_versions`
                # has been deprecated since lancedb 0.21 and routes through
                # `to_lance()`, which needs `pylance` — a package the `vector` extra
                # does not install, so the old call raised ImportError on the first
                # erase that left a tombstone. Same idiom as
                # `kin_mind.maintenance.vector_optimize`. It also merges small files
                # and folds new rows into the index; neither of those removes a row,
                # and the only rows this drops are the ones deleted just above.
                #
                # `delete_unverified` stays false. It used to be true, and this runs
                # from a background job on a live store, where lancedb says to set it
                # only if no other process can be working on the dataset -- which a
                # background job cannot promise. It is not needed for the erasure
                # either: dropping the old versions is what takes the erased vectors
                # with them, and what the flag would additionally remove is files no
                # manifest references at all, left behind by an interrupted write.
                # Those cost space until a later pass; the flag costs the store.
                table.optimize(
                    cleanup_older_than=timedelta(seconds=0), delete_unverified=False
                )
