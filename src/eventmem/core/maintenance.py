from __future__ import annotations

from pathlib import Path

from .db import Missing, digest
from .engine import uid


def erase_set(conn, object_id):
    """What deleting `object_id` erases, as (record ids, source ids). The delete and its preview
    both ask here, so the set a console shows is the set that goes.

    A source takes every record that cites it, and each record takes what was built on it —
    records that name it as evidence and its parts — and so on down. A record takes its own
    raw sources too, because their bytes hold the same words. What a record does not take is a
    source it cites only through the records it was written from: a narrative or a summary cites
    the sources of the conversations it summarised, and deleting the summary is not deleting
    the owner's messages it was written about."""
    if object_id.startswith("src_"):
        sources, pending = {object_id}, {object_id}
    else:
        own = {r[0] for r in conn.execute("SELECT source_id FROM evidence WHERE record_id=?", (object_id,))}
        cited = {r[0] for r in conn.execute(
            "SELECT e.source_id FROM dependencies d JOIN evidence e ON e.record_id=d.evidence_id"
            " WHERE d.record_id=?", (object_id,))}
        sources, pending = own - cited, {object_id}
    for sid in sources:
        pending.update(r[0] for r in conn.execute("SELECT record_id FROM evidence WHERE source_id=?", (sid,)))
    records = set()
    while pending:
        current = pending.pop()
        if current in records:
            continue
        records.add(current)
        pending.update(r[0] for r in conn.execute(
            "SELECT record_id FROM dependencies WHERE evidence_id=?", (current,)))
        pending.update(r[0] for r in conn.execute("SELECT id FROM records WHERE parent_id=?", (current,)))
    return records, sources


def deletion_preview(engine, object_id):
    with engine.db.connect() as conn:
        closure, sources = erase_set(conn, object_id)
        records = {rid for rid in closure
                   if conn.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone()}
        sources = {sid for sid in sources
                   if conn.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()}
        if not records and not sources:
            raise Missing(object_id)
    return {
        "id": object_id,
        "record_count": len(records),
        "source_count": len(sources),
        "record_ids": sorted(records)[:100],
        "source_ids": sorted(sources),
        "effect": "Erase original sources and dependent records, revisions, indexes, caches, the mind's derived text and state history, and attachments. A derived summary is erased without the sources it cites. Existing external backups and history archives keep their copies; a history restore takes erased words out again.",
    }


def snapshot_url(engine, url, scope, title=""):
    import ipaddress
    import socket
    from urllib.parse import urlparse
    import httpx
    from .models import SourceInput

    current = url
    for _ in range(5):
        parsed = urlparse(current)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use an HTTP(S) URL without credentials")
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if any(
            not ipaddress.ip_address(address[4][0]).is_global for address in addresses
        ):
            raise ValueError(
                "Web imports require a public network address; upload private documents directly"
            )
        # Pin the validated address for this request so a second DNS resolution
        # cannot redirect a public import to a private service.
        pinned = httpx.URL(current).copy_with(host=addresses[0][4][0])
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "GET",
                pinned,
                headers={"Host": parsed.netloc},
                extensions={"sni_hostname": parsed.hostname},
            ) as response:
                if response.is_redirect:
                    from urllib.parse import urljoin

                    current = urljoin(current, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("Web snapshot exceeds 32 MiB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                mime = response.headers.get("content-type", "text/html").split(";")[0]
        return engine.receive(
            SourceInput(
                namespace="web",
                key=url,
                version=digest(raw)[:20],
                scope=scope,
                title=title or url,
                origin=url,
                media_type=mime,
                authority="document",
            ),
            raw,
        )
    raise ValueError("Too many redirects")


def make_download(engine, kind):
    from .transfer import backup, export_records

    directory = engine.db.root / "exports"
    directory.mkdir(exist_ok=True, mode=0o700)
    name = uid(kind) + (".tar.gz" if kind == "backup" else ".jsonl")
    output = directory / name
    result = (
        backup(engine, output) if kind == "backup" else export_records(engine, output)
    )
    return {
        "id": name,
        "status": "ready",
        "download_url": "/v1/maintenance/files/" + name,
        "details": result,
    }


def download_path(engine, name):
    if Path(name).name != name or not (
        name.startswith("backup_") or name.startswith("export_")
    ):
        raise Missing(name)
    path = engine.db.root / "exports" / name
    if not path.is_file():
        raise Missing(name)
    return path
