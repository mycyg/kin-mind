"""Cross-process background capacity, client reuse and explicit unknown usage."""
import contextvars
import threading
import time
import uuid
from contextlib import contextmanager

import httpx

from eventmem.core.db import dumps

_background = contextvars.ContextVar("kin_background_job", default=False)


@contextmanager
def background_calls():
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


_held = contextvars.ContextVar("kin_background_model_lease", default=None)


@contextmanager
def model_slot(provider, purpose):
    engine = getattr(provider, "engine", None)
    if not engine or not (getattr(provider, "background", False) or _background.get()) or _held.get():
        yield
        return
    key, stop = str(uuid.uuid4()), threading.Event()
    with engine.db.connect(write=True) as conn:
        from .autonomy_schema import SCHEMA
        # Schema normally exists through Mind; direct provider use is supported.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_model_leases'").fetchone():
            raise RuntimeError("deepseek-background-lease-unavailable")
        now = time.time()
        conn.execute("DELETE FROM mind_model_leases WHERE expires_at<=?", (now,))
        if conn.execute("SELECT COUNT(*) FROM mind_model_leases WHERE lane='background'").fetchone()[0] >= 2:
            raise RuntimeError("deepseek-background-capacity")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_foreground_leases'").fetchone() and conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1", (now,)).fetchone():
            raise RuntimeError("deepseek-foreground-priority")
        conn.execute("INSERT INTO mind_model_leases VALUES(?,?,?,?)", (key, "background", now + 90, dumps({"purpose": purpose})))
    def renew():
        while not stop.wait(20):
            with engine.db.connect(write=True) as conn:
                conn.execute("UPDATE mind_model_leases SET expires_at=? WHERE id=?", (time.time() + 90, key))
    worker = threading.Thread(target=renew, name="kin-model-lease", daemon=True)
    worker.start()
    token = _held.set(key)
    try:
        yield
    finally:
        _held.reset(token)
        stop.set()
        worker.join(timeout=2)
        with engine.db.connect(write=True) as conn:
            conn.execute("DELETE FROM mind_model_leases WHERE id=?", (key,))


@contextmanager
def request_client(provider, timeout, purpose):
    """One client per provider/thread; no shared mutable timeout across threads."""
    if not hasattr(provider, "_http_clients"):
        provider._http_clients = threading.local()
    clients = provider._http_clients
    client = getattr(clients, "client", None)
    if client is None or client.is_closed:
        client = clients.client = httpx.Client(transport=provider.transport)
    client.timeout = httpx.Timeout(timeout)
    with model_slot(provider, purpose):
        try:
            yield client
        except httpx.TimeoutException:
            provider.failure_receipt = {"provider": "deepseek", "model": "deepseek-flash", "reasoning": "high",
                                        "purpose": purpose, "usage": None, "usage_status": "unknown", "outcome": "timeout"}
            if hasattr(provider, "engine"):
                provider.engine.db.metric("model_timeout", 1, provider.failure_receipt)
            raise


def close_client(provider):
    local = getattr(provider, "_http_clients", None)
    if local is not None and getattr(local, "client", None) is not None:
        local.client.close()
