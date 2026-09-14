from __future__ import annotations

import json
from pathlib import Path

from .api import SessionBoundary, boundary
from .db import digest, dumps
from .envelopes import current_message
from .models import RecallRequest, Scope, SourceInput


def handle(engine, event, payload, *, receipt_only=False):
    """Host event normalization. Hosts provide observations, never recall rankings."""
    session = str(payload.get("session_id") or payload.get("sessionId") or "default")
    cwd = str(Path(payload.get("cwd") or ".").resolve())
    scope = Scope.model_validate(payload.get("scope") or {"project": cwd})
    scenario = payload.get("scenario", "tool")
    if event in {"start", "compact", "end"}:
        transcript = payload.get("transcript_path")
        if transcript and event in {"end", "compact"}:
            capture_transcript(engine, Path(transcript), session, scope)
        boundary_event = event
        if receipt_only or (
            payload.get("host") in {"claude-code", "codex"} and event == "compact"
        ):
            # PreCompact and offline replay save state without accounting for
            # context that the host has not actually injected.
            boundary_event = "checkpoint" if event != "end" else "end"
        elif (
            payload.get("host") in {"claude-code", "codex"}
            and event == "start"
            and payload.get("source") in {"compact", "clear"}
        ):
            boundary_event = "compact"
        return boundary(
            engine,
            SessionBoundary(
                session=session,
                scope=scope,
                event=boundary_event,
                scenario=scenario,
                command_id=payload.get("command_id")
                or digest([session, event, payload]),
                checkpoint=payload.get("checkpoint", {}),
            ),
        )
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    arguments = payload.get("tool_input", payload.get("args", {}))
    result = payload.get(
        "tool_response", payload.get("value", payload.get("contentText", ""))
    )
    raw_text = str(payload.get("text") or payload.get("prompt") or "")
    message = current_message(raw_text) if payload.get("role") == "user" else raw_text
    recalled = None
    if event == "message" and payload.get("recall_on_message") and not receipt_only and not payload.get("memory_context_managed"):
        # Query before receipt so the just-submitted prompt cannot echo back as
        # historical evidence or displace relevant earlier memories.
        recalled = engine.recall(
            RecallRequest(
                query=message[:4000],
                scope=scope,
                scenario=scenario,
                session=session,
                phase="passive",
            )
        )
    if event in {"tool", "message", "boundary"}:
        content = (
            dumps(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "result": result,
                    "error": payload.get("isError", False),
                }
            )
            if event == "tool"
            else str(raw_text or dumps(payload.get("data", payload)))
        )
        key = str(
            payload.get("tool_use_id")
            or payload.get("callId")
            or payload.get("command_id")
            or digest([event, session, content])
        )
        source = engine.receive(
            SourceInput(
                namespace="host:" + str(payload.get("host", "generic")),
                key=session + ":" + key,
                session=session,
                scope=scope,
                text=content,
                title=tool or event,
                kind="episode" if event == "tool" else "observation",
                authority="operation"
                if event in {"tool", "boundary"}
                else "model"
                if payload.get("role") == "assistant"
                else "explicit",
                metadata={
                    "host_event": event,
                    "tool": tool,
                    "action": arguments,
                    "outcome": result,
                    **({"role": payload["role"]} if payload.get("role") else {}),
                    **(
                        {"turn_id": payload["turn_id"]}
                        if payload.get("turn_id")
                        else {}
                    ),
                },
                extract=bool(payload.get("extract", event == "message")),
            )
        )
    if event in {"tool", "pre_action"} and not receipt_only and not payload.get("memory_context_managed"):
        if isinstance(arguments, dict):
            cue = " ".join(
                str(arguments.get(key, ""))
                for key in ("file_path", "path", "command", "query", "description")
            )
        else:
            cue = str(arguments)
        if payload.get("isError"):
            cue += " " + str(payload.get("errorMessage", ""))
        if not cue.strip():
            return {"status": "received", "id": source["id"]} if event == "tool" else {}
        return engine.recall(
            RecallRequest(
                query=cue[:4000],
                scope=scope,
                scenario=scenario,
                session=session,
                phase="passive",
            )
        )
    return {"status": "received", "id": source["id"], **(recalled or {})}


def capture_transcript(engine, path, session, scope):
    # Byte offsets and hashes survive retries without a model watermark. Every
    # complete source line is received before any background model job runs.
    with path.open("rb") as file:
        offset = 0
        for line in file:
            start, offset = offset, offset + len(line)
            if not line.endswith(b"\n"):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            message = record.get("message", record)
            role = message.get("role", record.get("type", "unknown"))
            content = message.get("content")
            if not content:
                continue
            text = content if isinstance(content, str) else dumps(content)
            engine.receive(
                SourceInput(
                    namespace="transcript",
                    key=f"{session}:{start}:{digest(line)[:16]}",
                    session=session,
                    scope=scope,
                    text=text,
                    title=f"{role} message",
                    authority="explicit" if role == "user" else "model",
                    extract=role == "user",
                    origin=str(path),
                    metadata={"byte_offset": start, "byte_end": offset, "role": role},
                )
            )
