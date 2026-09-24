"""Claude Code v1 adapter: durable host normalization through the common core."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "tool"
    payload = json.load(sys.stdin)
    payload["host"] = "claude-code"
    root = Path(os.environ.get("EVENTMEM_HOME", str(Path.home() / ".memorypalace")))
    from eventmem.core.db import digest, dumps

    spool = root / "host-spool"
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    from eventmem.paths import atomic_write

    pending = spool / (digest([event, payload]) + ".json")
    atomic_write(pending, dumps({"event": event, "payload": payload}))
    try:
        token = (root / "local-token").read_text().strip()
        with httpx.Client(
            base_url=os.environ.get("EVENTMEM_URL", "http://127.0.0.1:8319"),
            timeout=7,
            headers={"Authorization": "Bearer " + token},
        ) as client:
            response = client.post(
                "/v1/host/events", json={"event": event, "payload": payload}
            )
            response.raise_for_status()
            result = response.json()
        pending.unlink(missing_ok=True)
    except (OSError, httpx.HTTPError):
        # Receipt remains local if the service is unavailable. Replay is performed
        # by the service worker; no independent host recall policy is introduced.
        result = {"text": ""}
    text = result.get("text", "")
    hook_names = {
        "start": "SessionStart",
        "pre_action": "PreToolUse",
        "tool": "PostToolUse",
        "compact": "PreCompact",
        "end": "SessionEnd",
        "message": "UserPromptSubmit",
    }
    if text:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": hook_names[event],
                        "additionalContext": text,
                    }
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
