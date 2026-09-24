"""Python HTTP SDK driven by the generated OpenAPI operation table."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import httpx

from ._operations import OPERATIONS


class Client:
    def __init__(
        self,
        url="http://127.0.0.1:8319",
        token=None,
        *,
        root=None,
        transport=None,
        timeout=30,
    ):
        if token is None:
            token = (
                (
                    Path(
                        root
                        or os.environ.get(
                            "EVENTMEM_HOME", Path.home() / ".memorypalace"
                        )
                    )
                    / "local-token"
                )
                .read_text()
                .strip()
            )
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
            transport=transport,
        )

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, operation, body=None, *, params=None, files=None, **path_params):
        spec = OPERATIONS[operation]
        path = spec["path"]
        for name, value in path_params.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        if "{" in path:
            raise ValueError("Missing path parameter")
        if hasattr(body, "model_dump"):
            body = body.model_dump(mode="json")
        kwargs = {"params": params}
        if files:
            kwargs.update(files=files, data=body)
        elif body is not None:
            kwargs["json"] = body
        response = self.http.request(spec["method"], path, **kwargs)
        response.raise_for_status()
        return (
            response.json()
            if "json" in response.headers.get("content-type", "")
            else response.content
        )

    def __getattr__(self, name):
        if name not in OPERATIONS:
            raise AttributeError(name)
        return lambda body=None, **kwargs: self.call(name, body, **kwargs)

    def upload(self, path, metadata):
        metadata = (
            metadata.model_dump(mode="json")
            if hasattr(metadata, "model_dump")
            else metadata
        )
        with Path(path).open("rb") as file:
            return self.call(
                "upload_source",
                {"metadata": json.dumps(metadata)},
                files={
                    "file": (
                        Path(path).name,
                        file,
                        metadata.get("media_type", "application/octet-stream"),
                    )
                },
            )


class DeliveryInbox:
    """Durable deduplication for callbacks with a transactional application effect.

    The handler receives the inbox SQLite connection. Write local effects in that
    transaction; external side effects require their own idempotency key.
    """

    def __init__(self, path):
        self.path = str(path)
        # A connection's own `with` ends its transaction but leaves it open; closing() closes
        # it, so an inbox that accepts for months holds no connections behind it (E3-21).
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,body TEXT NOT NULL)"
            )

    def accept(self, delivery, handler):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT body FROM deliveries WHERE id=?", (delivery["id"],)
            ).fetchone()
            if existing:
                return False
            handler(conn, delivery)
            conn.execute(
                "INSERT INTO deliveries VALUES(?,?)",
                (delivery["id"], json.dumps(delivery)),
            )
        return True


__all__ = ["Client", "DeliveryInbox"]


def __getattr__(name):
    """Expose the contract types advertised by the generated client stub."""
    from importlib import import_module

    _types = import_module(__name__ + "._types")
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return getattr(_types, name)
    except AttributeError:
        raise AttributeError(name) from None
