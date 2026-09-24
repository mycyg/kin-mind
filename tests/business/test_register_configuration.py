"""A release's configuration declaration is registered as a versioned source: host configuration,
never extracted, never a shared experience and never growth, once per version and content."""
import json

import pytest

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput
from eventmem.core.read_policy import ReadPolicy

from kin_mind import register_configuration as command
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

KIN = Scope(project="personal", persona="Kin", collection="default", world="real")
DECLARATION = {"agent_version": "kin-20260925", "models": {"work": "gpt-6-sol"}, "flags": {"records": True}}


@pytest.fixture
def store(tmp_path):
    engine = Engine(tmp_path / "MemoryPalace")
    first = engine.receive(SourceInput(namespace="kin-owner-input", key="setup", text="我们开始吧", scope=KIN,
                                       authority="explicit", metadata={"role": "user", "host_event": "message"}))
    mind = Mind(engine, KIN)
    mind.initialize(agent_version="fixture-v1", evidence_ids=[engine.source(first["id"])["record_ids"][0]])
    MemoryContinuity(mind).configure({"records": True})
    return engine, mind


def run(engine, tmp_path, declaration, version="kin-20260925", capsys=None, name="declaration.json"):
    path = tmp_path / name
    path.write_text(declaration if isinstance(declaration, str) else json.dumps(declaration, indent=2))
    code = command.main(["--root", str(engine.db.root), "--scope", json.dumps(KIN.model_dump()),
                         "--version", version, "--declaration", str(path)])
    return code, json.loads(capsys.readouterr().out)


def counts(engine):
    with engine.db.connect() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sources", "records", "revisions")}


def test_a_declaration_is_host_configuration_kept_once_per_version_and_content(store, tmp_path, capsys):
    engine, mind = store
    with engine.db.connect() as conn:
        state_before = conn.execute("SELECT revision,data FROM mind_state").fetchall()
        config_before = conn.execute("SELECT data FROM mind_memory_config").fetchall()
    code, first = run(engine, tmp_path, DECLARATION, capsys=capsys)
    assert code == 0 and first["state"] == "registered" and first["version"] == "kin-20260925"
    assert first["sourceId"].startswith("src_") and len(first["sha256"]) == 64
    source = engine.source(first["sourceId"])
    assert source["namespace"] == command.NAMESPACE and source["authority"] == "operation"
    assert source["model"] == "not_requested" and source["version"] == "kin-20260925+" + first["sha256"][:16]
    # What it is: host configuration, shown to the self-knowledge view and to audit, and never
    # recalled as something that happened.
    root = engine.get(source["record_ids"][0])
    with engine.db.connect() as conn:
        row = conn.execute("SELECT class,rule FROM source_evidence_class WHERE source_id=?", (first["sourceId"],)).fetchone()
    assert (row["class"], row["rule"]) == ("role_configuration", "origin-configuration")
    assert ReadPolicy.load(engine, KIN, "experience_recall").refusal(root) == "not_experience:role_configuration"
    assert ReadPolicy.load(engine, KIN, "self_knowledge_view").visible(root)
    # Not growth, and not a settings change: the mind's state and the memory settings are as they were.
    with engine.db.connect() as conn:
        assert conn.execute("SELECT revision,data FROM mind_state").fetchall() == state_before
        assert conn.execute("SELECT data FROM mind_memory_config").fetchall() == config_before
        assert not conn.execute("SELECT 1 FROM jobs WHERE kind IN ('extract','extract_part')").fetchone()

    # The same declaration again, whatever its layout, is the same source.
    before = counts(engine)
    code, again = run(engine, tmp_path, json.dumps(DECLARATION, separators=(",", ":"), sort_keys=False), capsys=capsys)
    assert code == 0 and again == {**first, "state": "unchanged"} and counts(engine) == before

    # A changed declaration under the same agent version is a new version of the source, and the
    # one before it is superseded rather than removed.
    code, changed = run(engine, tmp_path, {**DECLARATION, "flags": {"records": False}}, capsys=capsys)
    assert code == 0 and changed["state"] == "registered" and changed["sourceId"] != first["sourceId"]
    assert changed["sha256"] != first["sha256"]
    assert engine.get(root["id"])["status"] == "superseded"
    assert engine.get(engine.source(changed["sourceId"])["record_ids"][0])["status"] == "active"


def test_what_cannot_be_registered_fails_and_creates_nothing(store, tmp_path, capsys):
    engine, mind = store
    code, report = run(engine, tmp_path, "[1, 2, 3]", capsys=capsys)
    assert code == 1 and report["state"] == "failed" and report["sourceId"] is None and "JSON object" in report["error"]
    code, report = run(engine, tmp_path, "{not json", capsys=capsys)
    assert code == 1 and report["state"] == "failed"
    code, report = run(engine, tmp_path, DECLARATION, version="has spaces", capsys=capsys)
    assert code == 1 and report["state"] == "failed"
    missing = tmp_path / "mistyped"
    path = tmp_path / "declaration.json"
    assert command.main(["--root", str(missing), "--scope", json.dumps(KIN.model_dump()), "--version", "v1",
                         "--declaration", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed" and not missing.exists()
    with engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace=?", (command.NAMESPACE,)).fetchone()
