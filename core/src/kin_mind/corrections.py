"""A memory set right where it is kept (her `correct_memory` tool, and the owner's 记错了 on her page).

The correction joins the memory itself, under its words, so wherever it is recalled from then on it
comes with its correction; what was made from it (an event's digest) is made again (lifecycle). The
words that said it was wrong are a source already -- the owner's message, or what the owner wrote on
her page, taken in as the owner's own words -- and her next review takes them in like any other: it
sets right what else rests on the old version (appraisal SYSTEM, 事实更正必须优先于先前的推测).
"""
from eventmem.core.db import digest
from eventmem.core.models import RevisionInput

LIMIT = 2000


def correct(mind, memory_id, correction, evidence_id):
    if not isinstance(correction, str) or not correction.strip() or len(correction) > LIMIT:
        raise ValueError(f"A correction is 1..{LIMIT} characters")
    if not isinstance(memory_id, str) or not memory_id.startswith(("mem_", "graph_")):
        raise ValueError("memory_id is a mem_ or graph_ id from recall")
    engine, line = mind.engine, "\n更正：" + correction.strip()
    command = "correction:" + digest([memory_id, correction, evidence_id])[:32]
    with engine.db.connect() as conn:
        # What told her it was wrong is evidence she has: a source of hers, current.
        if not mind._fresh(conn, mind._evidence(conn, [evidence_id])):
            raise ValueError("evidence_id is not a current source")
    if memory_id.startswith("mem_"):
        record = engine.get(memory_id)
        revised = engine.revise(memory_id, RevisionInput(expected_revision=record["revision"], command_id=command,
                                action="correct", content=record["content"] + line, reason="更正"))
        return {"state": "corrected", "memory_id": memory_id, "revision": revised["revision"]}
    from .graph import EventGraph
    graph = EventGraph(mind)
    with engine.db.connect() as conn:
        node = graph.get(conn, memory_id, follow=True)
        # A host event's node keeps no words of its own: recall shows its message, which the correction joins.
        words = node.get("text") or "\n".join(engine._get(conn, rid)["content"] for rid in node.get("record_ids", [])[:1]) or node["title"]
    # It rests on what it was made of and on what corrected it: recall still says whose words it was.
    graph.revise({"command_id": command, "id": node["id"], "expected_revision": node["revision"], "action": "correct",
                  "changes": {"text": words + line}, "evidence_ids": [*node.get("source_ids", []), evidence_id], "reason": "更正"})
    return {"state": "corrected", "memory_id": node["id"]}
