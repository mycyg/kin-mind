"""Kin's lasting rhythm and temporary attention, without scheduled sleep or wake times.

Interaction windows are useful context. They do not decide Kin's own phase, and an arriving
message does not overwrite the phase she last chose. Reads project attention; only a successful
assessment changes the stored rhythm.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

INTERACTION_SCHEMA = """
CREATE INDEX IF NOT EXISTS mind_source_versions ON sources(namespace,source_key,scope,received_at)
 WHERE deleted=0;
CREATE INDEX IF NOT EXISTS mind_owner_interactions ON sources(scope,occurred_at,id)
 WHERE deleted=0 AND (namespace='kin-owner-input' OR substr(namespace,1,5)='host:')
 AND json_extract(data,'$.authority')='explicit'
 AND json_extract(data,'$.metadata.role')='user'
 AND json_extract(data,'$.metadata.host_event')='message';
CREATE INDEX IF NOT EXISTS mind_human_interactions ON sources(scope,occurred_at,id)
 WHERE deleted=0 AND (namespace IN ('kin-owner-input','kin-companion-input') OR substr(namespace,1,5)='host:')
 AND json_extract(data,'$.authority')='explicit'
 AND json_extract(data,'$.metadata.role')='user'
 AND json_extract(data,'$.metadata.host_event')='message';
"""


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# The one phase that means the role is not up. `drowsy` is still on its way there.
REST_PHASE = "resting"


def quiet_hours(contact, at):
    """Whether `at` falls inside the owner's do-not-disturb window, in their own timezone."""
    start, end = contact.get("quiet_start"), contact.get("quiet_end")
    if type(start) is not int or type(end) is not int or start == end:
        return False
    hour = stamp(at).astimezone(ZoneInfo(contact.get("timezone") or "Asia/Singapore")).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def at_rest(view, at):
    """Rest/contact timing, independent of temporary attention to an input."""
    rhythm = view.get("rhythm") or {}
    return ((rhythm.get("phase") or rhythm.get("base_phase")) == REST_PHASE
            or quiet_hours(view.get("contact") or {}, at))


def interaction_windows(conn, scope, at, timezone="Asia/Singapore", *, recipient_person_id="person:xiaoguang", conversation_key=None):
    """Activity in the selected conversation, never another person's private messages.

    Legacy unannotated owner messages belong to Xiaoguang. A group is its own
    recipient; its human speakers count in that group, not in either private chat.
    """
    cutoff = (stamp(at) - timedelta(days=14)).isoformat()
    rows = conn.execute(
        "SELECT s.id,s.occurred_at FROM sources s WHERE s.scope=? AND s.deleted=0 "
        "AND s.occurred_at>=? AND s.occurred_at<=? "
        "AND (s.namespace IN ('kin-owner-input','kin-companion-input') OR substr(s.namespace,1,5)='host:') "
        "AND json_extract(s.data,'$.authority')='explicit' "
        "AND json_extract(s.data,'$.metadata.role')='user' "
        "AND json_extract(s.data,'$.metadata.host_event')='message' "
        "AND COALESCE(json_extract(s.data,'$.metadata.recipient_person_id'),json_extract(s.data,'$.metadata.speaker_person_id'),'person:xiaoguang')=? "
        "AND (? IS NULL OR json_extract(s.data,'$.metadata.conversation_key')=? "
        "OR (s.namespace!='kin-companion-input' AND json_extract(s.data,'$.metadata.conversation_key') IS NULL AND ?='person:xiaoguang')) "
        "AND NOT EXISTS(SELECT 1 FROM sources newer WHERE newer.namespace=s.namespace "
        "AND newer.source_key=s.source_key AND newer.scope=s.scope AND newer.deleted=0 "
        "AND newer.received_at>s.received_at) ORDER BY s.occurred_at,s.id",
        (scope, cutoff, at, recipient_person_id, conversation_key, conversation_key, recipient_person_id),
    ).fetchall()
    windows, seen = [], set()
    for row in rows:
        # Namespace plus original channel ID are already durable ingestion keys.
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        time = stamp(row["occurred_at"])
        if windows and (time - stamp(windows[-1]["end"])).total_seconds() <= 1800:
            windows[-1]["end"] = row["occurred_at"]
            windows[-1]["last_source_id"] = row["id"]
        else:
            windows.append(
                {
                    "start": row["occurred_at"],
                    "end": row["occurred_at"],
                    "last_source_id": row["id"],
                }
            )
    hours = [0] * 24
    days = set()
    for window in windows:
        local = stamp(window["start"]).astimezone(ZoneInfo(timezone))
        hours[local.hour] += 1
        days.add(local.date().isoformat())
    return {
        "mode": "interaction-led",
        "window_days": 14,
        "join_minutes": 30,
        "timezone": timezone,
        "recipient_person_id": recipient_person_id,
        "window_count": len(windows),
        "active_days": len(days),
        "hourly_window_starts": hours,
        "recent_windows": windows[-8:],
        "last_owner_at": windows[-1]["end"] if windows else None,
        "last_owner_source_id": windows[-1]["last_source_id"] if windows else None,
        "sample_status": "forming"
        if len(days) < 3 or len(windows) < 5
        else "observing",
    }


def rhythm_view(entry, at, interactions, *, fresh=True):
    if not entry:
        return {
            "mode": "independent",
            "status": "forming",
            "phase": "forming",
            "base_phase": "forming",
            "attention_for": None,
            "return_to": None,
            "interactions": interactions,
            "needs_review": False,
        }
    elapsed = max(0.0, (stamp(at) - stamp(entry["at"])).total_seconds())
    value = entry["target"] + (entry["alertness"] - entry["target"]) * 0.5 ** (
        elapsed / (60 * entry["half_life_minutes"])
    )
    # phase is the single chosen phase; base_phase remains a compatibility alias.
    # Attention already has its own independent field, so a second phase could
    # otherwise silently override "resting" with a stale "awake".
    phase = entry.get("phase") or entry["base_phase"]
    attention = entry.get("attention_for")
    return_to = entry.get("return_to")
    latest = interactions.get("last_owner_at")
    reviewed_at = max(stamp(entry["at"]), stamp(entry.get("attention_cleared_at") or entry["at"]))
    if latest and stamp(latest) > reviewed_at:
        # Arrival draws attention, never proves wakefulness or a new mood. A task/urgent
        # assessment keeps its stronger attention until another assessment clears it.
        attention = attention if attention in {"task", "urgent"} else "owner_message"
        return_to = return_to or phase
    # The phase Kin gave stays hers when its half-life has passed: no timer moves her into or out
    # of rest. Its end only asks her to look again (review_due; K1-04).
    review_due = elapsed >= entry["half_life_minutes"] * 60
    return {
        "mode": "independent",
        "status": "current",
        "phase": phase,
        "base_phase": phase,
        "attention_for": attention,
        "attention_input_id": entry.get("attention_input_id"),
        "attention_at": entry.get("attention_at"),
        "return_to": return_to,
        "review_due": review_due,
        "proposed_phase": entry["phase"],
        "alertness": round(value, 3),
        "target": entry["target"],
        "half_life_minutes": entry["half_life_minutes"],
        "reason": entry.get("reason", ""),
        "note": entry.get("note"),
        "current_activity": entry.get("current_activity"),
        "previous": entry.get("previous"),
        "basis": "subjective",
        "observed_at": entry["at"],
        "event_id": entry["event_id"],
        "config_version": entry["config_version"],
        "evidence_ids": [r["record_id"] for r in entry.get("evidence", [])],
        "needs_review": not fresh,
        "interactions": interactions,
    }
