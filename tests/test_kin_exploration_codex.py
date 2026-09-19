"""Codex CLI executor: fake codex executables, never a real model call."""

import json
import os
import sys
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
    "sources": [{"url": "memory://s1", "title": "Supplied evidence"}],
    "open_questions": ["What remains open?"],
    "suggested_share": None,
}

# The citation validator rejects sources the run never supplied: the evidence
# text carries the URL the findings cite.
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
    # C7-10: no generic shell or image viewer — the read-only sandbox would
    # otherwise let them read anywhere, bypassing the computer file filter.
    assert "features.shell_tool=false" in argv and "features.view_image=false" in argv
    assert "features.multi_agent=false" in argv and "mcp_servers={}" in argv
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
    report = run_codex(fake, TOPIC, job, **codex_kwargs())
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
    assert report["input_sources"] == [{"id": "s1", "source_id": "s1", "revision": 3}]
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
    assert "memory://<source_id>" in observed["prompt"]
    assert "Citation contract" in observed["prompt"]


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
        "import signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + THREAD_STARTED
        # Ready-handshake: the marker proves the child started before we cancel.
        + "pathlib.Path('child-ready').write_text('ready')\n"
        + "time.sleep(0.2)\n"
        # The cancel already happened; this late completion must not count.
        + "open(last, 'w').write(json.dumps(" + repr(FINDINGS) + "))\n"
        + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\n"
        + "sys.stdout.flush()\n"
        + "time.sleep(30)\n",
    )
    ready = job / "child-ready"
    def canceled():
        if ready.exists():
            return True
        time.sleep(0.05)
        return False
    report = run_codex(fake, {"question": "synthetic"}, job, canceled=canceled, **codex_kwargs())
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
    first = run_codex(interrupted, {**TOPIC, "source_ids": ["s1", "s2"]},
                      tmp_path / "job-1",
                      canceled=lambda: time.monotonic() - began > 0.5, **codex_kwargs())
    assert first["state"] == "preempted" and first["checkpoint"]["attempt"] == 1
    assert first["checkpoint"]["input_sources"] == [{"id": "s1", "source_id": "s1", "revision": 3}]
    resumed = fake_codex(tmp_path / "fake-resumed", OBSERVE + COMPLETE)
    second = run_codex(resumed, {**TOPIC, "source_ids": ["s1", "s2"]},
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
    with pytest.raises(CodexUnavailable) as isolation:
        codex_env(tmp_path / "isolated", env={"CODEX_HOME": "/tmp/not-isolated"},
                  extra_env_keys=["CODEX_HOME"])
    assert isolation.value.reason == "codex-env-isolation-refused"


def test_codex_computer_reading_is_the_only_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    readable = tmp_path / "readable"
    readable.mkdir()
    (readable / "notes.txt").write_text("A synthetic note")
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    computer = {"enabled": True, "roots": [str(readable)], "exclude_roots": [],
                "previous": []}
    report = run_codex(fake, TOPIC, job, computer=computer, **codex_kwargs())
    assert report["state"] == "complete"
    assert report["capabilities"] == {"computer": True, "search": False, "fetch": False}
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
    report = run_codex(fake, TOPIC, tmp_path / "job2", **codex_kwargs())
    observed = json.loads((tmp_path / "job2" / "observed.json").read_text())
    assert "mcp_servers={}" in observed["argv"]
    assert report["capabilities"] == {"computer": False, "search": False, "fetch": False}
    assert "Citation contract" in observed["prompt"]


def test_codex_computer_use_is_a_real_separate_fixed_mcp_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("KIN_TEST_ACTION_REVIEW", "ephemeral-synthetic-review-token")
    monkeypatch.setenv("KIN_TEST_CUA_SURFACES", "browser,computer")
    readiness = {"state": "ready", "protocol": "mcp",
                 "tools": ["js", "turn_ended"], "bootstrap": "cua.getState",
                 "scope": "host-exploration"}
    monkeypatch.setattr(
        "kin_mind.computer_use.probe_backend_readiness",
        lambda *_args, **_kwargs: readiness,
    )
    job = tmp_path / "job"
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    computer = {
        "enabled": True,
        "file_reader_enabled": False,
        "ui": {
            "enabled": True, "browser": "chrome", "host_allowlist": ["example.com"],
            "allow_browser_click": True,
            "allowed_browser_effects": ["local_edit"],
            "browser_element_grants": [{"action": "click", "host": "example.com",
                                        "expected_text": "Save", "effect": "local_edit"}],
            "action_review": {
                "enabled": True, "base_url": "http://127.0.0.1:4567/v1",
                "env_key": "KIN_TEST_ACTION_REVIEW", "model": "deepseek-flash",
                "reasoning": "high", "allowed_categories": ["local_reversible"],
            },
            "backend": {"command": "/bin/false", "args": [],
                        "env_vars": ["KIN_TEST_CUA_SURFACES"]},
        },
    }
    report = run_codex(fake, TOPIC, job, computer=computer, **codex_kwargs())
    assert report["state"] == "complete"
    assert report["capabilities"] == {
        "computer": True, "search": False, "fetch": False,
        "browser": True, "computer_interaction": True,
    }
    assert report["computer_use_backend"] == readiness
    observed = json.loads((job / "observed.json").read_text())
    assert any(flag.startswith("mcp_servers.kin_ui.command=") for flag in observed["argv"])
    assert "mcp_optional_startup_grace_ms=10000" in observed["argv"]
    assert "mcp_servers.kin_ui.omit_tools_from=[]" in observed["argv"]
    assert "mcp_servers.kin_ui.tool_timeout_sec=90" in observed["argv"]
    assert "mcp_servers.kin_ui.required=true" in observed["argv"]
    assert any(flag == ('mcp_servers.kin_ui.env_vars=["KIN_TEST_CUA_SURFACES",'
                        '"KIN_TEST_ACTION_REVIEW"]')
               for flag in observed["argv"])
    assert observed["env"]["KIN_TEST_CUA_SURFACES"] == "browser,computer"
    assert not any(flag.startswith("mcp_servers.kin_computer.command=") for flag in observed["argv"])
    assert "Screenshots are not exposed" in observed["prompt"]
    ui_config = json.loads((job / "computer-use.json").read_text())
    assert ui_config["execution_id"] == "job" and ui_config["attempt"] == 1
    assert ui_config["allowed_apps"] == [] and ui_config["allow_browser_click"] is True
    assert ui_config["browser_element_grants"][0]["expected_text"] == "Save"
    assert ui_config["native_element_grants"] == []
    assert ui_config["backend"] == {
        "command": "/bin/false", "args": [], "env_vars": ["KIN_TEST_CUA_SURFACES"],
    }
    assert ui_config["action_review"]["env_key"] == "KIN_TEST_ACTION_REVIEW"
    assert "ephemeral-synthetic-review-token" not in (job / "computer-use.json").read_text()
    assert "browser,computer" not in (job / "computer-use.json").read_text()
    assert "browser,computer" not in (job / "input.json").read_text()
    assert "browser,computer" not in (job / "receipt.json").read_text()
    assert "browser,computer" not in observed["prompt"]
    # Completed sources are host-sealed only after Findings validation. A later
    # exploration can verify the exact origin instead of trusting a bare URL.
    source = report["result"]["sources"][0]
    assert source["receipt"]["receipt_format"] == "kin-source-receipt-v1"
    assert source["receipt"]["verified_by_execution"] == "job"


def test_codex_refuses_configured_but_unready_computer_use_before_dispatch(
        tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("KIN_TEST_CUA_SURFACES", "browser,computer")
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic-backend-startup-failure")

    monkeypatch.setattr("kin_mind.computer_use.probe_backend_readiness", unavailable)
    computer = {"enabled": True, "file_reader_enabled": False, "ui": {
        "enabled": True,
        "backend": {"command": "/bin/false", "args": [],
                    "env_vars": ["KIN_TEST_CUA_SURFACES"]},
    }}
    with pytest.raises(CodexUnavailable) as failed:
        run_codex(fake, TOPIC, tmp_path / "job-unready", computer=computer,
                  **codex_kwargs())
    assert failed.value.reason == "computer-use-backend-unavailable"
    assert not (tmp_path / "job-unready" / "observed.json").exists()


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
    execution = allowed / "execution"
    execution.mkdir()
    (execution / "computer-use.json").write_text("private host config")
    reader = ComputerReader({"roots": [str(allowed)], "exclude_roots": [str(excluded)],
                             "internal_deny_roots": [str(execution)],
                             "ledger": str(tmp_path / "ledger.json")})
    with pytest.raises(ValueError, match="resource-outside-authorized-roots"):
        reader.read_resource(outside / "secret.txt")
    with pytest.raises(ValueError, match="private-runtime-material-excluded"):
        reader.read_resource(excluded / "inner.txt")
    with pytest.raises(ValueError, match="host-execution-material-excluded"):
        reader.read_resource(execution / "computer-use.json")
    with pytest.raises(ValueError, match="host-execution-material-excluded"):
        reader.list_files(execution)
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


def test_inline_backend_env_never_reaches_run_artifacts_under_a_broad_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fake = fake_codex(tmp_path / "fake-codex", COMPLETE)
    job = tmp_path / "job"
    secret = "sk-inline-secret_123456789"
    computer = {
        "enabled": True, "roots": [str(tmp_path)], "exclude_roots": [],
        "snapshot_command": [sys.executable, "-c", "import json;print(json.dumps({}))"],
        "ui": {
            "enabled": True,
            "backend": {"command": "/bin/false", "env": {"FOO": secret}},
        },
    }
    with pytest.raises(ValueError, match="computer-use-backend-inline-env-refused"):
        run_codex(fake, TOPIC, job, computer=computer, **codex_kwargs())
    files = [path for path in job.rglob("*") if path.is_file()]
    assert files
    assert all(secret not in path.read_text(errors="ignore") for path in files)
    reader_config = json.loads((job / "computer-reader.json").read_text())
    assert "ui" not in reader_config
    assert reader_config["internal_deny_roots"] == [str(job)]


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
        return {"state": "complete", "partial": False, "executor": "kimi-cli", "provider": "kimi-cli",
                "result": {"summary": "Synthetic", "findings": [], "sources": [],
                           "open_questions": [], "suggested_share": None}}

    Explorations(mind).run("fake", tmp_path / "jobs", "test-v1", runner=kimi_style_runner)
    metadata = next(s for s in received if s.namespace == "kin-exploration").metadata
    assert metadata["executor"] == "kimi-cli" and metadata["provider"] == "kimi-cli"


def test_host_explore_backend_absent_uses_codex_and_requires_its_command(tmp_path):
    """W1: kimi is gone — the only executor is codex, configured or paused."""
    from kin_mind.host import dispatch

    _, mind, _ = exploration_world(tmp_path)
    with pytest.raises(ValueError, match="exploration_command"):
        dispatch(host_config(tmp_path, mind), "explore", {})
    waiting = dispatch(host_config(tmp_path, mind, exploration_backend="kimi"), "explore", {})
    assert waiting["state"] == "waiting" and waiting["reason"] == "exploration-backend-unknown"


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

    _, mind, source = exploration_world(tmp_path)
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + complete_citing(source))
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
    assert result["capabilities"] == {"computer": False, "search": True, "fetch": True}
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


# --- C7 acceptance matrix additions --------------------------------------------

SRC = Path(__file__).resolve().parents[1] / "src"


def test_codex_unbacked_citation_is_rejected(tmp_path, monkeypatch):
    """W1: a URL the run never read or was never given as evidence is invented."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    unread = {**FINDINGS, "sources": [{"url": "https://unread.example.com/page", "title": "Never read"}]}
    body = ("open(last, 'w').write(json.dumps(" + repr(unread) + "))\n"
            + THREAD_STARTED
            + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 2}}) + '\\n')\n"
            + "sys.stdout.flush()\n")
    fake = fake_codex(tmp_path / "fake-codex", body)
    report = run_codex(fake, {"question": "no source mentions that URL"}, job,
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"
    assert report["result"] is None and report["partial"] is True
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert any("https://unread.example.com/page" in gap for gap in checkpoint["gaps"])


def test_codex_citation_backed_by_the_observation_ledger(tmp_path, monkeypatch):
    """C7-6/7: a computer citation must map to a ledger observation; a path the
    reader never produced is rejected."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "notes.txt").write_text("Synthetic ledger note")
    computer = {"enabled": True, "roots": [str(fixture)], "exclude_roots": [], "previous": []}

    def write_fake(path, cite_locator):
        cite = "entry['locator']" if cite_locator else repr(str(fixture / "never-read.txt"))
        path.write_text(
            "#!" + sys.executable + "\n"
            "import json, sys\n"
            "if sys.argv[1:2] == ['--version']:\n"
            "    sys.stdout.write('codex-cli 0.155.0\\n')\n"
            "    raise SystemExit(0)\n"
            "argv = sys.argv[1:]\n"
            "last = argv[argv.index('--output-last-message') + 1]\n"
            "sys.stdin.read()\n"
            "sys.path.insert(0, " + repr(str(SRC)) + ")\n"
            "from kin_mind.computer import ComputerReader\n"
            "cfg = json.loads(open('computer-reader.json').read())\n"
            "entry = ComputerReader(cfg).read_resource(" + repr(str(fixture / 'notes.txt')) + ")\n"
            "payload = {'summary': 'Read the note', 'findings': ['It is green'], 'sources': "
            "[{'url': " + cite + ", 'title': 'notes.txt'}], 'open_questions': [], 'suggested_share': None}\n"
            "open(last, 'w').write(json.dumps(payload))\n"
            "sys.stdout.write(json.dumps({'type': 'thread.started', 'thread_id': 'th_ledger'}) + '\\n')\n"
            "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}) + '\\n')\n"
            "sys.stdout.flush()\n"
        )
        path.chmod(0o700)
        return path

    backed = run_codex(write_fake(tmp_path / "fake-backed", True),
                       {"question": "read the fixture"}, tmp_path / "job-backed",
                       computer=computer, **codex_kwargs())
    assert backed["state"] == "complete"
    assert backed["result"]["sources"][0]["url"].endswith("notes.txt")
    assert backed["observations"] and backed["observations"][0]["locator"].endswith("notes.txt")
    unbacked = run_codex(write_fake(tmp_path / "fake-unbacked", False),
                         {"question": "read the fixture"}, tmp_path / "job-unbacked",
                         computer=computer, **codex_kwargs())
    assert unbacked["state"] == "failed" and unbacked["reason"] == "unbacked-citation"


def test_codex_empty_output_is_failed_not_complete(tmp_path, monkeypatch):
    """C7-regression: turn.completed with no final message at all."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fake = fake_codex(tmp_path / "fake-codex", THREAD_STARTED
                      + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 0}}) + '\\n')\nsys.stdout.flush()\n")
    report = run_codex(fake, TOPIC, tmp_path / "job", **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "missing-final-result"
    assert report["result"] is None
    # The model did run and reported usage for the empty turn: reported, not zero.
    assert report["usage"]["status"] == "reported"


def test_codex_wrong_tool_call_is_recorded_not_fatal(tmp_path, monkeypatch):
    """C7-regression: a tool the run does not have fails in-band; the run may still complete."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fake = fake_codex(
        tmp_path / "fake-codex",
        THREAD_STARTED
        + "sys.stdout.write(json.dumps({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'mcp_tool_call', 'name': 'read_computer_resource', 'server': 'kin_computer'}}) + '\\n')\n"
        + "sys.stdout.write(json.dumps({'type': 'item.completed', 'item': {'id': 'i2', 'type': 'error', 'message': 'unsupported call: send_message'}}) + '\\n')\n"
        + "open(last, 'w').write(json.dumps(" + repr({**FINDINGS, 'open_questions': []}) + "))\n"
        + "sys.stdout.write(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 9, 'output_tokens': 4}}) + '\\n')\n"
        + "sys.stdout.flush()\n",
    )
    report = run_codex(fake, TOPIC, tmp_path / "job", **codex_kwargs())
    assert report["state"] == "complete"
    assert report["tool_results"][0]["type"] == "mcp_tool_call"
    assert report["tool_results"][0]["tool"] == "read_computer_resource"
    assert "unsupported call: send_message" in report["errors"]


def test_codex_lease_loss_preempts_with_a_versioned_checkpoint(tmp_path, monkeypatch):
    """C7-19: the lease/owner signal mid-run preempts; the checkpoint carries the
    input sources with their versions so a later attempt resumes honestly."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(tmp_path / "fake-codex", THREAD_STARTED + "import time\ntime.sleep(30)\n")
    began = time.monotonic()
    report = run_codex(fake, TOPIC, job,
                       canceled=lambda: time.monotonic() - began > 0.5, **codex_kwargs())
    assert report["state"] == "preempted" and report["result"] is None
    checkpoint = json.loads((job / "checkpoint.json").read_text())
    assert checkpoint["attempt"] == 1 and checkpoint["state"] == "preempted"
    assert checkpoint["input_sources"] == [{"id": "s1", "source_id": "s1", "revision": 3}]
    assert checkpoint["continuation"]


def test_codex_budget_is_capped_at_twenty_minutes(tmp_path, monkeypatch):
    """C7-17: the total budget ceiling is 1200 seconds, inclusive of everything."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    fake = fake_codex(tmp_path / "fake-codex", COMPLETE)
    with pytest.raises(ValueError):
        run_codex(fake, TOPIC, tmp_path / "job-over", budget_seconds=1201, **codex_kwargs())
    with pytest.raises(ValueError):
        run_codex(fake, TOPIC, tmp_path / "job-zero", budget_seconds=0, **codex_kwargs())


def test_executor_writes_only_inside_its_workdir(tmp_path, monkeypatch):
    """C7-12: the executor touches nothing outside the per-exploration workdir."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    report = run_codex(fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE),
                       TOPIC, tmp_path / "job", **codex_kwargs())
    assert report["state"] == "complete"
    created = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")} - before
    assert created and all(path == "job" or path.startswith("job/") or path == "fake-codex" for path in created)


def test_two_processes_claim_the_same_exploration_once(tmp_path):
    """C7-15: a cross-process race on the same scope settles exactly one winner."""
    world = tmp_path / "shared"
    exploration_world(world)
    child = tmp_path / "child.py"
    child.write_text(
        "import json, sys, time\n"
        "sys.path.insert(0, " + repr(str(SRC)) + ")\n"
        "from eventmem.core import Engine\n"
        "from eventmem.core.models import Scope\n"
        "from kin_mind.state import Mind\n"
        "from kin_mind.exploration import Explorations\n"
        "engine = Engine(sys.argv[1])\n"
        "mind = Mind(engine, Scope(persona='synthetic-codex-explorer'))\n"
        "def runner(*a, **k):\n"
        "    time.sleep(1.5)\n"
        "    return {'state': 'complete', 'partial': False, 'executor': 'codex-cli', 'provider': 'deepseek',\n"
        "            'result': {'summary': 'Done', 'findings': [], 'sources': [], 'open_questions': [], 'suggested_share': None}}\n"
        "result = Explorations(mind).run('fake', sys.argv[2], 'test-v1', runner=runner)\n"
        "print(json.dumps({'state': result['state'], 'id': result.get('id')}))\n"
    )
    import subprocess

    processes = [
        subprocess.Popen([sys.executable, str(child), str(world / "memory"), str(tmp_path / "jobs")],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outcomes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr[-800:]
        outcomes.append(json.loads(stdout.strip()))
    assert sorted(outcome["state"] for outcome in outcomes) == ["complete", "waiting"]
    ids = {outcome.get("id") for outcome in outcomes if outcome["state"] == "complete"}
    assert len(ids) == 1
    from eventmem.core import Engine

    with Engine(world / "memory").db.connect() as conn:
        rows = conn.execute("SELECT state FROM mind_explorations").fetchall()
    assert len(rows) == 1 and rows[0][0] == "complete"


def test_duplicate_result_submission_is_idempotent(tmp_path):
    """C7-22: the kin-exploration source is keyed; the same payload twice is one row."""
    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput

    engine = Engine(tmp_path / "memory")
    scope = Scope(persona="synthetic-codex-explorer")
    payload = SourceInput(namespace="kin-exploration", key="explore_x", scope=scope,
                          authority="model", kind="observation", session="explore_x",
                          text='{"state":"complete"}')
    first = engine.receive(payload)
    second = engine.receive(payload)
    assert first["id"] == second["id"]
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE namespace='kin-exploration'").fetchone()[0] == 1


def test_source_correction_after_submission_flags_review_without_a_model_call(tmp_path):
    """C7-23: evidence corrected after the run settles leaves the review event
    needs-review; no stale appraisal is ever dispatched."""
    from test_kin_mind import FakeReviewer

    from eventmem.core.models import SourceInput
    from kin_mind.actions import ActionEvents
    from kin_mind.appraisal import Appraisal, Appraisals
    from kin_mind.exploration import Explorations

    engine, mind, _ = exploration_world(tmp_path)

    def correcting_runner(*args, **kwargs):
        # A versioned correction of the desire's evidence lands after the claim.
        engine.receive(SourceInput(namespace="synthetic", key="question", version="2",
                                   scope=mind.scope, text="Corrected evidence text"))
        return {
            "state": "complete", "partial": False, "executor": "codex-cli", "provider": "deepseek",
            "result": {"summary": "Done", "findings": [], "sources": [],
                       "open_questions": [], "suggested_share": None},
        }

    result = Explorations(mind).run("fake", tmp_path / "jobs", "test-v1", runner=correcting_runner)
    assert result["state"] == "complete"  # valid when claimed; the review layer reacts

    actions, jobs = ActionEvents(mind), Appraisals(mind)
    actions.drain(jobs)
    with engine.db.connect() as conn:
        event = conn.execute("SELECT state FROM mind_action_events WHERE kind='exploration-result'").fetchone()
        assert event[0] == "needs-review"
        assert conn.execute("SELECT COUNT(*) FROM mind_appraisals").fetchone()[0] == 0
    reviewer = FakeReviewer(Appraisal(reason="Reviewed"))
    assert jobs.run_one(reviewer)["state"] == "idle"
    assert reviewer.calls == 0  # never paid to review on stale evidence


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


# --- C2 dispatch indirection: the bridge-published exploration gateway ---------

def test_exploration_gateway_state_file_lifecycle(tmp_path):
    """C2: the published baseUrl is honored only while its writer is alive."""
    from kin_mind.codex_executor import exploration_gateway_base_url

    state = tmp_path / "exploration-gateway.json"
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:45678/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid()}))
    assert exploration_gateway_base_url(state) == "http://127.0.0.1:45678/v1"

    missing = tmp_path / "never-written.json"
    with pytest.raises(CodexUnavailable) as gone:
        exploration_gateway_base_url(missing)
    assert gone.value.reason == "exploration-gateway-missing"

    # A dead writer's file is stale and not honored.
    import subprocess

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:45678/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": dead.pid}))
    with pytest.raises(CodexUnavailable) as stale:
        exploration_gateway_base_url(state)
    assert stale.value.reason == "exploration-gateway-stale"

    # Only the bridge's loopback gateway is ever a valid target.
    state.write_text(json.dumps({"baseUrl": "https://api.deepseek.com/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid()}))
    with pytest.raises(CodexUnavailable) as invalid:
        exploration_gateway_base_url(state)
    assert invalid.value.reason == "exploration-gateway-invalid"


def test_action_review_gateway_is_resolved_per_dispatch_and_fails_closed(tmp_path, monkeypatch):
    from kin_mind.codex_executor import (
        computer_action_review_gateway_base_url,
        exploration_capabilities,
        prepare_codex_exploration,
    )

    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("KIN_COMPUTER_ACTION_REVIEW_TOKEN", "ephemeral-review-token")
    fake = fake_codex(tmp_path / "fake-codex", COMPLETE)
    state = tmp_path / "computer-action-review-gateway.json"
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:42001/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid()}))
    base = {
        "exploration_command": str(fake), "exploration_model_provider": PROVIDER,
        "computer_action_review_gateway_state_file": str(state),
        "computer_exploration": {"enabled": True, "ui": {
            "enabled": True, "backend": {"command": "/bin/false"},
            "allow_browser_click": True,
            "allowed_browser_effects": ["local_edit"],
            "browser_element_grants": [
                {"action": "click", "host": "example.com", "expected_text": "Save",
                 "effect": "local_edit"},
            ],
            "action_review": {"enabled": True,
                              "allowed_categories": ["navigation", "local_reversible"]},
        }},
    }
    prepared = prepare_codex_exploration(base)
    review = prepared["computer"]["ui"]["action_review"]
    assert review["base_url"] == "http://127.0.0.1:42001/v1"
    assert review["env_key"] == "KIN_COMPUTER_ACTION_REVIEW_TOKEN" and review["available"] is True
    assert "base_url" not in base["computer_exploration"]["ui"]["action_review"]
    capabilities = exploration_capabilities(base, computer_override=prepared["computer"])["capabilities"]
    assert capabilities["computer_interaction"]["available"] is True
    assert capabilities["ui_permissions"]["local_reversible"] is True
    assert capabilities["write_experiment"]["available"] is False

    # Extra fields cannot smuggle a token or alternative endpoint through state.
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:42001/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid(),
                                 "token": "must-not-be-read"}))
    with pytest.raises(CodexUnavailable):
        computer_action_review_gateway_base_url(state)
    prepared = prepare_codex_exploration(base)
    review = prepared["computer"]["ui"]["action_review"]
    assert review["enabled"] is False and "base_url" not in review
    capabilities = exploration_capabilities(base, computer_override=prepared["computer"])["capabilities"]
    assert capabilities["browser"]["available"] is True
    assert capabilities["computer_interaction"]["available"] is False


def test_prepare_reads_the_gateway_state_file_only_without_static_base_url(tmp_path, monkeypatch):
    """C2: config wins over the state file; missing both pauses without claiming."""
    from kin_mind.codex_executor import prepare_codex_exploration

    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("KIN_EXPLORATION_GATEWAY_TOKEN", "synthetic-bridge-token")
    fake = fake_codex(tmp_path / "fake-codex", COMPLETE)
    state = tmp_path / "exploration-gateway.json"
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:41001/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid()}))
    base = {"exploration_backend": "codex", "exploration_command": str(fake)}

    # The state file supplies the address; the token env var name defaults.
    prepared = prepare_codex_exploration({**base, "exploration_gateway_state_file": str(state)})
    assert prepared["state"] == "ready"
    report = prepared["runner"](str(fake), TOPIC,
                                tmp_path / "job", budget_seconds=60, canceled=lambda: False,
                                model="deepseek-flash")
    assert report["state"] == "complete"
    assert report["provider"] == "deepseek"

    # An explicit static base_url wins over the state file.
    prepared = prepare_codex_exploration({**base, "exploration_gateway_state_file": str(state),
                                        "exploration_model_provider": PROVIDER})
    assert prepared["state"] == "ready"
    report = prepared["runner"](str(fake), TOPIC,
                                tmp_path / "job2", budget_seconds=60, canceled=lambda: False,
                                model="deepseek-flash")
    assert report["state"] == "complete"

    # Missing both: the exploration waits, nothing is claimed and nothing runs kimi.
    unprepared = prepare_codex_exploration(base)
    assert unprepared["state"] == "waiting"
    assert unprepared["reason"] == "exploration-gateway-unconfigured"

    # A stale state file pauses the same way.
    import subprocess

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:41001/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": dead.pid}))
    stale = prepare_codex_exploration({**base, "exploration_gateway_state_file": str(state)})
    assert stale["state"] == "waiting" and stale["detail"] == "exploration-gateway-stale"


def test_host_explore_codex_dispatch_reads_the_state_file(tmp_path, monkeypatch):
    """C2: end to end — dispatch with no static provider base_url reads the bridge file."""
    from kin_mind.exploration import Explorations
    from kin_mind.host import dispatch

    _, mind, source = exploration_world(tmp_path)
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + complete_citing(source))
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    monkeypatch.setenv("KIN_EXPLORATION_GATEWAY_TOKEN", "synthetic-bridge-token")
    state = tmp_path / "state" / "exploration-gateway.json"
    state.parent.mkdir(0o700, parents=True)
    state.write_text(json.dumps({"baseUrl": "http://127.0.0.1:41002/v1",
                                 "startedAt": "2026-09-19T00:00:00Z", "pid": os.getpid()}))
    config = host_config(tmp_path, mind, exploration_backend="codex",
                         exploration_command=str(fake),
                         exploration_gateway_state_file=str(state))
    result = dispatch(config, "explore", {})
    assert result["state"] == "complete"
    assert result["executor"] == "codex-cli" and result["provider"] == "deepseek"
    observed = json.loads((Path(config["exploration_directory"]) / result["id"] / "observed.json").read_text())
    argv = observed["argv"]
    assert 'model_providers.deepseek.base_url="http://127.0.0.1:41002/v1"' in argv
    assert 'model_providers.deepseek.env_key="KIN_EXPLORATION_GATEWAY_TOKEN"' in argv
    assert observed["env"]["KIN_EXPLORATION_GATEWAY_TOKEN"] == "synthetic-bridge-token"

    # Without the file the same dispatch waits and claims nothing.
    state.unlink()
    waiting = dispatch(config, "explore", {})
    assert waiting["state"] == "waiting"
    assert waiting["detail"] == "exploration-gateway-missing"
    assert len(Explorations(mind).recent()) == 1  # only the completed one exists


# --- W1 acceptance: source ledger, web tools, capabilities, accounting ---------

def test_web_tools_mcp_wired_and_capabilities_recorded(tmp_path, monkeypatch):
    """W1.1/W1.4: web enabled injects kin_web as a second host-owned MCP server."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    job = tmp_path / "job"
    fake = fake_codex(tmp_path / "fake-codex", OBSERVE + COMPLETE)
    report = run_codex(fake, TOPIC, job, web={"enabled": True}, **codex_kwargs())
    assert report["state"] == "complete"
    assert report["capabilities"] == {"computer": False, "search": True, "fetch": True}
    assert report["accounting"]["usage_level"] == "codex-turn-aggregate"
    assert report["accounting"]["cache_read_separate"] is True
    observed = json.loads((job / "observed.json").read_text())
    argv = observed["argv"]
    assert any(flag.startswith("mcp_servers.kin_web.command=") for flag in argv)
    assert not any(flag.startswith("mcp_servers.kin_computer.command=") for flag in argv)  # no computer server in this run
    assert "mcp_servers={}" not in argv
    assert 'mcp_servers.kin_web.default_tools_approval_mode="approve"' in argv
    assert "web_search" in observed["prompt"] and "read_page" in observed["prompt"]
    reader_config = json.loads((job / "web-reader.json").read_text())
    assert reader_config["search_endpoint"] and reader_config["execution_id"] == "job"


def test_citation_counterexamples_never_pass(tmp_path, monkeypatch):
    """W1.6: unread link, URL prefix, directory prefix, searched-not-read, failed
    read, superseded source — none may pass as verified content."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")

    def fake_citing(url):
        payload = {**FINDINGS, "sources": [{"url": url, "title": "Synthetic"}]}
        return fake_codex(tmp_path / ("fake-" + str(abs(hash(url)) % 10**8)),
                          "open(last, 'w').write(json.dumps(" + repr(payload) + "))\n"
                          + THREAD_STARTED
                          + "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')\n"
                          + "sys.stdout.flush()\n")

    # A read page's URL prefix is not the page.
    seeded = tmp_path / "job-seed"
    web_dir = seeded
    web_dir.mkdir(parents=True)
    (web_dir / "web-reader.json").write_text(json.dumps({"execution_id": "explore_x", "attempt": 1,
        "ledger": str(web_dir / "web-observations.json"), "allow_hosts": []}))
    from kin_mind.web_read import WebReader
    WebReader({"execution_id": "explore_x", "attempt": 1,
                                "ledger": str(web_dir / "web-observations.json")})._receipt(
        tool="read_page", status="observed", requested="https://example.com/page",
        text="Real content", title="Real Page")
    # URL prefix of the observed page: not citable.
    report = run_codex(fake_citing("https://example.com"), TOPIC, tmp_path / "job-prefix",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"
    # Directory prefix of an observed file path: not citable.
    report = run_codex(fake_citing("/tmp"), TOPIC, tmp_path / "job-dirprefix", **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"
    # Searched but never read: search_result is visibility, not content.
    WebReader({"execution_id": "explore_x", "attempt": 1,
                          "ledger": str(web_dir / "web-observations.json")})._receipt(
        tool="web_search", status="search_result", requested="https://search.invalid/",
        extra={"query": "q", "results": [{"title": "Hit", "url": "https://hit.example.com", "snippet": "s"}]})
    report = run_codex(fake_citing("https://hit.example.com"), TOPIC, tmp_path / "job-searched",
                       web={"enabled": True}, **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"
    # A failed read is never a source.
    WebReader({"execution_id": "explore_x", "attempt": 1,
               "ledger": str(web_dir / "web-observations.json")})._receipt(
        tool="read_page", status="failed", requested="https://failed.example.com", reason="fetch-failed")
    report = run_codex(fake_citing("https://failed.example.com"), TOPIC, tmp_path / "job-failed",
                       web={"enabled": True}, **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"
    # Fabricated citation: nothing anywhere.
    report = run_codex(fake_citing("https://fabricated.example.com"), TOPIC, tmp_path / "job-fabricated",
                       **codex_kwargs())
    assert report["state"] == "failed" and report["reason"] == "unbacked-citation"


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


def test_continuation_reverifies_and_drops_superseded(tmp_path, monkeypatch):
    """W1.3: a carried memory receipt whose evidence moved on is superseded."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    from kin_mind.source_ledger import seal_source_receipt

    carried_web = seal_source_receipt({
        "state": "observed", "locator": "https://read.example.com/page",
        "version": "b" * 64, "evidence_id": "web_" + "2" * 32, "title": "Read",
        "basis": "read_page", "recorded_at": "2026-09-19T00:00:00+00:00",
        "execution_id": "explore_prior", "attempt": 1, "tool": "read_page",
        "adapter": "kin-web-reader-v1",
    }, execution_id="explore_prior", attempt=1)
    continuation = {"exploration_id": "explore_prior", "attempt": 1, "gaps": [],
                    "sources_used": [
                        {"state": "historical", "locator": "memory://s1", "version": 3,
                         "evidence_id": "s1", "title": "", "basis": "supplied-evidence"},
                        carried_web]}
    current = {**TOPIC}  # s1 is at revision 3 here
    fake = fake_codex(tmp_path / "fake-cont", OBSERVE + COMPLETE)
    report = run_codex(fake, current, tmp_path / "job", continuation=continuation, **codex_kwargs())
    assert report["state"] == "complete" and report["attempt"] == 2
    assert report["continuation_dropped"] == []
    # The same carried receipt against a corrected evidence revision is superseded.
    moved = {**TOPIC, "known_evidence": [dict(TOPIC["known_evidence"][0], revision=4)]}
    report = run_codex(fake_codex(tmp_path / "fake-moved", OBSERVE + COMPLETE),
                       moved, tmp_path / "job-moved", continuation=continuation, **codex_kwargs())
    assert report["continuation_dropped"] == [{"locator": "memory://s1", "reason": "superseded-or-absent"}]
    assert report["state"] == "complete"  # the fresh run's own citations are fine


def test_unverified_partial_stays_draft_in_checkpoint(tmp_path, monkeypatch):
    """W1.3: an interrupted partial's unread citations are drafts, not sources."""
    monkeypatch.setenv("KIN_TEST_DS_KEY", "sk-synthetic")
    draft = {**FINDINGS, "sources": [{"url": "memory://s1", "title": "verified"},
                                     {"url": "https://unread.example.com/x", "title": "draft"}]}
    fake = fake_codex(tmp_path / "fake-draft",
                      "open(last, 'w').write(json.dumps(" + repr(draft) + "))\n"
                      + THREAD_STARTED + "import time\ntime.sleep(30)\n")
    began = time.monotonic()
    report = run_codex(fake, TOPIC, tmp_path / "job",
                       canceled=lambda: time.monotonic() - began > 0.5, **codex_kwargs())
    assert report["state"] == "preempted"
    checkpoint = json.loads((tmp_path / "job" / "checkpoint.json").read_text())
    assert [s["cited_as"] for s in checkpoint["sources_used"]] == ["memory://s1"]
    assert checkpoint["sources_used"][0]["state"] == "historical"
    assert checkpoint["unverified_claims"] == ["https://unread.example.com/x"]


def test_organize_old_material_completes_but_blocked_verification_waits(tmp_path):
    """W1.4/W1.6: DS decides; the host settles accordingly. Organizing existing
    material completes; a needed-but-unavailable verification waits."""
    from kin_mind.exploration import Explorations

    _, mind, _source = exploration_world(tmp_path)
    explorer = Explorations(mind)

    def organized(*args, **kwargs):
        return {"state": "complete", "partial": False, "executor": "codex-cli", "provider": "deepseek",
                "result": {"summary": "Organized", "findings": [], "sources": [],
                           "open_questions": [], "suggested_share": None}}

    first = explorer.run("fake", tmp_path / "jobs", "test-v1", runner=organized)
    assert first["state"] == "complete" and mind.read()["desires"][0]["status"] == "completed"

    _, mind2, _ = exploration_world(tmp_path / "second")
    explorer2 = Explorations(mind2)

    def blocked(*args, **kwargs):
        return {"state": "complete", "partial": False, "executor": "codex-cli", "provider": "deepseek",
                "result": {"summary": "Needs a page nobody can fetch now", "findings": [],
                           "sources": [], "open_questions": ["the page"], "suggested_share": None,
                           "assistance_needed": {"action": "Re-run when search is back",
                                                 "reason": "web fetch unavailable",
                                                 "completion": "the page is read"}}}

    second = explorer2.run("fake", tmp_path / "jobs2", "test-v1", runner=blocked)
    assert second["state"] == "complete"
    assert mind2.read()["desires"][0]["status"] == "waiting"  # not consumed as done


def test_capability_context_is_versioned_and_shared(tmp_path):
    """W1.4: one versioned structure; availability is facts, not keywords/scores."""
    from kin_mind.codex_executor import exploration_capabilities

    config = {"agent_version": "v1", "exploration_command": "/bin/echo",
              "computer_exploration": {"enabled": True}}
    caps = exploration_capabilities(config)
    assert caps["capabilities"]["search"]["available"] is True
    assert caps["capabilities"]["fetch"]["available"] is True
    assert caps["capabilities"]["computer"]["available"] is True
    assert caps["capabilities"]["write_experiment"]["available"] is False
    assert caps["decisions"] is False and caps["computer"] is True
    assert caps["executor"] == "codex-cli" and caps["version"] == "v1"
    same = exploration_capabilities(config)["capabilities_version"]
    assert same == caps["capabilities_version"]
    off = exploration_capabilities({**config, "exploration_web": {"enabled": False}})
    assert off["capabilities"]["search"]["available"] is False
    assert off["capabilities"]["search"]["reason"] == "exploration-web-disabled"
    assert off["capabilities_version"] != caps["capabilities_version"]
    no_command = exploration_capabilities({"agent_version": "v1"})
    assert no_command["capabilities"]["search"]["available"] is False
    assert no_command["capabilities"]["search"]["reason"] == "exploration-command-unconfigured"


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
