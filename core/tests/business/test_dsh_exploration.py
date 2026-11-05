"""Exploration on a DSH helper: the plugin's relay answers in place of Codex (dsh_executor)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from kin_mind.dsh_executor import MODEL, prepare_dsh_exploration

TOPIC = {"question": "synthetic", "topic": "synthetic", "known_evidence": [], "source_ids": []}
PAGE, UNREAD = "https://example.org/page", "https://example.org/unread"


def relay(tmp_path, answer):
    """A stand-in for the plugin's loopback relay (bridge.ts) and the model file that names it."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "key": self.headers["x-api-key"], "body": body})
            reply = json.dumps(answer(body)).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tmp_path / "state"
    (state / "credentials").mkdir(parents=True)
    (state / "credentials" / "kin-bridge.env").write_text("RESPONSES_RELAY_API_KEY=synthetic-key\n")
    (state / "model-config.json").write_text(json.dumps({"version": 1, "providers": {"kin": {
        "id": "kin", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "protocol": "anthropic-messages",
        "credentialFile": "kin-bridge.env", "models": ["task"]}},
        "roleProfiles": {"memoryRecall": {"providerId": "kin", "model": "task"}}}))
    engine = SimpleNamespace(model_config_file=state / "model-config.json", db=SimpleNamespace(root=tmp_path))
    return prepare_dsh_exploration({}, engine), seen, server


def test_a_page_the_helper_read_is_citable_and_one_it_did_not_is_dropped(tmp_path):
    findings = {"summary": "读到了", "findings": ["页面说了一件事"], "open_questions": [], "evidence_map": None,
                "sources": [{"url": PAGE, "title": "page"}, {"url": UNREAD, "title": "unread"}]}
    prepared, seen, server = relay(tmp_path, lambda body: {
        "state": "complete", "session": "child-1", "text": "```json\n" + json.dumps(findings) + "\n```",
        "reads": [{"call": "c1", "url": PAGE, "status": 200, "text": "页面正文", "at": "2026-11-05T00:00:00+00:00"},
                  {"call": "c2", "url": UNREAD, "status": 404, "text": "not found", "at": "2026-11-05T00:00:01+00:00"}]})
    report = prepared["runner"](None, TOPIC, tmp_path / "run", budget_seconds=60, model=MODEL, web={"enabled": True})
    server.shutdown()
    assert seen[0]["path"] == "/v1/kin/helper" and seen[0]["key"] == "synthetic-key"
    assert "web_fetch" in seen[0]["body"]["prompt"] and seen[0]["body"]["cwd"].endswith("run")
    assert report["state"] == "complete" and report["executor"] == "dsh-helper"
    assert [source["url"] for source in report["result"]["sources"]] == [PAGE]
    assert report["result"]["sources"][0]["receipt"]["tool"] == "web_fetch"
    assert {r["state"] for r in report["web_observations"]} == {"observed", "failed"}


def test_a_stop_closes_the_request_and_the_run_is_preempted(tmp_path):
    prepared, seen, server = relay(tmp_path, lambda body: time.sleep(30) or {"state": "complete"})
    stop = time.monotonic() + 1.5
    started = time.monotonic()
    report = prepared["runner"](None, TOPIC, tmp_path / "run", budget_seconds=60, model=MODEL,
                                canceled=lambda: time.monotonic() > stop)
    server.shutdown()
    assert report["state"] == "preempted" and time.monotonic() - started < 15


def test_without_the_plugin_relay_the_exploration_waits(tmp_path):
    engine = SimpleNamespace(model_config_file=tmp_path / "missing.json", db=SimpleNamespace(root=tmp_path))
    assert prepare_dsh_exploration({}, engine)["reason"] == "exploration-executor-unavailable"
