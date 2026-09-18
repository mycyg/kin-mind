"""Codex CLI executor: fake codex executables, never a real model call."""

import json
import os
import time
from pathlib import Path

import pytest

from kin_mind.codex_executor import (
    codex_argv,
    codex_env,
    codex_final_result,
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
    "sources": [{"url": "https://example.com", "title": "Synthetic source"}],
    "open_questions": ["What remains open?"],
    "suggested_share": None,
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
            text="Explore a source-backed question",
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


# Variables the interpreter/runtime itself adds after exec (the macOS CLT python3
# shim, CoreFoundation): not passed by the executor, so excluded from the allowlist
# assertion. The security property is that no host secret crosses.
RUNTIME_ADDED = {"CPATH", "LIBRARY_PATH", "MANPATH", "SDKROOT", "__CF_USER_TEXT_ENCODING"}

ALLOWED_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
               "CODEX_HOME", "KIN_TEST_DS_KEY"}


def test_codex_argv_and_env_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    schema_capable = {**PROVIDER, "supports_output_schema": True}
    argv = codex_argv(
        "codex", tmp_path / "job", model="deepseek-flash", reasoning="high",
        schema_file=tmp_path / "job" / "findings-schema.json",
        last_file=tmp_path / "job" / "result-1.json", provider=schema_capable,
    )
    assert argv[1] == "exec" and argv[-1] == "-"  # the prompt travels on stdin
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--json", "--output-schema"):
        assert flag in argv
    # DeepSeek's official codex doc prescribes API-key login; this CLI version
    # rejects preferred_auth_method, so the api login method alone is pinned.
    assert 'forced_login_method="api"' in argv
    assert not any(flag.startswith("preferred_auth_method") for flag in argv)
    # An operator-managed model catalog is injected only when configured.
    assert not any(flag.startswith("model_catalog_json") for flag in argv)
    with_catalog = codex_argv(
        "codex", tmp_path / "job", model="deepseek-flash", reasoning="high",
        schema_file=tmp_path / "job" / "findings-schema.json",
        last_file=tmp_path / "job" / "result-1.json", provider=schema_capable,
        model_catalog=tmp_path / "models.json",
    )
    assert f'model_catalog_json="{tmp_path}/models.json"' in with_catalog
    # DeepSeek's strict json_schema takes scalars only: the default provider gets
    # no --output-schema and the schema rides inside the prompt instead.
    argv = codex_argv(
        "codex", tmp_path / "job", model="deepseek-flash", reasoning="high",
        schema_file=tmp_path / "job" / "findings-schema.json",
        last_file=tmp_path / "job" / "result-1.json", provider=PROVIDER,
    )
    assert "--output-schema" not in argv and "--output-last-message" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "job")
    assert argv[argv.index("--model") + 1] == "deepseek-flash"
    assert 'model_reasoning_effort="high"' in argv
    assert 'model_provider="deepseek"' in argv
    assert 'model_providers.deepseek.base_url="https://gateway.synthetic.invalid/v1"' in argv
    assert 'model_providers.deepseek.wire_api="responses"' in argv
    assert 'model_providers.deepseek.env_key="KIN_TEST_DS_KEY"' in argv
    assert not any(".codex" in flag for flag in argv)
    env = codex_env(tmp_path / "job" / "codex-home", env_key="KIN_TEST_DS_KEY")
    assert env["CODEX_HOME"] == str(tmp_path / "job" / "codex-home")
    assert env["KIN_TEST_DS_KEY"] == "sk-synthetic"
    assert set(env) <= ALLOWED_ENV
    with pytest.raises(CodexUnavailable) as missing:
        codex_env(tmp_path, env_key="KIN_TEST_ABSENT_KEY")
    assert missing.value.reason == "codex-credential-env-missing"


def test_codex_final_result_bounded_repair():
    assert codex_final_result(json.dumps(FINDINGS)) is not None
    fenced = "Research is complete.\n\n```json\n" + json.dumps(FINDINGS) + "\n```"
    assert codex_final_result(fenced).open_questions == ["What remains open?"]
    assert codex_final_result("no json at all") is None
    assert codex_final_result(json.dumps({"summary": 42, "findings": "no"})) is None
    ambiguous = json.dumps(FINDINGS) + "\n" + json.dumps(FINDINGS)
    assert codex_final_result(ambiguous) is None


def test_codex_complete_run(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-must-not-leak")
    job = tmp_path / "job"
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    report = run_codex(fake, {"question": "synthetic", "source_ids": ["s1"]}, job,
                       **codex_kwargs())
    assert report["state"] == "complete" and report["reason"] is None
    assert report["result"]["summary"] == "A sourced finding"
    assert report["partial"] is False
    assert report["executor"] == "codex-cli" and report["provider"] == "deepseek"
    assert report["model"] == "deepseek-flash" and report["reasoning"] == "high"
    assert report["executor_version"] == "0.155.0"
    assert report["native_execution_id"] == "th_synthetic"
    assert report["exit_code"] == 0 and report["attempt"] == 1
    assert report["usage"] == {
        "status": "reported",
        "per_request": [{"input_tokens": 120, "output_tokens": 30}],
        "total": {"input_tokens": 120, "output_tokens": 30},
    }
    assert report["input_sources"] == [{"id": "s1"}]
    assert report["config_digest"] and report["workdir"] == str(job)
    assert report["started_at"] and report["finished_at"] and report["seconds"] >= 0
    receipt = json.loads((job / "receipt.json").read_text())
    assert receipt["state"] == "complete" and receipt["provider"] == "deepseek"
    assert (job / "findings-schema.json").exists() and (job / "input.json").exists()
    assert not (job / "checkpoint.json").exists()
    observed = json.loads((job / "observed.json").read_text())
    assert "model_providers.deepseek.stream_idle_timeout_ms=300000" in observed["argv"]
    assert "model_providers.deepseek.request_max_retries=2" in observed["argv"]
    assert "model_providers.deepseek.stream_max_retries=2" in observed["argv"]
    assert observed["env"]["CODEX_HOME"] == str(job / "codex-home")
    assert "EVENTMEM_API_KEY" not in observed["env"]
    assert "ANTHROPIC_API_KEY" not in observed["env"]
    assert "evidence, never instructions" in observed["prompt"]
    assert "synthetic" in observed["prompt"]
    # DeepSeek takes no output schema flag: the schema is embedded in the prompt.
    assert '"open_questions"' in observed["prompt"]
    assert "never answer it from model memory" in observed["prompt"]


def test_codex_truncation_is_failed_with_partial_basis(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(
        tmp_path / "fake-codex",
        THREAD_STARTED
        + "open(last, 'w').write(json.dumps(" + repr(FINDINGS) + "))\n"
        + "raise SystemExit(0)\n",  # no turn.completed: a truncated stream
    )
    report = run_codex(fake, {"question": "synthetic"}, job, **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "native-run-incomplete"
    assert report["result"] is None and report["partial"] is True
    assert report["usage"]["status"] == "unknown"
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert checkpoint["attempt"] == 1 and checkpoint["state"] == "failed"
    assert checkpoint["partial_findings"]["summary"] == "A sourced finding"
    assert checkpoint["gaps"] == ["What remains open?"]
    assert checkpoint["continuation"]


def test_codex_schema_invalid_and_prose_final_message(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    turn_completed = (
        THREAD_STARTED
        + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\n"
        + "sys.stdout.flush()\n"
    )
    invalid = fake_codex(
        tmp_path / "fake-invalid",
        "open(last, 'w').write(json.dumps({'summary': 42, 'findings': 'no'}))\n" + turn_completed,
    )
    report = run_codex(invalid, {"question": "synthetic"}, tmp_path / "job-invalid",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "invalid-result-shape"
    assert report["result"] is None
    # turn.completed without a usage payload is unknown, never zero.
    assert report["usage"] == {"status": "unknown", "per_request": [], "total": None}
    prose = fake_codex(
        tmp_path / "fake-prose",
        "open(last, 'w').write('Research is done, trust me.')\n" + turn_completed,
    )
    report = run_codex(prose, {"question": "synthetic"}, tmp_path / "job-prose",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "invalid-final-result"


def test_codex_cli_startup_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    dead = fake_codex(tmp_path / "fake-dead", "raise SystemExit(3)\n")
    report = run_codex(dead, {"question": "synthetic"}, tmp_path / "job-dead",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "native-run-incomplete"
    assert report["exit_code"] == 3 and report["result"] is None
    # No thread.started frame: nothing ever reached the model.
    assert report["usage"] == {"status": "not_dispatched", "per_request": [], "total": None}
    refused = fake_codex(
        tmp_path / "fake-refused",
        THREAD_STARTED
        + "sys.stdout.write(json.dumps({'type': 'turn.failed', 'error': "
          "{'message': 'Missing environment variable'}}) + '\\n')\n"
        + "sys.stdout.flush()\n"
        + "raise SystemExit(1)\n",
    )
    report = run_codex(refused, {"question": "synthetic"}, tmp_path / "job-refused",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["exit_code"] == 1
    assert report["native_execution_id"] == "th_synthetic"
    assert report["usage"]["status"] == "unknown"
    assert "Missing environment variable" in report["errors"]


def test_codex_budget_timeout_writes_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(
        tmp_path / "fake-slow",
        THREAD_STARTED + "import time\ntime.sleep(30)\n",
    )
    started = time.monotonic()
    report = run_codex(fake, {"question": "synthetic"}, job, budget_seconds=1,
                       **codex_kwargs())
    assert report["state"] == "timed-out" and time.monotonic() - started < 8
    assert report["result"] is None and report["exit_code"] != 0
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert checkpoint["state"] == "timed-out" and checkpoint["attempt"] == 1
    assert checkpoint["partial_findings"] is None and checkpoint["gaps"] == []
    assert checkpoint["input_sources"] == []


def test_codex_preemption_kills_the_whole_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(
        tmp_path / "fake-group",
        "import subprocess, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(os.path.join(os.getcwd(), 'child.pid'), 'w').write(str(child.pid))\n"
        + THREAD_STARTED
        + "time.sleep(30)\n",
    )
    began = time.monotonic()
    report = run_codex(fake, {"question": "synthetic"}, job,
                       canceled=lambda: time.monotonic() - began > 0.5, **codex_kwargs())
    assert report["state"] == "preempted" and report["result"] is None
    assert json.loads((job / "checkpoint.json").read_text())["state"] == "preempted"
    child_pid = int((job / "child.pid").read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the child's process survived the group termination")


def test_codex_late_result_is_never_a_result(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(
        tmp_path / "fake-late",
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + THREAD_STARTED
        + "time.sleep(1.5)\n"
        # The cancel already happened; this late completion must not count.
        + "open(last, 'w').write(json.dumps(" + repr(FINDINGS) + "))\n"
        + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\n"
        + "sys.stdout.flush()\n"
        + "time.sleep(30)\n",
    )
    began = time.monotonic()
    report = run_codex(fake, {"question": "synthetic"}, job,
                       canceled=lambda: time.monotonic() - began > 0.3, **codex_kwargs())
    assert report["state"] == "preempted"
    assert report["result"] is None and report["partial"] is True
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert checkpoint["partial_findings"]["summary"] == "A sourced finding"


def test_codex_continuation_reads_the_prior_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    interrupted = fake_codex(
        tmp_path / "fake-interrupted",
        THREAD_STARTED
        + "open(last, 'w').write(json.dumps(" + repr(FINDINGS) + "))\n"
        + "import time\ntime.sleep(30)\n",
    )
    began = time.monotonic()
    first = run_codex(interrupted, {"question": "synthetic", "source_ids": ["s1", "s2"]},
                      tmp_path / "job-1",
                      canceled=lambda: time.monotonic() - began > 0.5, **codex_kwargs())
    assert first["state"] == "preempted" and first["checkpoint"]["attempt"] == 1
    assert first["checkpoint"]["input_sources"] == [{"id": "s1"}, {"id": "s2"}]
    resumed = fake_codex(tmp_path / "fake-resumed", OBSERVE + COMPLETE)
    second = run_codex(resumed, {"question": "synthetic", "source_ids": ["s1", "s2"]},
                       tmp_path / "job-2", continuation=first["checkpoint"], **codex_kwargs())
    assert second["state"] == "complete" and second["attempt"] == 2
    observed = json.loads((tmp_path / "job-2" / "observed.json").read_text())
    argv = observed["argv"]
    assert argv[argv.index("--output-last-message") + 1].endswith("result-2.json")
    prompt = observed["prompt"]
    assert "What remains open?" in prompt and "https://example.com" in prompt
    assert "do not repeat completed work" in prompt
    assert json.loads((tmp_path / "job-2" / "continuation.json").read_text())["attempt"] == 1


def test_codex_preflight_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    with pytest.raises(CodexUnavailable) as missing:
        run_codex(tmp_path / "no-such-codex", {}, tmp_path / "job", **codex_kwargs())
    assert missing.value.reason == "codex-cli-missing"
    old = fake_codex(tmp_path / "fake-old", "raise SystemExit(0)\n", version="0.100.0")
    with pytest.raises(CodexUnavailable) as outdated:
        run_codex(old, {}, tmp_path / "job", **codex_kwargs())
    assert outdated.value.reason == "codex-cli-too-old"
    with pytest.raises(CodexUnavailable) as no_model:
        run_codex(old, {}, tmp_path / "job", **{k: v for k, v in codex_kwargs().items() if k != "model"})
    assert no_model.value.reason == "codex-model-missing"
    with pytest.raises(CodexUnavailable) as no_provider:
        run_codex(old, {}, tmp_path / "job", model="deepseek-flash", reasoning="high")
    assert no_provider.value.reason == "codex-provider-missing"
    monkeypatch.delenv("KIN_TEST_DS_KEY", raising=False)
    ok = fake_codex(tmp_path / "fake-ok", COMPLETE)
    with pytest.raises(CodexUnavailable) as credential:
        run_codex(ok, {}, tmp_path / "job", **codex_kwargs())
    assert credential.value.reason == "codex-credential-env-missing"


def test_codex_computer_reading_is_the_only_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    readable = tmp_path / "readable"
    readable.mkdir()
    (readable / "notes.txt").write_text("A synthetic note")
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    computer = {"enabled": True, "roots": [str(readable)], "exclude_roots": [],
                "previous": []}
    report = run_codex(fake, {"question": "synthetic"}, job, computer=computer,
                       **codex_kwargs())
    assert report["state"] == "complete"
    assert report["capabilities"] == {"computer": True, "web_search": False}
    observed = json.loads((job / "observed.json").read_text())
    argv = observed["argv"]
    assert "mcp_servers={}" not in argv
    assert any(flag.startswith("mcp_servers.kin_computer.command=") for flag in argv)
    args_flag = next(flag for flag in argv if flag.startswith("mcp_servers.kin_computer.args="))
    assert "kin_mind.computer" in args_flag and "computer-reader.json" in args_flag
    env_flag = next(flag for flag in argv if flag.startswith("mcp_servers.kin_computer.env="))
    assert "PYTHONPATH" in env_flag
    # The reader config lives in the private workdir and names only the authorized root.
    reader_config = json.loads((job / "computer-reader.json").read_text())
    assert reader_config["roots"] == [str(readable)]
    assert reader_config["ledger"] == str(job / "computer-observations.json")
    assert "read_computer_context" in observed["prompt"]
    assert str(readable) in observed["prompt"]  # authorized_roots are data
    # Without computer exploration there is no MCP server at all.
    report = run_codex(fake, {"question": "synthetic"}, tmp_path / "job2", **codex_kwargs())
    observed = json.loads((tmp_path / "job2" / "observed.json").read_text())
    assert "mcp_servers={}" in observed["argv"]
    assert report["capabilities"] == {"computer": False, "web_search": False}
    assert "open_questions as an unknown gap" in observed["prompt"]


def test_computer_reader_refusals_hold(tmp_path):
    from kin_mind.computer import ComputerReader

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "note.txt").write_text("Synthetic")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Not authorized")
    excluded = allowed / "private"
    excluded.mkdir()
    (excluded / "inner.txt").write_text("Excluded")
    reader = ComputerReader({"roots": [str(allowed)], "exclude_roots": [str(excluded)],
                             "ledger": str(tmp_path / "ledger.json")})
    with pytest.raises(ValueError, match="resource-outside-authorized-roots"):
        reader.read_resource(outside / "secret.txt")
    with pytest.raises(ValueError, match="private-runtime-material-excluded"):
        reader.read_resource(excluded / "inner.txt")
    # A symlink inside the root pointing outside resolves before the check.
    link = allowed / "linked.txt"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(ValueError, match="resource-outside-authorized-roots"):
        reader.read_resource(link)
    # Runtime material inside an authorized root stays excluded by name.
    (allowed / "auth.json").write_text("{}")
    with pytest.raises(ValueError, match="credential-or-runtime-material-excluded"):
        reader.read_resource(allowed / "auth.json")
    assert reader.read_resource(allowed / "note.txt")["locator"].endswith("note.txt")


def test_explorations_run_records_executor_unavailability_without_fallback(tmp_path):
    from kin_mind.exploration import Explorations

    _, mind, _ = exploration_world(tmp_path)

    def runner(*args, **kwargs):
        raise CodexUnavailable("codex-cli-too-old", "0.100.0")

    explorer = Explorations(mind)
    result = explorer.run("fake", tmp_path / "jobs", "test-v1", runner=runner)
    assert result["state"] == "failed"
    assert result["waiting_reason"] == "codex-cli-too-old"
    assert result["executor"] == "codex-cli" and result["provider"] is None
    row = explorer.recent()[0]
    assert row["error"] == "exploration-executor-unavailable"
    # The exploration is paused with a recorded reason, not completed or retried as kimi.
    assert mind.read()["desires"][0]["status"] == "waiting"


def test_explorations_run_continues_from_the_prior_checkpoint(tmp_path):
    from kin_mind.exploration import Explorations

    _, mind, _ = exploration_world(tmp_path)
    seen = []

    def runner(*args, **kwargs):
        seen.append(kwargs.get("continuation"))
        if len(seen) == 1:
            return {
                "state": "preempted", "partial": True, "result": None,
                "executor": "codex-cli", "provider": "deepseek",
                "checkpoint": {"exploration_id": "explore_prior", "attempt": 1,
                               "gaps": ["What remains open?"], "sources_used": [],
                               "input_sources": [{"id": "s1"}]},
            }
        return {
            "state": "complete", "partial": False,
            "executor": "codex-cli", "provider": "deepseek",
            "result": {"summary": "Done", "findings": [], "sources": [],
                       "open_questions": [], "suggested_share": None},
        }

    runner.wants_continuation = True
    explorer = Explorations(mind)
    first = explorer.run("fake", tmp_path / "jobs", "test-v1", runner=runner)
    assert first["state"] == "preempted"
    # The settled result queues an action appraisal; the review loop clears it
    # before the next exploration may start.
    from test_kin_mind import FakeReviewer

    from kin_mind.actions import ActionEvents
    from kin_mind.appraisal import Appraisal, Appraisals

    actions, jobs = ActionEvents(mind), Appraisals(mind)
    actions.drain(jobs)
    jobs.run_one(FakeReviewer(Appraisal(reason="Reviewed")))
    actions.drain(jobs)
    second = explorer.run("fake", tmp_path / "jobs", "test-v1", runner=runner)
    assert second["state"] == "complete"
    assert seen[0] is None
    assert seen[1]["attempt"] == 1 and seen[1]["gaps"] == ["What remains open?"]


def test_exploration_result_metadata_distinguishes_executor_and_provider(tmp_path, monkeypatch):
    from kin_mind.exploration import Explorations

    engine, mind, _ = exploration_world(tmp_path)
    received = []
    original = engine.receive

    def capture(source_input, **kwargs):
        received.append(source_input)
        return original(source_input, **kwargs)

    monkeypatch.setattr(engine, "receive", capture)

    def codex_style_runner(*args, **kwargs):
        return {
            "state": "complete", "partial": False, "executor": "codex-cli",
            "provider": "deepseek", "model": "deepseek-flash", "reasoning": "high",
            "result": {"summary": "Synthetic", "findings": [], "sources": [],
                       "open_questions": [], "suggested_share": None},
        }

    result = Explorations(mind).run("fake", tmp_path / "jobs", "test-v1",
                                    runner=codex_style_runner)
    assert result["state"] == "complete"
    metadata = next(s for s in received if s.namespace == "kin-exploration").metadata
    assert metadata["executor"] == "codex-cli"
    assert metadata["provider"] == "deepseek"
    assert metadata["model"] == "deepseek-flash" and metadata["reasoning"] == "high"


def test_legacy_metadata_still_says_kimi(tmp_path, monkeypatch):
    from kin_mind.exploration import Explorations

    engine, mind, _ = exploration_world(tmp_path)
    received = []
    original = engine.receive

    def capture(source_input, **kwargs):
        received.append(source_input)
        return original(source_input, **kwargs)

    monkeypatch.setattr(engine, "receive", capture)

    def kimi_style_runner(*args, **kwargs):
        return {"state": "complete", "partial": False, "provider": "kimi-cli",
                "result": {"summary": "Synthetic", "findings": [], "sources": [],
                           "open_questions": [], "suggested_share": None}}

    Explorations(mind).run("fake", tmp_path / "jobs", "test-v1", runner=kimi_style_runner)
    metadata = next(s for s in received if s.namespace == "kin-exploration").metadata
    assert metadata["executor"] == "kimi-cli" and metadata["provider"] == "kimi-cli"


def test_host_explore_backend_absent_keeps_the_kimi_requirement(tmp_path):
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    with pytest.raises(KeyError):
        dispatch(host_config(tmp_path, mind), "explore", {})


def test_host_explore_codex_missing_command_is_a_clear_error(tmp_path):
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    with pytest.raises(ValueError, match="exploration_command"):
        dispatch(host_config(tmp_path, mind, exploration_backend="codex"), "explore", {})


def test_host_explore_codex_missing_cli_waits_without_claiming(tmp_path):
    from kin_mind.exploration import Explorations
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    result = dispatch(
        host_config(
            tmp_path, mind, exploration_backend="codex",
            exploration_command=str(tmp_path / "no-such-codex"),
            exploration_model_provider={k: v for k, v in PROVIDER.items() if k != "env_key"},
        ),
        "explore", {},
    )
    assert result["state"] == "waiting"
    assert result["reason"] == "exploration-executor-unavailable"
    assert result["detail"] == "codex-cli-missing"
    assert Explorations(mind).recent() == []  # nothing was claimed or mutated


def test_host_explore_codex_missing_credential_env_waits(tmp_path, monkeypatch):
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    fake = fake_codex(tmp_path / "fake-codex", COMPLETE)
    monkeypatch.delenv("KIN_TEST_DS_KEY", raising=False)
    result = dispatch(
        host_config(tmp_path, mind, exploration_backend="codex",
                    exploration_command=str(fake), exploration_model_provider=PROVIDER),
        "explore", {},
    )
    assert result["state"] == "waiting"
    assert result["reason"] == "exploration-credential-env-missing"


def test_host_explore_codex_end_to_end(tmp_path, monkeypatch):
    from kin_mind.exploration import Explorations
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-must-not-leak")
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"models": []}))
    config = host_config(
        tmp_path, mind, exploration_backend="codex", exploration_command=str(fake),
        exploration_model="deepseek-flash", exploration_reasoning="high",
        exploration_budget_seconds=1200, exploration_max_running=1,
        exploration_model_provider=PROVIDER, exploration_model_catalog=str(catalog),
    )
    result = dispatch(config, "explore", {})
    assert result["state"] == "complete"
    assert result["executor"] == "codex-cli" and result["provider"] == "deepseek"
    assert result["model"] == "deepseek-flash" and result["reasoning"] == "high"
    assert result["usage"]["status"] == "reported"
    row = Explorations(mind).recent()[0]
    assert row["executor"] == "codex-cli" and row["provider"] == "deepseek"
    job = Path(config["exploration_directory"]) / result["id"]
    observed = json.loads((job / "observed.json").read_text())
    env, argv = observed["env"], observed["argv"]
    assert env["CODEX_HOME"] == str(job / "codex-home")
    assert env["KIN_TEST_DS_KEY"] == "sk-synthetic"
    assert "EVENTMEM_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert set(env) - RUNTIME_ADDED <= ALLOWED_ENV
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == str(job)
    assert 'model_provider="deepseek"' in argv
    assert 'forced_login_method="api"' in argv
    assert f'model_catalog_json="{catalog}"' in argv
