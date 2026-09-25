"""An exploration's CLI runs in a session of its own, out of the worker's process group; the worker
reports that group to the host that owns it and ends it with itself (CR3-MM-02). Real processes,
no model: a fake codex that starts a process of its own and keeps running."""

import json
import os
import select
import signal
import subprocess
import sys
import time

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

