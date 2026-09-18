"""Evidence that work has stopped, in place of the caller's word for it.

Every recovery command used to believe whoever called it: `workers_stopped=True`
was the whole check, and the host passed it unconditionally at every start. One
wrong flag was therefore enough to interrupt an evaluation that was still running.
The parameter stays, so no caller breaks, but it is no longer the evidence. What
counts instead is what the machine can be asked: a lease that has not expired yet,
a pid that still belongs to the process that claimed it, a heartbeat younger than
the interval that writes it.

Every answer here fails closed. Where the evidence is missing or unreadable — no
`ps` to ask, an unparsable record, a table this store has never created — the
verdict is that the work is alive, because interrupting live work is the fault
this module exists to prevent. A caller that wants the old behaviour turns the
`liveness_checks` flag off and gets exactly the previous code back.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from eventmem.core.db import Conflict

from .autonomy_schema import optimized

FLAG = "liveness_checks"
# The service rewrites `status.json` every 15 s. Three missed beats is the point at
# which a silent host is a stopped host, and still short enough that an operator
# waiting for quiet does not wait for minutes.
HEARTBEAT_STALE_SECONDS = 45.0
# What an exploration adds to its own budget before it counts as overdue. The
# budget only covers the child process; receiving observations, embedding them and
# settling the wish all happen afterwards while the row is still `running`, so a
# healthy exploration must never look expired. An hour of slack is cheap: a crashed
# one is already recognised by its dead pid, and this bound only catches a worker
# that hangs without dying.
EXPLORATION_MARGIN_SECONDS = 3600.0
# A probe, not a lock to hold: if another writer has the database, that is the
# answer, and waiting the engine's usual 30 s for it would tell us nothing more.
EXCLUSIVE_PROBE_MS = 250

# The five tables a lease can live in. `scope` is the column to filter on where the
# table has one; the model ledger and the core job queue are shared by every scope,
# so a lease there belongs to whoever holds it and still means "not quiet".
LEASE_TABLES = (
    ("mind_appraisals", "lease", "scope"),
    ("mind_plan_runs", "lease_until", "scope"),
    ("mind_model_leases", "expires_at", None),
    ("mind_foreground_leases", "expires_at", "scope"),
    ("jobs", "lease_until", None),
)

LEASE_FRESH = "A worker lease is still fresh; recovery waits for it to expire"


def checks_enabled(conn, scope):
    """Default on, like the other stage flags: an explicit false restores the
    previous behaviour, where the boolean parameter was the only check."""
    return optimized(conn, scope, FLAG)


def _ps_path():
    # The absolute path first, as the host's own pid checks use it: a `ps` found on
    # PATH is a `ps` someone else can choose for us.
    return "/bin/ps" if Path("/bin/ps").exists() else shutil.which("ps")


def probe_process(pid):
    """What the operating system says about one pid: whether it exists, when it
    started and what it is running.

    `known` is false when the question could not be asked at all. A caller must
    read that as "alive", never as "dead": a missing `ps` is not a dead worker.
    """
    unknown = {"alive": True, "known": False, "started": None, "command": None}
    try:
        number = int(str(pid).strip())
    except (TypeError, ValueError):
        return {"alive": False, "known": True, "started": None, "command": None}
    if number <= 1:
        # 0 and 1 are the kernel and the init process. Neither is ever a worker of
        # ours, so a record naming one names nothing that can still be running.
        return {"alive": False, "known": True, "started": None, "command": None}
    command = _ps_path()
    if not command:
        return unknown
    try:
        done = subprocess.run([command, "-p", str(number), "-o", "lstart=,command="],
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return unknown
    line = done.stdout.strip()
    if not line:
        # `ps` answers an absent pid with an empty listing and a non-zero status.
        # That is a real answer, not a failure.
        return {"alive": False, "known": True, "started": None, "command": None}
    parts = line.split(None, 5)
    if len(parts) < 5:
        return unknown
    return {"alive": True, "known": True, "started": " ".join(parts[:5]),
            "command": parts[5] if len(parts) > 5 else ""}


def process_gone(pid, *, started=None, command=None, probe=None):
    """True only when the process can be shown to be gone.

    Gone means: no such pid, or a pid that is now some other process. A recycled
    pid keeps the number and loses the start time, which is why the start time is
    recorded at all; a `command` that no longer matches says the same thing.

    `probe` is resolved when the question is asked, not when this module is
    imported, so a caller can hand in a process table of its own.
    """
    seen = (probe or probe_process)(pid)
    if not seen["known"]:
        return False
    if not seen["alive"]:
        return True
    if started is not None:
        if not seen["started"]:
            return False
        return seen["started"] != started
    if command is not None:
        if seen["command"] is None:
            return False
        return command not in seen["command"]
    # A live pid with nothing to identify it by is a live pid.
    return False


def self_record(*, deadline, probe=None):
    """What this process must leave behind so a later one can tell whether it is
    still running. Stored inside an existing `data` document, never a new column."""
    pid = os.getpid()
    seen = (probe or probe_process)(pid)
    return {"pid": pid, "started": seen["started"] if seen["known"] else None, "deadline": deadline}


def record_expired(record, *, now=None):
    now = time.time() if now is None else now
    deadline = (record or {}).get("deadline")
    return isinstance(deadline, (int, float)) and deadline <= now


def record_alive(record, *, now=None, probe=None):
    """Whether the worker a record describes may still be running.

    A record that says nothing — an older row written before this module existed —
    cannot be shown to be dead, so it is alive. The `liveness_checks` flag off is
    the way back to interrupting such a row unconditionally.
    """
    if not isinstance(record, dict) or "pid" not in record:
        return True
    if record_expired(record, now=now):
        return False
    return not process_gone(record["pid"], started=record.get("started"), probe=probe)


def fresh_leases(conn, scope, *, now=None):
    """Every unexpired lease, by table. A table this store never created holds none."""
    now = time.time() if now is None else now
    held = []
    for table, column, scoped in LEASE_TABLES:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        # The table and column names are this module's own constants, never input.
        sql = f"SELECT COUNT(*) FROM {table} WHERE {column}>?"
        arguments = [now]
        if scoped:
            sql += f" AND {scoped}=?"
            arguments.append(scope)
        count = conn.execute(sql, arguments).fetchone()[0]
        if count:
            held.append({"table": table, "held": count})
    return held


def running_appraisal_leases(conn, scope, *, now=None):
    """The rows the operational recoveries would release. A `running` row whose
    lease has not expired is held by a worker that is still evaluating it."""
    now = time.time() if now is None else now
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_appraisals'").fetchone():
        return []
    return [row[0] for row in conn.execute(
        "SELECT id FROM mind_appraisals WHERE scope=? AND state='running' AND lease>? ORDER BY id", (scope, now))]


def refuse_while_appraisal_leased(conn, scope, *, now=None):
    """The guard `migrate_operational` and `recover_batched` use in place of the
    boolean. Both release every `running` row, so a held one must stop them."""
    held = running_appraisal_leases(conn, scope, now=now)
    if held:
        raise Conflict(LEASE_FRESH, kind="runtime", code="worker-lease-fresh", target=held[0], actual=len(held))


def live_explorations(conn, scope, *, now=None, probe=None):
    """Running exploration rows whose worker cannot be shown to be gone."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mind_explorations'").fetchone():
        return []
    live = []
    for row in conn.execute("SELECT id,data FROM mind_explorations WHERE scope=? AND state='running' ORDER BY id", (scope,)):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            data = {}
        if record_alive(data.get("liveness"), now=now, probe=probe):
            live.append(row["id"])
    return live


def _pid_file_quiet(entry, *, probe=None):
    """Whether one pid file proves nothing of ours is running.

    A file that is absent, or that holds no pid at all, claims no process — the
    host's own start-up check reads it the same way. A file we cannot read is a
    different matter: that is missing evidence, and missing evidence means alive.
    """
    path = Path(entry["pid_file"])
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {"quiet": True, "reason": "no-pid-file"}
    except OSError:
        return {"quiet": False, "reason": "pid-file-unreadable"}
    try:
        pid = int(text.strip())
    except ValueError:
        return {"quiet": True, "reason": "pid-file-holds-no-pid"}
    command = entry.get("command")
    if command is None:
        # Nothing to recognise the process by, so a live pid cannot be cleared.
        seen = (probe or probe_process)(pid)
        if seen["known"] and not seen["alive"]:
            return {"quiet": True, "reason": "process-gone", "pid": pid}
        return {"quiet": False, "reason": "no-command-configured", "pid": pid}
    if process_gone(pid, command=command, probe=probe):
        return {"quiet": True, "reason": "process-gone-or-reused", "pid": pid}
    return {"quiet": False, "reason": "process-running", "pid": pid}


def _status_quiet(path, *, now):
    if not path:
        return {"quiet": False, "reason": "status-file-not-configured"}
    try:
        status = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"quiet": False, "reason": "status-file-missing"}
    except (OSError, ValueError):
        return {"quiet": False, "reason": "status-file-unreadable"}
    if status.get("state") == "stopped":
        return {"quiet": True, "reason": "host-stopped"}
    beat = status.get("heartbeatAt")
    if not isinstance(beat, str):
        return {"quiet": False, "reason": "no-heartbeat-recorded"}
    try:
        at = datetime.fromisoformat(beat.replace("Z", "+00:00"))
    except ValueError:
        return {"quiet": False, "reason": "heartbeat-unreadable"}
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = now - at.timestamp()
    if age > HEARTBEAT_STALE_SECONDS:
        return {"quiet": True, "reason": "heartbeat-stale", "age_seconds": round(age, 3)}
    return {"quiet": False, "reason": "heartbeat-fresh", "age_seconds": round(age, 3)}


def _exclusive_quiet(path):
    """`BEGIN EXCLUSIVE` asks the question every other clause answers indirectly:
    is there a writer? It is rolled back at once and holds nothing."""
    try:
        conn = sqlite3.connect(path, timeout=EXCLUSIVE_PROBE_MS / 1000, isolation_level=None)
    except sqlite3.Error:
        return {"quiet": False, "reason": "database-unreachable"}
    try:
        conn.execute(f"PRAGMA busy_timeout={EXCLUSIVE_PROBE_MS}")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        return {"quiet": True, "reason": "exclusive-lock-available"}
    except sqlite3.Error:
        return {"quiet": False, "reason": "database-busy"}
    finally:
        conn.close()


def quiescence(mind, config=None, *, now=None, probe=None):
    """The proof compaction rests on: nothing of ours is running, anywhere.

    Five clauses, in the order that costs least and disturbs least. The exclusive
    probe is last and is only attempted once everything else is quiet, so a running
    host is recognised without ever reaching for the database's write lock.

    The pid files and the status file live outside this repository, so their paths
    come from the host configuration (`liveness.processes`, `liveness.status_file`).
    A path that is not configured is missing evidence, which is not quiet.
    """
    now = time.time() if now is None else now
    settings = (config or {}).get("liveness") or {}
    checks, scope = [], mind.scope.key()
    processes = settings.get("processes") or []
    if not processes:
        checks.append({"check": "processes", "quiet": False, "reason": "no-pid-files-configured"})
    for entry in processes:
        checks.append({"check": "process", "name": entry.get("name") or entry.get("pid_file"),
                       **_pid_file_quiet(entry, probe=probe)})
    checks.append({"check": "host-status", **_status_quiet(settings.get("status_file"), now=now)})
    with mind.engine.db.connect() as conn:
        held = fresh_leases(conn, scope, now=now)
        checks.append({"check": "leases", "quiet": not held, "reason": "leases-fresh" if held else "no-fresh-lease",
                       "tables": held})
        live = live_explorations(conn, scope, now=now, probe=probe)
        checks.append({"check": "explorations", "quiet": not live,
                       "reason": "exploration-running" if live else "no-live-exploration", "explorations": live})
    if all(check["quiet"] for check in checks):
        checks.append({"check": "exclusive-probe", **_exclusive_quiet(mind.engine.db.path)})
    else:
        checks.append({"check": "exclusive-probe", "quiet": False, "reason": "not-attempted-while-noisy"})
    return {"quiet": all(check["quiet"] for check in checks), "checked_at": mind.clock(), "checks": checks,
            "blocking": [check for check in checks if not check["quiet"]]}
