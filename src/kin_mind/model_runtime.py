"""Cross-process background capacity, client reuse and explicit unknown usage."""
import threading
from contextlib import contextmanager

import httpx

# The ledger, the lanes and the context they travel in live in model_lanes. These names stay
# importable from here, where their callers have always found them.
from .model_lanes import ModelAdmissionWait, background_calls, configure, evaluation_slot, slot  # noqa: F401


def verified_decision(receipt):
    """Accept the host-confirmed native profile or a legacy DS high receipt."""
    native = receipt.get("native_receipt")
    if native:
        return bool(native.get("native_turn_id") and native.get("native_session_id") and native.get("verified_at")
                    and all(native.get(k) and native[k] == receipt.get(k) for k in ("provider", "model", "reasoning")))
    return receipt.get("provider") == "deepseek" and receipt.get("reasoning") == "high"


def model_slot(provider, purpose, *, default=None):
    """Delegates to the one admission implementation. `default` is the lane a call site
    declares for itself when nothing above it declared one."""
    return slot(provider, purpose, default=default)


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


def configure_capacity(engine, limit):
    """One database-wide limit, shared by every process and scope."""
    return configure(engine, limit)
