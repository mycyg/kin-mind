"""Small, timestamped public dialogue windows, independent of memory summaries.

Clock envelopes are generated at use time, outside cached evidence. Restored
history keeps its original times and never becomes another owner input.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json

from eventmem.core.db import Conflict, Missing
# The envelope openings have one definition, in the read policy. This lane filtered by a copy
# of one of them; it now asks that list, which is the built-in set plus the host's own.
from eventmem.core.read_policy import HOST_PREFIXES, envelope_prefixes, host_envelope

TIMEZONE = "Asia/Singapore"
RECENT_EXCHANGES = 4


def is_public_dialogue(event, prefixes=HOST_PREFIXES):
    return (event.get('kind') in {'owner-message', 'assistant-message', 'delivery'}
            and bool(event.get('text')) and not event.get('internal')
            and event.get('origin') not in {'runtime-notice', 'runtime-status', 'host-control'}
            and (event.get('kind') != 'delivery' or event.get('state') == 'accepted')
            and not host_envelope(event['text'], prefixes))


def utc_time(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Conversation timestamps require a timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def clock_context(now, zone=TIMEZONE):
    stamp = datetime.fromisoformat(utc_time(now))
    return {"current_time": stamp.isoformat(), "local_time": stamp.astimezone(ZoneInfo(zone)).isoformat(),
            "timezone": zone, "authority": "host-clock", "historical_times_are_not_now": True}


def split_recent(items, exchanges=RECENT_EXCHANGES):
    """Keep complete user/assistant exchanges, including a preceding question.

    Consecutive owner bubbles form one input block; assistant bubbles do not
    consume the exchange count. Items are already ordered chronologically.
    """
    starts = [i for i, item in enumerate(items) if item.get("role") == "user"
              and (i == 0 or items[i-1].get("role") != "user")]
    if not starts:
        return items[-4:], items[:-4]
    start = starts[-exchanges] if len(starts) >= exchanges else 0
    while start > 0 and items[start-1].get("role") == "assistant":
        start -= 1
    return items[start:], items[:start]


def redundant_public_summaries(events):
    """Recognize a public turn aggregate already represented by sent bubbles.

    Require exact ordered text and nearby receipt times. This only removes a
    duplicate view; original operation and source records remain untouched.
    """
    redundant, deliveries = set(), []
    for event in sorted(events, key=lambda e: (utc_time(e['at']), e['id'])):
        if event.get('kind') == 'owner-message':
            deliveries = []
        elif event.get('kind') == 'delivery' and event.get('state') == 'accepted' and event.get('text'):
            deliveries.append(event)
        elif event.get('kind') == 'assistant-message' and event.get('text'):
            stamp = datetime.fromisoformat(utc_time(event['at']))
            nearby = [e for e in deliveries if 0 <= (stamp-datetime.fromisoformat(utc_time(e['at']))).total_seconds() <= 180]
            for start in range(len(nearby)):
                if '\n\n'.join(e['text'] for e in nearby[start:]) == event['text']:
                    redundant.add(event['id'])
                    break
    return redundant


def dialogue_rows(mind, *, exchanges=RECENT_EXCHANGES, include_historical=True):
    """Page past multi-bubble output instead of counting bubbles as turns."""
    with mind.engine.db.connect() as conn:
        rows = []
        for page in range(16):
            chunk = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND kind IN ('owner-message','assistant-message','delivery') "
                + ("AND COALESCE(json_extract(data,'$.historical'),0)=0 " if not include_historical else "")
                + "ORDER BY julianday(occurred_at) DESC,seq DESC LIMIT 128 OFFSET ?", (mind.scope.key(), page*128)).fetchall()
            rows.extend(chunk)
            roles = ["user" if e["kind"] == "owner-message" else "assistant" for e in (json.loads(r["data"]) for r in reversed(rows)) if e.get("text")]
            blocks = sum(role == "user" and (i == 0 or roles[i-1] != "user") for i, role in enumerate(roles))
            if len(chunk) < 128 or blocks > exchanges:
                break
        return rows


def recent_dialogue(mind, *, exchanges=RECENT_EXCHANGES):
    rows = dialogue_rows(mind, exchanges=exchanges)
    redundant = redundant_public_summaries([json.loads(r['data']) for r in rows])
    with mind.engine.db.connect() as conn:
        prefixes = envelope_prefixes(mind.engine, conn)
        items, seen = [], {}
        for row in reversed(rows):
            event = json.loads(row["data"])
            if event['id'] in redundant:
                continue
            if not is_public_dialogue(event, prefixes):
                continue
            try:
                refs = mind._evidence(conn, [event["source_id"]])
                if not mind._fresh(conn, refs):
                    continue
                source = conn.execute("SELECT received_at FROM sources WHERE id=?", (event["source_id"],)).fetchone()
            except (Conflict, Missing, KeyError):
                continue
            role = "user" if event["kind"] == "owner-message" else "assistant"
            item = {k: event[k] for k in ("id", "kind", "text", "at", "source_id", "channel", "state", "message_id", "input_id", "bubble_id") if k in event}
            item.update(seq=row["seq"], role=role, occurred_at=utc_time(event["at"]),
                        received_at=utc_time(event.get("received_at") or (source[0] if source else None)),
                        time_basis=event.get("time_basis", "recorded-event"), historical=bool(event.get("historical")), instruction_authority="historical-data")
            # Public output and its acceptance event are two pieces of evidence
            # for one bubble. Identical utterances at other times remain intact.
            key = (role, item["text"])
            previous = seen.get(key)
            if role == "assistant" and previous and previous["kind"] != item["kind"] and abs((datetime.fromisoformat(previous["occurred_at"]) - datetime.fromisoformat(item["occurred_at"])).total_seconds()) < 30:
                if item["kind"] == "delivery":
                    previous.update(delivery_at=item["occurred_at"], delivery_state="accepted", message_id=item.get("message_id"))
                continue
            seen[key] = item
            items.append(item)
    recent, _ = split_recent(items, exchanges)
    return recent
