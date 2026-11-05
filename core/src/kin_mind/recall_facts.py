"""Read-time attribution and dates, kept separate from remembered prose."""
import json
from datetime import datetime


def elapsed_hours(at, now):
    if not at:
        return None
    try:
        start = datetime.fromisoformat(at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            return None
        return round((end - start).total_seconds() / 3600, 1)
    except (ValueError, TypeError):
        return None


def source_facts(conn, scope, source_ids, now):
    """Only explicit identity fields; source time does not date a story inside it."""
    if not source_ids:
        return {}
    annotated = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_companion_sources'").fetchone()
    found = []
    for sid in dict.fromkeys(source_ids):
        row = conn.execute("SELECT data,occurred_at,received_at FROM sources WHERE id=? AND scope=? AND deleted=0",
                           (sid, scope)).fetchone()
        if not row:
            continue
        metadata = json.loads(row["data"]).get("metadata", {})
        annotation = conn.execute("SELECT data FROM mind_companion_sources WHERE scope=? AND source_id=?", (scope, sid)).fetchone() if annotated else None
        identity = {**metadata, **(json.loads(annotation[0]) if annotation else {})}
        at = metadata.get("original_occurred_at") or row["occurred_at"]
        fact = {"source_id": sid, "source_at": at, "recorded_at": row["received_at"],
                "source_age_hours": elapsed_hours(at, now)}
        for key in ("speaker_person_id", "recipient_person_id", "subject_ids"):
            if identity.get(key):
                fact[key] = identity[key]
        participants = [{k: p[k] for k in ("person_id", "role") if k in p}
                        for p in identity.get("participants", []) if p.get("person_id")]
        if participants:
            fact["participants"] = participants
        found.append(fact)
    if not found:
        return {}
    # Keep the earliest and latest evidence when many sources support one item.
    found.sort(key=lambda f: f["source_at"] or "")
    shown = found if len(found) <= 2 else [found[0], found[-1]]
    return {"as_of": now, "sources": shown,
            **({"other_source_count": len(found) - len(shown)} if len(found) > len(shown) else {})}
