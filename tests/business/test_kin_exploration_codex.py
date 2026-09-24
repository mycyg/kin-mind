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

# Added by the child's own runtime rather than passed to it: the macOS toolchain shim, and
# Python's C-locale coercion (PEP 538) when LANG is unset.
RUNTIME_ADDED = {"CPATH", "LIBRARY_PATH", "MANPATH", "SDKROOT", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"}

ALLOWED_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
               "CODEX_HOME", "KIN_TEST_DS_KEY"}

SRC = Path(__file__).resolve().parents[2] / "src"

HOST_SECRETS = {"DEEPSEEK_API_KEY": "host-only", "OPENAI_API_KEY": "host-only",
                "FEISHU_APP_SECRET": "host-only", "EVENTMEM_API_KEY": "host-only"}

# The hermetic test runner carries its own guard into every child process; that is the runner's
# doing, not the executor's, and only while the runner is active.
GUARD_CARRIED = {"KIN_PROTECTED_ROOTS", "KIN_REMAP", "KIN_BLOCK_NETWORK", "KIN_BLOCKED_PORTS",
                 "KIN_PROTECTED_PIDS", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "NODE_OPTIONS"}

def test_the_exploration_child_sees_only_its_allowlisted_environment(tmp_path, monkeypatch):
    """No channel or model credential of the host reaches the codex child: only the allow-list,
    its own CODEX_HOME and the one provider key named for the run."""
    for key, value in {**HOST_SECRETS, "KIN_TEST_DS_KEY": "provider-key"}.items():
        monkeypatch.setenv(key, value)
    child = codex_env(tmp_path / "codex-home", env_key="KIN_TEST_DS_KEY")
    assert set(child) <= ALLOWED_ENV and child["KIN_TEST_DS_KEY"] == "provider-key"
    assert child["CODEX_HOME"] == str(tmp_path / "codex-home")
    with pytest.raises(CodexUnavailable):
        codex_env(tmp_path / "codex-home", env_key="CODEX_HOME")
    fake = fake_codex(tmp_path / "fake-observe", OBSERVE + COMPLETE)
    run_codex(fake, TOPIC, tmp_path / "job-observe", **codex_kwargs())
    observed = json.loads(next((tmp_path / "job-observe").rglob("observed.json")).read_text())
    carried = GUARD_CARRIED if os.environ.get("KIN_PROTECTED_ROOTS") else set()
    assert set(observed["env"]) - RUNTIME_ADDED - carried <= ALLOWED_ENV
    assert not set(observed["env"]) & set(HOST_SECRETS)
    assert observed["env"]["KIN_TEST_DS_KEY"] == "provider-key"

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

# A stand-in for `codex exec`: no model and no real CLI (item 10). It reads the host's own kin_web
# server from the -c overrides it was given and drives that reader the way the model would:
# search, read the page it found, cite the read with its evidence id. The frames it prints are
# the CLI's JSON events, so the host's parsing, ledger and citation checks all run for real.
DRIVER = """#!{python}
import json, os, re, sys
argv = sys.argv[1:]
if argv[:1] == ['--version']:
    sys.stdout.write('codex-cli 0.156.1\\n'); raise SystemExit(0)
overrides = [argv[i + 1] for i, a in enumerate(argv) if a == '-c']
value = lambda key: next(o.split('=', 1)[1] for o in overrides if o.startswith(key + '='))
config_file = json.loads(value('mcp_servers.kin_web.args'))[-1]
sys.path.insert(0, re.search(r'PYTHONPATH="([^"]+)"', value('mcp_servers.kin_web.env')).group(1))
from kin_mind.web_read import WebReader
open(os.path.join(os.getcwd(), 'observed.json'), 'w').write(json.dumps({{'argv': argv, 'env': dict(os.environ)}}))
last = argv[argv.index('--output-last-message') + 1]
sys.stdin.read()
reader = WebReader(json.loads(open(config_file).read()))
def emit(frame):
    sys.stdout.write(json.dumps(frame) + '\\n'); sys.stdout.flush()
def call(n, tool, output):
    emit({{'type': 'item.completed', 'item': {{'id': 'item_%d' % n, 'type': 'mcp_tool_call', 'server': 'kin_web',
          'tool': tool, 'status': 'completed', 'result': {{'content': [{{'type': 'text', 'text': json.dumps(output)}}]}}}}}})
emit({{'type': 'thread.started', 'thread_id': 'th_driver'}})
found = reader.search('probe', max_results=3)
call(1, 'web_search', {{'state': found['state'], 'evidence_id': found['evidence_id'], 'results': found['results']}})
page = reader.read_page(found['results'][0]['url'])
call(2, 'read_page', {{'state': page['state'], 'evidence_id': page['evidence_id'], 'locator': page['locator']}})
payload = {{'summary': 'The answer is teal.', 'findings': ['The page says teal.'],
           'sources': [{{'url': page['locator'], 'title': 'Probe Page'}}], 'open_questions': [],
           'suggested_share': None, 'evidence_map': {{'1': [page['evidence_id']]}}}}
open(last, 'w').write(json.dumps(payload))
emit({{'type': 'turn.completed', 'usage': {{'input_tokens': 50, 'output_tokens': 10}}}})
"""


def test_tool_round_trip_search_read_cite_with_a_stand_in_cli(tmp_path, monkeypatch):
    """W1.6 round trip without the real CLI: a stub serves the web targets, the stand-in drives
    the host's own web reader, and the run uses Kin's Codex home (item 9, item 10)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")

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

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    driver = tmp_path / "codex-driver"
    driver.write_text(DRIVER.format(python=sys.executable))
    driver.chmod(0o700)
    kin_home = tmp_path / "kin-codex-home"
    kin_home.mkdir()
    job = tmp_path / "job"
    try:
        report = run_codex(driver, {"question": "What does the probe page say?"}, job,
                           web={"enabled": True, "search_endpoint": base + "/search", "allow_hosts": ["127.0.0.1"]},
                           budget_seconds=60, codex_home=kin_home, executor_source="runtime-bundle", **codex_kwargs())
    finally:
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
    # No phone path: the tool surface has no send/message tool.
    assert not any("send" in str(t) or "message" in str(t.get("tool", "")) for t in report["tool_results"])
    seen = json.loads((job / "observed.json").read_text())
    assert seen["env"]["CODEX_HOME"] == str(kin_home) and "--ignore-user-config" in seen["argv"]
    assert not (job / "codex-home").exists()



def _stub_site():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self):
            port = str(self.server.server_address[1])
            body = (('<a class="result__a" href="/l/?uddg=http%3A%2F%2F127.0.0.1%3A' + port + '%2Fpage">P</a>')
                    if self.path.startswith("/search") else
                    "<html><head><title>Probe Page</title></head><body>The answer is teal.</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_failed_citation_keeps_what_the_run_read_and_its_draft(tmp_path, monkeypatch):
    """K2-08: one unread citation fails the run, but the page it did read and its draft stay in
    the checkpoint the next attempt starts from."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    server, base = _stub_site()
    driver = tmp_path / "codex-driver"
    body = DRIVER.format(python=sys.executable).replace(
        "'sources': [{'url': page['locator'], 'title': 'Probe Page'}]",
        "'sources': [{'url': page['locator'], 'title': 'Probe Page'}, {'url': 'https://unread.example/x', 'title': 'Never read'}]")
    assert "unread.example" in body
    driver.write_text(body)
    driver.chmod(0o700)
    try:
        report = run_codex(driver, {"question": "What does the probe page say?"}, tmp_path / "job",
                           web={"enabled": True, "search_endpoint": base + "/search", "allow_hosts": ["127.0.0.1"]},
                           budget_seconds=60, **codex_kwargs())
    finally:
        server.shutdown()
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation" and report["result"] is None
    checkpoint = report["checkpoint"]
    assert checkpoint["partial_findings"]["summary"] == "The answer is teal."
    read = [entry for entry in checkpoint["sources_used"] if entry["state"] == "observed"]
    assert read and read[0]["locator"].endswith("/page")
    assert any("unread.example" in gap for gap in checkpoint["gaps"])


def test_a_citation_prefers_what_this_run_read_over_a_carried_receipt():
    """K4-17: the version read now wins over the checkpoint's older receipt for the same page."""
    from kin_mind.source_ledger import legitimize
    old = {"state": "historical", "locator": "https://site.example/a", "version": "v1", "evidence_id": "old"}
    new = {"state": "observed", "locator": "https://site.example/a", "version": "v2", "evidence_id": "new"}
    assert legitimize([new, old], "https://site.example/a")["version"] == "v2"
    assert legitimize([old], "https://site.example/a")["version"] == "v1"

def test_exploration_runs_the_runtime_bundle_codex_not_the_floating_cli(tmp_path):
    """K2-15, item 9: an explicit native_codex_command wins; else the verified runtime bundle's
    binary; the legacy command only where no bundle is installed; a changed manifest waits."""
    import hashlib
    from kin_mind.codex_executor import native_codex, native_codex_home
    host = tmp_path / "host"
    runtime = host / "state" / "mobile-runtime"
    bundle = runtime / "versions" / "codex-0.156.1-bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "codex").write_text("#!/bin/sh\n")
    manifest = json.dumps({"runtime": {"codex": {"path": "bin/codex"}}}).encode()
    (bundle / "manifest.json").write_bytes(manifest)
    legacy = {"host_root": str(host), "exploration_command": "/Users/someone/.local/bin/codex"}
    assert native_codex(legacy) == (legacy["exploration_command"], "legacy-command")
    (runtime / "activation.json").write_text(json.dumps({"current": {
        "bundle_id": "codex-0.156.1-bundle", "manifest_sha256": hashlib.sha256(manifest).hexdigest()}}))
    assert native_codex(legacy) == (str((bundle / "bin" / "codex").resolve()), "runtime-bundle")
    assert native_codex({**legacy, "native_codex_command": "/fixed/codex"}) == ("/fixed/codex", "configured")
    (bundle / "manifest.json").write_bytes(manifest + b" ")
    with pytest.raises(CodexUnavailable):
        native_codex(legacy)
    assert native_codex_home(legacy) is None
    (host / "state" / "codex-home").mkdir()
    assert native_codex_home(legacy) == host / "state" / "codex-home"


def test_a_running_exploration_stops_for_kin_not_for_a_new_message(tmp_path):
    """N7: a new owner message does not end the run; Kin setting the wish down does."""
    from kin_mind.exploration import Explorations
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import DesireChange

    _, mind, source = exploration_world(tmp_path)
    desire = next(iter(mind.read()["desires"]))
    revision = mind.read()["revision"]
    mind.manage_desire(DesireChange(command_id="start-wish", agent_version="test-v1", expected_revision=revision,
                                    evidence_ids=[source], action="start", desire_id=desire["id"], reason="Starting"))
    stop = Explorations(mind)._stop_when(lambda: False, desire["id"], every=0)
    MemoryContinuity(mind).ingest({"id": "owner-interjects", "kind": "owner-message", "text": "Hi", "at": mind.clock()})
    assert stop() is False
    revision = mind.read()["revision"]
    mind.manage_desire(DesireChange(command_id="set-down", agent_version="test-v1", expected_revision=revision,
                                    evidence_ids=[source], action="abandon", desire_id=desire["id"], reason="Kin chose to stop"))
    assert stop() is True
    assert Explorations(mind)._stop_when(lambda: True, desire["id"])() is True


def test_a_run_whose_worker_died_frees_the_slot_at_the_next_look(tmp_path):
    """K2-09: a running exploration whose worker the host killed does not block exploration until
    the next restart; the next status check takes it back and the wish is wanted again."""
    import time as clock_time
    from kin_mind.exploration import Explorations
    from kin_mind.exploration_cadence import ExplorationCadence
    from kin_mind.state import DesireChange
    engine, mind, source = exploration_world(tmp_path)
    desire = next(iter(mind.read()["desires"]))
    mind.manage_desire(DesireChange(command_id="start", agent_version="test-v1", expected_revision=mind.read()["revision"],
                                    evidence_ids=[source], action="start", desire_id=desire["id"], reason="Started"))
    explorer = Explorations(mind)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", ("explore_dead", mind.scope.key(), "running", mind.clock(),
            json.dumps({"desire_id": desire["id"], "liveness": {"pid": 999999, "started": None, "deadline": clock_time.time() - 1}})))
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", ("explore_old", mind.scope.key(), "interrupted", mind.clock(), "{}"))
    ExplorationCadence(mind).status()
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_explorations WHERE id='explore_dead'").fetchone()[0] == "interrupted"
    assert mind.read()["desires"][0]["status"] == "wanted"
    assert explorer.reclaim_dead() == []


def test_the_resident_worker_answers_in_order_and_keeps_long_work_out(tmp_path):
    """Item 6 (§5.7): one frame per line, answered in order; a long action is refused here, an
    unreadable frame is answered, and nothing but frames reaches the answer stream."""
    import io
    from kin_mind.host import serve
    _, mind, _ = exploration_world(tmp_path)
    config = host_config(tmp_path, mind)
    frames = [{"id": "1", "action": "read", "args": {}, "timeoutMs": 5000},
              {"id": "2", "action": "explore", "args": {}, "timeoutMs": 5000},
              {"id": "3", "action": "candidate", "args": {}, "timeoutMs": 5000}]
    stdin = io.StringIO("".join(json.dumps(f) + "\n" for f in frames) + "not json\n")
    out = io.StringIO()
    serve(config, stdin, out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == ["1", "2", "3", None]
    assert answers[0]["ok"] and answers[0]["result"]["state"]["revision"] >= 1
    assert answers[1] == {"id": "2", "ok": False, "error": {"error": "ValueError", "kind": "semantic", "code": "not-a-resident-action"}}
    assert answers[2]["ok"] and "eligible" in answers[2]["result"]
    assert answers[3]["error"]["code"] == "invalid-frame"


def test_the_minute_review_asks_the_resident_worker_before_a_process_is_started(tmp_path):
    """T-14: `review-due` does the minute's bookkeeping in the resident worker and says whether an
    appraisal is there to run; nothing runs while history compaction owns the store (WS6)."""
    from kin_mind.appraisal import Appraisals
    from kin_mind.history import COMPACTING, COMPACTION_MARKER
    from kin_mind.host import RESIDENT_ACTIONS, dispatch
    _, mind, _ = exploration_world(tmp_path)
    config = host_config(tmp_path, mind)
    assert "review-due" in RESIDENT_ACTIONS and "review" not in RESIDENT_ACTIONS
    assert dispatch(config, "review-due", {}) == {"state": "idle", "action": False, "enrichment": False}
    Appraisals(mind).enqueue_maintenance("snapshot-1", "test-v1")
    assert dispatch(config, "review-due", {"tick": False}) == {"state": "due", "action": True, "enrichment": False}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO meta VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=1", (COMPACTION_MARKER,))
    for action in ("review-due", "review"):
        assert dispatch(config, action, {}) == {"state": "paused", "reason": COMPACTING}


def test_an_exploration_reader_never_reads_the_host_the_memory_store_or_kins_home(tmp_path, monkeypatch):
    """Deployment condition (mind review 2026-09-24): the owner may authorize a root broad enough
    to hold the host and the memory store; every run's reader refuses them, Kin's Codex home and a
    symlink into them all the same, and still reads her own files."""
    from kin_mind.codex_executor import prepare_codex_exploration
    from kin_mind.computer import ComputerReader
    documents = tmp_path / "Documents"
    host, memory = documents / "host", documents / "MemoryPalace"
    home = host / "state" / "codex-home"
    for directory in (home, memory, documents / "notes"):
        directory.mkdir(parents=True, exist_ok=True)
    (documents / "notes" / "trip.md").write_text("synthetic notes")
    (documents / "notes" / "shortcut").symlink_to(memory, target_is_directory=True)
    fake = fake_codex(tmp_path / "fake-reader", COMPLETE)
    config = {"root": str(memory), "host_root": str(host), "exploration_command": str(fake),
              "exploration_model_provider": PROVIDER, "computer_exploration": {"enabled": True, "roots": [str(documents)]}}
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    prepared = prepare_codex_exploration(config)
    assert prepared["state"] == "ready"
    assert prepared["computer"]["protected_roots"] == [str(host), str(memory)]
    run_codex(fake, TOPIC, tmp_path / "job-reader", computer=prepared["computer"], codex_home=home, **codex_kwargs())
    reader = ComputerReader(json.loads((tmp_path / "job-reader" / "computer-reader.json").read_text()))
    assert reader.checked_path(documents / "notes" / "trip.md") == (documents / "notes" / "trip.md").resolve()
    for path in (host / "mind-config.json", memory / "memory.sqlite3", memory / "persona-policy.json",
                 documents / "notes" / "shortcut" / "memory.sqlite3", home / "auth.json"):
        with pytest.raises(ValueError, match="host-execution-material-excluded"):
            reader.checked_path(path)
