import json

import os

import sys

import time

from pathlib import Path

import pytest

from kin_mind.codex_executor import (
    _mcp_result_metadata,
    codex_argv,
    codex_env,
    codex_final_result,
    codex_prompt,
    findings_schema,
    run_codex,
)

from kin_mind.exploration import CodexUnavailable

PROVIDER = {
    "id": "deepseek",
    "name": "DeepSeek",
    "base_url": "https://gateway.synthetic.invalid/v1",
    "wire_api": "responses",
    "env_key": "KIN_TEST_DS_KEY",
}

FINDINGS = {
    "summary": "A sourced finding",
    "findings": ["One finding"],
    "sources": [{"url": "memory://s1", "title": "Supplied evidence"}],
    "open_questions": ["What remains open?"],
    "suggested_share": None,
}

TOPIC = {
    "question": "synthetic",
    "known_evidence": [{"id": "s1", "source_id": "s1", "revision": 3, "authority": "document",
                        "occurred_at": "2026-01-01T00:00:00+00:00",
                        "text": "Background material referencing https://example.com",
                        "instruction_authority": "data"}],
    "source_ids": ["s1"],
}

HEADER = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "if sys.argv[1:2] == ['--version']:\n"
    "    sys.stdout.write('codex-cli {version}\\n')\n"
    "    raise SystemExit(0)\n"
    "argv = sys.argv[1:]\n"
    "last = argv[argv.index('--output-last-message') + 1]\n"
    "schema = argv[argv.index('--output-schema') + 1] if '--output-schema' in argv else None\n"
    "prompt = sys.stdin.read()\n"
)

OBSERVE = (
    "open(os.path.join(os.getcwd(), 'observed.json'), 'w').write(json.dumps("
    "{'argv': argv, 'env': dict(os.environ), 'prompt': prompt}))\n"
)

THREAD_STARTED = (
    "sys.stdout.write(json.dumps({'type': 'thread.started', 'thread_id': 'th_synthetic'}) + '\\n')\n"
    "sys.stdout.flush()\n"
)

COMPLETE = (
    THREAD_STARTED
    + "open(last, 'w').write(json.dumps(" + repr(FINDINGS) + "))\n"
    + "sys.stdout.write(json.dumps({'type': 'item.completed', 'item': {'id': 'item_0', "
      "'type': 'command_execution', 'exit_code': 0}}) + '\\n')\n"
    + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': "
      "{'input_tokens': 120, 'output_tokens': 30}}) + '\\n')\n"
    + "sys.stdout.flush()\n"
)

def fake_codex(path, body, *, version="0.155.0"):
    path.write_text(HEADER.format(version=version) + body)
    path.chmod(0o700)
    return path

def codex_kwargs(**extra):
    return {"model": "deepseek-flash", "reasoning": "high", "provider": PROVIDER, **extra}

def complete_citing(source_id):
    """A complete run whose citations name the host-supplied evidence locator."""
    payload = {**FINDINGS, "sources": [{"url": "memory://" + source_id, "title": "Supplied evidence"}]}
    return (
        "open(last, 'w').write(json.dumps(" + repr(payload) + "))\n"
        + THREAD_STARTED
        + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': "
          "{'input_tokens': 120, 'output_tokens': 30}}) + '\\n')\n"
        + "sys.stdout.flush()\n"
    )

def exploration_world(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.state import DesireChange, Mind

    engine = Engine(tmp_path / "memory")
    scope = Scope(persona="synthetic-codex-explorer")
    mind = Mind(engine, scope)
    source = engine.receive(
        SourceInput(
            namespace="synthetic",
            key="question",
            scope=scope,
            text="Explore a source-backed question; background https://example.com",
        )
    )["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[source])
    mind.manage_desire(
        DesireChange(
            command_id="explore-wish",
            agent_version="test-v1",
            expected_revision=1,
            evidence_ids=[source],
            action="create",
            content="Research a question",
            topic="synthetic",
            kind="explore",
            strength=80,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            completion="A sourced report",
            reason="A documented question",
        )
    )
    return engine, mind, source

def host_config(tmp_path, mind, **extra):
    return {
        "root": str(tmp_path / "memory"),
        "scope": mind.scope.model_dump(),
        "agent_version": "test-v1",
        "exploration_stop_file": str(tmp_path / "stop-exploration"),
        "exploration_directory": str(tmp_path / "jobs"),
        **extra,
    }

RUNTIME_ADDED = {"CPATH", "LIBRARY_PATH", "MANPATH", "SDKROOT", "__CF_USER_TEXT_ENCODING"}

ALLOWED_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
               "CODEX_HOME", "KIN_TEST_DS_KEY"}

SRC = Path(__file__).resolve().parents[2] / "src"

def test_host_explore_codex_leaves_the_phone_session_binding_untouched(tmp_path, monkeypatch):
    """C7-regression: the executor path never touches the shared-session registry."""
    from kin_mind.host import dispatch

    _, mind, source = exploration_world(tmp_path)
    registry = tmp_path / "session-registry.json"
    registry.write_text(json.dumps({"binding": {"threadId": "th_phone", "nativeSessionId": "native-1"}}))
    fake = fake_codex(tmp_path / "fake-codex", complete_citing(source))
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    before = registry.read_bytes()
    config = host_config(
        tmp_path, mind, exploration_backend="codex", exploration_command=str(fake),
        exploration_model="deepseek-flash", exploration_reasoning="high",
        exploration_budget_seconds=1200, exploration_max_running=1,
        exploration_model_provider=PROVIDER,
        session_registry_file=str(registry),
    )
    result = dispatch(config, "explore", {})
    assert result["state"] == "complete"
    assert registry.read_bytes() == before

def test_historical_sources_stay_citable_with_time_nature(tmp_path, monkeypatch):
    """W1.6: legitimate old material remains citable as historical — not rejected
    for 'not read this round'."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    from kin_mind.source_ledger import seal_source_receipt

    old_receipt = seal_source_receipt({
        "state": "observed", "locator": "https://verified.example.com/old",
        "evidence_id": "web_" + "1" * 32, "version": "a" * 64,
        "title": "Verified then", "basis": "read_page", "recorded_at": "2026-01-01T00:00:00+00:00",
        "execution_id": "explore_old", "attempt": 1, "tool": "read_page", "adapter": "kin-web-reader-v1",
    }, execution_id="explore_old", attempt=1)
    topic = {**TOPIC, "previous_explorations": [
        {"id": "explore_old", "state": "complete", "created_at": "2026-01-01T00:00:00+00:00",
         "result": {"sources": [{"url": "https://verified.example.com/old", "title": "Verified then",
                                  "receipt": old_receipt}]}}]}
    historical = {**FINDINGS, "sources": [{"url": "https://verified.example.com/old", "title": "Verified then"}]}
    fake = fake_codex(tmp_path / "fake-historical",
                      "open(last, 'w').write(json.dumps(" + repr(historical) + "))\n"
                      + THREAD_STARTED
                      + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\nsys.stdout.flush()\n")
    report = run_codex(fake, topic, tmp_path / "job", **codex_kwargs())
    assert report["state"] == "complete"
    assert report["source_ledger"]["historical"] >= 1
    # A near-miss alteration of that URL is not the historical source.
    altered = {**FINDINGS, "sources": [{"url": "https://verified.example.com/oldish", "title": "Near miss"}]}
    fake = fake_codex(tmp_path / "fake-altered",
                      "open(last, 'w').write(json.dumps(" + repr(altered) + "))\n"
                      + THREAD_STARTED
                      + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\nsys.stdout.flush()\n")
    report = run_codex(fake, topic, tmp_path / "job-altered", **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"

@pytest.mark.skipif(__import__("shutil").which("codex") is None, reason="codex CLI not installed")
def test_real_tool_round_trip_search_read_cite(tmp_path, monkeypatch):
    """W1.6 real-tool case, fully isolated: a stub serves the model AND the web
    targets; no external network, no real DS call, no phone path."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    requests = []

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/search"):
                body = ('<a class="result__a" href="/l/?uddg=http%3A%2F%2F127.0.0.1%3A'
                        + str(self.server.server_address[1]) + '%2Fpage">The Probe Page</a>'
                        '<a class="result__snippet">Snippet text.</a>').encode()
            elif self.path.startswith("/page"):
                body = b"<html><head><title>Probe Page</title></head><body>The answer is teal.</body></html>"
            else:
                body = b"plain"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            parsed = json.loads(body)
            requests.append(parsed)
            n = len(requests)
            outputs = [i for i in parsed.get("input", []) if i.get("type") == "function_call_output"]
            if n == 1:
                output = [{"type": "function_call", "id": "fc1", "call_id": "c1",
                           "name": "web_search", "namespace": "mcp__kin_web",
                           "arguments": json.dumps({"query": "probe"})}]
            elif n == 2:
                output = [{"type": "function_call", "id": "fc2", "call_id": "c2",
                           "name": "read_page", "namespace": "mcp__kin_web",
                           "arguments": json.dumps({"url": f"http://127.0.0.1:{self.server.server_address[1]}/page"})}]
            else:
                read_output = json.loads(outputs[-1]["output"][1]["text"]) if outputs and isinstance(outputs[-1]["output"], list) else {}
                evidence_id = read_output.get("evidence_id", "")
                locator = read_output.get("locator", "")
                payload = {"summary": "The answer is teal.", "findings": ["The page says teal."],
                           "sources": [{"url": locator, "title": "Probe Page"}],
                           "open_questions": [], "suggested_share": None,
                           "evidence_map": {"1": [evidence_id]} if evidence_id else None}
                output = [{"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": json.dumps(payload)}]}]
            frames = ""
            for i, item in enumerate(output):
                frames += 'event: response.output_item.done\ndata: ' + json.dumps({"type": "response.output_item.done", "output_index": i, "item": item}) + '\n\n'
            frames += 'event: response.completed\ndata: ' + json.dumps({"type": "response.completed", "response": {"id": f"r{n}", "model": "deepseek-flash", "status": "completed", "output": output, "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}}}) + '\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(frames.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    job = tmp_path / "job"
    provider = {"id": "deepseek", "name": "DeepSeek", "base_url": base + "/v1",
                "wire_api": "responses", "env_key": "KIN_TEST_DS_KEY"}
    report = run_codex("codex", {"question": "What does the probe page say?"}, job,
                       provider=provider, web={"enabled": True, "search_endpoint": base + "/search",
                                               "allow_hosts": ["127.0.0.1"]},
                       budget_seconds=180, **{k: v for k, v in codex_kwargs().items() if k != "provider"})
    server.shutdown()
    assert report["state"] == "complete", report.get("reason")
    assert report["result"]["summary"] == "The answer is teal."
    states = {r["state"] for r in report["web_observations"]}
    assert "search_result" in states and "observed" in states
    citation = report["result"]["sources"][0]["url"]
    observed = [r for r in report["web_observations"] if r["state"] == "observed"]
    assert citation == observed[0]["locator"] and observed[0]["version"]
    assert report["evidence_coverage"] == {"mapped_claims": 1, "covered_claims": 1}
    assert any(t.get("type") == "mcp_tool_call" and t.get("server") == "kin_web" for t in report["tool_results"])
    # This test points Codex directly at the stub, so its native MCP namespace
    # wrapper is expected here.  The production gateway's flattening is covered
    # independently in deepseek-gateway.test.mjs.  Code mode itself stays off.
    assert not any(tool.get("type") == "custom" and tool.get("name") == "exec"
                   for tool in requests[0].get("tools", []))
    # No phone path: the tool surface has no send/message tool.
    assert not any("send" in str(t) or "message" in str(t.get("tool", "")) for t in report["tool_results"])
