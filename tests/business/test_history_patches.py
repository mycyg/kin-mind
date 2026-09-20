import json

from datetime import datetime, timedelta

import pytest

from test_history_layer import evolve, revert

from eventmem.core.db import Conflict, dumps

from kin_mind import history

from kin_mind.history_admin import dispatch

from kin_mind.memory import MemoryContinuity

from kin_mind.state import AffectiveEvent, DesireChange

pytest_plugins = ("test_kin_mind",)

UNCHANGED_ROW = {"request", "snapshot", "state_hash"}

def patches(mind, on=True):
    MemoryContinuity(mind).configure({"history_patches": on})

def live(mind):
    """The exact text the mind's state is stored as right now."""
    with mind.engine.db.connect() as conn:
        return conn.execute("SELECT data FROM mind_state WHERE scope=?",
                            (mind.scope.key(),)).fetchone()[0]

def drive(mind, source, clock, *, rounds=8, start=0):
    """Revisions the way the host writes them, and the exact state each one left behind.

    The ground truth is taken from `mind_state` as each command commits, because a rebuild
    compared against the row it was rebuilt from proves nothing at all."""
    texts, revision = {}, mind.read()["revision"]
    texts[revision] = live(mind)

    def note(revision):
        texts[revision] = live(mind)
        return revision

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
                command_id="abandon-" + str(index), agent_version="synthetic-v1",
                expected_revision=revision, evidence_ids=[source("reconsidered-" + str(index))],
                action="abandon", desire_id=wish["desire_id"], reason="No longer current"))["revision"])
        if index % 7 == 0:
            revision = note(mind.configure_contact({
                "command_id": "preference-" + str(index), "agent_version": "synthetic-v1",
                "expected_revision": revision, "evidence_ids": [source("owner-preference-" + str(index))],
                "wait_for_reply": bool(index % 2), "reason": "The owner said which they prefer"})["revision"])
    return texts

def rows(mind):
    """revision -> the row's parsed contents, or None where a test has broken one on purpose."""
    def parsed(text):
        try:
            return json.loads(text)
        except ValueError:
            return None

    with mind.engine.db.connect() as conn:
        return {row["revision"]: parsed(row["data"]) for row in conn.execute(
            "SELECT revision,data FROM mind_events WHERE scope=? ORDER BY revision",
            (mind.scope.key(),)).fetchall()}

def rebuilds(mind, texts):
    """Every captured revision, rebuilt from whatever its row turned out to be, byte for byte."""
    with mind.engine.db.connect() as conn:
        for revision, text in texts.items():
            assert history.canonical(history.materialize(conn, mind.scope.key(), revision)) == text

def run(mind, action, request=None):
    return dispatch(mind, action, request or {})

def test_every_revision_rebuilds_byte_identical_when_the_flag_is_turned_on_midway(setup):
    mind, source, clock = setup
    texts = drive(mind, source, clock, rounds=6)
    first_new = max(texts) + 1
    patches(mind)
    texts |= drive(mind, source, clock, rounds=6, start=6)
    stored = rows(mind)
    # The boundary is real: whole documents below it, patches above, and the first patch is
    # computed against a row the previous release wrote.
    assert not any(history.is_patch(data) for revision, data in stored.items() if revision < first_new)
    crossing = min(revision for revision, data in stored.items() if history.is_patch(data))
    assert stored[crossing]["base"] < first_new
    assert set(stored[stored[crossing]["base"]]) == UNCHANGED_ROW
    rebuilds(mind, texts)

def test_the_flag_turned_back_off_reads_both_shapes_and_writes_the_old_one(setup):
    mind, source, clock = setup
    patches(mind)
    texts = drive(mind, source, clock, rounds=4)
    patches(mind, on=False)
    first_old = max(texts) + 1
    texts |= drive(mind, source, clock, rounds=2, start=9)
    stored = rows(mind)
    assert any(history.is_patch(data) for data in stored.values())
    assert all(set(data) == UNCHANGED_ROW for revision, data in stored.items() if revision >= first_old)
    rebuilds(mind, texts)
    view = mind.read(history=100)["history"]
    assert len(view) == len(texts)
    for entry in view:
        assert history.canonical(entry["snapshot"]) == texts[entry["revision"]]
