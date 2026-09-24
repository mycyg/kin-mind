"""A backup is the whole store and a restore brings all of it back (E2-09, S1-13): the persona's
canonical text, configuration versions and history archives travel with the database, every
record is embedded again, and a restore of a restored store still rebuilds. The canon is put in
place as the owner's approval record names it (test_persona_approval.py)."""
import hashlib
import json
import sqlite3
import tarfile
import threading

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from eventmem.core.transfer import STORE_FOLDERS, backup, restore


def receive(engine, key, text):
    return engine.receive(SourceInput(namespace="kin-owner-input", key=key, text=text, scope=Scope(),
                                      authority="explicit", extract=False,
                                      metadata={"role": "user", "host_event": "message"}))["id"]


TEXTS = {"core": "【MY_PERSONA_LOAD】合成角色。【/MY_PERSONA_LOAD】", "voice": "合成的说话方式。", "maintenance": "合成的维护约定。"}
HASHES = {k + "_sha256": hashlib.sha256(v.encode()).hexdigest() for k, v in TEXTS.items()}
CANON = json.dumps({"schema": 1, "version": "persona-v3", "scope": Scope().model_dump(), "requires_owner_confirmation": True,
                    "approved_source": "src_" + "a" * 32, "mutable_trait_keys": [], **TEXTS, **HASHES}, ensure_ascii=False)
APPROVED = {"version": "persona-v3", **HASHES}  # the host's record of the owner's confirmation


def fill(root):
    (root / "persona-policy.json").write_text(CANON)
    (root / "configuration-versions").mkdir()
    (root / "configuration-versions" / "v1.json").write_text('{"synthetic": 1}')
    (root / "archive").mkdir()
    with sqlite3.connect(root / "archive" / "mind-events-v1-synthetic.sqlite3") as conn:
        conn.execute("CREATE TABLE archived(id TEXT)")
    (root / "local-token").write_text("never-in-a-backup")


def test_a_backup_carries_the_store_files_and_a_restore_embeds_again(tmp_path):
    engine = Engine(tmp_path / "live")
    ids = [receive(engine, f"k{i}", f"synthetic memory {i}") for i in range(3)]
    fill(engine.db.root)
    archive = tmp_path / "first.tar.gz"
    report = backup(engine, archive)
    assert report["store_files"] == 3
    with tarfile.open(archive) as saved:
        names = set(saved.getnames())
    assert {"persona-policy.json", "configuration-versions/v1.json",
            "archive/mind-events-v1-synthetic.sqlite3"} <= names
    assert "local-token" not in names and not any(n.startswith(("vectors", "cache")) for n in names)
    assert set(STORE_FOLDERS) >= {"archive", "configuration-versions"}

    restored = restore(archive, tmp_path / "second", persona_approval=APPROVED)
    assert restored["embeddings"] == 3
    second = Engine(tmp_path / "second")
    assert (tmp_path / "second" / "persona-policy.json").read_text() == CANON
    assert (tmp_path / "second" / "archive" / "mind-events-v1-synthetic.sqlite3").is_file()
    with second.db.connect() as conn:
        pending = {json.loads(row[0])["record_id"] for row in conn.execute(
            "SELECT payload FROM jobs WHERE kind='embed' AND state='pending'")}
        records = {row[0] for row in conn.execute("SELECT id FROM records WHERE deleted=0")}
        rebuilds = conn.execute("SELECT unique_key FROM jobs WHERE kind='rebuild'").fetchall()
    assert pending == records and len(records) == 3 and len(rebuilds) == 1

    # A backup of the restored store, restored again: its first restore's jobs are complete
    # in it, and the second restore still rebuilds and embeds.
    with second.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='complete'")
    again = tmp_path / "again.tar.gz"
    backup(second, again)
    assert restore(again, tmp_path / "third", persona_approval=APPROVED)["embeddings"] == 3
    with Engine(tmp_path / "third").db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='rebuild' AND state='pending'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'").fetchone()[0] == 3
    assert ids


class Copying:
    """The backup's source connection: before it copies, another writer takes the write lock."""

    def __init__(self, conn, write):
        self.conn, self.write = conn, write

    def backup(self, target, *args, **kwargs):
        writer = threading.Thread(target=self.write)
        writer.start()
        writer.join(20)
        return self.conn.backup(target, *args, **kwargs)

    def close(self):
        self.conn.close()


def test_a_backup_does_not_hold_the_write_lock(tmp_path, monkeypatch):
    """Writers carry on while the database is copied (S1-13)."""
    from eventmem.core import transfer

    engine = Engine(tmp_path / "live")
    receive(engine, "first", "before the backup")
    written, original, first = [], sqlite3.connect, [True]

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        if first.pop() if first else False:
            return Copying(conn, lambda: written.append(receive(Engine(tmp_path / "live"), "during", "x")))
        return conn

    monkeypatch.setattr(transfer.sqlite3, "connect", connect)
    backup(engine, tmp_path / "copy.tar.gz")
    assert written
