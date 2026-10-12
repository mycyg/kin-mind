import json

from datetime import datetime, timedelta, timezone

import pytest

from kin_mind import history

from kin_mind.memory import MemoryContinuity

from kin_mind.state import AffectiveEvent, DesireChange, Evolution

pytest_plugins = ("test_kin_mind", "test_autonomous_plans")

def drive(mind, source, clock, *, rounds=50, start=0):
    """Revisions the way the host writes them: observations, wishes, and owner preferences.

    Driven through the public API with an injected clock, so what lands in `mind_events` is the
    real thing and not a fixture's idea of it. Returns the exact state text each revision left."""
    revision, made = mind.read()["revision"], {}

    def note(value):
        with mind.engine.db.connect() as conn:
            made[value] = conn.execute("SELECT data FROM mind_state WHERE scope=?",
                                       (mind.scope.key(),)).fetchone()[0]
        return value

    for index in range(start, start + rounds):
        clock[0] += timedelta(minutes=7)
        revision = note(mind.record(AffectiveEvent(
            command_id="observation-" + str(index), agent_version="synthetic-v1",
            expected_revision=revision, evidence_ids=[source("observed-" + str(index))],
            values={"mood": 40 + index % 50, "curiosity": 30 + index % 60},
            reason="A sourced synthetic observation"))["revision"])
        wish = mind.manage_desire(DesireChange(
            command_id="wish-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
            evidence_ids=[source("wish-source-" + str(index))], action="create",
            content="Share finding " + str(index), topic="synthetic", kind="contact", strength=50,
            expires_at=(datetime.fromisoformat(mind.clock()) + timedelta(days=3)).isoformat(),
            completion="The owner has it", reason="A finding worth discussing"))
        revision = note(wish["revision"])
        if index % 3 == 0:
            revision = note(mind.manage_desire(DesireChange(
                command_id="abandon-" + str(index), agent_version="synthetic-v1", expected_revision=revision,
                evidence_ids=[source("reconsidered-" + str(index))], action="abandon",
                desire_id=wish["desire_id"], reason="No longer current"))["revision"])
        if index % 7 == 0:
            revision = note(mind.configure_contact({
                "command_id": "preference-" + str(index), "agent_version": "synthetic-v1",
                "expected_revision": revision, "evidence_ids": [source("owner-preference-" + str(index))],
                "wait_for_reply": bool(index % 2), "reason": "The owner said which they prefer"})["revision"])
    return made

def both_formats(mind, source, clock, rounds):
    """Whole-state rows first, then patch rows from the production writer once its switch is on."""
    texts = drive(mind, source, clock, rounds=rounds)
    MemoryContinuity(mind).configure({"history_patches": True})
    texts |= drive(mind, source, clock, rounds=rounds, start=rounds)
    with mind.engine.db.connect() as conn:
        shapes = {history.is_patch(json.loads(row[0])) for row in conn.execute(
            "SELECT data FROM mind_events WHERE scope=?", (mind.scope.key(),))}
    assert shapes == {True, False}
    return texts

def evolve(mind, source, clock):
    """One personality evolution, with the prospective check the commit requires. Returns its
    event id, which is what a later reversion names."""
    from eventmem.core.self_knowledge import (
        AssessmentInput,
        ClaimInput,
        PredictionInput,
        SelfKnowledge,
    )
    from kin_mind.compat import stamp as compatibility
    from kin_mind.memory import MemoryContinuity

    MemoryContinuity(mind).configure({"trait_ledger": False})
    knowledge = SelfKnowledge(mind.engine, mind.scope)
    with mind.engine.db.connect() as conn:
        compat = compatibility(mind, conn)

    def record_id(key):
        # The self-knowledge layer keeps its own wall clock, so its evidence has to precede it.
        clock[0] = datetime.now(timezone.utc)
        return mind.engine.source(source(key))["record_ids"][0]

    refs = [record_id("interaction-" + str(n)) for n in range(1, 4)]
    claim = knowledge.claim(ClaimInput(command_id="hypothesis", aspect="curiosity", context="source-checking",
        agent_version="synthetic-v1", claim="I prefer checking a primary source", evidence_ids=refs), compat=compat)
    prediction = knowledge.predict(PredictionInput(command_id="prospective", claim_id=claim["id"],
        expected_revision=1, case_id="future-case", behavior="Check a primary source",
        information="The next query has not been answered", probability=0.8), compat=compat)
    assessment = knowledge.assess(AssessmentInput(command_id="assess", prediction_id=prediction["id"],
        expected_revision=1, outcome=True, evidence_ids=[record_id("later-observed-behavior")],
        note="Observed source check"), compat=compat)
    clock[0] = datetime.now(timezone.utc)
    return mind.record(AffectiveEvent(command_id="evolve", agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=refs,
        reason="A prospective trial with independent interactions",
        evolution=Evolution(claim_id=claim["id"], assessment_id=assessment["id"],
                            baseline_changes={"curiosity": 77}, half_life_changes={"curiosity": 52.8})))

def revert(mind, source, clock, event_id, *, command="revert"):
    clock[0] = datetime.now(timezone.utc)
    return mind.record(AffectiveEvent(command_id=command, agent_version="synthetic-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[source(command + "-correction")],
        reason="Explicit user correction", evolution=Evolution(revert_event_id=event_id)))

def test_read_history_keeps_its_keys_and_its_answer_across_the_two_formats(setup):
    """Each snapshot is the revision as it was stored, as a model may be shown it: its evidence
    references without their sources' metadata (the owner's decision, 2026-09-28; evidence_refs)."""
    from kin_mind.evidence_refs import shown
    mind, source, clock = setup
    texts = both_formats(mind, source, clock, rounds=7)
    view = mind.read(history=30)["history"]
    assert len(view) == 30
    for entry in view:
        assert set(entry) == {"id", "kind", "revision", "occurred_at", "request", "snapshot"}
        assert history.canonical(entry["snapshot"]) == history.canonical(shown(json.loads(texts[entry["revision"]])))
    # The layer itself answers with the stored revision, byte for byte.
    with mind.engine.db.connect() as conn:
        for entry in history.entries(conn, mind.scope.key(), 30):
            assert history.canonical(entry["snapshot"]) == texts[entry["revision"]]

def test_current_history_neither_creates_nor_requires_content_evidence_keys(setup):
    mind, source, clock = setup
    drive(mind, source, clock, rounds=2)
    MemoryContinuity(mind).configure({"history_patches": True})
    for turn in range(2):
        with mind.engine.db.connect() as conn:
            assert "last_evidence_key" not in mind._load(conn)
            assert not conn.execute("SELECT 1 FROM mind_evidence_keys WHERE scope=?", (mind.scope.key(),)).fetchone()
        drive(mind, source, clock, rounds=2, start=2 + 2 * turn)
    with mind.engine.db.connect() as conn:
        latest = conn.execute("SELECT data FROM mind_events WHERE scope=? AND kind='affect' "
                              "ORDER BY revision DESC LIMIT 1", (mind.scope.key(),)).fetchone()[0]
        assert history.is_patch(json.loads(latest))
        data = json.loads(latest)
        assert "state_hash" not in data and "base_hash" not in data
        assert set(data["snapshot"]) == {"revision"}
        assert history.materialize(conn, mind.scope.key(), mind._load(conn)["revision"]) == mind._load(conn)


def test_legacy_checksum_metadata_is_ignored_but_database_parent_structure_is_required(setup):
    from eventmem.core.db import Conflict, dumps
    from kin_mind.history_status import verify
    mind, source, clock = setup
    texts = both_formats(mind, source, clock, rounds=3)
    with mind.engine.db.connect(write=True) as conn:
        for row in conn.execute("SELECT revision,data FROM mind_events WHERE scope=?", (mind.scope.key(),)).fetchall():
            data = json.loads(row["data"])
            data["state_hash"] = "legacy-mismatched-state-hash"
            if history.is_patch(data):
                data["base_hash"] = "legacy-mismatched-parent-hash"
            conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                         (dumps(data), mind.scope.key(), row["revision"]))
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == text
    report = verify(mind)
    assert report["state"] == "readable" and report["failed"] == 0 and report["readable"] == report["rows"]
    assert not {"verified", "unverifiable", "unverifiable_reason"} & report.keys()
    with mind.engine.db.connect(write=True) as conn:
        row = conn.execute("SELECT revision,data FROM mind_events WHERE scope=? ORDER BY revision DESC LIMIT 1", (mind.scope.key(),)).fetchone()
        data = json.loads(row["data"])
        assert history.is_patch(data)
        data["base"] = row["revision"]
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?", (dumps(data), mind.scope.key(), row["revision"]))
    with mind.engine.db.connect() as conn, pytest.raises(Conflict) as failed:
        history.materialize(conn, mind.scope.key(), row["revision"])
    assert failed.value.code == history.REBUILD_FAILED


def test_old_compaction_manifest_hashes_are_compatible_without_becoming_new_content_gates(setup, tmp_path):
    from kin_mind import history_compaction as compaction
    mind, *_ = setup
    with mind.engine.db.connect() as conn:
        manifest = compaction._freeze_manifest(compaction._survey(conn, mind.scope.key(), 0),
                                              mind.scope.key(), 0, at=mind.clock())
    assert set(manifest["source"]) == {"rows", "first", "head"}
    assert all(set(ref) == {"id", "kind", "occurred_at"} for ref in manifest["rows"].values())
    # Existing manifests can carry old content proofs. Only their existing DB row references matter.
    manifest["source"].update(hash="obsolete", structure_hash="obsolete")
    for ref in manifest["rows"].values():
        ref["sha256"] = "obsolete"
    backup = tmp_path / "synthetic-backup.sqlite"
    compaction._take_backup(mind.engine.db, backup)
    checked = compaction._backup_verify(mind.scope.key(), backup, manifest, at=mind.clock())
    assert checked["name"] == backup.name
    assert compaction._structure(compaction._backup_identity(backup, mind.scope.key())) == compaction._structure(manifest["source"])


@pytest.fixture
def maintenance_archive(setup, monkeypatch):
    """The public storage maintenance path against an isolated real SQLite history."""
    import sqlite3
    from kin_mind import history_compaction as compaction
    mind, source, clock = setup
    both_formats(mind, source, clock, rounds=2)
    monkeypatch.setattr(compaction, "_quiet_check",
                        lambda mind, config: {"check": "quiescence", "ready": True, "reason": "synthetic-store-is-quiet"})
    completed = compaction.compact(mind, {}, apply=True)
    archive = compaction.archive_dir(mind.engine.db) / completed["archive"]
    assert compaction.compact_verify(mind)["state"] == "verified"
    with sqlite3.connect(archive) as conn:
        revision, data = conn.execute("SELECT revision,data FROM mind_events_v1 ORDER BY revision DESC LIMIT 1").fetchone()
        assert history.is_patch(json.loads(data)), "The archived original really was a patch"
    return mind, archive, revision


@pytest.mark.parametrize("broken", ['{"broken":', '{}',
    '{"format":2,"base":999999,"patch":[],"snapshot":{"revision":1}}',
    '{"format":2,"base":0,"patch":[],"snapshot":{"revision":1}}',
    '{"format":2,"base":1,"patch":[["invalid",["revision"],2]],"snapshot":{"revision":1}}'])
@pytest.mark.parametrize("deep", [False, True])
def test_archive_verification_reports_unreadable_or_malformed_history_as_corrupt(maintenance_archive, broken, deep):
    import sqlite3
    from kin_mind import history_compaction as compaction
    mind, archive, revision = maintenance_archive
    with sqlite3.connect(archive) as conn:
        conn.execute("UPDATE mind_events_v1 SET data=? WHERE revision=?", (broken, revision))
    checked = compaction.compact_verify(mind, deep=deep)
    assert checked["state"] == "incomplete" and checked["archive_corrupt"] == 1


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("broken", ['{"broken":', '{"snapshot":{"revision":999999,"text":"wrong revision"}}'])
def test_a_valid_archived_patch_requires_the_current_revision_to_be_rebuildable(maintenance_archive, deep, broken):
    from kin_mind import history_compaction as compaction
    mind, _, revision = maintenance_archive
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                     (broken, mind.scope.key(), revision))
    checked = compaction.compact_verify(mind, deep=deep)
    assert checked["state"] == "incomplete" and checked["archive_corrupt"] == 0
    assert revision in checked["unrebuildable"] and checked["unrebuildable_count"] == 1

def test_an_unregistered_history_command_is_refused_by_name():
    from kin_mind import history_admin

    with pytest.raises(ValueError):
        history_admin.dispatch(None, "history-nothing-registered", {})
