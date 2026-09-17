from __future__ import annotations

import json
import math

from .db import Conflict, Missing, digest, dumps
from .models import Scope, now


def prepare_communities(engine, scope):
    import igraph as ig

    with engine.db.connect() as conn:
        dirty = conn.execute(
            "SELECT r.id,r.data,d.revision FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.scope=? AND r.deleted=0 AND r.status='active' AND d.revision=r.revision ORDER BY r.id LIMIT 2000",
            (scope.key(),),
        ).fetchall()
        ids = {r["id"] for r in dirty}
        # Include bounded neighbors of changed records; unrelated topics are not
        # scanned or sent to a model.
        edges = []
        for rid in sorted(ids):
            rows = conn.execute(
                "SELECT subject,object FROM relations WHERE scope=? AND (subject=? OR object=?) LIMIT 30",
                (scope.key(), rid, rid),
            ).fetchall()
            edges.extend((r[0], r[1]) for r in rows)
        node_ids = sorted(ids | {x for e in edges for x in e})[:4000]
        # A topic family is offered as material that was lived. A role agreement, its examples,
        # a self-claim and a host envelope are none of those, so they are not clustered.
        from .read_policy import ReadPolicy

        policy = ReadPolicy.load(engine, scope, "experience_recall", conn=conn)
        records = {}
        for rid in node_ids:
            try:
                data = engine._get(conn, rid)
                if data["status"] == "active" and policy.visible(data):
                    records[rid] = data
            except Missing:
                pass
    node_ids = sorted(records)
    index = {rid: i for i, rid in enumerate(node_ids)}
    graph = ig.Graph(
        n=len(node_ids),
        edges=[(index[a], index[b]) for a, b in edges if a in index and b in index],
        directed=False,
    )
    if not node_ids:
        return lambda conn: None
    # Exact normalized topic labels are deterministic candidate links, not facts.
    topics = {}
    for rid, data in records.items():
        topic = data["attributes"].get("topic")
        if topic:
            topics.setdefault(str(topic), []).append(index[rid])
    for members in topics.values():
        graph.add_edges((members[0], other) for other in members[1:])
    graph.simplify()
    clusters = (
        graph.community_leiden(objective_function="modularity", n_iterations=2)
        if graph.ecount()
        else [[i] for i in range(len(node_ids))]
    )
    families = []
    clusters = [
        list(cluster)[start : start + 1000]
        for cluster in clusters
        for start in range(0, len(cluster), 1000)
    ]
    for cluster in clusters:
        members = sorted(node_ids[i] for i in cluster)
        if len(members) < 2:
            continue
        fid = "family_" + digest([scope.key(), members])[:24]
        subgraph = graph.induced_subgraph(cluster)
        layout = subgraph.layout_fruchterman_reingold(dim=3, niter=100)
        positions = {
            node_ids[original]: list(layout[i]) for i, original in enumerate(cluster)
        }
        title = (
            records[members[0]]["attributes"].get("topic")
            or records[members[0]]["title"]
            or "Topic"
        )
        families.append(
            {
                "id": fid,
                "scope": scope.model_dump(),
                "kind": "family",
                "state": "candidate",
                "revision": 1,
                "title": str(title)[:200],
                "members": members,
                "positions": positions,
                "basis": "Leiden on explicit relations and topic labels",
                "generated_at": now(),
            }
        )

    def apply(conn):
        # The graph was prepared outside the writer transaction. Retry the job
        # when a correction or deletion has changed any of its inputs.
        for rid, snapshot in records.items():
            try:
                current = engine._get(conn, rid)
            except Missing:
                raise Conflict("Community input was deleted") from None
            if (
                current["revision"] != snapshot["revision"]
                or current["status"] != "active"
            ):
                raise Conflict("Community input revision changed")
        for family in families:
            conn.execute(
                "INSERT OR IGNORE INTO families VALUES(?,?,?,?,?,?)",
                (family["id"], scope.key(), "family", "candidate", 1, dumps(family)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO family_revisions VALUES(?,?,?)",
                (family["id"], 1, dumps(family)),
            )
            for rid in family["members"]:
                conn.execute(
                    "INSERT OR IGNORE INTO members VALUES(?,?,?)",
                    (
                        family["id"],
                        rid,
                        dumps({"basis": family["basis"], "state": "candidate"}),
                    ),
                )
        for row in dirty:
            conn.execute(
                "DELETE FROM dirty WHERE record_id=? AND revision=?",
                (row["id"], row["revision"]),
            )

    return apply


class Organizer:
    def __init__(self, engine):
        self.engine = engine

    def list(self, scope, limit=100, cursor=""):
        with self.engine.db.connect() as conn:
            return [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT data FROM families WHERE scope=? AND id>? ORDER BY id LIMIT ?",
                    (scope.key(), cursor, min(200, limit)),
                )
            ]

    def create(self, scope, title, members, kind="volume"):
        from .engine import uid

        if kind not in {"volume", "family"} or not 1 <= len(members) <= 1000:
            raise ValueError("A family or volume needs 1–1000 members")
        with self.engine.db.connect(write=True) as conn:
            for rid in members:
                data = self.engine._get(conn, rid)
                if data["scope"] != scope.model_dump():
                    raise Conflict("Members must share a scope")
            fid = uid(kind)
            data = {
                "id": fid,
                "scope": scope.model_dump(),
                "kind": kind,
                "state": "candidate",
                "revision": 1,
                "title": title,
                "members": sorted(set(members)),
                "positions": {},
                "basis": "Explicit membership",
            }
            conn.execute(
                "INSERT INTO families VALUES(?,?,?,?,?,?)",
                (fid, scope.key(), kind, "candidate", 1, dumps(data)),
            )
            conn.execute(
                "INSERT INTO family_revisions VALUES(?,?,?)", (fid, 1, dumps(data))
            )
            for rid in data["members"]:
                conn.execute(
                    "INSERT INTO members VALUES(?,?,?)",
                    (fid, rid, dumps({"basis": "explicit"})),
                )
            return data

    def change(
        self,
        fid,
        revision,
        action,
        *,
        members=None,
        target=None,
        title=None,
        target_revision=None,
    ):
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM families WHERE id=?", (fid,)).fetchone()
            if not row:
                raise Missing(fid)
            if row["revision"] != revision:
                raise Conflict("Family revision changed")
            data = json.loads(row["data"])
            if action == "rollback":
                old = conn.execute(
                    "SELECT data FROM family_revisions WHERE id=? AND revision=?",
                    (fid, target_revision),
                ).fetchone()
                if not old:
                    raise Missing("Family revision")
                data = json.loads(old[0])
            elif action == "publish":
                data["state"] = "published"
            elif action == "archive":
                data["state"] = "archived"
            elif action == "merge":
                other = conn.execute(
                    "SELECT * FROM families WHERE id=?", (target,)
                ).fetchone()
                if not other or other["scope"] != row["scope"] or target == fid:
                    raise Conflict("Merge target must share scope")
                other_data = json.loads(other["data"])
                data["members"] = sorted(set(data["members"] + other_data["members"]))
                other_data.update(
                    state="merged", merged_into=fid, revision=other_data["revision"] + 1
                )
                conn.execute(
                    "UPDATE families SET state='merged',revision=?,data=? WHERE id=?",
                    (other_data["revision"], dumps(other_data), target),
                )
                conn.execute(
                    "INSERT INTO family_revisions VALUES(?,?,?)",
                    (target, other_data["revision"], dumps(other_data)),
                )
            elif action in {"revise", "split"}:
                if members is None:
                    raise ValueError("Members required")
                if action == "split":
                    if not set(members) < set(data["members"]):
                        raise ValueError("Split requires a proper subset of members")
                    from .engine import uid

                    split_id = uid(data["kind"])
                    split_data = data | {
                        "id": split_id,
                        "revision": 1,
                        "members": sorted(set(members)),
                        "title": title or data["title"],
                        "state": "candidate",
                    }
                    conn.execute(
                        "INSERT INTO families VALUES(?,?,?,?,?,?)",
                        (
                            split_id,
                            row["scope"],
                            row["kind"],
                            "candidate",
                            1,
                            dumps(split_data),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO family_revisions VALUES(?,?,?)",
                        (split_id, 1, dumps(split_data)),
                    )
                    for rid in members:
                        conn.execute(
                            "INSERT INTO members VALUES(?,?,?)",
                            (split_id, rid, '{"basis":"split"}'),
                        )
                    data["members"] = sorted(set(data["members"]) - set(members))
                    data["split_into"] = split_id
                else:
                    data["members"] = sorted(set(members))
                    if title:
                        data["title"] = title
            else:
                raise ValueError("Unknown family action")
            if len(data["members"]) > 1000:
                raise ValueError("Family exceeds 1000 members")
            for rid in data["members"]:
                if Scope(**self.engine._get(conn, rid)["scope"]).key() != row["scope"]:
                    raise Conflict("Member scope mismatch")
            data["revision"] = revision + 1
            conn.execute(
                "UPDATE families SET state=?,revision=?,data=? WHERE id=?",
                (data["state"], data["revision"], dumps(data), fid),
            )
            conn.execute(
                "INSERT INTO family_revisions VALUES(?,?,?)",
                (fid, data["revision"], dumps(data)),
            )
            conn.execute("DELETE FROM members WHERE family_id=?", (fid,))
            for rid in data["members"]:
                conn.execute(
                    "INSERT INTO members VALUES(?,?,?)",
                    (fid, rid, dumps({"basis": action})),
                )
            self.engine.db.bump(conn)
            return data

    def graph(self, scope, family_id=None, limit=150):
        limit = max(1, min(limit, 300))
        with self.engine.db.connect() as conn:
            positions = {}
            if family_id:
                row = conn.execute(
                    "SELECT data FROM families WHERE id=? AND scope=?",
                    (family_id, scope.key()),
                ).fetchone()
                if not row:
                    raise Missing(family_id)
                data = json.loads(row[0])
                ids = data["members"][:limit]
                positions = data.get("positions", {})
            else:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM records WHERE scope=? AND deleted=0 ORDER BY importance DESC LIMIT ?",
                        (scope.key(), limit),
                    )
                ]
            nodes = []
            for i, rid in enumerate(ids):
                try:
                    record = self.engine._get(conn, rid)
                    nodes.append(
                        {
                            "id": rid,
                            "title": record["title"] or record["kind"],
                            "kind": record["kind"],
                            "status": record["status"],
                            "position": positions.get(
                                rid,
                                [
                                    math.cos(i * 2.4) * math.sqrt(i + 1),
                                    math.sin(i * 2.4) * math.sqrt(i + 1),
                                    (i % 7) - 3,
                                ],
                            ),
                        }
                    )
                except Missing:
                    pass
            allowed = {n["id"] for n in nodes}
            edges = []
            for rid in allowed:
                edges.extend(
                    dict(r)
                    for r in conn.execute(
                        "SELECT id,subject,object,predicate FROM relations WHERE subject=? LIMIT 40",
                        (rid,),
                    )
                    if r["object"] in allowed
                )
        return {
            "nodes": nodes,
            "edges": edges,
            "limit": limit,
            "layout": "precomputed" if positions else "deterministic",
        }
