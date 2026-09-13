"""Interaction-led rhythm projections. There is no scheduled sleep or wake time."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import digest


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def interaction_windows(conn, scope, at, timezone="Asia/Singapore"):
    cutoff = (stamp(at) - timedelta(days=14)).isoformat()
    rows = conn.execute(
        "SELECT id,occurred_at,data,namespace FROM sources WHERE scope=? AND deleted=0 "
        "AND occurred_at>=? AND occurred_at<=? AND "
        "(namespace='kin-owner-input' OR namespace LIKE 'host:%') ORDER BY occurred_at,id",
        (scope, cutoff, at),
    ).fetchall()
    windows, seen = [], set()
    for row in rows:
        data = json.loads(row["data"])
        meta = data.get("metadata", {})
        if (
            data.get("authority") != "explicit"
            or meta.get("role") != "user"
            or meta.get("host_event") != "message"
        ):
            continue
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
        "window_count": len(windows),
        "active_days": len(days),
        "hourly_window_starts": hours,
        "recent_windows": windows[-8:],
        "last_owner_at": windows[-1]["end"] if windows else None,
        "last_owner_source_id": windows[-1]["last_source_id"] if windows else None,
        "sample_status": "forming"
        if len(days) < 3 or len(windows) < 5
        else "observing",
        "fingerprint": digest(windows),
    }


def rhythm_view(entry, at, interactions, *, fresh=True):
    if not entry:
        return {
            "mode": "interaction-led",
            "status": "forming",
            "phase": "forming",
            "interactions": interactions,
            "needs_review": False,
        }
    elapsed = max(0.0, (stamp(at) - stamp(entry["at"])).total_seconds())
    value = entry["target"] + (entry["alertness"] - entry["target"]) * 0.5 ** (
        elapsed / (60 * entry["half_life_minutes"])
    )
    phase = entry["phase"]
    latest = interactions.get("last_owner_at")
    if phase in {"resting", "drowsy"} and latest and stamp(latest) > stamp(entry["at"]):
        since = max(0, (stamp(at) - stamp(latest)).total_seconds())
        phase = "roused" if since < 1200 else "recovering"
    elif elapsed >= entry["half_life_minutes"] * 60:
        if value < 20:
            phase = "resting"
        elif value < 40:
            phase = "drowsy"
        elif entry["target"] < entry["alertness"]:
            phase = "settling"
        elif value >= 70:
            phase = "awake"
        else:
            phase = "recovering"
    return {
        "mode": "interaction-led",
        "status": interactions["sample_status"],
        "phase": phase,
        "proposed_phase": entry["phase"],
        "alertness": round(value, 3),
        "target": entry["target"],
        "half_life_minutes": entry["half_life_minutes"],
        "reason": entry["reason"],
        "basis": "runtime_inferred",
        "observed_at": entry["at"],
        "event_id": entry["event_id"],
        "config_version": entry["config_version"],
        "evidence_ids": [r["record_id"] for r in entry["evidence"]],
        "needs_review": not fresh,
        "interactions": interactions,
    }
