"""One admission ledger for every model call, shared by the Python host and the Node adapters.

A lane follows the purpose its caller declares, never the name of the function that ends up
calling the model (compression serves a waiting user and an idle queue alike):

  background  bounded by meta.kin_background_model_limit; yields to any foreground session
  user-work   one reserved slot that does not yield. A held work task keeps a foreground
              lease, so the review that would release it could otherwise never be admitted
  foreground  always admitted; the row only records who is calling

Rows live in mind_model_leases without a new column: purpose and holder sit in `data`, and
older code, which counts lane='background' only, never sees the other two lanes. A row that
is not renewed expires after TTL seconds and is swept at the next admission. That is the whole
of crash recovery, for a Python worker and for a Node adapter alike.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext, contextmanager
from pathlib import Path

from eventmem.core.db import Conflict, dumps

LANES = ("background", "user-work", "foreground")
TTL = 90.0
RENEW_SECONDS = 20.0
# The appraisal row: beaten while its evaluation runs, never shortened.
ROW_BEAT_SECONDS = 30.0
ROW_EXTEND_SECONDS = 180.0
# No heartbeat outlives this. A worker that hangs without dying loses its row and its slot
# like one that crashed, only later; the longest honest evaluation stays well inside it.
MAX_HOLD_SECONDS = 3600.0
# Lease operations answer quickly or not at all; only a background admission may queue
# behind a long writer, as it always has.
LEDGER_BUSY_MS = 2000
BACKGROUND_BUSY_MS = 30000
CAPACITY_KEY = "kin_background_model_limit"
DEFAULT_BACKGROUND_LIMIT = 2
USER_WORK_LIMIT = 1
RETRY_SECONDS = 30
# core/jobs.py and recovery.py match on these two strings: they stay byte-identical.
WAIT_CAPACITY = "deepseek-background-capacity"
WAIT_FOREGROUND = "deepseek-foreground-priority"
WAIT_USER_WORK = "deepseek-user-work-capacity"
WAIT_LEDGER = "deepseek-ledger-busy"
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,119}")

clock = time.time
# Per database: until when a caller that never waits for the ledger does not ask it again.
_busy_until = {}


class ModelAdmissionWait(RuntimeError):
    """No request was admitted; waiting is not a failed model attempt."""


_background = contextvars.ContextVar("kin_background_job", default=False)
_declared = contextvars.ContextVar("kin_model_lane", default=None)
_current = contextvars.ContextVar("kin_model_lease", default=None)
_carrier = contextvars.ContextVar("kin_model_lease_handoff", default=None)


@contextmanager
def background_calls():
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


@contextmanager
def declared(lane, purpose=""):
    """The caller states what the work beneath it is for; every model call inside follows.

    The outermost statement stands. A job, or a host action that already said what it serves,
    is not overruled by a helper further in that only sees its own arguments."""
    if lane not in LANES:
        raise ValueError("Unknown model lane")
    carrier = _carrier.get()
    if _declared.get() is not None or _background.get() or (carrier is not None and carrier.background):
        yield
        return
    token = _declared.set((lane, label(purpose)))
    try:
        yield
    finally:
        _declared.reset(token)


def context_lane(purpose, access_origin="user_query"):
    """A context somebody is waiting for is foreground; maintenance and Kin's own drafts are not."""
    return "foreground" if purpose in {"chat", "work", "read", "startup"} and access_origin == "user_query" else "background"


def label(value):
    """Status shows purposes and holders, so they are short static labels and never text."""
    value = str(value or "")
    return value if LABEL.fullmatch(value) else "unlabelled"


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())


def enabled(conn):
    """One ledger serves every scope, so the switch is machine-wide: a scope that turns
    model_lanes off restores the previous admission for all of them."""
    from .autonomy_schema import optimized
    if not _table(conn, "mind_memory_config"):
        return True
    return all(optimized(conn, row[0], "model_lanes") for row in conn.execute("SELECT scope FROM mind_memory_config").fetchall())


def capacity(conn):
    """(limit, source). A missing limit is never a silent 2: the source says so and admission writes a metric."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (CAPACITY_KEY,)).fetchone()
    return (max(1, min(8, row[0])), "configured") if row else (DEFAULT_BACKGROUND_LIMIT, "default-unconfigured")


def foreground_active(conn, scope=None):
    """User work has priority on the whole machine: a scope isolates data, not the model's attention."""
    if not _table(conn, "mind_foreground_leases"):
        return False
    if scope is not None and not enabled(conn):
        return bool(conn.execute("SELECT 1 FROM mind_foreground_leases WHERE scope=? AND expires_at>?", (scope, clock())).fetchone())
    return bool(conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1", (clock(),)).fetchone())


def _metric(conn, name, data):
    from eventmem.core.models import now

    from .maintenance import trim_metrics
    conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)", (name, 1, now(), dumps(data)))
    # The ledger writes into the same telemetry table as the engine, so it bounds it
    # the same way: one ring per name with the flag on, the shared ring without it.
    trim_metrics(conn, name)


def _busy(error):
    return isinstance(error, sqlite3.OperationalError) and ("locked" in str(error) or "busy" in str(error))


def ledger_path(root):
    return Path(root).expanduser().resolve() / "memory.sqlite3"


class Ledger:
    """The only admission implementation. Every answer is a plain state, so the API, the CLI
    and the in-process slot say the same thing: admitted, wait, lost, busy, disabled, unavailable."""

    def __init__(self, path, *, busy_ms=None):
        self.path, self.busy_ms = Path(path), LEDGER_BUSY_MS if busy_ms is None else busy_ms

    @contextmanager
    def _open(self):
        conn = sqlite3.connect(self.path, timeout=self.busy_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=%d" % self.busy_ms)
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _run(self, work):
        if not self.path.exists():
            return {"state": "unavailable", "reason": "ledger-missing"}
        try:
            with self._open() as conn:
                if not _table(conn, "mind_model_leases"):
                    return {"state": "unavailable", "reason": "ledger-missing"}
                return work(conn, clock())
        except sqlite3.OperationalError as error:
            if not _busy(error):
                raise
            return {"state": "busy", "reason": "ledger-busy"}

    @staticmethod
    def _pools(conn, now):
        held = dict(conn.execute("SELECT lane,COUNT(*) FROM mind_model_leases WHERE expires_at>? GROUP BY lane", (now,)).fetchall())
        limit, source = capacity(conn)
        return {"background": {"limit": limit, "source": source, "held": held.get("background", 0)},
                "user-work": {"limit": USER_WORK_LIMIT, "source": "reserved", "held": held.get("user-work", 0), "yields_to_foreground": False},
                "foreground": {"limit": None, "source": "unbounded", "held": held.get("foreground", 0)}}

    def acquire(self, lane, purpose, *, holder="", ttl=TTL, lease_id=None):
        if lane not in LANES:
            raise ValueError("Unknown model lane")
        if not LABEL.fullmatch(str(purpose)) or (holder and not LABEL.fullmatch(str(holder))):
            raise ValueError("A lease purpose and holder are short static labels")
        if lease_id is not None and not (isinstance(lease_id, str) and len(lease_id) >= 8 and LABEL.fullmatch(lease_id)):
            raise ValueError("A lease id is a caller-chosen unique label")
        if type(ttl) not in {int, float} or not 15 <= ttl <= 300:
            raise ValueError("Lease seconds must be between 15 and 300")

        def work(conn, now):
            if not enabled(conn):
                return {"state": "disabled"}
            # Crash recovery: whatever stopped renewing is gone before anything is counted.
            conn.execute("DELETE FROM mind_model_leases WHERE expires_at<=?", (now,))
            if _table(conn, "mind_foreground_leases"):
                conn.execute("DELETE FROM mind_foreground_leases WHERE expires_at<=?", (now,))
            key = lease_id or str(uuid.uuid4())
            known = conn.execute("SELECT lane FROM mind_model_leases WHERE id=?", (key,)).fetchone()
            if known and known["lane"] != lane:
                raise ValueError("A lease id is a caller-chosen unique label")
            pools = self._pools(conn, now)
            pool = pools[lane]
            reason = None
            if lane == "background" and pool["source"] != "configured":
                _metric(conn, "model_capacity_unconfigured", {"limit": pool["limit"], "source": pool["source"]})
            if known:
                pass  # The same caller asking again, after an answer it never received.
            elif lane == "user-work" and pool["held"] >= pool["limit"]:
                reason = WAIT_USER_WORK
            elif lane == "background" and pool["held"] >= pool["limit"]:
                reason = WAIT_CAPACITY
            elif lane == "background" and foreground_active(conn):
                reason = WAIT_FOREGROUND
            if reason:
                return {"state": "wait", "reason": reason, "retry_after_seconds": RETRY_SECONDS, "capacity": pool}
            conn.execute("INSERT OR REPLACE INTO mind_model_leases VALUES(?,?,?,?)", (key, lane, now + ttl, dumps(
                {"purpose": purpose, "holder": holder, "acquired_at": now, "ttl": ttl})))
            return {"state": "admitted", "capacity": {**pool, "held": pool["held"] + (0 if known else 1)},
                    "lease": {"id": key, "lane": lane, "purpose": purpose, "expires_at": now + ttl, "ttl_seconds": ttl,
                              "renew_after_seconds": round(ttl / 3, 3)}}
        return self._run(work)

    def renew(self, lease_id, *, ttl=TTL):
        if type(ttl) not in {int, float} or not 15 <= ttl <= 300:
            raise ValueError("Lease seconds must be between 15 and 300")

        def work(conn, now):
            # The row count is the whole check. Every admission sweeps what expired before it
            # counts, so a row that is still here was given to nobody else, however late this
            # renewal comes; a row that is gone may already be somebody else's slot.
            if not conn.execute("UPDATE mind_model_leases SET expires_at=? WHERE id=?", (now + ttl, lease_id)).rowcount:
                return {"state": "lost", "id": lease_id}
            return {"state": "renewed", "lease": {"id": lease_id, "expires_at": now + ttl, "ttl_seconds": ttl, "renew_after_seconds": round(ttl / 3, 3)}}
        return self._run(work)

    def release(self, lease_id):
        def work(conn, now):
            # Nothing to return means it expired and was swept: the caller learns its result was late.
            gone = not conn.execute("DELETE FROM mind_model_leases WHERE id=?", (lease_id,)).rowcount
            return {"state": "lost" if gone else "released", "id": lease_id}
        return self._run(work)

    def note(self, name, data):
        """A metric through the same short connection: telemetry never queues a caller behind a writer."""
        def work(conn, now):
            _metric(conn, name, data)
            return {"state": "recorded"}
        try:
            return self._run(work)
        except sqlite3.Error:
            return {"state": "busy", "reason": "ledger-busy"}

    def status(self):
        def work(conn, now):
            return status(conn, now)
        return self._run(work)


def status(conn, now=None):
    """Lanes, capacity with its source and who holds what. Labels only, never text."""
    now = clock() if now is None else now
    if not _table(conn, "mind_model_leases"):
        return {"state": "unavailable", "reason": "ledger-missing"}
    holders = []
    for row in conn.execute("SELECT id,lane,expires_at,data FROM mind_model_leases WHERE expires_at>? ORDER BY lane,expires_at", (now,)).fetchall():
        data = json.loads(row["data"] or "{}")
        holders.append({"lease": row["id"][:8], "lane": row["lane"], "purpose": label(data.get("purpose")), "holder": label(data["holder"]) if data.get("holder") else None,
                        "held_seconds": round(now - data["acquired_at"], 3) if isinstance(data.get("acquired_at"), (int, float)) else None,
                        "expires_in_seconds": round(row["expires_at"] - now, 3)})
    sessions = conn.execute("SELECT COUNT(*) FROM mind_foreground_leases WHERE expires_at>?", (now,)).fetchone()[0] if _table(conn, "mind_foreground_leases") else 0
    return {"state": "ready", "enabled": enabled(conn), "lanes": Ledger._pools(conn, now), "holders": holders,
            "foreground_sessions": sessions, "background_yields": bool(sessions), "ttl_seconds": TTL}


def lease_command(root, request):
    """The CLI fallback of the lease routes, answered before any engine is opened."""
    ledger, op = Ledger(ledger_path(root)), request.get("op")
    if op == "acquire":
        return ledger.acquire(request.get("lane"), request.get("purpose", ""), holder=request.get("holder", ""),
                              ttl=request.get("ttl_seconds", TTL), lease_id=request.get("id"))
    if op == "renew":
        return ledger.renew(str(request.get("id", "")), ttl=request.get("ttl_seconds", TTL))
    if op == "release":
        return ledger.release(str(request.get("id", "")))
    if op == "status":
        return ledger.status()
    raise ValueError("Unknown lease operation")


def configure(engine, limit):
    """One database-wide limit, shared by every process and scope."""
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("Background model capacity must be between 1 and 8")
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CAPACITY_KEY, limit))
    return {"background_model_limit": limit, "foreground_priority": True, "source": "configured", "user_work_limit": USER_WORK_LIMIT}


def sync_capacity(engine, config, *, startup=False):
    """The host states its capacity in its configuration file; every process reads it from meta.

    At start-up the file wins. Between restarts it only fills a gap, so an operator's
    configure-model-capacity holds until the next restart."""
    limit = config.get("background_model_limit")
    if limit is None:
        return None
    if type(limit) is not int or not 1 <= limit <= 8:
        if startup:
            # Not a reason to keep the host down, and not silent either.
            engine.db.metric("model_capacity_invalid", 1, {"source": "host-config"})
        return None
    with engine.db.connect() as conn:
        current = conn.execute("SELECT value FROM meta WHERE key=?", (CAPACITY_KEY,)).fetchone()
    if current and (current[0] == limit or not startup):
        return None
    return configure(engine, limit)


class _Pulse:
    """Calls `beat` every `seconds` until it is stopped or the beat says there is nothing left to keep."""

    def __init__(self, beat, seconds, name):
        self.beat, self.seconds, self.done = beat, seconds, threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def _run(self):
        started = time.monotonic()
        while not self.done.wait(self.seconds):
            if time.monotonic() - started > MAX_HOLD_SECONDS or not self.beat():
                return

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.done.set()
        self.thread.join(timeout=2)


class Lease:
    """A held row. `lost` is set by the renewal that finds no row and by the release that
    deletes none. A caller that already parsed the provider's usage may leave it in `usage`:
    a quarantined call is then accounted with it instead of as unknown."""

    def __init__(self, ledger, granted, late):
        self.ledger, self.id, self.lane, self.purpose = ledger, granted["id"], granted["lane"], granted["purpose"]
        self.ttl, self.late, self.usage = granted["ttl_seconds"], late, None
        self.lost = self.abandoned = self.superseded = self.closed = False
        self.reason, self.pulse = None, None

    def start(self):
        self.pulse = _Pulse(self.renew, RENEW_SECONDS, "kin-model-lease").start()
        return self

    def renew(self):
        """False once there is nothing left to renew. An unreachable ledger is tried again at the next beat."""
        if self.closed or self.abandoned or self.lost:
            return False
        try:
            answer = self.ledger.renew(self.id, ttl=self.ttl)
        except sqlite3.Error:
            return True
        if answer["state"] in {"lost", "unavailable"} and not self.closed:
            self.lost, self.reason = True, "renewal-found-no-row"
        return not self.lost

    def abandon(self):
        """Its caller stopped waiting. Nothing renews the row again, so a call that never
        returns cannot keep the slot beyond TTL."""
        self.abandoned = True
        if self.pulse:
            self.pulse.done.set()

    def usable(self):
        """Before a nested call: nothing more is paid for under a lease or a row that is gone."""
        if self.lost and self.lane != "foreground":
            raise Conflict("Model lease was lost during the evaluation", kind="runtime", code="model-lease-lost", target=self.id)
        if self.superseded:
            raise Conflict("Appraisal row was taken over during the evaluation", kind="runtime", code="lease-lost")

    def close(self):
        self.closed = True
        if self.pulse:
            self.pulse.stop()
        try:
            # A result is never lost because its row could not be deleted: the row expires on its own.
            answer = self.ledger.release(self.id)
        except sqlite3.Error:
            answer = {"state": "busy"}
        facts = {"lease": self.id[:8], "lane": self.lane, "purpose": self.purpose}
        if self.abandoned:
            # Its caller left at the deadline; the call itself still meters whatever it used.
            self.ledger.note("model_call_abandoned", {**facts, "reason": "caller-deadline"})
            return
        if answer["state"] == "lost":
            self.lost, self.reason = True, self.reason or "row-missing-at-release"
        if self.lost and self.lane != "foreground":
            # Unknown is never zero. An evaluation keeps its calls' receipts on its own row.
            usage = {"usage": self.usage, "usage_status": "reported"} if self.usage else {
                "usage_status": "kept-with-attempt" if self.late == "flag" else "unknown"}
            self.ledger.note("model_lease_lost", {**facts, "reason": self.reason, **usage})


class Handoff:
    """What a thread started on the caller's behalf needs from it. A new thread begins with an
    empty context, so without this a nested call would take a second slot of its own."""

    def __init__(self):
        lease, outer = _current.get(), _carrier.get()
        # Only what lanes introduced travels: with model_lanes off a thread starts as empty as before.
        self.lease, self.lane = lease if isinstance(lease, Lease) else None, _declared.get()
        self.background = bool(_background.get() or (outer is not None and outer.background))
        self.taken, self.abandoned = [], False

    @contextmanager
    def adopt(self):
        tokens = [(_current, _current.set(self.lease)), (_declared, _declared.set(self.lane)), (_carrier, _carrier.set(self))]
        try:
            yield self
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    def took(self, lease):
        self.taken.append(lease)
        if self.abandoned:
            lease.abandon()

    def abandon(self):
        self.abandoned = True
        for lease in list(self.taken):
            lease.abandon()


def handoff():
    return Handoff()


@contextmanager
def _legacy_slot(engine, purpose, background):
    """The admission before lanes, kept as it was for model_lanes=false."""
    if not background:
        yield None
        return
    key, stop = str(uuid.uuid4()), threading.Event()
    with engine.db.connect(write=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_model_leases'").fetchone():
            raise RuntimeError("deepseek-background-lease-unavailable")
        now = time.time()
        conn.execute("DELETE FROM mind_model_leases WHERE expires_at<=?", (now,))
        configured = conn.execute("SELECT value FROM meta WHERE key='kin_background_model_limit'").fetchone()
        limit = max(1, min(8, configured[0])) if configured else 2
        if conn.execute("SELECT COUNT(*) FROM mind_model_leases WHERE lane='background'").fetchone()[0] >= limit:
            raise ModelAdmissionWait("deepseek-background-capacity")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_foreground_leases'").fetchone() and conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1", (now,)).fetchone():
            raise ModelAdmissionWait("deepseek-foreground-priority")
        conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", (key, "background", now + 90, dumps({"purpose": purpose})))
    def renew():
        while not stop.wait(20):
            with engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_model_leases SET expires_at=? WHERE id=?", (time.time() + 90, key))
    worker = threading.Thread(target=renew, name="kin-model-lease", daemon=True)
    worker.start()
    token = _current.set(key)
    try:
        yield None
    finally:
        _current.reset(token)
        stop.set()
        worker.join(timeout=2)
        with engine.db.connect(write=True) as conn:
            conn.execute("DELETE FROM mind_model_leases WHERE id=?", (key,))


@contextmanager
def slot(provider, purpose, *, default=None, late="raise"):
    """Admit one model call, or a whole evaluation whose nested calls reuse this lease.

    The lane is what the caller declared: declared() above it, the older background marks,
    or `default` for a call site that knows what it serves. Nothing declared keeps the
    previous behaviour, no lease at all, and leaves a metric so the caller can be found."""
    engine = getattr(provider, "engine", None)
    if not engine:
        yield None
        return
    parent = _current.get()
    if parent is not None:
        if isinstance(parent, Lease):
            parent.usable()
        yield parent if isinstance(parent, Lease) else None
        return
    marked = bool(getattr(provider, "background", False) or _background.get())
    ambient, carrier = _declared.get(), _carrier.get()
    with engine.db.connect() as conn:
        active = enabled(conn)
    if not active:
        with _legacy_slot(engine, purpose, marked):
            yield None
        return
    lane = ambient[0] if ambient else "background" if marked or (carrier is not None and carrier.background) else default
    if lane is None:
        Ledger(engine.db.path).note("model_lane_undeclared", {"purpose": label(purpose)})
        yield None
        return
    if lane != "background" and time.monotonic() < _busy_until.get(str(engine.db.path), 0):
        # The ledger was busy a moment ago, and these lanes never wait for it.
        yield None
        return
    ledger = Ledger(engine.db.path, busy_ms=BACKGROUND_BUSY_MS if lane == "background" else None)
    granted = ledger.acquire(lane, label(purpose), holder="python:%d" % os.getpid())
    if granted["state"] == "wait":
        raise ModelAdmissionWait(granted["reason"])
    if granted["state"] != "admitted" and lane == "background":
        # Nothing was admitted and nothing was paid for: a wait, not a failed attempt.
        if granted["state"] == "unavailable":
            raise RuntimeError("deepseek-background-lease-unavailable")
        raise ModelAdmissionWait(WAIT_LEDGER)
    if granted["state"] != "admitted":
        # Foreground and user work go ahead unrecorded rather than wait for the ledger.
        if len(_busy_until) > 64:
            _busy_until.clear()
        _busy_until[str(engine.db.path)] = time.monotonic() + RETRY_SECONDS
        try:
            yield None
        finally:
            Ledger(engine.db.path).note("model_lane_unrecorded", {"lane": lane, "purpose": label(purpose), "reason": granted.get("reason", granted["state"])})
        return
    lease = Lease(ledger, granted["lease"], late)
    if carrier is not None:
        carrier.took(lease)
    lease.start()
    token, failed = _current.set(lease), False
    try:
        yield lease
    except BaseException:
        failed = True
        raise
    finally:
        _current.reset(token)
        lease.close()
        if lease.lost and not lease.abandoned and lease.lane != "foreground" and late == "raise" and not failed:
            # Quarantined: the late result never reaches its caller, so nothing can commit it.
            raise Conflict("Model lease was lost before the result returned", kind="runtime", code="model-lease-lost", target=lease.id)


class Evaluation:
    """What run_one holds for one attempt: the admitted lease and the heartbeat of its queue row."""

    def __init__(self, lease, engine, row_id, token):
        self.lease, self.engine, self.row_id, self.token = lease, engine, row_id, token
        self.row_lost, self.pulse = False, None

    def beat(self):
        """Extend the row lease while this attempt still owns the row; never shorten it."""
        try:
            with self.engine.db.connect(write=True) as conn:
                changed = conn.execute(
                    "UPDATE mind_appraisals SET lease=MAX(lease,?) WHERE id=? AND state='running' AND json_extract(data,'$.attempt_token')=?",
                    (clock() + ROW_EXTEND_SECONDS, self.row_id, self.token)).rowcount
        except sqlite3.Error:
            return True
        if not changed:
            self.row_lost = True
            if self.lease is not None:
                self.lease.superseded = True
        return bool(changed)

    def verify(self, conn):
        """Inside the commit: a result that outlived its model lease stays on the row as a
        proposal with its receipt, and is never applied."""
        lease = self.lease
        if self.row_lost:
            raise Conflict("Appraisal attempt was superseded", kind="runtime", code="model-lease-lost", target=self.row_id)
        if lease is None or lease.lane == "foreground":
            return
        if lease.lost or not conn.execute("SELECT 1 FROM mind_model_leases WHERE id=?", (lease.id,)).fetchone():
            lease.lost, lease.reason = True, lease.reason or "row-missing-at-commit"
            raise Conflict("Model lease was lost before commit", kind="runtime", code="model-lease-lost", target=lease.id)


@contextmanager
def evaluation_slot(provider, engine, row_id, token):
    """One admission and one row heartbeat for a whole appraisal attempt.

    run_one closes this in a `finally`, so nothing is raised on the way out: a lease lost
    under the attempt is refused where its result would commit, by verify()."""
    with (nullcontext(None) if getattr(provider, "native_review", False) else slot(provider, "appraisal:" + row_id, late="flag")) as lease:
        guard = Evaluation(lease, engine, row_id, token)
        with engine.db.connect() as conn:
            active = enabled(conn)
        if active:
            guard.pulse = _Pulse(guard.beat, ROW_BEAT_SECONDS, "kin-appraisal-row").start()
        try:
            yield guard
        finally:
            if guard.pulse:
                guard.pulse.stop()
