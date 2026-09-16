import pytest
from pydantic import ValidationError

from kin_mind.appraisal import Appraisal, appraisal_context
from kin_mind.session_advice import SessionAdvice, advice_record


def context():
    return {"id": "snapshot-v1", "binding": {"generation": 1}, "lastCompaction": {"id": "compact-1", "completedAt": 100},
            "evidence": [{"id": "correction-1", "at": 200}]}


def test_rotation_requires_actual_compaction_and_later_evidence():
    proposal = SessionAdvice(action="rotate", reason="Observed reference error", evidenceIds=["correction-1"], compactionId="compact-1")
    receipt = {"model": "deepseek-flash", "reasoning": "high"}
    assert advice_record(proposal, context(), receipt, "event-1")["snapshotId"] == "snapshot-v1"
    missing = context()
    missing["lastCompaction"] = None
    with pytest.raises(ValueError, match="completed compaction"):
        advice_record(proposal, missing, receipt, "event-1")
    older = context()
    older["evidence"][0]["at"] = 50
    with pytest.raises(ValueError, match="post-compaction"):
        advice_record(proposal, older, receipt, "event-1")


def test_unknown_or_revised_observations_do_not_confirm_degradation():
    proposal = SessionAdvice(action="prepare", reason="Review", evidenceIds=["invented"], compactionId="compact-1")
    with pytest.raises(ValueError, match="unknown"):
        advice_record(proposal, context(), {}, "event-1")
    proposal.evidenceIds = ["correction-1"]
    snapshot = context()
    snapshot["evidence"][0]["needsReview"] = True
    with pytest.raises(ValueError, match="post-compaction"):
        advice_record(proposal, snapshot, {}, "event-1")


def test_legacy_appraisals_work_and_new_context_survives_projection():
    assert Appraisal(reason="No update").session_advice is None
    value = appraisal_context({"state": {"scope": {}}, "session_context": context()})
    assert value["session_context"] == context()
    with pytest.raises(ValidationError):
        SessionAdvice(action="force", reason="Bypass")


def test_keep_or_compact_does_not_claim_native_operation_completion():
    for action in ["keep", "recall", "compact", "defer"]:
        value = advice_record(SessionAdvice(action=action, reason="Review"), context(), {}, "event-1")
        assert "completed" not in value
        assert value["decision"]["action"] == action
    assert advice_record(None, context(), {}, "event-1") is None
