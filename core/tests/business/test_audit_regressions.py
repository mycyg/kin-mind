"""Regression tests for bugs fixed in the full-repository audit (synthetic fixtures only)."""
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.models import RecordInput, Scope, SourceInput

from kin_mind import play
from kin_mind.actions import ActionEvents
from kin_mind.autonomy_models import PlanStep
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.corrections import correct
from kin_mind.creation import accept_result
from kin_mind.evidence_classes import owner_statement
from kin_mind.mcp import register_mind_tools
from kin_mind.memory import MemoryContinuity
from kin_mind.memory_items import Items
from kin_mind.plans import AutonomousPlans
from kin_mind.private_notes import PrivateNotes
from kin_mind.reinforcement import strengths
from kin_mind.sharing import ShareLedger
from kin_mind.state import Mind


def _setup(tmp_path):
    clock = [datetime.now(timezone.utc)]
    engine = Engine(tmp_path)
    scope = Scope(persona="synthetic")
    mind = Mind(engine, scope, clock=lambda: clock[0].isoformat())

    def receive(key, text, *, speaker="person:person_a", recipient="person:person_a", authority="explicit"):
        return engine.receive(
            SourceInput(
                namespace="test",
                key=key,
                version="1",
                scope=scope,
                text=text,
                authority=authority,
                occurred_at=clock[0].isoformat(),
                metadata={
                    "role": "user",
                    "host_event": "message",
                    "speaker_person_id": speaker,
                    "recipient_person_id": recipient,
                },
            )
        )["id"]

    init_id = receive("configuration", "initial setup")
    mind.initialize(agent_version="synthetic-v1", evidence_ids=[init_id])
    return mind, receive, clock


def test_reinforcement_strengths_chunks_over_sql_variable_limit(tmp_path):
    mind, _, _ = _setup(tmp_path)
    identifiers = [f"mem_{i}" for i in range(35_000)]
    with mind.engine.db.connect() as conn:
        scores = strengths(conn, mind.scope.key(), identifiers, mind.clock())
    assert len(scores) == 35_000
    assert scores["mem_0"]["uses"] == 0
    assert scores["mem_34999"]["strength"] == 0.0


def test_owner_statement_rejects_non_owner_speakers():
    record = {"generated": False, "confirmation": "explicit", "attributes": {"role": "user"}}
    assert owner_statement({**record, "attributes": {"role": "user", "speaker_person_id": "person:person_a"}})
    assert not owner_statement({**record, "attributes": {"role": "user", "speaker_person_id": "person:companion_b"}})
    assert not owner_statement(record, sources=[{"metadata": {"speaker_person_id": "person:companion_b"}}])
    assert owner_statement(record, sources=[{"metadata": {"speaker_person_id": "person:person_a"}}])


def test_mcp_read_affective_state_strips_intimacy_outside_owner_private(tmp_path):
    mind, _, _ = _setup(tmp_path)

    class Server:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register

    server = Server()
    register_mind_tools(server, mind.engine)
    read_tool = server.tools["read_affective_state"]

    owner_view = CompanionView(mind.engine, mind.scope, "person:person_a", "conv-owner")
    companion_view = CompanionView(mind.engine, mind.scope, "person:companion_b", "conv-companion")

    with companion_scope(owner_view):
        owner_state = read_tool(scope=mind.scope)
    with companion_scope(companion_view):
        other_state = read_tool(scope=mind.scope)
    unscoped_state = read_tool(scope=mind.scope)

    assert "flirtation" in owner_state["dimensions"]
    assert "flirtation" not in other_state["dimensions"]
    assert "flirtation" not in unscoped_state["dimensions"]
    assert "interaction_style" not in other_state
    assert "interaction_style" not in unscoped_state


def test_corrections_cascade_from_deleted_memory_without_deleting_user_message(tmp_path):
    mind, receive, _ = _setup(tmp_path)
    note_src = receive("note-src", "Tuesday schedule")
    mem = mind.engine.add_record(
        RecordInput(
            kind="observation",
            content="Meeting is at noon.",
            source_ids=[note_src],
            scope=mind.scope,
        ),
        command_id="add-mem-1",
    )
    correction_src = receive("fix-src", "Actually it moved to 3pm.")
    fixed = correct(mind, mem["id"], "Moved to 3pm.", correction_src)
    assert fixed["state"] == "corrected"

    # Deleting the corrected memory must NOT delete the owner's evidence message.
    mind.engine.delete(mem["id"])
    assert mind.engine.source(correction_src)["id"] == correction_src

    # Recreate and verify deleting the correction evidence DOES cascade to the correction record.
    note_src2 = receive("note-src-2", "Friday dinner")
    mem2 = mind.engine.add_record(
        RecordInput(
            kind="observation",
            content="Dinner on Friday.",
            source_ids=[note_src2],
            scope=mind.scope,
        ),
        command_id="add-mem-2",
    )
    correction_src2 = receive("fix-src-2", "No, moved to Saturday.")
    correct(mind, mem2["id"], "Moved to Saturday.", correction_src2)
    mind.engine.delete(correction_src2)
    with pytest.raises(Missing):
        mind.engine.get(mem2["id"])


def test_private_notes_reject_tombstoned_sources_and_hide_internal_provenance(tmp_path):
    mind, receive, _ = _setup(tmp_path)
    src = receive("private-src", "Something private")
    notes = PrivateNotes(mind)
    owner_view = CompanionView(mind.engine, mind.scope, "person:person_a", "conv-owner")

    with companion_scope(owner_view):
        saved = notes.save({
            "id": "pnote_1",
            "group_id": "deliv_1",
            "text": "Felt quietly happy about that.",
            "source_ids": [src],
            "provider": "synthetic-provider",
            "model": "synthetic-model",
            "session": "sess_1",
            "conversation_key": "conv-owner",
        })
        assert saved["state"] == "saved"
        loaded = notes.read(semantic=False)

    entry = loaded["notes"][0]
    for hidden in ("rests_on", "provider", "model", "session", "conversation_key"):
        assert hidden not in entry

    # Once source is tombstoned/deleted, a new private note citing it must be rejected.
    mind.engine.delete(src)
    with companion_scope(owner_view):
        with pytest.raises(Conflict):
            notes.save({
                "id": "pnote_2",
                "group_id": "deliv_2",
                "text": "Trying deleted evidence.",
                "source_ids": [src],
            })


def test_share_ledger_normalizes_owner_recipient_and_scopes_cancel(tmp_path):
    mind, receive, _ = _setup(tmp_path)
    ledger = ShareLedger(mind)
    src = receive("finding-src", "Interesting theorem")
    finding_id = "graph_finding_synthetic"
    now = mind.clock()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO mind_graph_nodes VALUES(?,?,?,?,?,?,?,?)",
            (finding_id, mind.scope.key(), "finding", 1, now, now, "active",
             dumps({"id": finding_id, "kind": "finding", "owner_id": "exp_1", "content_version": 1, "source_ids": [src]})),
        )
        ledger._save_coverage(conn, {
            "recipient": "owner",
            "unit_id": finding_id,
            "version": 1,
            "bubble_id": "b1",
            "share_id": "share_1",
            "state": "accepted",
            "message_id": "msg_1",
            "at": now,
        })
        assert ledger.coverage(conn, finding_id, recipient="person:person_a")["state"] == "shared"
        assert ledger.coverage(conn, finding_id, recipient="owner")["state"] == "shared"

        conn.execute(
            "INSERT INTO mind_share_reservations VALUES(?,?,?,?,?,?,?)",
            (mind.scope.key(), "owner", finding_id, 1, "draft_owner", "prepared", dumps({"draft_ids": ["draft_owner"]})),
        )
        conn.execute(
            "INSERT INTO mind_share_reservations VALUES(?,?,?,?,?,?,?)",
            (mind.scope.key(), "person:companion_b", finding_id, 1, "draft_other", "prepared", dumps({"draft_ids": ["draft_other"]})),
        )

    ledger.cancel("draft_owner")
    with mind.engine.db.connect() as conn:
        owner_state = conn.execute(
            "SELECT state FROM mind_share_reservations WHERE scope=? AND recipient='owner' AND unit_id=?",
            (mind.scope.key(), finding_id),
        ).fetchone()[0]
        other_state = conn.execute(
            "SELECT state FROM mind_share_reservations WHERE scope=? AND recipient='person:companion_b' AND unit_id=?",
            (mind.scope.key(), finding_id),
        ).fetchone()[0]
    assert owner_state == "canceled"
    assert other_state == "prepared"


def test_action_events_reset_needs_review_when_payload_changes(tmp_path):
    mind, _, _ = _setup(tmp_path)
    events = ActionEvents(mind)
    with mind.engine.db.connect(write=True) as conn:
        events.emit(conn, "plan-review", "plan_1:1", {"step": 1, "note": "initial"})
        action_id = conn.execute("SELECT id FROM mind_action_events WHERE kind='plan-review'").fetchone()[0]
        conn.execute("UPDATE mind_action_events SET state='needs-review' WHERE id=?", (action_id,))

        # Identical payload keeps needs-review when job reference is not missing.
        events.emit(conn, "plan-review", "plan_1:1", {"step": 1, "note": "initial"})
        assert conn.execute("SELECT state FROM mind_action_events WHERE id=?", (action_id,)).fetchone()[0] == "needs-review"

        # Changed payload re-queues to pending.
        events.emit(conn, "plan-review", "plan_1:1", {"step": 1, "note": "rescheduled"})
        assert conn.execute("SELECT state FROM mind_action_events WHERE id=?", (action_id,)).fetchone()[0] == "pending"


def test_memory_items_drops_classified_missing_without_hiding_unknown_target(tmp_path):
    mind, _, _ = _setup(tmp_path)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _item_probe(id TEXT PRIMARY KEY)")
        items = Items(conn)

        def apply():
            with items.item("notes", "bad_item") as apply_bad:
                if apply_bad:
                    raise Missing("Sharing source protected")
            with items.item("notes", "good_item") as apply_good:
                if apply_good:
                    conn.execute("INSERT INTO _item_probe VALUES('ok')")

        items.run(apply)
        rows = conn.execute("SELECT id FROM _item_probe").fetchall()
    assert [r[0] for r in rows] == ["ok"]
    assert items.dropped["bad_item"]["code"] == "sharing-source-protected"

    with mind.engine.db.connect(write=True) as conn:
        unknown_items = Items(conn)
        with pytest.raises(Missing):
            def apply_unknown():
                with unknown_items.item("notes", "bad_person") as go:
                    if go:
                        raise Missing("person:person_a")
            unknown_items.run(apply_unknown)


def test_create_step_guidance_and_empty_artifacts_reason(tmp_path):
    assert "文件" in PlanStep.model_json_schema()["properties"]["actor"]["description"]
    assert "存成工作区文件" in play.PROMPT

    mind, receive, _ = _setup(tmp_path)
    MemoryContinuity(mind).configure({
        "records": True,
        "semantic_actions": True,
        "autonomous_plans": True,
        "creative_execution": True,
    })
    ev_src = receive("sketch-idea", "Draw a synthetic text sketch")
    plans = AutonomousPlans(mind)
    plan = plans.manage({
        "command_id": "create:sketch",
        "action": "create",
        "key": "synthetic-sketch",
        "goal": "Create a small text sketch",
        "motivation": "Synthetic creative test",
        "reason": "Sourced idea",
        "evidence_ids": [ev_src],
        "steps": [{
            "id": "make",
            "actor": "create",
            "goal": "Write sketch.txt in workspace",
            "completion": "Non-empty file created",
        }],
    })
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [ev_src])
        plans.decide(
            conn,
            {
                "plan_id": plan["id"],
                "step_id": "make",
                "expected_revision": plan["revision"],
                "action": "execute",
                "reason": "Execute now",
                "evidence_ids": [ev_src],
            },
            "decision:exec:1",
            {"provider": "deepseek", "reasoning": "high", "agent_version": "synthetic-v1"},
            refs,
        )
    claimed = plans.claim("create", "worker")
    assert claimed["state"] == "claimed"
    run = claimed["run"]
    settled = accept_result(
        mind,
        {"creation_directory": str(tmp_path), "agent_version": "synthetic-v1"},
        {
            "run_id": run["id"],
            "owner": "worker",
            "fence": run["fence"],
            "result": {"state": "failed", "reason": "empty-artifacts", "receipt": {"workspace": str(tmp_path)}},
        },
    )
    assert settled["state"] == "failed"
    assert settled["result"]["reason"] == "empty-artifacts"
