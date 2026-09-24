from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .db import Conflict, dumps
from .engine import Engine
from .persona import approve_canon

# What a store holds outside its database that nothing rebuilds from it: the persona's
# canonical text, Kin's self-knowledge, the versions of configuration and persona, and the
# archives history compaction moved old events into. A backup carries them; credentials,
# locks, caches and vectors (re-embedded after a restore) stay out (E2-09).
STORE_FILES = ("persona-policy.json", "kin-self-knowledge.json")
STORE_FOLDERS = ("configuration-versions", "persona-versions", "archive")
CHUNK = 1024 * 1024


def file_digest(path):
    """A file's SHA-256, read a chunk at a time: a backup never holds a whole database in
    memory to hash it (S1-13)."""
    found = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            found.update(chunk)
    return found.hexdigest()


def store_files(root):
    """The STORE_FILES and the files under STORE_FOLDERS that exist, as paths relative to the
    root. Links are never followed or copied."""
    root = Path(root)
    found = [name for name in STORE_FILES if (root / name).is_file() and not (root / name).is_symlink()]
    for name in STORE_FOLDERS:
        folder = root / name
        if folder.is_dir() and not folder.is_symlink():
            found += sorted(path.relative_to(root).as_posix() for path in folder.rglob("*")
                            if path.is_file() and not path.is_symlink())
    return found


def store_file_name(name):
    """Whether a backup entry is one of the store's own files."""
    parts = PurePosixPath(name).parts
    return (name in STORE_FILES or (len(parts) >= 2 and parts[0] in STORE_FOLDERS)) and not any(
        part in {"", ".", ".."} for part in parts)


def _referenced_blobs(conn):
    blobs = {
        row[0]
        for row in conn.execute(
            "SELECT blob FROM sources WHERE deleted=0 AND blob IS NOT NULL "
            "UNION SELECT json_extract(data,'$.locator.blob') FROM records WHERE deleted=0 "
            "UNION SELECT json_extract(v.data,'$.locator.blob') FROM revisions v "
            "JOIN records r ON r.id=v.record_id WHERE r.deleted=0"
        )
        if row[0] is not None
    }
    if any(
        not isinstance(key, str)
        or len(key) != 64
        or any(c not in "0123456789abcdef" for c in key)
        for key in blobs
    ):
        raise ValueError("Stored attachment has an invalid digest")
    return blobs


def backup(engine, output: Path):
    output = output.expanduser().resolve()
    if output.exists():
        raise Conflict("Backup output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memorypalace-backup-") as temp:
        snapshot = Path(temp)
        # SQLite's online backup in one step reads one consistent snapshot of the store.
        # Nothing takes the write lock: every other writer carries on meanwhile (S1-13).
        source = sqlite3.connect(engine.db.path, timeout=30)
        target = sqlite3.connect(snapshot / "memory.sqlite3")
        try:
            source.backup(target)
        finally:
            source.close()
        try:
            blobs = _referenced_blobs(target)
        finally:
            target.close()
        (snapshot / "blobs").mkdir()
        for key in blobs:
            blob = engine.db.blobs / key
            if blob.is_symlink() or not blob.is_file():
                raise ValueError(f"Missing stored attachment {key}")
            shutil.copyfile(blob, snapshot / "blobs" / key)
        extras = store_files(engine.db.root)
        for name in extras:
            copy = snapshot / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(engine.db.root / name, copy)
        manifest = {
            p.relative_to(snapshot).as_posix(): file_digest(p)
            for p in snapshot.rglob("*")
            if p.is_file()
        }
        if any(manifest["blobs/" + key] != key for key in blobs):
            raise ValueError("Stored attachment checksum mismatch")
        (snapshot / "manifest.json").write_text(
            dumps({"format": "memorypalace-1", "files": manifest})
        )
        with tarfile.open(output, "w:gz") as archive:
            for path in snapshot.iterdir():
                archive.add(path, arcname=path.name, recursive=True)
    output.chmod(0o600)
    return {
        "path": str(output),
        "files": len(manifest),
        "store_files": len(extras),
        "bytes": output.stat().st_size,
        "indexes": "Rebuilt and re-embedded after a restore",
        "credentials": "Excluded",
    }


def restore(archive_path: Path, target: Path, *, persona_approval=None):
    """Restore a backup into an empty, isolated target.

    A backup that carries a persona canon puts that canon in place for the restored store, so
    it is restored only as `persona_approval` names it: the owner's approval record the host
    keeps (mind-config `persona_contract`: the version and all three hashes). With no record,
    an incomplete one or a canon that differs from it, nothing is restored and the canon is
    left for host review. The canon's version history (persona-versions/) travels as it was."""
    target = target.expanduser()
    if target.is_symlink():
        raise ValueError("Restore target must not be a symlink")
    target = target.resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Conflict("Restore requires an empty isolated target")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".memorypalace-restore-", dir=target.parent))
    try:
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Unsafe archive entry")
                if member.isfile():
                    output = stage / path
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, output.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        manifest = json.loads((stage / "manifest.json").read_text())
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "memorypalace-1"
            or not isinstance(manifest.get("files"), dict)
        ):
            raise ValueError("Unknown backup format")
        if "memory.sqlite3" not in manifest["files"]:
            raise ValueError("Backup has no database")
        for name, expected in manifest["files"].items():
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not (
                    name == "memory.sqlite3"
                    or store_file_name(name)
                    or (
                        len(relative.parts) == 2
                        and relative.parts[0] == "blobs"
                        and len(relative.parts[1]) == 64
                        and all(c in "0123456789abcdef" for c in relative.parts[1])
                    )
                )
            ):
                raise ValueError("Unexpected backup content")
            path = (stage / name).resolve()
            if (
                not path.is_relative_to(stage.resolve())
                or file_digest(path) != expected
            ):
                raise ValueError("Backup checksum mismatch")
        actual = {
            p.relative_to(stage).as_posix()
            for p in stage.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        if actual != set(manifest["files"]):
            raise ValueError("Backup contains unverified files")
        persona = None
        if "persona-policy.json" in manifest["files"]:
            try:
                canon = json.loads((stage / "persona-policy.json").read_text(encoding="utf-8"))
            except ValueError:
                raise ValueError("Persona contract needs host review") from None
            persona = approve_canon(canon, persona_approval)["version"]
        with sqlite3.connect((stage / "memory.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database is corrupt")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"meta", "sources", "records", "revisions", "jobs", "outbox"} <= tables:
                raise ValueError("Unrecognized backup database schema")
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if not version or version[0] != 1:
                raise ValueError("Unsupported backup database schema")
            referenced = _referenced_blobs(conn)
            for key in referenced:
                if manifest["files"].get("blobs/" + key) != key:
                    raise ValueError(f"Backup is missing attachment {key}")
        # Old valid backups may contain orphan blobs; leave the archive intact.
        for name in manifest["files"]:
            if name.startswith("blobs/") and name[6:] not in referenced:
                (stage / name).unlink()
        (stage / "manifest.json").unlink()
        engine = Engine(stage)
        # Every restore is its own: a backup taken from a restored store already holds the
        # previous restore's jobs as complete, and they would not run again under their keys.
        stamp = uuid.uuid4().hex
        with engine.db.connect(write=True) as conn:
            conn.execute(
                "UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE state='running'"
            )
            conn.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
            conn.execute(
                "UPDATE vector_indexes SET data=json_set(data,'$.state','pending')"
            )
            embeddings = requeue_embeddings(engine, conn, stamp)
        engine.enqueue("rebuild", {}, f"restore-rebuild:{stamp}")
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise Conflict("Restore target became nonempty")
            target.rmdir()
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "path": str(target),
        "status": "restored",
        "rebuild": "queued",
        "embeddings": embeddings,
        "contacts": "Prior sending state requires reconciliation",
        "persona_version": persona,
    }


def requeue_embeddings(engine, conn, stamp):
    """The vectors are not in a backup, so every record the index holds is embedded again after
    a restore. Its earlier embed jobs are complete in the restored store and would never run a
    second time under their own keys, which left semantic recall empty for good (E2-09)."""
    queued = 0
    for row in conn.execute("SELECT data FROM records WHERE deleted=0").fetchall():
        data = json.loads(row[0])
        if not engine._indexable(conn, data):
            continue
        payload = {"record_id": data["id"], "revision": data["revision"]}
        engine.enqueue("embed", payload, f"embed:{data['id']}:{data['revision']}:{stamp}", conn=conn)
        if data.get("locator", {}).get("type") in {"image", "keyframe"}:
            engine.enqueue("visual_embed", payload, f"visual-embed:{data['id']}:{data['revision']}:{stamp}",
                           conn=conn)
        queued += 1
    return queued


def export_records(engine, output: Path):
    if output.exists():
        raise Conflict("Export output already exists")
    with engine.db.connect() as conn, output.open("x", encoding="utf-8") as f:
        for row in conn.execute("SELECT data FROM records WHERE deleted=0 ORDER BY id"):
            f.write(row[0] + "\n")
    output.chmod(0o600)
    return {"path": str(output), "format": "jsonl"}
