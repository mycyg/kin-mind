"""The Claude Code plugin's hook entry: hooks/run.sh runs `python -m eventmem.hooks.bridge`.

The bridge is the plugin's adapter to the service, not part of the removed `.memory` stack
(a7e6334 took it along by mistake). Each hook call is spooled first and then posted to
/v1/host/events; when the service cannot be reached the receipt stays in the spool for the
worker to replay, and the hook still exits 0 without output so Claude Code carries on."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = {"session_id": "synthetic-session", "tool_name": "Read", "tool_input": {"file_path": "/tmp/example"}}


def hook(home: Path, url: str, event: str = "tool") -> subprocess.CompletedProcess:
    env = {**os.environ, "EVENTMEM_HOME": str(home), "EVENTMEM_URL": url}
    return subprocess.run([sys.executable, "-m", "eventmem.hooks.bridge", event], input=json.dumps(PAYLOAD),
                          capture_output=True, text=True, env=env, timeout=60)


def home_with_token(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "local-token").write_text("synthetic-token\n")
    return home


def test_the_plugin_hooks_run_the_bridge_module():
    assert "-m eventmem.hooks.bridge" in (ROOT / "hooks" / "run.sh").read_text()
    assert all("eventmem.hooks.bridge" in hook["command"]
               for entries in json.loads((ROOT / "examples" / "settings.json").read_text())["hooks"].values()
               for entry in entries for hook in entry["hooks"])


def test_an_unreachable_service_leaves_the_event_in_the_spool(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    home = home_with_token(tmp_path)
    answer = hook(home, f"http://127.0.0.1:{port}")
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout == ""
    spooled = list((home / "host-spool").glob("*.json"))
    assert len(spooled) == 1
    assert json.loads(spooled[0].read_text()) == {"event": "tool", "payload": {**PAYLOAD, "host": "claude-code"}}
    assert (home / "host-spool").stat().st_mode & 0o777 == 0o700


def test_a_delivered_event_leaves_no_receipt_and_returns_the_service_context(tmp_path):
    received = []

    class Service(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - the http.server interface
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Authorization"], body))
            answer = json.dumps({"text": "synthetic recalled context"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = home_with_token(tmp_path)
        answer = hook(home, f"http://127.0.0.1:{server.server_address[1]}", event="start")
    finally:
        server.shutdown()
        server.server_close()
    assert answer.returncode == 0, answer.stderr
    assert received == [("/v1/host/events", "Bearer synthetic-token",
                         {"event": "start", "payload": {**PAYLOAD, "host": "claude-code"}})]
    assert list((home / "host-spool").glob("*.json")) == []
    assert json.loads(answer.stdout) == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": "synthetic recalled context"}}
