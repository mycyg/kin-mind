"""Synthetic behavior, failure injection and migration tests for continuity."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
import test_kin_mind as shared
from test_kin_mind import FakeReviewer, event, wish

from eventmem.core.db import Conflict
from eventmem.core.models import RecordInput, SourceInput
from kin_mind.appraisal import (
    Appraisal,
    Appraisals,
    DeepSeek,
    Wish,
    WishUpdate,
    appraisal_context,
)
from kin_mind.continuity import (
    ConcernChange,
    ConcernProposal,
    ContinuityConfig,
    RhythmProposal,
    Understanding,
)
from kin_mind.expression import TENDENCIES
from kin_mind.profile import DIMENSIONS
from kin_mind.state import DesireChange, Mind


@pytest.fixture
def setup(tmp_path):
    return shared.setup.__wrapped__(tmp_path)


@pytest.fixture
def enabled(setup):
    mind, source, clock = setup
    mind.configure_continuity(
        ContinuityConfig(
            command_id="enable",
            agent_version="continuity-test-v1",
            expected_revision=1,
            evidence_ids=[source("enable")],
            features={
                k: True for k in ("interpretation", "concerns", "expression", "rhythm")
            },
            reason="The synthetic owner enables continuity",
        )
    )
    return mind, source, clock


def concern(mind, source, key="interview", **changes):
    values = {
        "action": "create",
        "key": key,
        "kind": "care",
        "content": "The owner mentioned an interview",
        "topic": "interview",
        "intensity": 70,
        "basis": "inferred",
        "confidence": 0.9,
        "reason": "Remember the pending interview",
    }
    values.update(changes)
    return mind.manage_concern(
        ConcernChange(
            command_id="concern-" + key,
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source(key)],
            **values,
        )
    )


def update(mind, source, cid, key, action, **extra):
    return mind.manage_concern(
        ConcernChange(
            command_id=key,
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source(key)],
            concern_id=cid,
            action=action,
            reason="A new sourced assessment",
            **extra,
        )
    )


def owner(
    mind,
    clock,
    key,
    *,
    namespace="kin-owner-input",
    authority="explicit",
    role="user",
    event="message",
):
    return mind.engine.receive(
        SourceInput(
            namespace=namespace,
            key=key,
            text="Synthetic interaction",
            scope=mind.scope,
            authority=authority,
            occurred_at=clock[0].isoformat(),
            metadata={"role": role, "host_event": event},
        )
    )["id"]


def test_disabled_legacy_state_preserves_defaults_and_tool_compatibility(setup):
    mind, source, _ = setup
    before = mind.read()
    assert not any(before["continuity"]["features"].values())
    assert before["expression"] is None and before["rhythm"]["status"] == "disabled"
    with pytest.raises(Conflict, match="disabled"):
        concern(mind, source)
    assert mind.read()["revision"] == 1
    assert set(TENDENCIES) == set(DIMENSIONS)


def test_concern_survives_sent_wish_then_resolves_with_new_evidence(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source)["concern_id"]
    wish(mind, source, concern_ids=[cid])
    mind.record(event(mind, source, "contact-drive", {"initiative": 90}))
    attempt = mind.claim_contact(owner_epoch="owner")
    mind.settle_contact(attempt_id=attempt["id"], state="pending")
    mind.settle_contact(
        attempt_id=attempt["id"], state="accepted", message_id="synthetic-delivery"
    )
    view = mind.read()
    assert view["desires"][0]["status"] == "completed"
    assert view["concerns"][0]["status"] == "active"
    update(
        mind, source, cid, "interview-result", "resolve", basis="explicit", confidence=1
    )
    view = mind.read(history=10)
    assert view["concerns"][0]["status"] == "resolved"
    assert view["concerns"][0]["intensity"] == 0
    assert any(h["kind"] == "concern" for h in view["history"])
    update(mind, source, cid, "another-interview", "reopen", intensity=60)
    assert mind.read()["concerns"][0]["recurrence_count"] == 1


def test_silence_eases_without_resolving_or_rewriting_clock(enabled):
    mind, source, clock = enabled
    concern(mind, source)
    original = mind.read()["concerns"][0]
    for _ in range(60):
        clock[0] += timedelta(hours=1)
        mind.read()
    view = mind.read()["concerns"][0]
    assert view["status"] == "easing" and view["intensity"] == 35
    assert view["updated_at"] == original["updated_at"]
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    assert restarted.read()["concerns"] == mind.read()["concerns"]


def test_source_replay_persists_beyond_display_window_and_summary(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source)["concern_id"]
    original = source("interview")
    for i in range(30):
        update(mind, source, cid, f"evidence-{i}", "update", intensity=70)
    derived = mind.engine.add_record(
        RecordInput(
            scope=mind.scope,
            kind="summary",
            content="Summary of the interview",
            source_ids=[original],
            valid_from=mind.clock(),
        ),
        command_id="summary",
    )
    identifier = derived["id"]
    result = mind.manage_concern(
        ConcernChange(
            command_id="old-summary-replay",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[identifier],
            concern_id=cid,
            action="update",
            intensity=100,
            reason="Replayed summary",
        )
    )
    assert result["replayed"]
    assert mind.read()["concerns"][0]["intensity"] == 70
    with mind.engine.db.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM mind_concern_evidence").fetchone()[0]
            == 31
        )


def test_same_activation_evidence_cannot_resolve_a_concern(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source)["concern_id"]
    replay = mind.manage_concern(
        ConcernChange(
            command_id="resolve-without-result",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source("interview")],
            concern_id=cid,
            action="resolve",
            reason="No new result",
        )
    )
    assert replay["replayed"] and mind.read()["concerns"][0]["status"] == "active"


def test_uncertain_interpretation_and_corrected_source_leave_review_markers(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source, confidence=0.3)["concern_id"]
    assert mind.read()["concerns"][0]["needs_review"]
    assert mind.read()["selected_concerns"] == []
    with pytest.raises(Conflict, match="review"):
        wish(mind, source, concern_ids=[cid])
    update(mind, source, cid, "clarification", "update", basis="explicit", confidence=1)
    fingerprint = mind.read()["expression"]["fingerprint"]
    source("clarification", "Corrected statement", version="2")
    view = mind.read()
    assert view["concerns"][0]["needs_review"]
    assert view["selected_concerns"] == []
    assert view["expression"]["fingerprint"] != fingerprint


def test_deleted_source_invalidates_compiled_guidance(enabled):
    mind, source, _ = enabled
    mind.record(event(mind, source, "flirty", {"flirtation": 98, "focus": 95}))
    before = mind.read()["expression"]
    assert "focused-flirt" in [g["id"] for g in before["guidance"]]
    sid = source("flirty")
    # The public deletion transaction is covered by store tests; simulate its source tombstone here.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (sid,))
    after = mind.read()["expression"]
    assert after["fingerprint"] != before["fingerprint"]
    assert "focused-flirt" not in [g["id"] for g in after["guidance"]]


@pytest.mark.parametrize(
    "scores,expected",
    [
        ({"longing": 92, "playfulness": 90}, "longing-play"),
        ({"closeness": 90, "mood": 15}, "quiet-closeness"),
        ({"flirtation": 95, "focus": 95}, "focused-flirt"),
        ({"curiosity": 95, "solitude": 90}, "private-curiosity"),
    ],
)
def test_mixed_expression_changes_without_extra_model_call(enabled, scores, expected):
    mind, source, _ = enabled
    before = mind.read()["expression"]["fingerprint"]
    mind.record(event(mind, source, expected, scores))
    view = mind.read()
    assert expected in [g["id"] for g in view["expression"]["guidance"]]
    assert len(view["expression"]["guidance"]) <= 3
    assert view["expression"]["fingerprint"] != before
    assert all(
        g["evidence_ids"] for g in view["expression"]["guidance"] if g["dimensions"]
    )
    assert view["appraisal_summary"]["changes"][next(iter(scores))]["proposed"] == next(
        iter(scores.values())
    )


def test_rhythm_groups_real_interactions_and_ignores_maintenance(enabled):
    mind, _, clock = enabled
    assert mind.read()["rhythm"]["phase"] == "forming"
    owner(mind, clock, "first")
    for i in range(10):
        clock[0] += timedelta(minutes=1)
        owner(mind, clock, f"bubble-{i}")
    clock[0] += timedelta(minutes=31)
    owner(mind, clock, "second", namespace="host:codex")
    owner(mind, clock, "maintenance", event="tool")
    owner(mind, clock, "assistant", authority="model", role="assistant")
    owner(mind, clock, "internal", namespace="kin-internal-thought")
    view = mind.read()["rhythm"]["interactions"]
    assert view["window_count"] == 2 and sum(view["hourly_window_starts"]) == 2
    assert view["sample_status"] == "forming"


def test_rhythm_has_no_fixed_clock_and_true_owner_input_rouses(enabled):
    mind, source, clock = enabled
    original = event(mind, source, "rest", {})
    mind.record(
        original.model_copy(
            update={
                "rhythm": RhythmProposal(
                    phase="resting",
                    alertness=10,
                    target=80,
                    half_life_minutes=20,
                    reason="A temporary pause in interaction",
                )
            }
        )
    )
    base = mind.read()
    assert base["rhythm"]["phase"] == "resting"
    clock[0] += timedelta(minutes=1)
    owner(
        mind,
        clock,
        "receipt",
        event="assistant-result",
        authority="model",
        role="assistant",
    )
    assert mind.read()["rhythm"]["phase"] == "resting"
    owner(mind, clock, "owner-returned")
    assert mind.read()["rhythm"]["phase"] == "roused"
    clock[0] += timedelta(minutes=21)
    assert mind.read()["rhythm"]["phase"] == "recovering"
    assert mind.read()["contact"] == base["contact"]
    assert (
        Mind(mind.engine, mind.scope, clock=mind.clock).read()["rhythm"]
        == mind.read()["rhythm"]
    )


def test_one_appraisal_commits_understanding_concern_rhythm_and_wish(enabled):
    mind, source, _ = enabled
    sid = source("one-input")
    jobs = Appraisals(mind)
    jobs.enqueue([sid], "continuity-test-v1")
    proposal = Appraisal(
        reason="One joint assessment",
        values={"initiative": 85},
        understanding=Understanding(
            meaning="The owner has an upcoming interview",
            topic="interview",
            importance=80,
            confidence=0.9,
            basis="explicit",
        ),
        concerns=[
            ConcernProposal(
                action="create",
                key="interview",
                kind="care",
                content="An upcoming interview",
                topic="interview",
                intensity=75,
                basis="explicit",
                confidence=1,
                reason="A pending event",
            )
        ],
        rhythm=RhythmProposal(
            phase="awake",
            alertness=80,
            target=65,
            half_life_minutes=60,
            reason="Engaged conversation",
        ),
        wishes=[
            Wish(
                content="Ask about the result",
                topic="interview",
                kind="contact",
                strength=90,
                ttl_hours=24,
                completion="Message accepted",
                concern_ids=["interview"],
            )
        ],
    )
    reviewer = FakeReviewer(proposal)
    revision = mind.read()["revision"]
    assert jobs.run_one(reviewer)["state"] == "complete"
    view = mind.read(history=1)
    assert reviewer.calls == 1 and view["revision"] == revision + 1
    assert view["desires"][0]["concern_ids"] == [view["concerns"][0]["id"]]
    assert view["appraisal_summary"]["understanding"]["basis"] == "explicit"
    assert view["rhythm"]["phase"] == "awake"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',lease=0")
    assert jobs.run_one(reviewer)["state"] == "complete" and reviewer.calls == 1


def test_invalid_link_rolls_back_entire_appraisal_and_audit(enabled):
    mind, source, _ = enabled
    jobs = Appraisals(mind)
    jobs.enqueue([source("input")], "continuity-test-v1")
    reviewer = FakeReviewer(
        Appraisal(
            reason="Invalid proposed link",
            values={"mood": 95},
            concerns=[
                ConcernProposal(
                    action="create",
                    key="idea",
                    kind="curiosity",
                    content="A question",
                    topic="idea",
                    intensity=80,
                    basis="internal_thought",
                    confidence=1,
                    reason="A thought",
                )
            ],
            wishes=[
                Wish(
                    content="A wish",
                    topic="idea",
                    kind="contact",
                    strength=80,
                    ttl_hours=24,
                    completion="Sent",
                    concern_ids=["foreign-id"],
                )
            ],
        )
    )
    before = mind.read(history=10)
    assert jobs.run_one(reviewer)["state"] == "pending"
    after = mind.read(history=10)
    assert after["revision"] == before["revision"] and after["concerns"] == []
    assert (
        after["dimensions"] == before["dimensions"]
        and after["history"] == before["history"]
    )
    with mind.engine.db.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM mind_concern_evidence").fetchone()[0]
            == 0
        )


def test_history_write_failure_rolls_back_concern(monkeypatch, enabled):
    mind, source, _ = enabled
    before = mind.read()
    monkeypatch.setattr(
        mind,
        "_history",
        lambda *args: (_ for _ in ()).throw(OSError("synthetic audit failure")),
    )
    with pytest.raises(OSError):
        concern(mind, source)
    assert mind.read()["revision"] == before["revision"]
    assert mind.read()["concerns"] == []


def test_concurrent_config_change_uses_revision_and_command_id(enabled):
    mind, source, _ = enabled
    request = ContinuityConfig(
        command_id="parallel",
        agent_version="continuity-test-v1",
        expected_revision=mind.read()["revision"],
        evidence_ids=[source("parallel")],
        features={"expression": False},
        reason="One owner change",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: mind.configure_continuity(request), range(2)))
    assert results[0] == results[1]
    assert mind.read()["expression"] is None
    with pytest.raises(Conflict):
        mind.configure_continuity(request.model_copy(update={"command_id": "stale"}))


def test_concern_changes_invalidate_pending_wish_until_reassessed(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source)["concern_id"]
    did = wish(mind, source, concern_ids=[cid])["desire_id"]
    mind.record(event(mind, source, "drive", {"initiative": 90}))
    assert mind.contact_candidate()["eligible"]
    update(mind, source, cid, "new-detail", "update", intensity=55)
    assert mind.read()["desires"][0]["concern_needs_review"]
    assert not mind.contact_candidate()["eligible"]
    mind.manage_desire(
        DesireChange(
            command_id="reassess",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source("reassess")],
            desire_id=did,
            action="resume",
            reason="The planned question is still relevant",
        )
    )
    assert mind.contact_candidate()["eligible"]


def test_migration_links_live_wishes_without_resetting_them(enabled):
    mind, source, _ = enabled
    did = wish(mind, source)["desire_id"]
    mind.manage_desire(
        DesireChange(
            command_id="wait",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source("wait")],
            desire_id=did,
            action="wait",
            wait_condition="time",
            retry_after_seconds=1800,
            reason="Later",
        )
    )
    before = mind.read()
    jobs = Appraisals(mind)
    src = source("migration")
    first = jobs.migrate_continuity([src], "continuity-test-v1")
    assert jobs.migrate_continuity([src], "continuity-test-v1")["id"] == first["id"]
    reviewer = FakeReviewer(
        Appraisal(
            reason="Link only",
            values={"mood": 100},
            concerns=[
                ConcernProposal(
                    action="create",
                    key="live",
                    kind="curiosity",
                    content="Remember the question",
                    topic="synthetic",
                    intensity=65,
                    basis="inferred",
                    confidence=0.9,
                    reason="A live wish",
                )
            ],
            wish_updates=[
                WishUpdate(
                    desire_id=did,
                    action="link",
                    concern_ids=["live"],
                    reason="Link the concern",
                )
            ],
        )
    )
    assert jobs.run_one(reviewer)["state"] == "complete"
    after = mind.read()
    assert after["dimensions"] == before["dimensions"]
    for key in (
        "status",
        "content",
        "expires_at",
        "contact_wait",
        "evidence",
        "reason",
    ):
        assert after["desires"][0][key] == before["desires"][0][key]
    assert len(after["desires"][0]["concern_ids"]) == 1


def test_shadow_and_independent_flags_preserve_private_records(enabled):
    mind, source, _ = enabled
    cid = concern(mind, source)["concern_id"]
    for i, activation in enumerate(("shadow", "active")):
        mind.configure_continuity(
            ContinuityConfig(
                command_id=f"activate-{i}",
                agent_version="continuity-test-v1",
                expected_revision=mind.read()["revision"],
                evidence_ids=[source(f"activation-{i}")],
                features={},
                activation=activation,
                reason="Switch activation",
            )
        )
        assert (mind.read()["expression"] is None) == (activation == "shadow")
    mind.configure_continuity(
        ContinuityConfig(
            command_id="disable",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[source("disable")],
            features={"concerns": False},
            reason="Disable only concern context",
        )
    )
    assert mind.read()["concerns"] == []
    assert mind.read()["expression"] is not None
    with mind.engine.db.connect() as conn:
        assert cid in mind._load(conn)["concerns"]


def test_private_reasoning_never_enters_continuity_result(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-only")
    seen = []

    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "SYNTHETIC_PRIVATE_TRACE"},
                    {"type": "text", "text": "SYNTHETIC_PRIVATE_TRACE"},
                    {
                        "type": "tool_use",
                        "name": "submit_appraisal",
                        "input": {
                            "reason": "No change",
                            "understanding": {
                                "meaning": "A greeting",
                                "topic": "chat",
                                "importance": 20,
                                "confidence": 0.9,
                                "basis": "explicit",
                            },
                        },
                    },
                ],
            },
        )

    provider = DeepSeek(
        "https://api.deepseek.com/anthropic",
        "deepseek-flash",
        "SYNTHETIC_KEY",
        transport=httpx.MockTransport(reply),
    )
    proposal, receipt = provider.appraise({"state": {"dimensions": {}}})
    assert "SYNTHETIC_PRIVATE_TRACE" not in json.dumps([proposal.model_dump(), receipt])
    assert len(seen) == 1 and seen[0]["output_config"]["effort"] == "max"
    assert seen[0]["max_tokens"] == 131072
    assert receipt["elapsed_ms"] >= 0


def test_one_batch_of_original_and_summary_has_one_evidence_identity(enabled):
    mind, source, _ = enabled
    original = source("joint-plan")
    summary = mind.engine.add_record(
        RecordInput(
            scope=mind.scope,
            kind="summary",
            content="A shared project",
            source_ids=[original],
            valid_from=mind.clock(),
        ),
        command_id="joint-summary",
    )
    result = mind.manage_concern(
        ConcernChange(
            command_id="joint-concern",
            agent_version="continuity-test-v1",
            expected_revision=mind.read()["revision"],
            evidence_ids=[original, summary["id"]],
            action="create",
            key="joint",
            kind="shared_plan",
            content="A shared project",
            topic="project",
            intensity=70,
            basis="explicit",
            confidence=1,
            reason="The summary and source describe the same event",
        )
    )
    assert result["concern_revision"] == 1
    with mind.engine.db.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM mind_concern_evidence").fetchone()[0]
            == 1
        )


def test_interaction_window_spans_midnight_and_expires_after_fourteen_days(enabled):
    mind, _, clock = enabled
    clock[0] = clock[0].replace(hour=15, minute=50, second=0, microsecond=0)
    owner(mind, clock, "before-local-midnight")
    clock[0] += timedelta(minutes=20)
    owner(mind, clock, "after-local-midnight")
    stats = mind.read()["rhythm"]["interactions"]
    assert stats["window_count"] == 1
    assert stats["hourly_window_starts"][23] == 1
    clock[0] += timedelta(days=14, minutes=1)
    assert mind.read()["rhythm"]["interactions"]["window_count"] == 0


def test_assessment_window_is_bounded_without_deleting_concerns(enabled):
    mind, source, _ = enabled
    concern(mind, source, key="interview")
    state = mind.read()
    prototype = state["concerns"][0]
    state["concerns"] += [
        {
            **prototype,
            "id": f"concern-{i}",
            "topic": "another topic",
            "content": "A separate event",
        }
        for i in range(50)
    ]
    projected = appraisal_context(
        {"state": state, "new_evidence": [{"text": "interview"}]}
    )
    assert len(projected["state"]["concerns"]) == 32
    assert prototype["id"] in {c["id"] for c in projected["state"]["concerns"]}
    assert len(state["concerns"]) == 51


def test_mcp_exposes_concerns_and_topic_read_without_replacing_existing_tools(enabled):
    from eventmem.core.mcp import create_mcp

    mind, _, _ = enabled
    server = create_mcp(mind.engine)
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert {"read_affective_state", "manage_concern", "manage_desire"} <= tools.keys()
    assert "query" in tools["read_affective_state"].parameters["properties"]


def test_selected_concerns_follow_query_and_never_exceed_three(enabled):
    mind, source, _ = enabled
    concern(mind, source, "exam", topic="考试", content="惦记考试结果")
    for i in range(4):
        concern(mind, source, f"other-{i}", topic="电影", content="期待一起聊电影")
    view = mind.read(query="考试结果怎样了")
    assert len(view["selected_concerns"]) == 3
    assert view["selected_concerns"][0]["topic"] == "考试"
