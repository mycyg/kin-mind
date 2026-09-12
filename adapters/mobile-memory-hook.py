"""Mobile-only memory hook: host receipts identify owner and internal inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


def filtered_event(raw, events: Path):
    """Only a host-created receipt may turn transport text into owner evidence.

    The receipt directory is private to the mobile host. Prompt markers alone
    have no authority. Internal output and tools remain in the native transcript
    and host event journal, but do not become interpersonal memory evidence.
    """
    name, session = raw.get("hook_event_name"), raw.get("session_id")
    if not session:
        return None
    binding = events / ("turn-" + hashlib.sha256(str(session).encode()).hexdigest() + ".json")
    if name == "UserPromptSubmit":
        markers = re.findall(r"<kin-host-event>([a-f0-9]{32})</kin-host-event>", str(raw.get("prompt", "")))
        if not markers:
            return None
        try:
            record = json.loads((events / (markers[-1] + ".json")).read_text())
            if record["sessionId"] != session:
                return None
        except (OSError, ValueError, KeyError):
            return None
        temporary = binding.with_suffix(".tmp-" + str(os.getpid()))
        temporary.write_text(json.dumps({**record, "turn_id": raw.get("turn_id")}))
        temporary.replace(binding)
        if record["kind"] != "owner":
            return None
        return {**raw, "prompt": record["text"]}
    if name in {"Stop", "PreToolUse", "PostToolUse"}:
        try:
            record = json.loads(binding.read_text())
            if record["kind"] != "owner":
                return None
            if raw.get("turn_id") and record.get("turn_id") and raw["turn_id"] != record["turn_id"]:
                return None
        except (OSError, ValueError, KeyError):
            return None
    return raw


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--scenario", default="companion")
    args = parser.parse_args()
    result = {}
    try:
        raw = filtered_event(json.load(sys.stdin), args.events)
        if raw:
            from eventmem.hooks.codex import run
            result = run(raw, root=args.root, url=args.url, scope=json.loads(args.scope), scenario=args.scenario)
    except Exception as error:
        print("Mobile memory hook unavailable (" + type(error).__name__ + ").", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
