"""The processes a mind worker runs its executions in, and who ends them (CR3-MM-02, CR4-MM-03).

An executor's CLI runs in a session of its own (`start_new_session`), so that the worker can end
the whole execution -- the CLI and everything it started -- without ending itself. That also
takes the execution out of the worker's own process group, which is the group the host signals
when it ends the worker; and a process the execution starts may leave the CLI's group and session
in turn, and outlive the CLI, adopted by launchd. A set of process groups cannot say that the
execution has ended.

So every process of an execution carries one mark, and the host owns the end of all of them:

* The host gives each worker a mark of its own (`KIN_WORKER_MARK`, a UUID) and knows it before
  the worker runs anything. The worker takes it out of its environment at once, so the processes
  it starts for itself (a shared service, say) do not carry it, and passes it explicitly into the
  environment of everything it starts for an execution (`execution_env`, `environment`): the
  executor's version probe, the snapshot tool and the converters, the Computer Use service and its
  readiness probe, the CLI, every MCP server the CLI starts and the service each of those starts in
  turn -- each hands it on by name, never by relying on an environment that is filtered on the way
  (CR5-MM-03). A process inherits it from there, into a new group or session as well. What the
  worker starts in its own group without a mark is still the execution's: the host ends what is
  left there once the worker has ended.
* The host reads this user's process table for the mark (mind-host.mjs, worker-ownership.mjs),
  follows the processes that carry it to their children and their groups, and counts the worker
  ended only once no process of the execution is left. A table it cannot read is an execution not
  known to have ended.
* The worker still tells the host each execution group the moment it exists, on the control
  channel the host opened for it (`KIN_WORKER_CONTROL_FD`, a JSON line `{"group": pgid}`), and
  again once it has seen the group empty (`{"group": pgid, "ended": true}`): that only starts the
  host's watch early. A report that never arrives costs nothing but that.
* A SIGTERM for the worker is a SIGTERM for each live group before the worker dies as it always
  has; a SIGTERM that arrives between a group's start and its report waits for the report.

A worker no host started (an operator's command line) marks its executions with a mark of its own
and still forwards its TERM; it only has no one to report to.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import threading
import time
import uuid
from contextlib import contextmanager

CONTROL_FD_ENV = "KIN_WORKER_CONTROL_FD"
MARK_ENV = "KIN_WORKER_MARK"
GRACE_SECONDS = 3.0
MARK = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _mark():
    """The host's mark for this worker, taken out of the environment on import like the channel:
    only the executions the worker starts carry it."""
    raw = os.environ.pop(MARK_ENV, "")
    return raw if MARK.fullmatch(raw) else None


_MARK = _mark()


def execution_env():
    """What an execution's environment adds, for itself and everything it starts: the mark the
    host finds all of it by. A worker no host started marks its executions with one of its own."""
    global _MARK
    if _MARK is None:
        _MARK = str(uuid.uuid4())
    return {MARK_ENV: _MARK}


def environment(base=None, marked=None):
    """The environment of a process started for an execution: `base` (this process's own by
    default) with the execution's mark (`marked`, else this process's) set by name."""
    return {**(os.environ if base is None else base), **(marked or execution_env())}


def checked(marked):
    """`marked` when it is an execution's mark as `execution_env` gives it, else nothing: a
    configuration may carry the mark on, and only the mark."""
    if isinstance(marked, dict) and set(marked) == {MARK_ENV} and MARK.fullmatch(str(marked[MARK_ENV])):
        return {MARK_ENV: marked[MARK_ENV]}
    return None


def _control():
    """The channel the host opened for this worker, taken out of the environment on import so no
    process the worker starts inherits a number that names nothing of its own; and used only if
    it is the pipe the host passed."""
    raw = os.environ.pop(CONTROL_FD_ENV, "")
    if not raw.isdigit():
        return None
    try:
        mode = os.fstat(int(raw)).st_mode
    except OSError:
        return None
    return int(raw) if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) else None


_CONTROL = _control()

_groups: set[int] = set()
_starting = False
_pending = False
_installed = False


def alive(pgid):
    """Whether any process is left in group `pgid`. A group that is gone, or no longer this
    user's to signal, is not."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _report(frame):
    if _CONTROL is None:
        return
    try:
        os.write(_CONTROL, (json.dumps(frame) + "\n").encode())
    except OSError:
        # The host is gone or never listened: nothing is left to tell, and the execution is
        # still ended here and by the forwarded TERM.
        pass


def _signal(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _terminated(signum, frame):
    global _pending
    if _starting:
        # Between a group's start and its report: the TERM waits for the report.
        _pending = True
        return
    _forward_and_exit()


def _forward_and_exit():
    for pgid in list(_groups):
        _signal(pgid, signal.SIGTERM)
    # The worker then ends as it did before it forwarded anything: by the default action.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def _install():
    global _installed
    if _installed or threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGTERM, _terminated)
    _installed = True


@contextmanager
def starting():
    """Wraps the start of an execution in a session of its own. Call the yielded function with
    the new process's pid (its group, with `start_new_session`) as soon as it exists; a SIGTERM
    that arrives meanwhile is handled once the group has been reported."""
    global _starting, _pending
    _install()
    _starting = True

    def started(pgid):
        _groups.add(int(pgid))
        _report({"group": int(pgid)})

    try:
        yield started
    finally:
        _starting = False
        if _pending:
            _pending = False
            _forward_and_exit()


def end(child, grace=GRACE_SECONDS):
    """Ends the group `child` leads -- the child and whatever it started in its session -- and
    reports it once it is empty: TERM, up to `grace` seconds for the group to empty, then KILL.
    A child that exited on its own may still have left processes in its group; they are ended
    the same way."""
    pgid = child.pid

    def emptied(seconds):
        deadline = time.monotonic() + seconds
        while True:
            child.poll()  # reaps the leader, which would otherwise keep the group alive
            if not alive(pgid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    child.poll()
    if alive(pgid):
        _signal(pgid, signal.SIGTERM)
        if not emptied(grace):
            _signal(pgid, signal.SIGKILL)
            emptied(grace)
    if not alive(pgid):
        _groups.discard(pgid)
        _report({"group": pgid, "ended": True})
