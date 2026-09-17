from __future__ import annotations

import json

from .db import Conflict, dumps
from .models import Scope
from .retrieval import tokens


def read_segment(
    engine,
    record_id,
    *,
    at=None,
    known_at=None,
    offset=0,
    length=12000,
    budget=4000,
    session=None,
):
    if offset < 0 or not 1 <= length <= 32000 or not 1 <= budget <= 32000:
        raise ValueError("Invalid read segment or token budget")
    result = engine.get(record_id, at=at, known_at=known_at)
    from kin_mind.context import Contexts, enabled
    scope = Scope(**result["scope"])
    if enabled(engine, scope):
        from kin_mind.state import Mind
        return Contexts(Mind(engine, scope)).read_record(result, offset=offset, length=length, budget=budget, session=session)
    content = result["content"]
    piece = content[offset : offset + length]
    if tokens(piece) > budget:
        lo, hi = 0, len(piece)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if tokens(piece[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        piece = piece[:lo]
    count = tokens(piece)
    if session:
        with engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session,)
            ).fetchone()
            scope = Scope(**result["scope"]).key()
            if row and row["scope"] != scope:
                raise Conflict("Session belongs to another scope")
            state = (
                json.loads(row["data"])
                if row
                else {"seen": {}, "used": 0, "resident": {}, "checkpoint": None}
            )
            state["seen"][record_id] = result["revision"]
            state["used"] += count
            conn.execute(
                "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (session, scope, dumps(state)),
            )
        engine.feedback(record_id, "read", session)
    from .read_policy import ReadPolicy

    # A read by id is an audit read. The text is all there; what is not experience is shown
    # under its class instead of the confirmation it was stored with.
    ReadPolicy.load(engine, Scope(**result["scope"]), "audit").present(result, result)
    result.update(
        content=piece,
        content_length=len(content),
        tokens=count,
        cursor=str(offset + len(piece)) if offset + len(piece) < len(content) else None,
    )
    return result
