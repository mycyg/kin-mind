"""An exploration's CLI runs in a session of its own, out of the worker's process group; the worker
reports that group to the host that owns it and ends it with itself (CR3-MM-02). Real processes,
no model: a fake codex that starts a process of its own and keeps running."""

import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from kin_mind import worker_groups
from kin_mind.codex_executor import run_codex
from test_kin_exploration_codex import COMPLETE, PROVIDER, TOPIC, codex_kwargs, fake_codex

# The fake CLI starts a process of its own, in its group (and off its output, so the executor's
# stream still ends with the CLI), and notes both pids.
SPAWNS = (
    "import subprocess, time\n"
    "helper = subprocess.Popen(['/bin/sleep', '120'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "open(os.path.join(os.getcwd(), 'pids.json'), 'w').write(json.dumps({'cli': os.getpid(), 'helper': helper.pid}))\n"
)
HANGS = "time.sleep(120)\n"

# A one-shot worker as the host starts one: its own group, the control channel on a pipe.
WORKER = (
    "import json, sys\n"
    "from kin_mind import worker_groups\n"
    "from kin_mind.codex_executor import run_codex\n"
    "executable, directory, topic, kwargs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])\n"
    "run_codex(executable, topic, directory, budget_seconds=120, **kwargs)\n"
)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def frames(fd, *, wait=0.0):
    """Every JSON line on the control pipe so far; waits up to `wait` seconds for the first."""
    data = b""
    deadline = time.monotonic() + wait
    while True:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()) if not data else 0)
        if not ready:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def cleanup(worker, tmp_path):
    """Whatever a failed assertion left running."""
    worker.kill()
    worker.wait()
    record = tmp_path / "run" / "pids.json"
    for pid in json.loads(record.read_text()).values() if record.exists() else []:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_worker(tmp_path, body):
    fake = fake_codex(tmp_path / "fake", body)
    read, write = os.pipe()
    env = {**os.environ, worker_groups.CONTROL_FD_ENV: str(write), PROVIDER["env_key"]: "synthetic-key"}
    worker = subprocess.Popen([sys.executable, "-c", WORKER, str(fake), str(tmp_path / "run"), json.dumps(TOPIC),
                               json.dumps(codex_kwargs())], env=env, pass_fds=(write,), start_new_session=True)
    os.close(write)
    return worker, read


def test_a_worker_ended_by_sigterm_ends_its_execution_group_before_it_dies(tmp_path):
    worker, control = start_worker(tmp_path, SPAWNS + HANGS)
    try:
        # The group is reported the moment it exists, before any work of its own.
        reported = frames(control, wait=20)
        assert reported and set(reported[0]) == {"group"}, reported
        group = reported[0]["group"]
        assert until(lambda: (tmp_path / "run" / "pids.json").exists())
        pids = json.loads((tmp_path / "run" / "pids.json").read_text())
        assert pids["cli"] == group and worker_groups.alive(group)
        # As the host ends a worker: a TERM to the worker's own group, which the CLI is not in.
        os.killpg(worker.pid, signal.SIGTERM)
        assert worker.wait(timeout=10) == -signal.SIGTERM, "the worker still dies of the TERM"
        # The worker passed its TERM on: the CLI and the process it started are gone with it.
        assert until(lambda: not worker_groups.alive(group), 5), "the execution outlived its worker"
        assert not pid_alive(pids["helper"])
    finally:
        os.close(control)
        cleanup(worker, tmp_path)


def test_a_finished_execution_leaves_nothing_in_its_group_and_says_so(tmp_path, monkeypatch):
    read, write = os.pipe()
    monkeypatch.setattr(worker_groups, "_CONTROL", write)
    monkeypatch.setenv(PROVIDER["env_key"], "synthetic-key")
    fake = fake_codex(tmp_path / "fake", SPAWNS + COMPLETE)
    record = tmp_path / "run" / "pids.json"
    try:
        run_codex(str(fake), TOPIC, tmp_path / "run", budget_seconds=60, **codex_kwargs())
        pids = json.loads(record.read_text())
        # The CLI exited on its own and left its helper running in its group: the group is ended
        # anyway, and the host hears it is empty.
        assert not worker_groups.alive(pids["cli"])
        assert not pid_alive(pids["helper"])
        assert frames(read, wait=1) == [{"group": pids["cli"]}, {"group": pids["cli"], "ended": True}]
    finally:
        os.close(read)
        os.close(write)
        for pid in json.loads(record.read_text()).values() if record.exists() else []:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_term_between_a_group_start_and_its_report_waits_for_the_report(tmp_path):
    driver = (
        "import os, signal, subprocess, time\n"
        "from kin_mind import worker_groups\n"
        "with worker_groups.starting() as started:\n"
        "    child = subprocess.Popen(['/bin/sleep', '120'], start_new_session=True)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    time.sleep(0.3)\n"
        "    started(child.pid)\n"
        "    time.sleep(0.3)\n"
        "print('never')\n"
    )
    read, write = os.pipe()
    worker = subprocess.Popen([sys.executable, "-c", driver], env={**os.environ, worker_groups.CONTROL_FD_ENV: str(write)},
                              pass_fds=(write,), stdout=subprocess.PIPE)
    os.close(write)
    try:
        out, _ = worker.communicate(timeout=20)
        assert worker.returncode == -signal.SIGTERM and b"never" not in out
        reported = frames(read, wait=1)
        assert len(reported) == 1 and set(reported[0]) == {"group"}
        assert until(lambda: not worker_groups.alive(reported[0]["group"]), 5), "the group started before the TERM was ended by it"
    finally:
        os.close(read)


def test_no_process_the_worker_starts_inherits_the_control_channel(tmp_path):
    read, write = os.pipe()
    probe = (
        "import os, subprocess, sys\n"
        "from kin_mind import worker_groups\n"
        "assert worker_groups._CONTROL is not None\n"
        "print(subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"KIN_WORKER_CONTROL_FD\"))'],"
        " capture_output=True, text=True).stdout.strip())\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], env={**os.environ, worker_groups.CONTROL_FD_ENV: str(write)},
                         pass_fds=(write,), capture_output=True, text=True, timeout=20)
    os.close(read)
    os.close(write)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"
    # A number that names no pipe of the host's is not taken for the channel.
    plain = tmp_path / "plain"
    with open(plain, "w") as handle:
        out = subprocess.run([sys.executable, "-c", "from kin_mind import worker_groups; print(worker_groups._CONTROL)"],
                             env={**os.environ, worker_groups.CONTROL_FD_ENV: str(handle.fileno())},
                             pass_fds=(handle.fileno(),), capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "None", out.stderr



def test_every_process_of_an_execution_carries_the_hosts_mark_and_the_worker_passes_on_no_other(tmp_path):
    """CR4-MM-03: the host's mark goes into the CLI's environment and into each MCP server's, and
    nowhere else: the worker takes it out of its own, so what it starts for itself carries none."""
    mark = "5d0c7a4e-3f9b-4c1a-8a8e-2b6f1c9d0e7f"
    probe = (
        "import json, os, subprocess, sys\n"
        "from kin_mind import worker_groups\n"
        "from kin_mind.codex_executor import codex_argv\n"
        "own = subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"KIN_WORKER_MARK\"))'],"
        " capture_output=True, text=True).stdout.strip()\n"
        "server = {'command': sys.executable, 'args': ['-m', 'kin_mind.web_read'], 'env': {'PYTHONPATH': '/src'}}\n"
        "argv = codex_argv('codex', '/tmp/x', model='m', reasoning='high', schema_file='/tmp/s', last_file='/tmp/l',"
        " provider={'id': 'p', 'name': 'P', 'base_url': 'https://gateway.invalid/v1', 'wire_api': 'responses'},"
        " web_mcp=server, execution_env=worker_groups.execution_env())\n"
        "print(json.dumps({'own': own, 'execution': worker_groups.execution_env(),"
        " 'server_env': next(argv[i + 1] for i, a in enumerate(argv) if a == '-c' and argv[i + 1].startswith('mcp_servers.kin_web.env='))}))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], env={**os.environ, worker_groups.MARK_ENV: mark},
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen["own"] == "None", "a process the worker starts for itself carries no mark"
    assert seen["execution"] == {"KIN_WORKER_MARK": mark}
    assert seen["server_env"] == 'mcp_servers.kin_web.env={PYTHONPATH="/src", KIN_WORKER_MARK="' + mark + '"}'


def test_the_cli_runs_with_the_mark_and_so_does_what_it_starts(tmp_path, monkeypatch):
    """The CLI's environment is an allow-list; the mark is on it, and a process the CLI starts, in a
    session of its own as well, inherits it."""
    monkeypatch.setattr(worker_groups, "_MARK", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
    monkeypatch.setenv(PROVIDER["env_key"], "synthetic-key")
    grandchild = (
        "import subprocess\n"
        "subprocess.run([sys.executable, '-c', 'import os, sys; open(os.path.join(os.getcwd(), \"grandchild.json\"), \"w\").write("
        "__import__(\"json\").dumps(dict(os.environ)))'], start_new_session=True)\n"
    )
    fake = fake_codex(tmp_path / "fake", "open(os.path.join(os.getcwd(), 'cli.json'), 'w').write(json.dumps(dict(os.environ)))\n"
                      + grandchild + COMPLETE)
    run_codex(str(fake), TOPIC, tmp_path / "run", budget_seconds=60, **codex_kwargs())
    for name in ("cli.json", "grandchild.json"):
        env = json.loads(next((tmp_path / "run").rglob(name)).read_text())
        assert env["KIN_WORKER_MARK"] == "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", name


# ---------------------------------------------------------------------------------------------
# CR5-MM-03: whatever the worker starts for an execution is handed the mark by name, from before the
# first process of the execution exists; a shared service is handed none. Real processes, no model.

MARK = "7c1e9d2a-5b3f-4e8a-9d6c-1a2b3c4d5e6f"


def test_the_computer_use_service_and_its_readiness_probe_are_handed_the_mark(tmp_path):
    """The MCP client starts the service in a session of its own, from an environment filtered by
    name: the mark is set in it by name, and nothing else rides along with it."""
    from kin_mind.computer_use import _backend_environment, probe_backend_readiness

    marked = {worker_groups.MARK_ENV: MARK}
    assert _backend_environment({"env_vars": []}, marked)[worker_groups.MARK_ENV] == MARK
    assert "TOKEN" not in _backend_environment({"env_vars": []}, {**marked, "TOKEN": "x"})
    assert worker_groups.MARK_ENV not in _backend_environment({"env_vars": []}, {worker_groups.MARK_ENV: "not-a-mark"})
    # The readiness probe's service notes its environment and leaves without answering.
    service = tmp_path / "service.py"
    service.write_text("import json, os, sys\nopen(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))\n")
    seen = tmp_path / "service.json"
    with pytest.raises(Exception):
        probe_backend_readiness({"command": sys.executable, "args": [str(service), str(seen)], "env_vars": []},
                                execution_id="x", attempt=1, timeout_seconds=10, execution_env=marked)
    assert json.loads(seen.read_text())[worker_groups.MARK_ENV] == MARK


def test_the_snapshot_tool_and_the_pdf_converter_are_handed_the_mark(tmp_path, monkeypatch):
    from kin_mind.computer import ComputerReader

    tool = tmp_path / "snapshot.py"
    tool.write_text("import json, os\nprint(json.dumps({'mark': os.environ.get('KIN_WORKER_MARK')}))\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    converter = tools / "pdftotext"
    converter.write_text("#!" + sys.executable + "\nimport os\nprint('mark ' + os.environ.get('KIN_WORKER_MARK', 'none'))\n")
    converter.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    document = tmp_path / "doc.pdf"
    document.write_bytes(b"%PDF-1.4 synthetic")
    reader = ComputerReader({"snapshot_command": [sys.executable, str(tool)], "roots": [str(tmp_path)],
                             "ledger": str(tmp_path / "ledger.json"), "execution_env": {worker_groups.MARK_ENV: MARK}})
    assert reader.context()["context"]["mark"] == MARK
    assert "mark " + MARK in reader.read_resource(str(document))["text"]
    # In a worker, with no configuration of its own (the computer-context action): the worker's mark.
    monkeypatch.setattr(worker_groups, "_MARK", MARK)
    plain = ComputerReader({"snapshot_command": [sys.executable, str(tool)], "ledger": str(tmp_path / "plain.json")})
    assert plain.context()["context"]["mark"] == MARK


def test_the_version_probe_is_handed_the_mark(tmp_path):
    from kin_mind.codex_executor import codex_cli_version

    fake = tmp_path / "codex"
    fake.write_text("#!" + sys.executable + "\nimport os, sys\n"
                    "open(sys.argv[0] + '.mark', 'w').write(os.environ.get('KIN_WORKER_MARK', ''))\n"
                    "print('codex-cli 0.155.0')\n")
    fake.chmod(0o700)
    assert codex_cli_version(fake, execution_env={worker_groups.MARK_ENV: MARK}) == "0.155.0"
    assert (tmp_path / "codex.mark").read_text() == MARK


def test_one_mark_is_taken_before_the_first_probe_and_reaches_every_process_of_the_run(tmp_path, monkeypatch):
    """The readiness probe runs before the CLI; it, the file reader's tools, the Computer Use
    server's service, the CLI and whatever the CLI would run all get the same mark."""
    import kin_mind.computer_use as computer_use

    monkeypatch.setattr(worker_groups, "_MARK", MARK)
    monkeypatch.setenv(PROVIDER["env_key"], "synthetic-key")
    probed = {}

    def probe(config, **kwargs):
        probed.update(kwargs)
        return {"state": "ready", "protocol": "mcp"}

    monkeypatch.setattr(computer_use, "probe_backend_readiness", probe)
    observe = "open(os.path.join(os.getcwd(), 'observed.json'), 'w').write(json.dumps({'argv': argv, 'env': dict(os.environ)}))\n"
    fake = fake_codex(tmp_path / "fake", observe + COMPLETE)
    computer = {"enabled": True, "roots": [str(tmp_path)],
                "ui": {"enabled": True, "backend": {"command": sys.executable, "args": []}}}
    run_codex(str(fake), TOPIC, tmp_path / "run", budget_seconds=60, computer=computer, **codex_kwargs())
    marked = {worker_groups.MARK_ENV: MARK}
    assert probed["execution_env"] == marked
    run = tmp_path / "run"
    assert json.loads((run / "computer-use.json").read_text())["execution_env"] == marked
    assert json.loads((run / "computer-reader.json").read_text())["execution_env"] == marked
    observed = json.loads((run / "observed.json").read_text())
    assert observed["env"][worker_groups.MARK_ENV] == MARK
    assert 'shell_environment_policy.set={KIN_WORKER_MARK="' + MARK + '"}' in observed["argv"]


def test_a_shared_service_woken_for_an_execution_is_handed_no_mark(tmp_path, monkeypatch):
    """The local embedding service outlives whoever woke it: an execution's end must not end it."""
    from eventmem.core import local_embedding

    started = {}

    class Exited:
        def poll(self):
            return 1

    def popen(argv, **kwargs):
        started["env"] = kwargs.get("env")
        return Exited()

    monkeypatch.setattr(local_embedding, "subprocess", SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setenv(worker_groups.MARK_ENV, MARK)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(RuntimeError, match="exited during startup"):
        local_embedding.ensure_started(tmp_path, f"http://127.0.0.1:{port}/v1", local_embedding.MODEL)
    assert started["env"] is not None and worker_groups.MARK_ENV not in started["env"]
    assert started["env"].get("PATH") == os.environ.get("PATH"), "everything else is inherited as before"


def test_the_version_probe_of_the_explore_action_carries_the_mark(tmp_path, monkeypatch):
    """The explore action asks the CLI's version once, in `prepare_codex_exploration`, and hands it
    to `run_codex`, which does not ask again: that probe is the run's first process, and it carries
    the run's mark by name (CL6-MM-09)."""
    from kin_mind.codex_executor import prepare_codex_exploration

    monkeypatch.setattr(worker_groups, "_MARK", MARK)
    monkeypatch.setenv(PROVIDER["env_key"], "synthetic-key")
    fake = tmp_path / "codex"
    fake.write_text("#!" + sys.executable + "\nimport os, sys\n"
                    "open(sys.argv[0] + '.mark', 'w').write(os.environ.get('KIN_WORKER_MARK', ''))\n"
                    "print('codex-cli 0.155.0')\n")
    fake.chmod(0o700)
    prepared = prepare_codex_exploration({
        "native_codex_command": str(fake),
        "exploration_model_provider": {"base_url": PROVIDER["base_url"], "env_key": PROVIDER["env_key"]},
    })
    assert prepared["state"] == "ready", prepared
    assert prepared["cli_version"] == "0.155.0"
    assert (tmp_path / "codex.mark").read_text() == MARK
