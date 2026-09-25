"""The process groups a mind worker runs its executions in, and who ends them (CR3-MM-02).

An executor's CLI runs in a session of its own (`start_new_session`), so that the worker can end
the whole execution -- the CLI and everything it started -- without ending itself. That also
takes the execution out of the worker's own process group, which is the group the host signals
when it ends the worker: a timeout or a shutdown ended the worker and left the execution
running, and the host counted the work over once the worker had gone.

So the execution groups have one owner besides the worker, the host:

* The worker tells the host each group the moment it exists, on the control channel the host
  opened for it (`KIN_WORKER_CONTROL_FD`, a JSON line `{"group": pgid}`), and again once it has
  seen the group empty (`{"group": pgid, "ended": true}`). A termination cannot fall between a
  group's start and its report: the worker's SIGTERM waits for the report (`starting`).
* A SIGTERM for the worker is a SIGTERM for each live group before the worker dies as it always
  has, so the execution hears it even when the worker is ended by someone other than the host.
* The host sends the same TERM, and the KILL after it, to each reported group itself, and does
  not count the worker ended until each has emptied (mind-host.mjs, WorkerGroups): it never
  relies on the worker's own `finally`, which a SIGKILL skips.

A worker the host did not open a channel for (an operator's command line) still forwards its
TERM; it only has no one to report to.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import threading
import time
from contextlib import contextmanager

CONTROL_FD_ENV = "KIN_WORKER_CONTROL_FD"
GRACE_SECONDS = 3.0


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
